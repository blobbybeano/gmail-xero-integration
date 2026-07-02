---
name: Email scan review safety
description: Skipped emails in the Gmail invoice scan must stay visible and human-overridable; own-company outgoing invoices are the one exception (dropped entirely).
---
Rule: any email the scan decides to skip for a *doubtful* reason (AI email
gate, or all attachments rejected by the photo/logo filter) must be stored as
a reviewable placeholder item — never silently dropped.

EXCEPTION: our OWN outgoing invoices (own sender domain/name, or subject
"... from <our company>", incl. customer reply/query threads) are dead space
per the owner's explicit instruction — dropped entirely, only counted in the
batch summary, never shown for review.

**Why:** a real batch silently lost ~55 emails (19 AI-gate skips, 36
attachment-filter drops) including genuine invoices; the admin had no way to
know or recover them. Conversely, dozens of own-invoice copies drowned the
review list, so the owner asked for them to be removed outright.

**How to apply:** placeholders carry no amount so they never import and never
count in card reconciliation. The admin can "Scan anyway" (force-process that
one email with ALL AI gates and the attachment filter bypassed — a human
override outranks the AI) or dismiss it. Any new skip path added to the scan
must go through the same record-the-skip mechanism, and any per-item
force-rescan must keep the batch summary buckets status-aware.

Related lesson: OCR on invoices addressed to us often extracts OUR OWN
company name as the "merchant" (biggest name on the page), making items
unrecognisable in review ("looks missing") and skewing AI account coding.
Always run the supplier fix-up (subject "from <Supplier> [for us]" → sender
name → sender domain) BEFORE dedupe/categorisation, in every scan path.
