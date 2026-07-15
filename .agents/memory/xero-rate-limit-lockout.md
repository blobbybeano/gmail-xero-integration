---
name: Xero rate-limit lockout has two signals
description: Why any Xero pause/resume logic must combine the in-memory 429 cooldown and the persisted state-file lockout
---

Xero throttling is tracked in TWO independent places and they are NOT kept in sync:

- **In-memory** `_XERO_RATE_LIMIT_UNTIL_TS` in `xero_client.py`, set by `_request` the moment it
  sees a real `429`. Read via `get_xero_rate_limit_until_ts()`. This is the one our OWN outgoing
  posts trip.
- **Persisted** `xero_lockout_until_ts` in the state file, set by the webhook/poll paths. Read via
  `persisted_xero_lockout_until(config)` / `xero_lockout_is_active(config)`.

**Rule:** Any pause/resume or "is Xero throttled right now" check must use `max()` of BOTH. Relying on
`xero_lockout_is_active(config)` (state file) alone silently misses 429s caused by our own bulk writes,
so an auto-pause loop keyed only on it will never fire and the job will error out instead of waiting.

**Why:** the bulk Cashflows CSV submit background job originally gated its pause loop on the state-file
signal only; a 429 from its own posts left the state file untouched, so it never paused.

**How to apply:** in the CSV submit job (`_run_submit_job` in `admin_web.py`) the combined check is
`_lockout_until_ts()` / `_lockout_active()`. Reuse that pattern for any new Xero batch/looping writer.

## Never blindly retry a partially-executed Xero batch
A single Cashflows batch plan (`_execute_plan`) issues several DEPENDENT writes (credit note create +
allocate, payment delete + recreate, batch payment, bank txns). Xero does not dedupe these by content.
If a 429/error fires mid-batch, retrying the whole batch DUPLICATES the writes already committed
(double credit notes / double payments). Safe design: only ever pause/resume BETWEEN batches
(pre-flight wait for a clear cooldown window), execute each batch exactly once, and on mid-batch failure
STOP with a "this batch may be part-done in Xero — check before re-running" message. Completed batches
persist their reconciled ref immediately so a resume skips them.
