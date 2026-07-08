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

## Incident Register

### 2026-07-03 Xero Webhook / Poller State Race

Observed incident:
- Xero marked `SW19 WC Mia` / `INV-5725` as paid.
- The Xero webhook synced the Calendar entry to green/paid.
- That Calendar write triggered a Google Calendar webhook, waking the poller.
- The poller still had an older in-memory state copy where the event was sent
  but not paid. Its later state save could overwrite the webhook's paid flag.
- Once local state forgot `paid`, the poller could ask Xero again on later
  targeted/hourly paid-status checks, contributing to 429 lockout risk.

Permanent rules:
- Webhook and poller state saves must use merge-safe persistence. A later
  poller save must never remove a newer webhook `invoice_paid_event_ids` or
  `invoice_sent_event_ids` fact.
- Payment truth from Xero webhook wins over older local poller memory.
- When a Xero webhook handles an invoice/event, store a short-lived
  `recent_xero_webhook_events` marker.
- When a Xero webhook has just handled an invoice as paid, duplicate Xero
  `UPDATE` webhook events for that same invoice must not repeatedly fetch the
  invoice again within the short duplicate window.
- Google webhook echoes caused by app-owned Calendar updates must not
  immediately re-query Xero for the same invoice/event.
- Poller self-heal probes for voided/deleted invoices must not run for entries
  already known as paid, and must skip recently webhook-handled invoices.
- Background paid-status checks must have the same failure discipline as
  draft/send actions: repeated failures for the same unchanged event/invoice
  are capped, then paused until the calendar entry is changed/re-saved.
- Do not replace merge-safe state saving with whole-file overwrites in poller
  or webhook paths.

Regression tests that must keep passing:
- `StateMergeTests.test_polling_save_preserves_webhook_paid_state`
- `StateMergeTests.test_merge_keeps_sent_paid_invoice_mapping_and_webhook_marker`
- `SafetySimulatorTests.test_default_suite_passes`
  - includes `xero_webhook_echo_does_not_recheck_xero`
  - includes `paid_sync_failures_stop_until_resave`

### 2026-07-06 Paid-Status Polling Pressure

Observed incident:
- After Xero came back online, normal invoice create/send work succeeded, but
  Google Calendar webhook echoes caused repeated `GET /Invoices/{id}` reads for
  the same recently sent invoices.
- The throttle spaced those reads out and no new 429 was observed, but the
  reads were unnecessary because Xero invoice-payment webhooks are the primary
  source of paid truth.

Permanent rules:
- Paid status is webhook-first. A Google calendar-level webhook must not make
  every sent/unpaid event on that calendar eligible for Xero paid polling.
- A direct event re-save may trigger a paid-status check for that exact entry.
- The scheduled paid-status sweep must default to `PAID_SYNC_SWEEP_EVENTS_PER_CYCLE=0`.
  If enabled, it must stay capped, recent-only, and never look back more than
  14 days.
- Operators handle older unpaid follow-up manually. Do not reintroduce broad
  historical polling as a convenience feature.
- Keep Xero call pacing (`XERO_MIN_REQUEST_INTERVAL_SECONDS`) in place even for
  webhook and admin-triggered Xero paths.

### 2026-07-07 Xero Pressure Display Staleness

Observed incident:
- The dashboard's Xero pressure card was reading `state["xero_pressure"]`, but
  the poller no longer refreshed that snapshot each cycle.
- The live app was processing Google Calendar webhooks, but the card still
  showed an old June value / "waiting for poller", making operators blind to
  whether work was genuinely delayed, paused, busy, or deferred by the Xero
  slot guard.

Permanent rules:
- Every poller cycle must write a fresh compact `xero_pressure` snapshot before
  sleeping, including `updated_at_ts`, `events_used`, `events_per_cycle`,
  `deferred_events`, `deferred_sample`, and `active_retry_count`.
- If Xero work is paused, locked, or another feature is holding the shared Xero
  busy guard, the pressure snapshot must say that explicitly.
- When the per-cycle Xero budget is full, skipped calendar work must be counted
  as deferred and sampled for the dashboard. Silent deferrals make save delays
  impossible to diagnose.
