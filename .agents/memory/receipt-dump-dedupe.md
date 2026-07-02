---
name: Receipt Dump dedupe model
description: The layered duplicate/suspicious detection used by the Field Expenses Receipt Dump.
---

Receipt Dump classifies each uploaded receipt through layered checks, in order:
1. Exact image already submitted — sha256 digest (first 16 hex) parsed from the stored filename
   (`{epoch}_{sha256[:16]}{suffix}`) vs existing `expense_receipts`.
2. Exact duplicate within the same upload / earlier dump batches — full file hash.
3. Logical match against existing receipts — normalised merchant + same date + amount within ±0.02.
   - Same person → flag as possible duplicate.
   - Different person → run the OpenAI-vision "receipt checker" comparing the two images;
     confirmed same → duplicate (discounted), differ/unavailable → suspicious (manual review).

**Why:** receipts arrive in bulk, often unsubmitted, sometimes the same physical receipt claimed
by two people; we must skip true duplicates/reconciled items but never silently drop a real claim.

**How to apply:** the cross-person image compare degrades gracefully (no key / PDF / missing image →
flag for review, never auto-discard). When the other person's local image file is gone and Xero is
on, the image is fetched back from Xero (attachments on the linked Invoice/BankTransaction).
The dedupe scan must load the full receipt history (not the default 1000-row cap) or it misses
older duplicates.

**Test batches must NOT pollute dedupe.** A "test mode" dump is a dry run that imports nothing,
but its items are still stored in `expense_dump_items`. `hashes_in_other_batches` therefore JOINs
`expense_dump_batches` and excludes `is_test=1` rows — otherwise re-uploading the same receipts to
test repeatedly flags every one as "Duplicate of another uploaded receipt" against the last test
run. Note: a test re-upload of a receipt that was *really* imported earlier will still be flagged
via the layer-1 `expense_receipts` digest check (that's legitimate — it really was submitted).
