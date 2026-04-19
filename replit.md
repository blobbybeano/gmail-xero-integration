# Google Calendar to Xero Bridge

## Overview
A web application that automates the creation of Xero invoices based on Google Calendar events. It monitors specified calendars for events marked with a configurable keyword (default: "DONE"), extracts invoice details, and creates draft invoices in Xero. Also supports logging to Google Sheets.

## Architecture
- **Backend**: Python 3.12 with Flask
- **WSGI Server**: Gunicorn (production)
- **APIs**: Google Calendar API, Google Sheets API, Xero API

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