- Deferred calendar-Xero work must be queued by exact event key and retried on a
  short controlled delay. Do not rely on the next broad calendar scan to find it:
  future jobs can sit outside the normal daily safety window.
- Deferred queues are queues, not permanent markers: when the poller consumes a
  deferred event key, the state merge must allow that key to be removed.
- A deferred event is not consumed merely because its retry time became due.
  Keep it queued until the event either receives a Xero processing slot, is
  blocked for human formatting input, or no longer needs Xero work. Removing it
  before the slot is granted can leave valid saves waiting silently.
- While the deferred queue is non-empty, the next poll delay must stay short.
  Do not fall back to the normal long poll interval just because the current
  cycle did not add a new deferred event.
- The pressure card is an operational safety tool, not decoration. Do not remove
  or let it become stale when changing poller, receipt, Cashflows, or Xero
  throttling code.

### 2026-06-16 Xero 429 Lockout

Observed incident:
- Xero entered a 429 cooldown until 2026-06-16 19:18 BST.
- The triggering live event was `SW6 G.C Lily Gray`, which had
  `Invoice profile: Lily Gray ❌ Customer does not exist`.
- The app attempted a Xero contact lookup for that invoice profile and Xero
  returned 429.
- Calendar webhooks were noisy around the same period, so even small Xero-side
  checks became dangerous.

Root causes fixed:
- The app previously allowed early lockout probes via `get_organisation()`
  before Xero's stored Retry-After timestamp had passed.
- Proactive Xero health checks were enabled by default, creating Xero traffic
  even when no user action needed it.
- An invoice profile already marked `❌ Customer does not exist` could still be
  considered Xero-work and consume the per-cycle Xero budget after unlock.
- Final green/completed entries were skipped before Xero calls, but the Xero
  per-cycle slot was reserved before that green skip. With a one-event Xero
  budget, old green entries could starve real pending Xero work without making
  a visible API call.

Permanent rules:
- Never probe Xero during a stored 429 lockout. Wait until
  `xero_lockout_until_ts` has expired.
- Keep `XERO_HEALTH_CHECK_SECONDS=0` unless there is a deliberate, tested
  reason to re-enable proactive checks.
- An `Invoice profile` line that already contains
  `❌ Customer does not exist` must not call Xero again or reserve Xero budget
  until a human edits the profile value.
- A final green title (`🟢`) must not reserve Xero budget. Green entries are
  immutable except the explicit existing email retry path.
- Calendar-level webhook noise must not imply event-level Xero intent.

Regression tests that must keep passing:
- `test_xero_health_check_disabled_when_interval_zero`
- `test_xero_health_check_waits_for_retry_after`
- `test_invoice_profile_missing_hint_detected`
- `test_invoice_profile_missing_hint_absent_for_normal_profile`
- `test_final_green_entry_blocks_xero_budget`
- `test_non_green_entry_can_use_xero_budget`
- `test_calendar_target_does_not_force_unpaid_invoice_check`
- `test_calendar_target_does_not_bypass_future_draft_limit`

Safe testing procedure:
- Do not test Xero lockout behavior by intentionally exhausting the live Xero
  tenant. Use unit tests or a fake Xero client that returns 429/Retry-After.
- If a real Xero demo-company test is required, run it from a separate app
  deployment with separate volume/state/admin DB/token files and separate
  calendar watches. Do not share the production Fly app, production state file,
  production Xero token, or production Google webhook targets.
- A 429 in any tenant handled by the production app sets the app-level
  `xero_lockout_until_ts` guard, so mixed demo/live testing inside production
  can pause live processing even if Xero's tenant quotas are separate.
- Before any Xero-related deployment, run:
  - `.venv/bin/python -m py_compile app/main.py app/event_processor.py app/admin_web.py`
  - `.venv/bin/python -m unittest discover -s tests`

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
- Xero Retry-After must be respected literally: do not probe Xero before the
  stored lockout timestamp expires, and keep proactive Xero health checks
  disabled by default (`XERO_HEALTH_CHECK_SECONDS=0`).
