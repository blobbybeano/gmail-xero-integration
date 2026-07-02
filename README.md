# Google Calendar -> Xero Bridge (Python)

Small, modular app that watches Google Calendar events for a keyword and sends matching events to Xero.

## What it does
- Polls Google Calendar for recently updated events
- Supports multiple active calendars (managed from admin web page)
- Prefills notes on newly created events with a customer template
- Checks for the keyword `DONE` (configurable)
- Extracts event details
- Sends the details to Xero (example creates a draft invoice)
- Optional Google Sheets logging for sent invoices (configurable fields)
- Provides standalone connectivity scripts for Google and Xero

## Setup
1. Create a virtual environment and install deps:
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

2. Configure environment:
```bash
cp .env.example .env
```
Fill in your values in `.env`.

Important for Xero granular scopes (newer apps):
```env
XERO_SCOPES=offline_access accounting.invoices accounting.payments accounting.contacts accounting.settings
```

3. Google Calendar auth:
- Create an OAuth client and download `credentials.json`
- The first run will open a browser to authorize and write `google_token.json`

4. Xero auth (easiest):
- Set in `.env`:
  - `XERO_CLIENT_ID`
  - `XERO_CLIENT_SECRET`
  - `XERO_REDIRECT_URI=http://localhost:8080/xero/callback`
- Open admin page and click **Connect / Reconnect Xero**
- Complete login and org selection in Xero
- The app stores token + tenant in `xero_token.json` and refreshes automatically.

Optional legacy helper:
```bash
python scripts/xero_oauth.py
```
This helper still works for manual OAuth testing.

## Run the app
```bash
python -m app.main
```

## Run admin web page
```bash
python -m app.admin_web
```

Then open `http://localhost:8080` and:
- Connect Google
- Connect Xero
- Select active calendars
- Select spreadsheet + sheet tab
- Tick which stats fields should be posted to Sheets

The worker (`app.main`) reads these settings live from `ADMIN_DB_FILE`.

## Connectivity tests
```bash
python scripts/test_env.py
python scripts/test_google.py
python scripts/test_xero.py
```

## Key files
- `app/main.py` main loop
- `app/admin_web.py` admin dashboard (login + Google connect + settings)
- `app/admin_store.py` persisted runtime settings
- `app/google_calendar.py` Google Calendar auth + queries
- `app/google_admin.py` Google OAuth + calendar/sheets listing for admin page
- `app/google_sheets.py` append stats rows to Sheets
- `app/event_processor.py` keyword check + extraction
- `app/xero_client.py` Xero REST wrapper
- `app/state.py` last sync tracking
- `app/receipts/` receipt-processing scaffold (feature-flagged, isolated)
- `app/cashflows_reconciliation.py` Cashflows settlement/Xero invoice matching engine

## Engineering Guardrails
- Read `docs/ENGINEERING_LOGIC_GUARDRAILS.md` before editing core logic.
- If you modify `app/main.py`, `app/admin_web.py`, or `app/event_processor.py`, update that document in the same commit.

## Notes
- By default it runs in `DRY_RUN=true` to avoid writes to Xero.
- It now de-duplicates sends and only posts each event once.
- The Xero payload is intentionally minimal and should be customized.
- The app now ignores any events created before the current run started.
- It polls for new/updated events every `POLL_SECONDS` (default 30s). Set `RUN_ONCE=true` to run a single pass.
- When `DONE` is present, it creates/updates a Xero Contact from the customer fields.
- If an `<invoice>...</invoice>` block is present, it creates a draft invoice from those lines.

## Cashflows Sync
- Admin route: `/cashflows-sync`
- Scans Xero `CFE SETT` bank lines, Cashflows settlements, and open Xero invoices.
- Use `Test API Reads` first after deployment. It reports Xero read counts and
  Cashflows settlement-read status without writing to Xero.
- If Cashflows settlement reads return 404 while the endpoint/action is being
  corrected, paste sample/manual settlement JSON into the page to test matching,
  review modals, and Xero submission payload previews without Cashflows API reads.
- Preview mode is read-only.
- Confirm is test-mode by default and prints the intended Xero payloads to the server log.
- The Review modal also shows the exact submission payload preview before confirm.
- Production writes require both:
  - `DRY_RUN=false`
  - `CASHFLOWS_RECONCILE_PRODUCTION=true`
- AI matching is off unless `CASHFLOWS_RECONCILE_AI_ENABLED=true`.
- Merchant fees use `CASHFLOWS_BANK_FEES_ACCOUNT_CODE`.

## Current Production Flow (Do Not Change)
- Trigger: event is processed when `Y/N =Y` is present in the notes body.
- Event title dots:
  - `🔵` newly formatted template
  - `🟠` edited/processing
  - `🟡` invoice sent, pending payment
  - `🟢` paid/complete
- Sheet routing rules:
  - `Master Sheet`: logs non-cash invoice/card flows (cash excluded)
  - `Cash (All)`: logs every cash payment
  - `Sales Tracking`: logs sales lines (`⬇sales⬇`) per calendar mapping
  - `Cash Tracking`: logs cash payments per calendar mapping

### Expected compact calendar format
```text
[notes]

[/notes]

[contact]
Customer name:
Customer email address:
Customer contact number:
[/contact]

[invoice]
job line = £35+VAT

⬇sales⬇
field upsell = £10+VAT

[/invoice]

Y/N =Y

[app-status]
Invoice total (ex VAT): £45.00
Invoice total (inc VAT): £54.00
Entry complete ✅
[/app-status]
```

## Fly.io
This repo includes:
- `Dockerfile`
- `fly.toml` with two process groups:
  - `web` (admin dashboard)
  - `worker` (calendar->xero processor)

Typical deploy flow:
```bash
fly launch --copy-config --no-deploy
fly secrets set ADMIN_PASSWORD=... WEB_SECRET_KEY=... XERO_CLIENT_ID=... XERO_CLIENT_SECRET=...
fly deploy
fly scale count web=1 worker=1
```
# gmail-xero-integration
