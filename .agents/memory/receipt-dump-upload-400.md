---
name: Receipt Dump upload 400 vs 413
description: Why bulk receipt-dump uploads can return HTTP 400, and how to debug multipart upload failures in this Flask app
---

# Multipart upload failures on the Receipt Dump route

The dump upload route reads files via `request.files.getlist("receipts")`, which is
where Werkzeug lazily parses the multipart body and can raise.

**Key distinction (Werkzeug 3.x):**
- All *limit* violations — `max_form_parts` (default 1000), `max_form_memory_size`
  (default 500KB, non-file fields only), and `MAX_CONTENT_LENGTH` — raise
  `RequestEntityTooLarge` → **HTTP 413**, never 400.
- A bare **HTTP 400** from this route is a `BadRequest`/`ValueError` from the
  `MultipartDecoder`: a *malformed or truncated* multipart body (e.g. the upload
  was interrupted, or a part had no `Content-Disposition`). Not a size/count limit.

**Why it's hard to reproduce:**
`app.test_client()` talks straight to the WSGI app and **bypasses the HTTP server's
body handling entirely**, so test-client uploads succeed (302) even when a real
browser upload through the dev server fails. Reproduce real HTTP-layer behaviour
with `curl` against the actual running server, not the test client. Note curl
login here needs the real `ADMIN_PASSWORD` secret (default `changeme` is not it).

**How to apply:** when a user reports the dump upload "not working" with a fast 400,
it's a malformed/truncated body, not too-many/too-large files. The route now catches
the parse exception, logs the real cause via `app.logger.warning(... %r ...)`, and
redirects to `/receipts/expenses/dump?error=upload` with a friendly banner — check
the workflow logs for that warning line to see the exact underlying ValueError.

**`/finish` MUST return immediately — never run OCR/AI synchronously in the request.**
`_dump_process` takes 1–3+ minutes for a typical batch. The Replit proxy cuts the
HTTP connection after ~60s; the JS `.catch` handler fires, resets the form, and the
user sees "nothing happened" even though processing continued (or silently died).
Fix: `/finish` loads staged files into memory, cleans up pdir, spawns a daemon
`threading.Thread` for `_dump_process`, and returns `{"url": ...}` immediately.
The results page detects `status in ("processing","finalizing")` and renders a
"Reading your receipts…" spinner with `<meta http-equiv='refresh' content='4'>` so
it auto-refreshes until `_dump_process` sets status to `ready`.

## Confirmed cause + resolution
The 400 is NOT a server/proxy size limit: single uploads up to 160MB succeed in
~3s server-to-server (`MAX_CONTENT_LENGTH` is 500MB). The interruption happens on
the user's *browser→proxy* uplink while sending a large batch of full-size phone
photos — unreproducible from the fast server side.

**Why client-side compression was rejected:** an earlier fix shrank each image in
a canvas before submit, but the user's receipts are faded/creased and must OCR at
full quality, and the canvas step also hung on some phones. Compression was fully
removed. **Decision: upload full-quality bytes; do not re-introduce lossy
client-side downscaling for receipts.**

**Current upload behaviour (per-file, not one giant POST):** a single multipart
POST of the whole batch kept truncating on the slow uplink (the 400 above), so the
form JS now uploads **one file at a time**: `POST .../dump/start` creates the batch
(returns JSON `batch_id`), then a sequential loop `POST .../dump/<id>/add` stages
each file (≤2 retries, a permanently-failed file is skipped so the batch still
completes), then `POST .../dump/<id>/finish` runs the existing `_dump_process` on
the staged files and returns the results URL. Status shows a true "Uploading X of
Y (N to go)" countdown.
**Why:** small independent requests rarely get cut off on a flaky link, a single
bad file no longer kills the whole batch, and the countdown is real. The old
single `POST /receipts/expenses/dump` is kept only as a no-JS fallback.
**`/finish` MUST stay idempotent:** a retried finish (likely on these slow links)
must not reprocess or clobber the batch — it's guarded by `status == "processing"`
(set `finalizing` before processing; `_dump_process` sets `ready` at the end).
**Staging:** files are written to `<receipts_upload_dir>/_dump_pending/<batch_id>/`
as `NNNN__<name>` (seq = current file count, safe because client uploads are
sequential); `finish` reads them sorted, strips the `NNNN__` prefix, re-sniffs
mime, then deletes the dir. A lost-response retry can double-stage one file, but
sha256 dedupe in `_dump_process` makes that harmless (only wasted OCR cost).
**Gotcha:** that `<script>` is rendered *inline mid-form*, BEFORE `#dump-submit` /
`#dump-status` exist in the DOM — so init MUST run on `DOMContentLoaded` (or be
placed after those nodes), else `getElementById` returns null and the button-
disable/status/anti-double-submit logic silently no-ops.

## OCR engine + HEIC
OCR is **Google Document AI** (`_run_document_ai` in `app/receipts/service.py`),
NOT OpenAI vision. (OpenAI vision is only the cross-person duplicate "receipt
checker".) Document AI only accepts JPEG/PNG/PDF/WebP/TIFF/GIF/BMP and returns a
cryptic 400 INVALID_ARGUMENT for anything else.
**Trust magic bytes, not the browser MIME** (`_exp_sniff_mime`) — phones often send
wrong/empty `Content-Type`.
**HEIC (iPhone default):** Document AI can't read it and most browsers can't
display it, so convert HEIC/HEIF → high-quality JPEG (q95) **on upload** via
`pillow-heif` + Pillow (`_exp_heic_to_jpeg`), so OCR, dedupe digests, and the
view-image route all work on JPEG. **Why q95:** receipts are faded/creased, keep
maximum legibility. `pillow-heif` + `Pillow` are runtime deps (in requirements.txt).