- Only reserve a per-cycle Xero event slot when the event genuinely needs a
  create, changed-draft update, explicit send, or paid-status reconcile. An
  unchanged event that already has a draft invoice must not consume the slot,
  otherwise it can starve later pending drafts on the same calendar.
- A calendar-level webhook target must not make every historical event on that
  calendar eligible for a Xero self-heal probe. Limit self-heal probes to an
  explicitly targeted event or a genuinely recent edit.
- Hourly reconcile windows and bounded cleanup queues.
- Draft-sync fingerprints are persisted on attempted draft-create calls so
  unchanged events do not repeatedly re-submit drafts during transient Xero failures.
- The attempted draft fingerprint must be written to persistent state before the
  Xero draft-create HTTP call. Keeping it only in memory is not enough: a deploy
  or process restart between the Xero response and the next state save can
  create a duplicate draft.
- Xero draft/send actions have a compact per-event action ledger:
  - human-readable line: `App status: ...`
  - machine-readable line: `[app]s=...;r=...;fp=...;x=...;w=...[/app]`
  - if the same draft/send fingerprint has failed enough times
    (`XERO_ACTION_MAX_ATTEMPTS`, default `2`), the app must stop that Xero
    action with `w=human_save` until a human saves changed content.
  - the ledger is intentionally separate from `[app-status]`, which is used by
    receipts/upload links on the receipts branch.
- Draft update must be fingerprint-gated (not generic `event.updated` gated):
  - in `app/main.py`, both draft-update paths now compute:
    - `_draft_sync_fingerprint(...)`
    - `last_draft_fp = get_draft_sync_fingerprint(...)`
    - `should_try_update = (draft_fp != last_draft_fp) or (not last_draft_fp)`
  - this is intentional anti-loop protection against webhook/metadata churn.

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
- A bounded upcoming actionable sweep intentionally omits `updatedMin` so old
  pre-filled `PROCESS DRAFT=Y` / `SEND=Y` entries are picked up when their job
  date approaches. Keep its lookahead short/capped and continue relying on state
  markers/Xero per-cycle limits for de-duplication.

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
- Accept common staff typos only when they normalize back to canonical labels
  (for example `Invoce profile:` -> `Invoice profile:`).
- Preserve internal sales section under `⬇Sales⬇`.
- Keep invoice totals/status lines in the expected formatting path.
- Bold handling currently runs through `event_processor` helpers; do not move ad-hoc to random call sites.
- Invoice-line normalizer must not reinterpret hyphenated descriptions as value separators.
  - `Pressure washing - Driveway = £165+VAT` must stay one description line.
- Repeated-separator corruption must be self-healed:
  - examples like `... = Driveway = = = £165+VAT` must normalize back to a single canonical line.
- The `⬇Sales⬇` area is the upsell section:
  - it must contribute to Xero invoice line items, invoice totals, and draft
    fingerprints exactly once,
  - it is also the source for the sales tracking spreadsheet,
  - mirrored copies above the marker must be removed/ignored.

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
      section above the marker, even though those lines are chargeable.
  - `extract_invoice_lines`:
    - parse customer-facing rows above `⬇Sales⬇`.
    - also parse rows below `⬇Sales⬇` as chargeable upsell invoice rows.
    - ignore matching above-marker copies of sales rows so each upsell is
      charged exactly once.

