---
name: Receipt Dump deletion safety
description: Rules for safely deleting receipt-dump batches and their image files
---

# Deleting a receipt dump

Receipt-dump batches and their uploaded images (under `receipts_upload_dir`,
default `receipt_uploads/`) accumulate forever. Deletion is exposed per-dump and
as a bulk "delete all test dumps" action.

## Rule: never unlink a still-referenced image
An image file is referenced by `stored_file` path in TWO places:
- `expense_dump_items.stored_file` (other dump batches), and
- `expense_receipts.stored_file` — when a dump item is imported, the SAME path is
  copied into the Field Expenses receipt (no copy of the file is made).

So before `os.remove`, check BOTH `dump_store.stored_file_in_use` (after deleting
this batch's rows) AND `expense_store.stored_file_in_use`. Otherwise deleting a
dump whose items were imported would break a receipt that already reached Xero.

**Why:** uploads are saved as unique `<ts>_<sha>.<ext>` files, so normally one
item per file — but the import path reuses the path, creating a second referencer.

## Rule: refuse delete while a batch is being read
`_dump_process` runs in a daemon thread and keeps calling `create_item` while the
batch status is `processing`/`finalizing`. There is no FK on `batch_id`, so a
delete mid-processing leaves orphan items + uncleaned files. Guard: run the
existing stuck-recovery first (flips a genuinely stuck `finalizing` batch to
`ready` so it stays deletable), then refuse if status is still
`processing`/`finalizing`.

**How to apply:** any future "delete item" / retention / cleanup work must apply
both rules. The whole app has no CSRF tokens (login-only POSTs) — deletion
endpoints match that existing posture; don't add CSRF only here.
