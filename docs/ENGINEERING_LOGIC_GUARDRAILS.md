# Engineering Logic Guardrails

This document is the source of truth for high-risk behavior in this app.
If you change logic in the files referenced below, update this document in the same commit.

## System Components

- `app/main.py`: poller loop, calendar event lifecycle, Xero draft/sent/paid flow, rate-limit handling, state markers.
- `app/admin_web.py`: admin dashboard, webhook handlers, manual sync/backfill actions, settings.
- `app/event_processor.py`: parsing/normalization of calendar notes, status-block rendering, title emoji semantics.
- `app/google_sheets.py`: sheet writes, dedupe signatures, backlog flushing.
- `app/state.py`: persistent markers for processed/sent/paid/draft-sync and retry cooldowns.
- `app/safety_simulator.py`: offline fake Google/Xero simulator for webhook loops, Xero failures, and old-entry handling.
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
- Xero health must be passive/event-driven:
  - do not call Xero Organisation or other endpoints just to probe health,
    clear lockout, or test whether a cooldown is over.
  - once a 429 lockout is recorded, wait until the recorded timestamp expires;
    the next real event-processing call may then proceed.
  - this preserves Xero calls for customer work and keeps every rate-limit
    event attributable to a real action where possible.
- Draft-sync fingerprints are persisted on attempted draft-create calls so
  unchanged events do not repeatedly re-submit drafts during transient Xero failures.
- Each event may carry a compact app-owned ledger at the bottom of the notes:
  - human line: `App status: ...`
  - machine line: `[app]s=...;r=...;fp=...;x=...;w=...[/app]`
  - users should not edit this block manually; it records the app state,
    reason, input/action fingerprint, Xero attempt count, and wait condition.
  - Xero draft/send actions are hard-budgeted by event/action/fingerprint
    (`XERO_ACTION_MAX_ATTEMPTS`, default 2). Once the same unchanged action
    reaches the budget, the app marks the entry as needing human input and
    must stop before calling Xero again.
- Google Calendar webhook feed messages are coalesced. A burst of webhook pings
  must not create a misleading live-feed storm; the webhook may still queue the
  exact changed calendar for incremental sync.
- On webhook-targeted calendar cycles, the app reads recently updated events on
  that calendar in addition to the normal date windows. This lets an old event
  edited today be considered once without bringing all old untouched events
  back into Xero processing.
- `DONE` entries with no invoice line items must mark the current calendar
  update as handled and then stop. A later staff re-save changes `event.updated`
  and allows the app to check the entry again after line items are added.
- Missing-line-item entries must not reserve a Xero processing slot. Mark the
  title once with `(Check Formatting)` and do not retry until the entry is
  re-saved. Once invoice lines exist, normal title updates must remove that
  marker. This malformed-entry hold must run before integration issue marking,
  title restamping, and Xero budget accounting.
- Red title stamping is reserved for genuinely blocking runtime failures:
  Xero disconnected, Google disconnected, or calendar read failure. Sheet
  routing/backlog issues and Google webhook registration warnings must not turn
  job titles red because the app can still process or recover from those paths.
- Draft update must be fingerprint-gated (not generic `event.updated` gated):
  - in `app/main.py`, both draft-update paths now compute:
    - `_draft_sync_fingerprint(...)`
    - `last_draft_fp = get_draft_sync_fingerprint(...)`
    - `should_try_update = (draft_fp != last_draft_fp) or (not last_draft_fp)`
  - this is intentional anti-loop protection against webhook/metadata churn.
