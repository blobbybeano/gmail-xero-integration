# Google Calendar to Xero Bridge

## Overview
A web application that automates the creation of Xero invoices based on Google Calendar events. It monitors specified calendars for events marked with a configurable keyword (default: "DONE"), extracts invoice details, and creates draft invoices in Xero. Also supports logging to Google Sheets.

## Architecture
- **Backend**: Python 3.12 with Flask (3.12 required — code uses 3.12-only f-string syntax)
- **WSGI Server**: Gunicorn (production)
- **APIs**: Google Calendar API, Google Sheets API, Xero API
- **AI**: OpenAI using the app's OWN API key (set in Settings, stored in the admin DB; falls back to `OPENAI_API_KEY` env var). Used for calendar parsing and auto-categorising Field Expenses receipts. Does NOT use Replit AI Integrations.

## Field Expenses — Receipt Dump
Bulk tool (under Field Expenses) for uploading many past, mostly-unsubmitted receipts at once:
- Each receipt is OCR'd, VAT-reconciled and AI-coded to a Xero account (split-aware).
- Duplicate detection: exact image (sha256), in-batch + cross-batch, and logical match
  (merchant + date + amount). Cross-person look-alikes are image-compared via OpenAI vision
  (the "receipt checker"), pulling the other person's image back from Xero when the local file
  is gone; confirmed matches are discounted, uncertain ones flagged for review.
- Card-feed reconciliation flags receipts missing from the company card feed (asks which account).
- Subcontractor batches balance owed receipts against Xero payments to the account, to date.
  Before a subcontractor batch is imported, a reconciliation preview page (`/dump/<id>/confirm`)
  shows the owed/paid/net balance and post-import projection; admin must explicitly confirm.
- "Test mode" toggle on upload runs the full pipeline (OCR, AI coding, dedupe) as a dry-run:
  the results page shows the AI's per-receipt account/VAT/split decisions but imports nothing
  (import + confirm routes hard-block test batches). Batch flag stored as `is_test` in dump_store.
- Includes an inline help guide and subtle "?" tooltips.
- All Xero-dependent checks are gated by `XERO_DISABLED` and degrade gracefully (explicit
  "paused" messaging) while Xero is off.
- Code: `app/receipts/dump_store.py` (batches/items storage), dump routes/pipeline/helpers in
  `app/admin_web.py`, plus `get_payments_to_account` / `get_attachments` / `get_attachment_content`
  in `app/xero_client.py`.

