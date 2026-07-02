---
name: Card feed check — Xero API limitation
description: Why card_feed_status="missing" does NOT mean "not on company card"
---

## The rule
`_dump_card_feed_check` queries `GET /BankTransactions?where=Date>=...&Date<=...`.
Xero only returns transactions that have been **reconciled** or **manually entered**.
Unreconciled bank-feed statement lines (visible on Xero's Reconcile screen) are
NOT returned. So "missing" can simply mean "not reconciled yet".

## Why this matters for status assignment
When a batch has `card_account` set, the admin has told us every receipt in the
batch came from that card. Downgrading to `STATUS_NEEDS_ACCOUNT` on "missing"
is wrong — it just asks the admin to re-confirm something they already told us.

**Fix applied:** if `batch.card_account` is truthy AND card check returns "missing",
keep `STATUS_NEW`. Only set `STATUS_NEEDS_ACCOUNT` when no card_account is
specified (we don't know if it's company card or personal).

## Also fixed: no account filtering in the check
Previously the check matched amount against SPEND tx from ANY bank account.
Added `card_account` param to `_dump_card_feed_check`; when set, transactions
on other accounts are filtered out using the same lenient substring normalisation
as `_dump_bank_feed_recon`.

## How to apply
Any future change to card-feed-check logic must respect the "unreconciled
statement lines are invisible" limitation. The reconciliation **display panel**
(`_dump_bank_feed_recon`) uses a ±365d window and is read-only/informational;
the **processing check** (`_dump_card_feed_check`) is ±10d and drives status.
Don't tighten the processing check window further — it already misses things.

## DEAD END: there is NO Accounting-API route to unreconciled statement lines
Investigated thoroughly (June 2026, live token against 'Pow Services Limited'):
- `GET /Reports/BankStatement` returns the raw statement lines incl. unreconciled,
  BUT it is gated behind a scope this app cannot get.
- **Granular scopes trap:** this Xero app was migrated to *granular* scopes
  (Web apps since March 2026). The deprecated broad scope `accounting.reports.read`
  is REJECTED at authorize time → adding it to REQUIRED_XERO_SCOPES silently
  **breaks the Xero reconnect** (user can't reauthorize). Do NOT add it.
- Even if it were accepted, `BankStatement` is **not** in `accounting.reports.read`'s
  resource list, nor in any granular `accounting.reports.*` scope. The only scopes
  that expose bank statement lines are `bankfeeds` (Bank Feeds API) and
  `finance.bankstatementsplus.read` / `finance.cashvalidation.read` (Finance API),
  and **all require Xero partner certification** — not feasible for this app.

**Conclusion:** matching dump receipts against *unreconciled* card payments cannot
be done via the Xero API. The viable alternative is to have the admin upload/export
the card statement (CSV) and match receipts against that. Confirm with user before
building (they dislike scope creep). Keep only valid granular scopes in
REQUIRED_XERO_SCOPES; never reintroduce `accounting.reports.read`.

## AUTHORITATIVE (Xero official docs, verified June 2026)
developer.xero.com/documentation/api/accounting/bankstatements states verbatim:
"unreconciled bank statement data is not exposed via public APIs due to
regulatory, contractual and risk constraints." This is Xero POLICY, not a scope
gap. The dedicated scope `accounting.reports.bankstatement.read` (enabled by the
app owner signing an addendum in the Xero developer portal, breaking change since
2 Apr 2024) unlocks the Bank Statement *report*, but that report does not surface
unreconciled lines. **Do not promise the user that any scope will expose
unreconciled card payments — it won't.** Route to use: admin uploads/export of the
card statement (CSV), or reconcile in Xero first then read via BankTransactions.
