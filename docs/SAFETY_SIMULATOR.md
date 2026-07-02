# Offline Safety Simulator

Run:

```bash
.venv/bin/python scripts/safety_simulation.py --json
```

This simulator uses fake Google Calendar and fake Xero services. It does not
load live credentials and does not call Google, Xero, email, or Sheets.

It is designed to catch the failure modes that have caused operational risk:

- app-owned calendar updates waking the app again,
- repeated Xero calls after the same unchanged failure,
- Xero token refresh success/failure before invoice work,
- temporary Xero disconnects flickering job titles red,
- missing invoice-line entries being checked repeatedly,
- old appointments being ignored until explicitly touched,
- webhook storms creating duplicate invoices,
- fast-forward diary loads with old/current/future jobs across multiple calendars.

The simulator deliberately models Google push notifications as noisy: every
user edit and every app patch can enqueue another webhook. Passing scenarios
must become idle without duplicate Xero mutations.

Current scenarios:

- `successful_invoice_send`
- `missing_lines_hold`
- `repeated_xero_429_stops`
- `token_refresh_before_draft`
- `xero_disconnect_does_not_red_flicker`
- `old_event_ignored_until_touched`
- `webhook_storm_no_duplicate_invoice`
- `fast_forward_current_diary_load`

The fast-forward scenario simulates a diary with old completed jobs, an old
unfinished job, current jobs, a malformed entry repaired by a later save, Google
webhook bursts, and Xero token refresh. It asserts that broad/hourly sweeps make
zero Xero calls for stale unfinished jobs, while a deliberate staff re-save
processes only the touched entry.

Before changing calendar/Xero processing logic, run:

```bash
.venv/bin/python -m unittest tests.test_safety_simulator tests.test_app_ledger
.venv/bin/python scripts/safety_simulation.py --json
```

If a scenario fails because it found an app-update loop or duplicate Xero call,
treat that as a production-risk bug unless the scenario is explicitly updated
with a documented reason.