## Email Invoice Scan (/receipts/emails)
Scans Gmail for supplier invoices and stages them like a receipt dump batch.
- Start form includes "Card these were paid from" (sticky, shares dump_last_card).
- NOTHING doubtful is silently dropped: emails skipped by the AI gate and
  emails whose attachments were all rejected by the photo/logo filter are
  stored as amountless placeholder items ("Skipped — check me"). EXCEPTION:
  our OWN outgoing invoices (own sender domain/name, or subject "... from Pow
  Services Limited" — incl. customer reply/query threads) are dead space and
  dropped entirely, only counted. Placeholders appear in a
  "Skipped emails — worth a quick check" group
  with two actions: "Scan anyway" (POST `/receipts/emails/<batch>/item/<id>/rescan`
  → `rescan_message` force-processes that one email, bypassing AI gates and the
  attachment filter but still running OCR/dedupe/categorise; summary buckets
  updated status-aware) and "Remove" (status=ignored). Placeholders never
  import (import only takes status "new") and never affect card recon.
- Merchant fix-up: OCR often extracts OUR OWN name as the merchant on invoices
  addressed to us (it's the biggest name on the page), making items
  unrecognisable in the list. `derive_supplier_merchant` replaces an
  own-company merchant with the real supplier: subject "from <Supplier> [for
  us]" → sender display name → sender email domain. Applied in scan + rescan.
- Every reviewable item card shows the AI-chosen Xero account as a pill
  ("AI → 401 — Audit & Accountancy fees", amber "No account yet" when the AI
  couldn't pick) plus a one-click "✕ Cross off — don't send to Xero" button
  (status=ignored; ignored cards get "↺ Restore — import after all"). Import
  only takes status "new", so crossed-off invoices never reach Xero.
- Per-row card-feed match pills (same `_CARD_PILL` kinds as the receipt dump)
  render on each item card via a recon_map built from `_dump_bank_feed_recon`;
  the "Still unreconciled" panel shows at the bottom. The batch card must be
  the Xero bank account NAME (e.g. "Pow Wash"), not an account number.
- Code: `app/receipts/email_pipeline.py` (scan + rescan), `email_store.py`,
  routes/cards in `app/admin_web.py`.

## Engineer / Subcontractor Phone Portal
A very simple, mobile-first portal for field staff, logged in with an
admin-set **username + password** (replaces the old no-password token links;
the `/expenses/<token>` routes still exist but are now login-gated).
- Login at `/portal` (sets `session['engineer_id']`); `/portal/logout`.
- Credentials are set per engineer on the admin expenses page; passwords are
  hashed with `werkzeug.security`. Each engineer can also be linked to ONE Plaid
  card (`plaid_account_id`) and reuses the existing receipt parser + AI coding.
- **Company-card engineers**: see only their own card's transactions (filtered by
  `plaid_account_id`) for ~the last month. A green tick appears once a matching
  receipt is uploaded; a line moves to a muted "Reconciled" list (and out of the
  active feed) once it is reconciled in Xero (`IsReconciled`). Xero reconciliation
  is read best-effort and cached ~10 min (`engineer_recon_cache`).
- **Subcontractors**: upload receipts (no bank rec), see a running owed balance
  (visible to admin too, shown as "Pay £X to settle") and a stable payment
  reference `PWSUB<id>`. One bank payment usually covers a COMBINATION of
  receipts, so when a reference-matching payment appears in the Plaid feed it is
  reconciled by amount (`_allocate_receipts_to_payment`, a 0/1 subset-sum in
  pennies, ±2p): only the matched receipts are settled, the rest stay owed.
  Over/under payments are automatic and flagged on the settlement `note`
  (warning shown to admin + subcontractor): `overpaid` (paid more than owed) or
  `review` (didn't match a clean combination — best subset auto-settled, rest
  owed). Matching is reference-only + idempotent per Plaid `transaction_id`
  (unique index), so a payment is never double-applied. Submitted/settled
  receipt photos older than ~30 days are purged from disk (figures kept, only
  when recoverable via `xero_id`), and pulled back from Xero on demand.
- Code: helpers + routes in `app/admin_web.py` (`_engineer_card_feed_html`,
  `_maybe_settle_subcontractor`, `_exp_purge_old_photos`,
  `_exp_pull_receipt_image_from_xero`); schema + settlements in
  `app/receipts/expense_store.py`.

## Card Feed — CSV upload (primary) + Enable Banking (dormant)
Dump receipts are matched to the company card's REAL payments because Xero's
API never exposes unreconciled bank-feed statement lines. As of July 2026 there
is NO free automated route to the company's Lloyds feed (Enable Banking has no
Lloyds UK production support; GoCardless closed to new sign-ups Jul 2025;
Plaid/TrueLayer have no free production tier; Xero's BankStatementsPlus is
gated behind partner certification — verified by live probe). So the PRIMARY
feed source is a manual CSV upload of the Lloyds internet-banking export.
- `/cardfeed` page: "Statement upload (CSV)" section — upload the standard
  Lloyds CSV (Transaction Date, Type, Sort Code, Account Number, Description,
  Debit Amount, Credit Amount, Balance). Rows are normalised (money OUT
  positive, money IN negative), deduped via deterministic ids (content hash +
  in-file occurrence index, so overlapping re-uploads are safe) and persisted
  in the admin DB for ~a year (`RETENTION_DAYS=366`), so the matcher always
  has history. Clear button wipes the store. Routes: `/cardfeed/csv-upload`,
  `/cardfeed/csv-clear`.