- `app/main.py`:
  - calendar-level Google webhooks must first use stored incremental sync tokens
    to resolve exact changed event IDs; when that succeeds, fetch/process those
    exact events and do not run a broader calendar scan for that notification;
  - missing/expired Google sync tokens may use a bounded fallback scan for that
    cycle, but token priming must not enqueue the historical result set as work;
  - paid-status reconcile is webhook-first. The scheduled paid sweep must be
    disabled by default (`PAID_SYNC_SWEEP_EVENTS_PER_CYCLE=0`); if deliberately
    enabled, it must stay capped, recent-only, and hard-limited to 14 days;
  - fallback webhook-targeted calendar scans must include a bounded recent-past actionable
    window (default 14 days) so unchanged `PROCESS DRAFT=Y` jobs are not missed
    after their appointment date;
  - the Xero per-cycle budget and fingerprint gates still apply to that lookback,
    so the broader read window must not create repeated draft/update calls;
  - a calendar-level webhook target is not event-level intent: it must not make
    every future draft immediately eligible or make every historical unpaid
    invoice consume the Xero slot; only an explicit event target, a recent event
    edit, or a deliberately enabled capped paid sweep may bypass those time
    gates;
  - draft update decision in both flow branches must remain fingerprint-led:
    - around `should_try_update` blocks (both occurrences),
    - never revert to `event_updated != last_invoice_update` as a primary update trigger.
  - send path (`has_send`) must perform a final pre-send draft sync for
    `CARD/INVOICE` payments:
    - update mutable draft invoice with current parsed `invoice_lines` before
      authorize/email/payment,
    - fail send with explicit error note if pre-send sync fails.
  - send path must build a send action fingerprint from draft fingerprint,
    invoice id, and payment mode before making Xero mutations. Do not remove
    this guard; it is what prevents repeated send/email/payment loops.
  - if Xero reports the invoice is already `PAID`, the app must mark the
    calendar entry sent/paid, write the normal sheet/sales rows, clear the
    action-attempt counter, and make no authorise/email/payment mutation.
  - if Xero reports the invoice is already `AUTHORISED`, the app must not
    re-authorise it.
  - `CARD` payment writes to Xero must use the calendar appointment date as the
    payment date, not the date the app happened to process the entry.
  - `CARD` payment writes from the Google Calendar/Xero flow must post to the
    Cashflows clearing account (`CASHFLOWS_CLEARING_ACCOUNT_CODE`, default
    `780 - Cashflow reconciliation`), not the real `Pow Wash` bank account. The
    Cashflows CSV reconciliation then creates the single net bank deposit that
    matches the `CFE SETT` bank-feed line. Keep the CSV reconciliation fallback
    that can move older CARD payments from `Pow Wash` into this clearing account,
    because historical invoices may still have been paid under the old design.

If any of the above is changed, run a targeted regression against:
- one line with hyphenated description (`A - B = £x+VAT`)
- one line with accidental extra equals (`A = B = = £x+VAT`)
- one event where `⬇Sales⬇` has values that must appear in invoice totals once
  and in sales tracking once
- one historically corrupted event where sales rows were mirrored above
  `⬇Sales⬇`
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
- Runtime receipt settings are stored in admin DB key `receipts_settings`.
- Signed receipt upload links are only injected for `PAYMENT TYPE = CARD` entries that show `Invoice sent ✅`.
- Receipt upload flow can call Google Document AI when receipts are enabled and parser settings are filled.
- Field Expenses receipt photos and approvals must save immediately, but
  lower-priority Xero bill/payment writes for subcontractor settlements are
  controlled by the visible Field Expenses setting
  `expense_settings.xero_submission_mode` plus
  `expense_settings.xero_submission_time`.
- Engineer-facing receipt pages must never create Xero bills as a hidden side
  effect. They may record that a payment settled receipts locally, then leave
  Xero submission waiting for admin timing/manual action.
- Automatic admin-page processing and the admin "Run due Xero submissions now"
  action are capped at 5 settlement bills per page load/click. Do not remove
  that cap without adding an equivalent Xero pressure guard.

Key files:
- `app/receipts/models.py`
- `app/receipts/store.py`
- `app/receipts/service.py`
- `app/admin_web.py` (`/receipts` routes)

## Cashflows Reconciliation Safety Contract

Current contract:
- `/cashflows-sync` is preview-first. `Scan & Preview Matches` may read Xero,
  Cashflows, and OpenAI, but must not mutate Xero.
- Confirmation is test-mode by default. Xero writes are blocked unless:
  - `DRY_RUN=false`, and
  - `CASHFLOWS_RECONCILE_PRODUCTION=true`.
- Preview results are stored server-side in admin DB key
  `cashflows_reconcile_preview`; confirm must reference that stored preview
  by `preview_id` and `match_id`.
- Browser-submitted match payloads must not be trusted for mutation.
- Matching order must remain:
  1. strict exact Cashflows net amount to `CFE SETT` bank amount within 5 days,
  2. mathematical combination scan,
  3. AI fuzzy fallback only for unresolved cases and only when
     `CASHFLOWS_RECONCILE_AI_ENABLED=true`.
