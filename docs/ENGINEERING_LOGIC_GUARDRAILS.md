# Engineering Logic Guardrails

This document is the source of truth for high-risk behavior in this app.
If you change logic in the files referenced below, update this document in the same commit.

## System Components

- `app/main.py`: poller loop, calendar event lifecycle, Xero draft/sent/paid flow, rate-limit handling, state markers.
- `app/admin_web.py`: admin dashboard, webhook handlers, manual sync/backfill actions, settings.
- `app/event_processor.py`: parsing/normalization of calendar notes, status-block rendering, title emoji semantics.
- `app/google_sheets.py`: sheet writes, dedupe signatures, backlog flushing.
- `app/state.py`: persistent markers for processed/sent/paid/draft-sync and retry cooldowns.
- `app/receipts/*`: scaffold-only receipt module (feature-flagged, isolated, no live write-through yet).

## Critical Invariants

- Never change existing invoice/calendar behavior without preserving:
  - sent/paid state markers,
  - title light semantics,
  - anti-loop protections (`processed`, `invoice_update_marker`, cooldown markers).
- New features must be feature-flagged OFF by default until validated.
- If a change touches event processing, check both webhook and poller paths.

## Title Light Semantics

- `🔵`: unprocessed/new
- `🟠`: draft/in-progress (including draft edits)
- `🟡`: sent/authorised and awaiting payment (invoice mode)
- `🟢`: paid/complete
- `🔴`: blocked/error integration state
- `✉️`: email send failure indicator (must survive state transitions)

Key files:
- `app/main.py` (`_expected_title_status`, `safe_update`)
- `app/event_processor.py` (`set_title_status_emoji`, `set_title_mail_emoji`)

Cash-complete resilience:
- If a diary entry already contains `Entry complete ✅` and cash marker in
  `[invoice]`, runtime state is self-healed to sent+paid and title must remain
  `🟢` (never regress to orange/yellow due to stale markers).

Card paid-state reconcile:
- For unchanged `CARD` entries, paid-state sync from Xero is allowed only when
  the event is in the past, or there is explicit send intent/history
  (`SEND NOW=Y`, `Invoice sent ✅`, `Invoice send failed ❌`, or sent/paid state).
- This reconcile path must never run for unrelated modes and must not edit
  invoice line items.

## Throttling Reduction Logic

Primary controls:
- Global Xero cooldown/lockout timestamps in state.
- Per-event Xero retry backoff and retry-after maps.
- Limited Xero events processed per cycle.
- Hourly reconcile windows and bounded cleanup queues.
- Draft-sync fingerprints are persisted on attempted draft-create calls so
  unchanged events do not repeatedly re-submit drafts during transient Xero failures.

Key files:
- `app/main.py` (`_XERO_*` constants, per-event retry maps, hourly reconcile sections)
- `app/xero_client.py` (`_request` 429 handling and retry-after capture)
- `app/admin_web.py` (webhook-driven targeted polling)

Do not remove or bypass these controls when adding new API calls.

## Memory Conservation Logic

Primary controls:
- Bounded live feed behavior (UI-side and server-side queue discipline).
- State pruning via `prune_state` and marker-only persistence.
- Backlog flush queues instead of unbounded in-memory retries.
- Controlled polling windows (targeted, daily safety scan, hourly reconcile).

Key files:
- `app/main.py` (scan window strategy and retry queue behavior)
- `app/state.py` (compact marker structures and pruning)

When introducing new queues/stores:
- cap growth,
- keep payloads minimal,
- prefer marker IDs over large blobs.

## Formatting and Parsing Guardrails

The app depends on stable structured blocks:
- `[notes]...[/notes]`
- `[contact]...[/contact]`
- `[invoice]...[/invoice]`
- `[app-status]...[/app-status]`

Rules:
- Keep parser-tolerant but output-canonical.
- Preserve internal sales section under `⬇Sales⬇`.
- Keep invoice totals/status lines in the expected formatting path.
- Bold handling currently runs through `event_processor` helpers; do not move ad-hoc to random call sites.

Key files:
- `app/event_processor.py` (`normalize_user_sections`, `extract_*`, `upsert_*`)
- `app/main.py` (where those helpers are invoked)

## Change Checklist (Required)

If you edit `app/main.py`, `app/event_processor.py`, or `app/admin_web.py`:

1. Run compile checks:
   - `.venv/bin/python -m py_compile app/main.py app/event_processor.py app/admin_web.py`
2. Verify no regressions in:
   - sent/paid -> light transitions
   - stale error alert cleanup
   - invoice draft update behavior for mutable vs non-mutable statuses
3. Verify no extra Xero call loops were introduced.
4. Update this document if behavior changed.

## Enforcement

Use the guardrail checker before commit/deploy:

```bash
python3 scripts/guardrail_check.py --staged
```

What it enforces:
- If critical files are touched (`app/main.py`, `app/event_processor.py`, `app/admin_web.py`, `app/google_sheets.py`, `app/state.py`):
  - compile check must pass
  - this guardrail doc must be included in the same change by default

Override (rare, with explicit intent):

```bash
python3 scripts/guardrail_check.py --staged --allow-missing-doc-update
```

## Receipt Scaffold Safety Contract

Current contract:
- `RECEIPTS_ENABLED=false` => no receipt feature execution.
- Receipt scaffold stores data only in `RECEIPTS_STORE_FILE`.
- No writes to calendar, Xero, or sheets from receipt routes/services.

Key files:
- `app/receipts/models.py`
- `app/receipts/store.py`
- `app/receipts/service.py`
- `app/admin_web.py` (`/receipts` routes)
