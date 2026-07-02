---
name: Email Invoice Importer scan
description: Why the Gmail invoice scan over-collected junk and appeared frozen, and the durable rules to keep it healthy.
---

# Email Invoice Importer — scan reliability

Two failure modes caused "the scan isn't working":

1. **Junk flood + slowness.** The Gmail query is `has:attachment`, which returns
   every email-signature logo, social icon and tracking pixel. The old
   `is_invoice_attachment` accepted any image/* part, so a single scan OCR'd
   ~120 signature images (all `amount: None`), taking many minutes.
   **Rule:** signature/decoration images are almost always **inline**
   (Content-Disposition: inline, or a Content-ID with no attachment
   disposition). Filter images by `inline` + a size floor, NOT by generic
   filenames. Do **not** blocklist `image001`/`img1234` — scanners/MFPs emit
   real invoices under those names; they're caught by the inline + size tests.
   PDFs should always pass (suppliers send PDF invoices), exempt from the size
   floor and from inline checks (inline PDFs are still real).

2. **Looks frozen.** The batch status was written only once, at the very end of
   the loop. During a long run the results page (which polls while
   status=="processing") spun forever.
   **Rule:** persist interim progress (status + partial counts) periodically
   during the scan loop so the polling UI shows movement and a killed/slow run
   is distinguishable from a hang.

**Why:** real complaint — a live scan created 121 junk items, stayed
"processing" indefinitely, and the user reported it broken even though images
were viewable.

**How to apply:** keep the inline/size logic in `is_invoice_attachment`
(`app/gmail_client.py`); `_walk_parts` must surface `inline` + `size` per part.
Scan loop + incremental `update_batch` live in
`app/receipts/email_pipeline.py::scan_email_batch`. Gmail stays read-only
(`gmail.readonly`, only list/get/attachments) — never add modify/label calls.

## Two-stage AI invoice gating (cost-ordered)

The scan applies two cheap, text-only AI gates, ordered by cost so the expensive
work is gated by the cheap work. **Both gates are RECALL-BIASED ("never miss a
bill"), NOT precision-biased.**

- **Gate 1 (`ai_is_invoice_email`)** runs on subject+sender+snippet BEFORE
  `get_attachments`/OCR. Cheapest, so it comes first and avoids download/OCR
  spend. KEEPS invoices, bills, statements of account and membership/subscription
  charges; drops ONLY clearly non-financial mail (marketing / newsletters /
  social / login / spam). When in doubt → keep.
- **Gate 2 (`ai_validate_invoice_doc`)** runs on the OCR'd text AFTER dedup but
  only for `STATUS_NEW` items (duplicates are already known). KEEPS invoices,
  bills, statements and membership charges; rejects ONLY clear
  quotes/estimates/proformas, logos/non-document images, or docs addressed to a
  DIFFERENT unrelated company.

**Recall-bias rule (durable):** a doc addressed to a staff member or director by
their PERSONAL name still counts as "us" — both prompts say so explicitly.
Statements and membership/subscription charges are payable and MUST be kept;
earlier precision-biased prompts that rejected "statements" / "not addressed to
us" silently dropped real Checkatrade membership statements, a Redwood/CJH
statement, and an RAC invoice addressed to a director — exactly what the user
demanded never happen ("I don't want anything missed").
**Why:** missing a real bill (false negative) is far costlier to the user than a
junk item reaching the review queue (false positive), because junk is visible and
overridable but a dropped invoice is invisible.

**Empty-OCR rule:** Gate 2 KEEPS docs with < ~12 chars of OCR text (sends to
review), it does NOT reject them. Small logos/icons are already removed upstream
by the `inline`+size filter in `gmail_client.is_invoice_attachment`, so a
text-less doc reaching Gate 2 is more likely a real PDF that OCR failed on.

**Rule:** both gates MUST fail OPEN (keep / is_invoice=True) when no OpenAI key
or on any exception, so the importer still works on filename/MIME heuristics
alone. Gate-2 rejects set `STATUS_NOT_INVOICE` (in `SKIP_STATUSES`); the results
UI shows them in a collapsed group with an editable dropdown so a wrongly-filtered
item can be overridden back to `new` and imported.

**Dedup rule (fixed):** `message_already_scanned` now only blocks on
`status='imported'` items — not on old unimported scan results. This means old
batches sitting around (no_account / not_invoice / ignored) no longer block a
fresh re-scan of the same date range. Only IMPORTED items block re-import.
**Why:** users were getting found:0 after a previous scan left unactioned items
in the DB, which silently blocked every subsequent scan of the same dates.
Test scripts should still clean up after themselves (use VERIFY prefix + delete
at the end) to avoid confusing the results list, but it's no longer catastrophic
if they don't.

**own-company detection — "from [name]" in subject (Path 3):** extended to apply
to ALL senders (not just Xero gateways). "from [own_name]" in subject reliably
identifies our outgoing invoices (customer reply threads included). Supplier
invoices say "for/to [us]" not "from [us]" so there are no false matches.

**own_names settings — union with defaults:** `_escan_settings()` unions saved
`own_company_names` with `_EMAIL_SCAN_DEFAULT_OWN_NAMES` so new default name
variants (e.g. "Pow Services Limited") are picked up for existing installs without
wiping user additions. Always add new trading names to the DEFAULT list, not just
the DB, or existing installs won't see them.

**Gmail octet-stream PDFs:** Gmail sometimes delivers real PDFs/images with
`Content-Type: application/octet-stream`. Fix: normalise the MIME from the filename
extension before calling analyze_upload. Document AI rejects the generic type but
accepts the specific type inferred from the extension.

**Amount fallback for non-standard receipts:** after `reconcile_amounts` returns
None (e.g. council/parking receipts with unusual table layouts), a regex scans
`raw_text` for the largest currency-style number (1-2 decimal places). Only runs
when there is no OCR error (text is available but structured extraction failed).

**Gotcha:** any new status used in `email_pipeline.py` must be imported from
`.email_store` at the top — `STATUS_NOT_INVOICE` was used before being imported,
which `NameError`s the whole batch into `status="error"` exactly on the new path.

**Word .docx invoices (real-world supplier behaviour):** suppliers attach real
invoices as `.docx` (e.g. Close Brothers Premium Finance / insurance renewals sent
as `Invoice.docx`). Document AI cannot OCR Word docs, so the scan branches: if
`is_word_doc(fname,mime)` it reads text directly with `extract_docx_text` (walks
paragraphs AND table cells — totals are usually in a table) and stores the file via
`receipt_service.store_file`, otherwise it uses `analyze_upload`. The existing
amount-fallback regex then pulls the total from the docx text, and Gate 2 validates
it. `.docx`/`_DOCX_MIME` must be in `gmail_client._INVOICE_EXTS`/`_INVOICE_MIMES`
AND in the octet-stream MIME-normalisation map (Gmail sends docx as octet-stream).
**Why:** an entire class of supplier invoices was silently dropped purely because
the attachment type wasn't accepted. Requires the `python-docx` package (imports as
`docx`).

**Gate 1 must KEEP insurance/policy-fee/direct-debit/VAT/utility mail explicitly:**
generic "when in doubt YES" was inconsistently dropping real insurance bills
("Outstanding Policy Fee", "Outstanding Direct Debit Mandate"). Both gates now name
these categories as always-keep. Mortgage-offer letters (e.g. HSBC) still reach the
review queue as a minor accepted false-positive — they land in `no_account`
(review), never auto-import, so recall is preserved without harm.

**Default scan window is 90 days** (`admin_web.py` `d_from_def`), widened from 30
because real misses (RAC invoices) were simply outside the old window, not dropped
by gates. When a user reports a "missing" invoice, check the date window BEFORE
assuming a gate/attachment bug.

**Email scan MUST load Xero accounts the same way the receipt dump does:**
`_at,_tid,_ = _load_xero_at_tid(config)` then `_get_xero_expense_accounts(_at,_tid,db)`.
A previous version called `_build_xero_client_safe()` / `_load_expense_accounts()`
which don't exist in `admin_web.py` (only nested in `main.py`) — the NameError was
swallowed by a bare `except`, leaving `exp_accounts=[]`, so `ai_categorise` returned
nothing and EVERY invoice landed in `no_account` ("Needs account").
**Why/rule:** the email importer is meant to mirror the receipt-dump pipeline — reuse
its helpers, don't invent parallel ones. When the whole batch shows `no_account`,
suspect empty `exp_accounts` (account-loading) before suspecting the AI.