- Production payloads must be inspectable in testing mode before enabling writes.
- The `/cashflows-sync` review modal must show the submission payload preview
  before confirmation, and `/cashflows-sync/diagnostics` must stay read-only.
- Merchant fees must map to `CASHFLOWS_BANK_FEES_ACCOUNT_CODE`; do not hard-code
  a different bank-fee account in route code.
- The Cashflows sync must stay outside the calendar worker. It must not consume
  the calendar event Xero processing slot or alter calendar event state.

Key files:
- `app/cashflows_reconciliation.py`
- `app/xero_client.py` (`get_bank_transactions`, `get_open_invoices`,
  `create_*_payload` helpers)
- `app/admin_web.py` (`/cashflows-sync` routes)

## Cashflows CSV Reconciliation Safety Contract

The live Cashflows settlement API has no usable public endpoint, so
reconciliation is driven by a manually-downloaded merchant-account statement
CSV that the user uploads. Preview is read-only. Submission is explicit: the
user must tick approved batches and press the submit button.

Current contract:
- `/cashflows-sync/upload-csv` parses the uploaded CSV and reads Xero
  (`get_bank_transactions`, `get_open_invoices`) to build a preview. It MUST
  NOT write to Xero.
- `/cashflows-sync/submit-csv-batches` accepts only checked batch IDs from the
  latest cached preview. It must rebuild money amounts from the server-side
  preview, not trust browser-submitted totals. By default it is test mode and
  prints/returns the Xero payloads without writing.
- CSV production writes require `DRY_RUN=false` AND
  `CASHFLOWS_CSV_SUBMIT_PRODUCTION=true`. Do not use the old API-flow
  `CASHFLOWS_RECONCILE_PRODUCTION` flag to enable CSV writes.
- A CSV batch checkbox means "approved for the submit button"; it must not
  itself call Xero.
- If Xero is unreachable (e.g. token expired → 403), the preview degrades
  gracefully: `xero_connected=false`, matching is skipped, parsed CSV totals
  and batch structure are still shown. Never fail the whole upload on a Xero
  read error.
- "Update basis" is enforced primarily by Xero state: only UNreconciled bank
  lines (`parse_xero_bank_lines` already drops reconciled + non-`CFE SETT`
  lines) and OPEN invoices (`parse_xero_invoices`) are considered. A secondary
  local guard, admin DB key `cashflows_csv_reconciled`
  (`get_cashflows_reconciled_refs` / `add_cashflows_reconciled_refs`), skips
  payouts this app has already reconciled. Phase 1 only READS this store; only
  a future Phase 2 write path may populate it.
- Statement model: `Sale Settlement` (Credit = gross), `Merchant Service
  Charge` (Debit = per-sale fee, keyed by `Sale Ref:`), `Decline Fee` (Debit),
  `Transfer for Remittance` (Debit = net payout that lands as a `CFE SETT`
  bank deposit). Amounts may contain comma thousands separators.
- Batch grouping is FIFO over the statement in row order: matured sale nets
  (gross − fee) and decline fees drain the running balance until a remittance
  amount is reached within `GROUP_TOLERANCE` (absorbs the small ~£0.04 decline
  fees / sub-penny rounding). Leftover un-drained sales are surfaced as
  "not paid out yet" and are expected to reconcile on a later upload.
- The bank-line match anchor is the EXACT payout amount (± half a penny) to an
  unreconciled `CFE SETT` line, preferring nearest date. Per-sale invoice
  matching is amount + date proximity. A sale is "ambiguous" only when more than
  one open invoice is tied at the same closest date distance; an ambiguous sale
  must NOT count toward a `ready` batch. A batch is `ready` only when a bank line
  is matched, every sale has an invoice, and `ambiguous_count == 0`; otherwise it
  is `needs_review` (ambiguity) or `waiting_invoices` (missing invoice). Each Xero
  bank line / invoice is consumed at most once per preview. AI tie-break for
  ambiguous sales is a Phase-2 enhancement; Phase 1 flags them for human review.