- Same-save submit/send is allowed only when safe:
  - if staff enter `PROCESS DRAFT = Y`, a payment type, and `SEND NOW = Y`
    together, the app may create the missing draft and then continue to send
    in the same event processing pass.
  - `SEND NOW = Y` is a one-shot command. After successful processing the
    status block must show `Invoice sent ✅` rather than leaving a reusable send
    command active; repeated sends are also gated by the app ledger/action
    fingerprint budget.
  - if the calendar notes already contain an `Invoice link:` but state has no
    stored invoice id for the event, the app must stop with an explicit
    duplicate-protection message instead of creating another Xero invoice.
  - this guard must run before contact/invoice Xero lookups so rate limits do
    not cause silent no-op behavior.

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
- Managed control prompts are explicit state, not defaults:
  - `PAYMENT TYPE (CARD/INVOICE) =` must remain blank until staff enter
    `CARD`, `INVOICE`, or `CASH`.
  - Never infer `CARD` from the prompt options text itself.
  - If Calendar/mobile editing places the answer on the next nonblank line
    immediately after the prompt, collapse it to the canonical single-line
    prompt before processing.
  - Apply the same immediate-next-line tolerance to `SEND NOW (Y/N) =` and
    `PROCESS DRAFT (Y/N) =`; do not scan arbitrary later notes for answers.
- Invoice-line normalizer must not reinterpret hyphenated descriptions as value separators.
  - `Pressure washing - Driveway = £165+VAT` must stay one description line.
- Repeated-separator corruption must be self-healed:
  - examples like `... = Driveway = = = £165+VAT` must normalize back to a single canonical line.

Key files:
- `app/event_processor.py` (`normalize_user_sections`, `extract_*`, `upsert_*`)
- `app/main.py` (where those helpers are invoked)

### Invoice Line Parsing Invariants (Critical)

These invariants exist specifically to prevent formatter loops and duplicate draft pushes:

- `app/event_processor.py`:
  - `normalize_user_sections` → `_norm_invoice`:
    - only treat `=`, `:`, `-` as separators when RHS is numeric amount.
    - keep descriptive hyphens in the left-side text.
    - repair legacy repeated `=` artifacts before writing canonical text.
  - `_parse_line_items`:
    - explicit parse only when RHS matches numeric amount pattern.
    - pre-clean repeated `=` artifact sequence before parse.
  - `sync_invoice_block_from_xero`:
    - self-heal historical corrupted descriptions from prior parser behavior.
    - never mirror lines from the `⬇Sales⬇` section into the customer-facing
      section above the marker (prevents visible duplicates while still keeping
      sales included in Xero totals).

- `app/main.py`:
  - draft update decision in both flow branches must remain fingerprint-led:
    - around `should_try_update` blocks (both occurrences),
    - never revert to `event_updated != last_invoice_update` as a primary update trigger.
  - send path (`has_send`) must perform a final pre-send draft sync for
    `CARD/INVOICE` payments:
    - update mutable draft invoice with current parsed `invoice_lines` before
      authorize/email/payment,
    - fail send with explicit error note if pre-send sync fails.

If any of the above is changed, run a targeted regression against:
- one line with hyphenated description (`A - B = £x+VAT`)
- one line with accidental extra equals (`A = B = = £x+VAT`)
- blank `PAYMENT TYPE (CARD/INVOICE) =` staying blank after status rebuild
- next-line control answers (`INVOICE` / `Y`) being collapsed and processed
- repeated webhook/calendar sync events with unchanged invoice data.

## Change Checklist (Required)

If you edit `app/main.py`, `app/event_processor.py`, or `app/admin_web.py`:

1. Run compile checks:
   - `.venv/bin/python -m py_compile app/main.py app/event_processor.py app/admin_web.py`
2. Verify no regressions in:
   - sent/paid -> light transitions
   - stale error alert cleanup
   - invoice draft update behavior for mutable vs non-mutable statuses
3. Verify no extra Xero call loops were introduced.
4. Specifically verify draft-update gating still depends on draft fingerprint delta (not metadata-only event update timestamps).
5. For calendar/Xero lifecycle changes, run:
   - `.venv/bin/python scripts/safety_simulation.py --json`
6. Update this document if behavior changed.

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
