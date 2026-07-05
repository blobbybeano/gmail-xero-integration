---
name: Xero kill-switch & scattered entry points
description: How to fully pause all outbound Xero traffic, and why a single chokepoint is not enough.
---

To pause ALL outbound Xero traffic, set env var `XERO_DISABLED` (truthy). It is
read by `xero_is_disabled()` / `guard_xero()` in `app/xero_client.py`.

**Why a single chokepoint is NOT enough:** Xero is reached from several places,
not just `XeroClient._request`. When auditing/blocking Xero calls you must cover
ALL of these:
- `XeroClient._request` — most API 2.0 calls.
- `XeroClient.attach_file_to_invoice` — does a DIRECT `requests.put` (does NOT go
  through `_request`); easy to miss.
- `refresh_xero_token` (identity token endpoint) — called from ~6 sites.
- `app/admin_web.py` makes several DIRECT `requests.get` calls to
  `api.xero.com` that bypass `XeroClient` entirely (expense accounts, tenant
  accounts/themes, draft invoices, connections list, webhook invoice fetch).
- `run.py` auto-starts a calendar→Xero **poller thread** — a background source of
  invoice writes independent of any web request.

**How to apply:** gate every one of the above on `xero_is_disabled()`. OAuth
connect/callback flows (`_exchange_xero_code`, `_get_xero_tenant_id`) are
intentionally left enabled — they are user-initiated reconnects and never fire on
their own. To re-enable Xero, delete/blank `XERO_DISABLED` and restart.

The expense-account list is snapshotted to the admin DB
(`xero_expense_accounts_snapshot`) on each successful fetch, so receipt category
prediction can still be tested offline while Xero is paused.