- Production CSV submission must not duplicate-pay invoices already marked paid
  in Xero. For an already-paid-only batch, the safe production write is a
  negative bank-side `SPEND` match-pack item for Cashflows merchant fees and any
  reviewed underpayment adjustment. The user then uses Xero `Find & Match` to
  select the existing invoice payment(s) plus that fee/adjustment item and press
  OK. Do not create a `RECEIVE`/clearing receipt for the gross or net Cashflows
  payout; that duplicates bank-account movement when invoice payments already
  exist.
- Underpayments/discounts must be handled by creating an `ACCRECCREDIT` sales
  credit note for the shortfall and allocating it to the selected invoice before
  the batch payment is posted. Do not silently rewrite original invoice line
  items to force a match.
- Overpayments may be represented as a separate "Parking" invoice only through
  the guarded submit flow.
- The CSV flow must stay outside the calendar worker and must not consume the
  calendar event Xero processing slot or alter calendar event state.

Key files:
- `app/cashflows_csv.py` (parser, FIFO allocation, matching, preview builder,
  `recommend_export_range`)
- `app/admin_store.py` (`get_cashflows_reconciled_refs`,
  `add_cashflows_reconciled_refs`)
- `app/admin_web.py` (`/cashflows-sync/upload-csv`,
  `/cashflows-sync/recommended-range`, CSV upload UI on the `/cashflows-sync`
  page)

## Receipts Branch Main-Parity Rule

The `feature/receipts-cashflows-sync` branch is allowed to add receipts,
Cashflows, Plaid, admin routes, parser helpers, and Xero helper methods, but the
calendar-to-Xero worker must behave like `main`.

Current rule:
- `app/main.py` must stay byte-for-byte identical to `main:app/main.py` on this
  branch unless a deliberate Xero/calendar change is also made on `main`.
- Receipt upload links, Cashflows reconciliation, email receipt scanning, and
  Plaid/card-feed tools must stay outside the live calendar worker. They must
  not change calendar colours, SEND/DONE handling, Xero draft/send/payment
  decisions, Xero pressure budgeting, or retry/lockout behaviour.
- Additive helpers are acceptable only when the core worker does not call them.
  Examples: `app/xero_client.py` Cashflows read/write-preview helpers,
  attachment helpers, and admin-only pause helpers.
- Regression guard: `tests/test_main_parity.py` compares `app/main.py` to
  `main:app/main.py`. Do not loosen that test to hide branch drift.

## Field Expenses / Receipt Dump Guardrails

- Receipt dump bank matching must never guess across all bank accounts. If a
  dump has no chosen card/bank account, show a clear "bank matching has not run"
  prompt and require the user to choose the account first.
- AI-selected receipt categories and fallback defaults must resolve to a real
  Xero expense account code before a receipt can be approved or imported. Human
  labels such as "materials" are only accepted if they unambiguously match one
  real Xero account name; otherwise the receipt stays in "Needs account".
- Fallback/default expense accounts are last resort only. Obvious receipt
  classes must be enforced after AI categorisation: diesel/DERV/AdBlue must not
  be posted to machinery fuel or machinery expenses; garages, tyres, car parts,
  MOT, brakes, servicing and repairs must be posted to vehicle repairs or a
  motor vehicle account; Screwfix/Toolstation/builders/roofing merchants should
  be materials unless the receipt itself clearly says otherwise. If the required
  Xero account cannot be found, hold the receipt for manual account selection
  instead of silently using the fallback.
- Do not let "Keep" or "Import" promote a receipt with a blank or invalid Xero
  category. Push it back to manual review instead.
- Duplicate protection is image-first where possible: identical stored receipt
  hashes are duplicates; same merchant/date/amount against an existing claim is
  suspicious unless the image comparison proves it is different. Existing Xero
  attachments may be fetched only for a known prior receipt record, not by
  broad-scanning Xero attachments.
- Receipt dump duplicate protection must also check Xero itself, not only the
  app's local receipt table. A same-date and same-amount Xero `SPEND` bank
  transaction means the expense is already submitted in Xero and must not be
  imported again. Cache these Xero reads by date window so a large dump does not
  create one Xero request per photo.
