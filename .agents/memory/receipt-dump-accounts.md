---
name: Receipt Dump accounts & card list
description: How the Receipt Dump fetches Xero accounts, why the card dropdown can be empty, and how AI-uncertain receipts are handled
---

# Fetching Xero accounts for the Receipt Dump

**Always fetch bank/expense accounts through the cached, scope-aware helper
`_get_tenant_acct_themes(at, tid)` (paired with `_load_xero_at_tid`), the same
path the Settings page uses — NOT a bare `build_xero_client().get_bank_accounts()`
call.**
**Why:** the bare call was unreliable and returned `[]` (so the upload form's
card/bank dropdown silently fell back to a free-text box). `_get_tenant_acct_themes`
filters `Status==ACTIVE` + `Type=="BANK"`, caches for 5 min, handles the 401/403
"missing accounting.settings scope" cases, and still serves cached accounts while
Xero is paused — so the dropdown stays populated.
**How to apply:** it returns raw Xero account dicts (`Name`, `Code`, `AccountID`),
not the `{name,id,code}` shape. The dump's `card_account` is stored as the account
**Name** (not code/ID) because `_dump_bank_feed_recon` matches against
`BankTransaction.BankAccount.Name` (case-insensitive). Known limitation: two BANK
accounts with the same display name are ambiguous for recon scoping.

# AI-uncertain receipts must be held for a manual account pick

When the AI can't confidently categorise a receipt to an expense account, the
item is routed to `STATUS_NEEDS_ACCOUNT` (not silently `STATUS_NEW` with a
fallback account). The "Fallback expense account" setting (per-person
`expense_account_code`, else global `default_expense_account`) is only a
*suggestion* pre-selected in the per-item dropdown — the admin must confirm/pick
before import.
**Why:** importing on a guessed/empty account silently mis-codes receipts; the
user wanted to choose when the AI is unsure.
**How to apply:** `NEEDS_ACCOUNT` is in `ACTIVE_STATUSES`, so these items show
the per-item account `<select>` + Keep/Ignore on the results page and appear in
the bank-feed recon preview. Import / subcontractor-confirm routes only act on
`STATUS_NEW`, so uncertain items are gated until reviewed. Test mode is unaffected
(still dry-run).
