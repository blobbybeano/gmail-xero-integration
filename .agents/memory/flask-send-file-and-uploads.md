---
name: Flask send_file paths & receipt upload safety
description: Two non-obvious gotchas when serving user-uploaded receipt files from this Flask app.
---

# Serving user-uploaded files (receipts / expense photos)

## send_file resolves relative paths against the app package dir, not CWD
Receipt photos are saved by `ReceiptService._save_file` under `receipts_upload_dir`
(default `receipt_uploads`), a path **relative to the process CWD** (the repo root,
because the app starts via `python run.py`). `flask.send_file` resolves a relative
path against the Flask app's `root_path` (the `app/` package dir), so passing the
stored relative path 500s with FileNotFoundError looking in `app/receipt_uploads/...`.

**Rule:** always `os.path.abspath(stored_file)` before `send_file` (and before the
`os.path.exists` guard).
**Why:** os.path.exists uses CWD (passes) but send_file uses root_path (fails) — the
mismatch makes it look like the file "exists but won't serve."

## Never trust client Content-Type for stored uploads
A receipt upload route that is reachable by a token holder (no password) must not
store or reflect the browser-supplied MIME type. Otherwise someone can upload active
HTML/JS and have it served from our own origin → stored XSS (worse if an admin opens
the link while logged in).

**Rule:** sniff magic bytes on upload (`_exp_sniff_mime`), reject anything that
isn't a known image/PDF, store the sniffed type, and when serving: send images
inline with the sniffed type, force `as_attachment` for PDF/unknown, and always set
`X-Content-Type-Options: nosniff`.
**How to apply:** any new public upload+serve pair in this app should reuse
`_exp_sniff_mime` rather than `file.content_type`.