- `app/card_feed.py` is the facade the rest of the app talks to
  (`is_connected` / `connection_status` / `get_cached_transactions`): it merges
  Enable Banking (if ever connected) with the CSV store, one normalised shape,
  so matching/settlement/engineer feeds don't care about the source. The CSV
  account number (e.g. 60563768) is what gets linked into
  `expense_engineers.plaid_account_id`.
- CSV logic: `app/csv_card_feed.py`. Enable Banking remains below as a dormant
  option in case the business ever banks with a supported institution.
- Connect/manage the bank at `/cardfeed` (login-gated). The connect flow is a
  bank redirect: pick the bank → POST /auth → bank login → `/cardfeed/callback`
  → POST /sessions → store. Credentials are env-only: `ENABLE_BANKING_APP_ID` and
  `ENABLE_BANKING_PRIVATE_KEY` (RSA .pem, multiline; used to sign RS256 JWTs;
  NEVER stored in the DB or logged). Optional: `ENABLE_BANKING_REDIRECT_URI`
  (default `{host}/cardfeed/callback`; must be whitelisted in the EB app),
  `ENABLE_BANKING_COUNTRY` (GB), `ENABLE_BANKING_PSU_TYPE` (business),
  `ENABLE_BANKING_VALID_DAYS`/`ENABLE_BANKING_FETCH_DAYS` (90). The bank
  `session_id` is encrypted at rest (Fernet, keyed off `WEB_SECRET_KEY`).
- Sessions last up to ~180 days (banks often cap consent at 90); reconnect at
  `/cardfeed` when matching goes quiet.
- Matching is date-tolerant (receipt OCR dates can't be trusted): price (penny) →
  ±~1 month → name similarity. Ambiguous hits are flagged for human review, never
  silently matched.
- The Enable Banking account `uid` is stored in the engineer column
  `plaid_account_id` (column name kept to avoid a migration; it is just an opaque
  "linked card account id"). Likewise `expense_settlements.plaid_tx_id` holds the
  EB transaction id. Cached card transactions are normalised with money-OUT
  (DBIT) positive and money-IN (CRDT) negative.
- Code: `app/enable_banking_client.py` (secure JWT client + encrypted session
  storage, mirrors the old plaid_client interface), `app/plaid_match.py`
  (provider-agnostic matcher, unchanged), routes + wiring in `app/admin_web.py`.

## Project Structure
- `app/` - Core application code
  - `admin_web.py` - Flask admin dashboard (main web UI)
  - `main.py` - Background worker/polling service
  - `config.py` - Configuration via environment variables
  - `event_processor.py` - Business logic for parsing calendar events
  - `google_calendar.py` / `google_admin.py` / `google_sheets.py` - Google API clients
  - `xero_client.py` - Xero API wrapper
  - `admin_store.py` - SQLite-backed runtime settings
  - `state.py` - Sync state tracking
- `run.py` - Entry point for the Flask web server (port 5000)
- `scripts/` - Utility/testing scripts
- `requirements.txt` - Python dependencies

## Running the Application
The Flask admin dashboard runs on port 5000 via `python run.py`.

## Configuration (Environment Variables)
Key environment variables (all have defaults):
- `ADMIN_USERNAME` / `ADMIN_PASSWORD` - Admin login (default: admin/changeme)
- `WEB_SECRET_KEY` - Flask session secret key
- `GOOGLE_CREDENTIALS_FILE` - Path to Google OAuth credentials JSON
- `GOOGLE_OAUTH_REDIRECT_URI` - OAuth callback URL
- `XERO_CLIENT_ID` / `XERO_CLIENT_SECRET` - Xero API credentials
- `XERO_REDIRECT_URI` - Xero OAuth callback URL
- `KEYWORD` - Event keyword to trigger processing (default: DONE)
- `DRY_RUN` - If true, don't create real invoices (default: true)

## Deployment
Configured for autoscale deployment running gunicorn on port 5000.
