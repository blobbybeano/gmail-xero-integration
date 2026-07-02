# Environment Setup — Replit ↔ Fly.io Switching Guide

This app runs in two environments: **Replit** (development/preview) and **Fly.io**
(production, managed via Codex). This file describes every difference between them
and what to change when switching.

---

## Quick summary

| Setting | Replit | Fly.io |
|---|---|---|
| Port | `5000` | `8080` |
| Data files | Local workspace paths | `/data/` persistent volume |
| Public URL | Your Replit dev domain | `https://gmail-xero-integration.fly.dev` |
| Secrets | Replit Secrets tab | `fly secrets set ...` |
| Start command | `python run.py` | `gunicorn -b 0.0.0.0:8080 ...` (fly.toml) |

---

## 1. Port

**Fly.io** runs on port `8080` (set in `fly.toml` via `WEB_PORT = "8080"`).  
**Replit** runs on port `5000` (the default in `run.py`).

`run.py` reads `WEB_PORT` from the environment, so this is automatic — no code change
needed. On Replit, just don't set `WEB_PORT`.

---

## 2. Data file paths

Fly.io uses a persistent volume mounted at `/data/`. Replit stores files locally in
the workspace.

| Variable | Fly.io value | Replit default |
|---|---|---|
| `ADMIN_DB_FILE` | `/data/admin.db` | `admin.db` (workspace root) |
| `STATE_FILE` | `/data/state.json` | `state.json` (workspace root) |
| `XERO_TOKEN_FILE` | `/data/xero_token.json` | `xero_token.json` |
| `GOOGLE_TOKEN_FILE` | `/data/google_token.json` | `google_token.json` |
| `GOOGLE_ADMIN_TOKEN_FILE` | `/data/google_admin_token.json` | `google_admin_token.json` |
| `GOOGLE_CREDENTIALS_FILE` | `/data/credentials.json` | `credentials.json` |

These are all set in `fly.toml [env]` for Fly.io. On Replit they fall back to local
paths automatically — **do not set these in Replit Secrets** unless you want to
override them.

---

## 3. Public base URL (for webhooks and OAuth callbacks)

The app needs to know its own public URL to register Google Calendar webhooks and
to construct OAuth redirect URIs.

**Resolution order** (in `app/main.py`):
1. `public_base_url` setting in the admin DB — **set this in Admin → Settings**
2. `FLY_APP_NAME` or `FLY_APP` environment variable → builds `https://<name>.fly.dev`
3. Falls back to `GOOGLE_OAUTH_REDIRECT_URI` or other candidates

### On Replit
Set `public_base_url` in the admin dashboard Settings page to your Replit dev domain:
```
https://<your-repl-name>.<your-username>.replit.dev
```
Leave `FLY_APP_NAME` unset — Replit will skip the Fly.io branch automatically.

### On Fly.io
`FLY_APP_NAME` is set automatically by the Fly.io runtime. You can also set
`public_base_url` in the admin dashboard to `https://gmail-xero-integration.fly.dev`.

---

## 4. OAuth redirect URIs

Google and Xero OAuth flows need redirect URIs that match the active environment.
These must be registered in the respective developer consoles AND set as env vars.

| Variable | Replit value | Fly.io value |
|---|---|---|
| `GOOGLE_OAUTH_REDIRECT_URI` | `https://<repl>.replit.dev/oauth2callback` | `https://gmail-xero-integration.fly.dev/oauth2callback` |
| `XERO_REDIRECT_URI` | `https://<repl>.replit.dev/xero/callback` | `https://gmail-xero-integration.fly.dev/xero/callback` |

If you have both URIs registered in Google Cloud Console and Xero developer portal,
you can set the correct one per environment in the respective Secrets store.

---

## 5. Secrets / environment variables

**Replit**: set in the Replit Secrets tab (padlock icon). Available at runtime as
`os.environ`.

**Fly.io**: set via `fly secrets set KEY=value`. Stored encrypted in Fly.io.

Both environments share the same secret names:
- `XERO_CLIENT_ID`, `XERO_CLIENT_SECRET`
- `ADMIN_USERNAME`, `ADMIN_PASSWORD`
- `WEB_SECRET_KEY`
- `PLAID_CLIENT_ID`, `PLAID_SECRET`
- `OPENAI_API_KEY` (or set via Admin → Settings in the app)

---

## 6. What auto-detects (nothing to change)

These differences are handled automatically by the code:

- **Port**: `run.py` reads `WEB_PORT`, defaults to `5000`
- **Fly.io URL**: `FLY_APP_NAME` only present on Fly.io; skipped on Replit
- **SQLite paths**: all default to workspace root on Replit
- **`pillow-heif`**: already installed in Replit (`v1.4.0`)

---

## 7. Codex ↔ Replit workflow

- **Codex** works on the `feature/receipts-cashflows-sync` branch on GitHub
- **Replit** is the live working environment (this workspace)
- After Codex pushes changes, pull them into Replit by downloading the changed files
  from the GitHub branch (do not do a full `git pull` — use targeted file updates to
  avoid overwriting Replit-only files like `app/admin_web.py`)
- After Replit work, push the full snapshot to `feature/receipts-cashflows-sync`
  using the GitHub API (as done in this session)

**Files Codex owns** (safe to overwrite from GitHub):
`app/main.py`, `app/event_processor.py`, `app/google_calendar.py`,
`app/google_sheets.py`, `app/state.py`, `app/xero_client.py`,
`app/safety_simulator.py`, `app/cashflows_csv.py`

**Files Replit owns** (never overwrite from GitHub):
`app/admin_web.py`, `app/plaid_client.py`, `app/plaid_match.py`,
`app/receipts/expense_store.py`, `app/receipts/dump_store.py`,
`app/receipts/email_pipeline.py`, `app/receipts/email_store.py`,
`app/cashflows_calendar.py`, `app/cashflows_reconciliation.py`,
`app/cashflows_sheet.py`
