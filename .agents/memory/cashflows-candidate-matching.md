---
name: Cashflows reconciliation candidate matching
description: How Cashflows CSV sales are matched to Xero invoices + calendar, and the non-obvious data quirks that break matching
---

# Cashflows ↔ Xero ↔ Calendar matching

The reconciliation matches merchant-CSV card sales to Xero invoices, using Google
Calendar appointments to identify the real customer. Pipeline lives in
`app/cashflows_csv.py::build_csv_reconciliation_preview`.

## Branding theme is NOT a reliable bank-transfer signal
- Rule: an invoice's authoritative "this is a Cashflows card payment" signal is its
  **GC- reference** (calendar booking) and/or presence in the correlation sheet's
  CARD rows — NOT its Xero branding theme.
- **Why:** in the live Powwash Xero org, the "Bank Account" branding theme
  (`BANK_TRANSFER_THEME_ID = a1ed21dc-...`) is applied to genuine card invoices.
  Observed: of paid invoices in a month, the bank-themed subset were *100%* GC-
  referenced card jobs. The old code unconditionally excluded that theme from the
  invoice pool, silently dropping every exact match (correct amount + date + GC ref
  + matching calendar customer). Those sales then showed as "missing invoice" and
  the candidate list filled with unrelated nearest-date invoices.
- **How to apply:** any card-eligibility filter must let an invoice through if it has
  a card signal, regardless of branding theme. Only exclude bank-themed invoices
  that ALSO lack a card signal. See `_has_card_signal` in `build_csv_reconciliation_preview`.

## Matching model (what the user expects)
1. Amount match first (sale gross ↔ invoice total, ±MONEY/2).
2. Cross-reference the calendar for the closest appointment that day.
3. Invoice customer name matching the calendar customer = confirmed match.
Calendar scoring is `cashflows_calendar.py::_score_parsed` (amount .6 / time .3 /
has-customer .05). Name overlap between invoice and calendar is computed in the
frontend candidate panel (`admin_web.py`).

## Diagnosing live
- Re-run the preview against the real CSV in `attached_assets/` with live Xero +
  Google: `build_csv_reconciliation_preview(config, csv_text, xero_client=..., correlation_sheet_id=..., calendar_ids=...)`.
- The cached preview in admin.db (`cashflows_csv_preview`) can be STALE — always
  re-run before concluding an invoice is missing.
- If an exact-amount invoice exists in Xero but appears neither matched nor as a
  candidate, suspect a pool-exclusion filter (branding theme, status, date range),
  not the ranking logic.

## Quick-invoice cheat action for stray card payments
- A standalone card payment with no matching Xero invoice (e.g. a £6.16 parking
  charge an engineer took on the terminal but never invoiced) can be resolved
  one-click from the CSV reconciliation preview: green "+ Quick invoice" button on
  noMatch / noAmountMatch rows -> POST `/cashflows-sync/create-quick-invoice` ->
  `XeroClient.create_simple_invoice`.
- Design: raises an AUTHORISED ACCREC invoice to a standard catch-all contact
  (default "Parking", passed as `Contact.Name` so Xero find-or-creates it) with one
  `LineAmountTypes:"Inclusive"` line so the invoice TOTAL equals the card amount
  exactly regardless of the account's default VAT. Reference = "Card terminal <ref>".
- **Why inclusive:** the merchant CSV amount is the all-in figure the customer paid;
  entering the card amount as a net line (a real mistake seen on a parking line)
  makes the invoice total drift up by VAT and stops it reconciling cleanly.
- Honors `config.dry_run` (DRY_RUN env; defaults to "false" -> live writes). In dry
  run it returns the payload and the UI shows "simulated (dry-run on)".

## Watch for double-collection, not just missing invoices
- A GC- (GoCardless direct-debit) invoice already collects its full total in ONE
  payment. If a line on it (e.g. parking) was ALSO taken separately on the card
  terminal, the item is collected twice -> the stray card sale is a duplicate to
  refund, NOT a top-up that makes up a shortfall. Check the invoice's Payments
  array (single full payment = already paid in full) before assuming a split.
