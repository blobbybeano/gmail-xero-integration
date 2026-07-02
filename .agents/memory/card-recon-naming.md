---
name: Card recon chosen-card naming
description: What value the batch "card" field must hold for card-feed reconciliation to find any lines.
---
Rule: the card chosen for a batch (receipt dump or email scan) must be the
Xero bank account NAME (e.g. "Pow Wash", "Charge Card - Dan"), never the raw
statement account number (e.g. "60563768").

**Why:** the reconciler filters CSV feed lines by mapping each file account id
→ its admin-assigned Xero account name, then substring-compares against the
chosen card. An account number matches nothing, so recon quietly keeps 0/N
lines and every receipt shows "No card match".

**How to apply:** card pickers must offer Xero account names (as the dump/email
upload forms do). When creating batches programmatically (tests, scripts), use
the mapped name from the account-labels store, not the number. Recon logs
"[recon] card feed: kept/seen for chosen card ..." — 0 kept with a populated
store means a naming mismatch.
