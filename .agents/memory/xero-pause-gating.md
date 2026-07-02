---
name: Xero kill-switch gating
description: How the app pauses all Xero access and what that means for new features.
---

The Xero integration can be globally paused. `xero_client._request` calls `guard_xero(...)`,
so **every** method that goes through `_request` raises when paused — you do not need to add
per-method checks for the network call itself.

**Why:** Xero was switched off (XERO_DISABLED) for an extended period; any feature that calls
Xero must keep working (read-only/offline) during the pause instead of throwing in the UI.

**How to apply:** For any Xero-dependent feature, either check `xero_is_disabled()` up front to
choose a degraded path, or wrap the call in try/except and show an explicit "paused"/"could not
read Xero" message. Snapshot-backed reads (e.g. expense account list) keep working offline.
Gated, defensive Xero code paths can be written/merged now and verified once Xero is back on.
