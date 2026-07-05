---
name: Receipt Dump duplicate detection
description: What counts as a "duplicate" in the Field Expenses Receipt Dump, and why
---

# Receipt Dump duplicate detection

Cross-batch duplicate detection (`hashes_in_other_batches` in `app/receipts/dump_store.py`)
must only count receipts that were **actually imported to Xero** (item status `imported`),
NOT receipts merely uploaded/reviewed in earlier dump batches.

**Why:** The whole purpose of the dump is to reconcile the bank feed. A receipt is only a
genuine duplicate once it has been pushed into Xero. Counting any prior upload as a duplicate
permanently blocked re-uploading the same photos (the user re-tests with the same image set
repeatedly), making every receipt show as "duplicate of another uploaded receipt". The user
was (rightly) certain nothing had reached Xero.

**How to apply:** In-batch dedup (`seen_in_batch`, identical file twice in ONE upload) is fine
to keep. The `existing_by_digest` / logical-match checks run against `expense_receipts`
(real submitted Field Expenses claims) and are also fine. Only the cross-batch check was wrong.
