---
name: Cashflows card payment detection
description: How to reliably distinguish card (Cashflows terminal) payments from Stripe/bank invoice payments in the reconciliation engine
---

## The reliable signal

The public Google Sheet (`1qIKZSte8XowkG-vrGZoGW_jIWEmPbYHBygeD8KV2uE4`) records every calendar booking with a `Payment Method` column (`CARD` or `INVOICE`). This is the ground truth — set at booking time by staff.

Each CARD row has:
- `Event ID` — matches the Xero invoice `Reference` field (`GC-YYYYMMDD-xxxx`)
- `Invoice Number` — matches the Xero `InvoiceNumber` (e.g. `INV-5461`)

**Why:** Stripe invoices also use the "Stripe Payment" Xero branding theme and all payments go to the same "Pow Wash" bank account with `IsReconciled=false` — no account-level distinction is possible. The sheet is the only ground-truth link.

**How to apply:** `app/cashflows_sheet.py::fetch_card_lookup(sheet_id)` fetches the public CSV export and returns `CardLookup(gc_refs, inv_numbers)`. Configured via `cashflows_correlation_sheet_id` in admin_store.

## Fallback heuristic (GC- prefix)

GC- prefix on Xero invoice `Reference` + branding theme ≠ Bank Account. Imperfect: some GC- invoices are paid by bank/Stripe days later, so the sheet is always preferred when present.

**Sheet is primary but NOT a hard gate.** When the sheet IS configured, a paid invoice that is missing from the sheet's CARD rows still falls back to the GC- prefix test rather than being dropped.
**Why:** the sheet can miss a booking that was never logged; gating purely on the sheet silently hides genuine card payments from reconciliation (real bug: a £258 card sale never appeared). Accepting GC- as a last resort recovers them.
**How to apply:** the sheet-configured `_is_card_paid` in `cashflows_csv.py` checks gc_refs → inv_numbers → then `reference.startswith("GC-")` before returning False.
