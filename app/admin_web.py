from __future__ import annotations

import datetime as dt
import base64
import json
import re
import secrets
import time
import urllib.parse
from pathlib import Path
from functools import wraps
from html import escape

import requests
from flask import Flask, redirect, request, session, url_for
from googleapiclient.errors import HttpError

from .admin_store import (
    DEFAULT_STATS_FIELDS,
    get_active_calendars,
    get_json_setting,
    get_sheet_target,
    get_seen_submitters,
    get_stats_fields,
    get_submitter_aliases,
    init_admin_store,
    set_submitter_aliases,
    set_active_calendars,
    set_json_setting,
    set_sheet_target,
    set_stats_fields,
)
from .config import load_config
from .google_admin import (
    build_calendar_service_from_creds,
    build_sheets_service_from_creds,
    list_calendars,
    list_spreadsheets,
    load_admin_credentials,
    oauth_authorization_url,
    oauth_exchange_code,
    save_admin_credentials,
)
from .config import AppConfig
from .xero_client import load_xero_token, save_xero_token, token_is_expired


XERO_AUTH_URL = "https://login.xero.com/identity/connect/authorize"
XERO_TOKEN_URL = "https://identity.xero.com/connect/token"
XERO_CONNECTIONS_URL = "https://api.xero.com/connections"


STAT_OPTIONS = [
    ("submitter", "Person who submitted invoice"),
    ("invoice_number", "Invoice number"),
    ("receipt_details", "Receipt details (when implemented)"),
    ("slot_datetime", "Diary slot date/time"),
    ("payment_datetime", "Payment date/time"),
    ("payment_method", "Payment method"),
    ("job_cost_ex_vat", "Job cost (ex VAT)"),
    ("job_cost_inc_vat", "Job cost (inc VAT)"),
]

_BASE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Powwash Admin</title>
<script src="https://cdn.tailwindcss.com"></script>
<style>
  body {{ font-family: 'Inter', system-ui, sans-serif; }}
  .status-dot-green {{ display:inline-block;width:10px;height:10px;border-radius:50%;background:#22c55e;margin-right:6px; }}
  .status-dot-red {{ display:inline-block;width:10px;height:10px;border-radius:50%;background:#ef4444;margin-right:6px; }}
  .status-dot-yellow {{ display:inline-block;width:10px;height:10px;border-radius:50%;background:#f59e0b;margin-right:6px; }}
</style>
</head>
<body class="bg-gray-50 min-h-screen">
{body}
</body>
</html>"""


def _page(body: str) -> str:
    return _BASE_HTML.format(body=body)


def _extract_spreadsheet_id(value: str) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", text)
    if m:
        return m.group(1)
    return text


def _first_url(text: str) -> str:
    m = re.search(r"https://[^\s\"'<>]+", text or "")
    return m.group(0) if m else ""


def _validate_google_credentials_json(raw: bytes) -> tuple[bool, str]:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception:
        return False, "File is not valid UTF-8 JSON."

    section = None
    if isinstance(payload, dict):
        if isinstance(payload.get("installed"), dict):
            section = payload["installed"]
        elif isinstance(payload.get("web"), dict):
            section = payload["web"]
    if not section:
        return False, "JSON must contain 'installed' or 'web' OAuth credentials."

    client_id = str(section.get("client_id") or "").strip()
    client_secret = str(section.get("client_secret") or "").strip()
    if not client_id or not client_secret:
        return False, "Credentials JSON is missing client_id/client_secret."
    return True, ""


def _sheets_status_data(
    config: AppConfig, creds, target: dict[str, str]
) -> tuple[bool, str]:
    if not creds:
        return False, "Not connected to Google yet."

    spreadsheet_id = (target.get("spreadsheet_id") or "").strip()
    sheet_name = (target.get("sheet_name") or "Sheet1").strip() or "Sheet1"
    if not spreadsheet_id:
        return False, "No spreadsheet URL or ID saved yet."

    scopes = set(getattr(creds, "scopes", []) or [])
    if "https://www.googleapis.com/auth/spreadsheets" not in scopes:
        return False, "Token is missing spreadsheets scope. Reconnect Google."

    try:
        service = build_sheets_service_from_creds(creds)
        meta = (
            service.spreadsheets()
            .get(
                spreadsheetId=spreadsheet_id,
                fields="properties.title,sheets.properties.title",
            )
            .execute()
        )
    except HttpError as exc:
        text = str(exc)
        reason = ""
        message = text
        try:
            payload = json.loads((exc.content or b"{}").decode("utf-8"))
            err = payload.get("error", {}) if isinstance(payload, dict) else {}
            message = err.get("message") or message
            for d in err.get("details", []) or []:
                if isinstance(d, dict) and d.get("reason"):
                    reason = d["reason"]
                    break
            if not reason:
                first_err = (err.get("errors") or [{}])[0]
                if isinstance(first_err, dict):
                    reason = first_err.get("reason", "")
        except Exception:
            pass

        if reason == "SERVICE_DISABLED":
            url = _first_url(text) or "https://console.cloud.google.com/apis/library/sheets.googleapis.com"
            return False, f"Google Sheets API is disabled. Enable it at: {url}"
        if exc.resp and exc.resp.status == 404:
            return False, "Spreadsheet not found. Check the URL/ID."
        if exc.resp and exc.resp.status == 403:
            return False, f"No access to this spreadsheet: {message}"
        return False, f"Failed to access spreadsheet: {message}"

    workbook = (meta.get("properties", {}) or {}).get("title", "")
    tabs = {
        ((s.get("properties", {}) or {}).get("title") or "").strip()
        for s in (meta.get("sheets") or [])
    }
    if sheet_name not in tabs:
        return False, f"Spreadsheet found but tab '{sheet_name}' does not exist in '{workbook or spreadsheet_id}'."

    return True, f"Connected — {workbook or spreadsheet_id} / {sheet_name}"


def _oauth_client_id(config: AppConfig) -> str:
    path = Path(config.google_credentials_file)
    if path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            section = payload.get("web") or payload.get("installed") or {}
            return str(section.get("client_id") or "")
        except Exception:
            return ""
    return ""


def _xero_scope_string(scopes: list[str]) -> str:
    parts = [s.strip() for s in scopes if s and s.strip()]
    return " ".join(parts)


def _get_xero_creds(config: AppConfig) -> tuple[str, str]:
    """Return (client_id, client_secret) from env vars or JSON settings store."""
    client_id = config.xero_client_id or str(
        get_json_setting(config.admin_db_file, "xero_client_id", "")
    ).strip()
    client_secret = config.xero_client_secret or str(
        get_json_setting(config.admin_db_file, "xero_client_secret", "")
    ).strip()
    return client_id, client_secret


def _xero_authorization_url(config: AppConfig, state: str) -> str:
    client_id, _ = _get_xero_creds(config)
    # Build manually so spaces in scope are encoded as %20 (not +) — Xero requires %20
    scope_str = _xero_scope_string(config.xero_scopes)
    base_params = urllib.parse.urlencode({
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": config.xero_redirect_uri,
        "state": state,
    })
    scope_param = "scope=" + urllib.parse.quote(scope_str, safe="")
    return f"{XERO_AUTH_URL}?{base_params}&{scope_param}"


def _exchange_xero_code(config: AppConfig, code: str) -> dict:
    client_id, client_secret = _get_xero_creds(config)
    basic = base64.b64encode(
        f"{client_id}:{client_secret}".encode()
    ).decode()
    headers = {"Authorization": f"Basic {basic}"}
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": config.xero_redirect_uri,
    }
    response = requests.post(XERO_TOKEN_URL, headers=headers, data=data, timeout=30)
    response.raise_for_status()
    token = response.json()
    token["issued_at"] = int(time.time())
    return token


def _get_xero_tenant_id(access_token: str) -> str:
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
    response = requests.get(XERO_CONNECTIONS_URL, headers=headers, timeout=30)
    response.raise_for_status()
    connections = response.json()
    if not connections:
        raise RuntimeError("No Xero organisation connections returned.")
    return connections[0].get("tenantId", "")


def _xero_status_data(config: AppConfig) -> tuple[bool, str, str]:
    """Returns (connected, status_text, tenant_id)"""
    token = load_xero_token(config.xero_token_file)
    client_id, client_secret = _get_xero_creds(config)
    has_credentials = bool(client_id and client_secret)
    if not has_credentials:
        return False, "Enter your Xero Client ID and Secret below, then click Save.", ""
    access = token.get("access_token")
    tenant = token.get("tenant_id")
    if access and tenant:
        exp = "expired" if token_is_expired(token) else "valid"
        return True, f"Connected (token {exp})", tenant
    return False, "Not connected — click Connect Xero below.", ""


def _save_submitter_aliases_from_form(config: AppConfig, form) -> dict[str, str]:
    current = get_submitter_aliases(config.admin_db_file)
    next_aliases = dict(current)
    prefix = "submitter_alias__"
    for k, v in form.items():
        if not k.startswith(prefix):
            continue
        email = k[len(prefix):].strip().lower()
        name = (v or "").strip()
        if not email:
            continue
        if name:
            next_aliases[email] = name
        else:
            next_aliases.pop(email, None)
    set_submitter_aliases(config.admin_db_file, next_aliases)
    return next_aliases


def _apply_alias_to_description(
    description: str,
    aliases: dict[str, str],
    fallback_email: str = "",
) -> tuple[str, bool]:
    if not description:
        return description, False

    m = re.search(
        r"(\[app-status\])(.*?)(\[/app-status\])",
        description,
        flags=re.I | re.S,
    )
    if not m:
        return description, False

    start, block, end = m.group(1), m.group(2), m.group(3)
    lines = block.splitlines()
    idx = None
    for i, line in enumerate(lines):
        plain = re.sub(r"<[^>]+>", "", line).strip().lower()
        if plain.startswith("submitted by:"):
            idx = i
            break

    mapped_name = ""
    if idx is not None:
        plain_line = re.sub(r"<[^>]+>", "", lines[idx]).strip()
        found = re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", plain_line)
        email = found.group(0).lower() if found else ""
        mapped_name = aliases.get(email, "")
        if not mapped_name:
            return description, False
        new_line = f"Submitted by: {mapped_name}"
        if lines[idx].strip() == new_line:
            return description, False
        lines[idx] = new_line
    else:
        mapped_name = aliases.get((fallback_email or "").strip().lower(), "")
        if not mapped_name:
            return description, False
        insert_at = len(lines)
        for i, line in enumerate(lines):
            plain = re.sub(r"<[^>]+>", "", line).strip().lower()
            if plain.startswith("payment type"):
                insert_at = i
                break
        lines.insert(insert_at, f"Submitted by: {mapped_name}")

    new_block = "\n".join(lines)
    new_description = description[: m.start()] + start + new_block + end + description[m.end():]
    return new_description, new_description != description


def _backfill_submitter_aliases(config: AppConfig, aliases: dict[str, str]) -> tuple[int, int]:
    creds = load_admin_credentials(config)
    if not creds or not aliases:
        return 0, 0

    service = build_calendar_service_from_creds(creds)
    now = dt.datetime.now(dt.timezone.utc)
    time_min = (now - dt.timedelta(days=365)).isoformat()
    time_max = (now + dt.timedelta(days=365)).isoformat()
    active_calendars = get_active_calendars(config.admin_db_file, config.google_calendar_id)

    updated_count = 0
    error_count = 0
    for calendar_id in active_calendars:
        page_token = None
        while True:
            resp = (
                service.events()
                .list(
                    calendarId=calendar_id,
                    timeMin=time_min,
                    timeMax=time_max,
                    singleEvents=True,
                    maxResults=250,
                    pageToken=page_token,
                    fields="items(id,description,creator/email,organizer/email),nextPageToken",
                )
                .execute()
            )
            for event in resp.get("items", []):
                description = event.get("description") or ""
                fallback_email = (
                    (event.get("creator", {}) or {}).get("email")
                    or (event.get("organizer", {}) or {}).get("email")
                    or ""
                )
                new_description, changed = _apply_alias_to_description(
                    description, aliases, fallback_email=fallback_email
                )
                if not changed:
                    continue
                try:
                    (
                        service.events()
                        .patch(
                            calendarId=calendar_id,
                            eventId=event["id"],
                            body={"description": new_description},
                        )
                        .execute()
                    )
                    updated_count += 1
                except Exception:
                    error_count += 1
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
    return updated_count, error_count


def _status_badge(ok: bool, text: str) -> str:
    if ok:
        return (
            f'<span class="inline-flex items-center gap-1.5 px-3 py-1 rounded-full text-sm font-medium bg-green-100 text-green-800">'
            f'<svg class="w-3.5 h-3.5" fill="currentColor" viewBox="0 0 20 20"><path fill-rule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zm3.707-9.293a1 1 0 00-1.414-1.414L9 10.586 7.707 9.293a1 1 0 00-1.414 1.414l2 2a1 1 0 001.414 0l4-4z" clip-rule="evenodd"/></svg>'
            f'{escape(text)}</span>'
        )
    return (
        f'<span class="inline-flex items-center gap-1.5 px-3 py-1 rounded-full text-sm font-medium bg-red-100 text-red-800">'
        f'<svg class="w-3.5 h-3.5" fill="currentColor" viewBox="0 0 20 20"><path fill-rule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zm-1-5a1 1 0 012 0v1a1 1 0 01-2 0v-1zm0-8a1 1 0 012 0v4a1 1 0 01-2 0V5z" clip-rule="evenodd"/></svg>'
        f'{escape(text)}</span>'
    )


def _btn_primary(label: str, href: str = "", form_action: str = "", extra_class: str = "") -> str:
    if href:
        return (
            f'<a href="{href}" class="inline-flex items-center gap-2 px-4 py-2 bg-indigo-600 hover:bg-indigo-700 '
            f'text-white text-sm font-medium rounded-lg transition-colors {extra_class}">{label}</a>'
        )
    return (
        f'<button type="submit" formaction="{form_action}" '
        f'class="inline-flex items-center gap-2 px-4 py-2 bg-indigo-600 hover:bg-indigo-700 '
        f'text-white text-sm font-medium rounded-lg transition-colors {extra_class}">{label}</button>'
    )


def _btn_secondary(label: str, href: str = "", form_action: str = "") -> str:
    if href:
        return (
            f'<a href="{href}" class="inline-flex items-center gap-2 px-4 py-2 bg-white hover:bg-gray-50 '
            f'text-gray-700 text-sm font-medium rounded-lg border border-gray-300 transition-colors">{label}</a>'
        )
    return (
        f'<button type="submit" formaction="{form_action}" '
        f'class="inline-flex items-center gap-2 px-4 py-2 bg-white hover:bg-gray-50 '
        f'text-gray-700 text-sm font-medium rounded-lg border border-gray-300 transition-colors">{label}</button>'
    )


def create_app() -> Flask:
    config = load_config()
    init_admin_store(config.admin_db_file)

    app = Flask(__name__)
    app.secret_key = config.web_secret_key
    app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024  # 2MB

    def require_login(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if not session.get("logged_in"):
                return redirect(url_for("login"))
            return fn(*args, **kwargs)
        return wrapper

    @app.get("/login")
    def login():
        return _page(f"""
        <div class="min-h-screen flex items-center justify-center bg-gradient-to-br from-indigo-50 to-blue-100 px-4">
          <div class="w-full max-w-md">
            <div class="bg-white rounded-2xl shadow-xl p-8">
              <div class="text-center mb-8">
                <div class="inline-flex items-center justify-center w-16 h-16 bg-indigo-100 rounded-2xl mb-4">
                  <svg class="w-8 h-8 text-indigo-600" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2"
                      d="M9 12l2 2 4-4m5.618-4.016A11.955 11.955 0 0112 2.944a11.955 11.955 0 01-8.618 3.04A12.02 12.02 0 003 9c0 5.591 3.824 10.29 9 11.622 5.176-1.332 9-6.03 9-11.622 0-1.042-.133-2.052-.382-3.016z"/>
                  </svg>
                </div>
                <h1 class="text-2xl font-bold text-gray-900">Powwash Admin</h1>
                <p class="text-gray-500 text-sm mt-1">Sign in to manage your integration settings</p>
              </div>
              <form method="post" class="space-y-5">
                <div>
                  <label class="block text-sm font-medium text-gray-700 mb-1.5">Username</label>
                  <input name="username" autocomplete="username"
                    class="w-full px-4 py-2.5 border border-gray-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500 focus:border-transparent"
                    placeholder="Enter your username">
                </div>
                <div>
                  <label class="block text-sm font-medium text-gray-700 mb-1.5">Password</label>
                  <input name="password" type="password" autocomplete="current-password"
                    class="w-full px-4 py-2.5 border border-gray-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500 focus:border-transparent"
                    placeholder="Enter your password">
                </div>
                <button type="submit"
                  class="w-full py-2.5 px-4 bg-indigo-600 hover:bg-indigo-700 text-white font-medium rounded-lg text-sm transition-colors">
                  Sign in
                </button>
              </form>
            </div>
          </div>
        </div>
        """)

    @app.post("/login")
    def login_post():
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        if username == config.admin_username and password == config.admin_password:
            session["logged_in"] = True
            return redirect(url_for("index"))
        return _page(f"""
        <div class="min-h-screen flex items-center justify-center bg-gradient-to-br from-indigo-50 to-blue-100 px-4">
          <div class="w-full max-w-md">
            <div class="bg-white rounded-2xl shadow-xl p-8">
              <div class="text-center mb-6">
                <div class="inline-flex items-center justify-center w-16 h-16 bg-red-100 rounded-2xl mb-4">
                  <svg class="w-8 h-8 text-red-600" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"/>
                  </svg>
                </div>
                <h1 class="text-2xl font-bold text-gray-900">Invalid credentials</h1>
                <p class="text-gray-500 text-sm mt-1">Please check your username and password.</p>
              </div>
              <a href="/login" class="block w-full text-center py-2.5 px-4 bg-indigo-600 hover:bg-indigo-700 text-white font-medium rounded-lg text-sm transition-colors">
                Try again
              </a>
            </div>
          </div>
        </div>
        """), 401

    @app.get("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.post("/save-xero-creds")
    @require_login
    def save_xero_creds():
        client_id = (request.form.get("xero_client_id") or "").strip()
        client_secret = (request.form.get("xero_client_secret") or "").strip()
        if client_id:
            set_json_setting(config.admin_db_file, "xero_client_id", client_id)
        if client_secret:
            set_json_setting(config.admin_db_file, "xero_client_secret", client_secret)
        if client_id or client_secret:
            session["save_notice"] = "success:Xero credentials saved. Now click Connect Xero."
        else:
            session["save_notice"] = "error:No credentials entered."
        return redirect(url_for("index"))

    @app.get("/connect-xero")
    @require_login
    def connect_xero():
        xero_client_id, xero_client_secret = _get_xero_creds(config)
        if not xero_client_id or not xero_client_secret:
            session["save_notice"] = "error:Xero connect failed: enter your Client ID and Secret in the Xero card first."
            return redirect(url_for("index"))
        state = secrets.token_urlsafe(24)
        xero_auth_url = _xero_authorization_url(config, state)
        session["xero_oauth_state"] = state
        session["xero_auth_url"] = xero_auth_url
        return redirect(url_for("index"))

    @app.get("/xero/callback")
    def xero_callback():
        code = request.args.get("code") or ""
        state = request.args.get("state") or ""
        err = request.args.get("error") or ""
        if err:
            return _page(f"""
            <div class="min-h-screen flex items-center justify-center bg-gray-50">
              <div class="bg-white rounded-2xl shadow p-8 max-w-md w-full text-center">
                <p class="text-red-600 font-medium mb-4">Xero OAuth error: {escape(err)}</p>
                <a href="/" class="text-indigo-600 hover:underline text-sm">Back to dashboard</a>
              </div>
            </div>
            """), 400
        expected_session = session.get("xero_oauth_state") or ""
        expected_store = str(
            get_json_setting(config.admin_db_file, "xero_oauth_pending_state", "")
        ).strip()
        state_ok = bool(state) and (state == expected_session or state == expected_store)
        if not code or not state_ok:
            return _page("""
            <div class="min-h-screen flex items-center justify-center bg-gray-50">
              <div class="bg-white rounded-2xl shadow p-8 max-w-md w-full text-center">
                <p class="text-red-600 font-medium mb-2">Xero callback invalid state/code.</p>
                <p class="text-gray-500 text-sm mb-4">Use one host consistently for login and callback, then retry.</p>
                <a href="/" class="text-indigo-600 hover:underline text-sm">Back to dashboard</a>
              </div>
            </div>
            """), 400
        try:
            token = _exchange_xero_code(config, code)
            tenant_id = _get_xero_tenant_id(token.get("access_token", ""))
        except Exception as exc:
            return _page(f"""
            <div class="min-h-screen flex items-center justify-center bg-gray-50">
              <div class="bg-white rounded-2xl shadow p-8 max-w-md w-full text-center">
                <p class="text-red-600 font-medium mb-4">Xero connect failed: {escape(str(exc))}</p>
                <a href="/" class="text-indigo-600 hover:underline text-sm">Back to dashboard</a>
              </div>
            </div>
            """), 400

        token["tenant_id"] = tenant_id
        save_xero_token(config.xero_token_file, token)
        session["logged_in"] = True
        session.pop("xero_oauth_state", None)
        session.pop("xero_auth_url", None)
        set_json_setting(config.admin_db_file, "xero_oauth_pending_state", "")
        set_json_setting(config.admin_db_file, "xero_auth_url", "")
        session["save_notice"] = "success:Xero connected successfully."
        return redirect(url_for("index"))

    @app.post("/upload-google-credentials")
    @require_login
    def upload_google_credentials():
        f = request.files.get("google_credentials")
        if not f or not f.filename:
            session["save_notice"] = "error:No credentials file selected."
            return redirect(url_for("index"))
        raw = f.read()
        ok, err = _validate_google_credentials_json(raw)
        if not ok:
            session["save_notice"] = f"error:Credentials upload failed: {err}"
            return redirect(url_for("index"))

        path = Path(config.google_credentials_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        set_json_setting(
            config.admin_db_file,
            "google_credentials_meta",
            {"uploaded_name": f.filename, "stored_path": str(path)},
        )
        session["save_notice"] = (
            f"success:Credentials uploaded ({f.filename}). Now click Connect Google to authorise."
        )
        return redirect(url_for("index"))

    @app.get("/connect-google")
    @require_login
    def connect_google():
        if not Path(config.google_credentials_file).exists():
            session["save_notice"] = (
                "error:No credentials file found. Please upload your Google OAuth JSON file first using the Upload JSON button."
            )
            return redirect(url_for("index"))
        try:
            auth_url, state = oauth_authorization_url(config)
        except Exception as exc:
            session["save_notice"] = f"error:Could not start Google OAuth: {exc}"
            return redirect(url_for("index"))
        print(f"[Google OAuth] redirect_uri={config.google_oauth_redirect_uri}")
        print(f"[Google OAuth] auth_url={auth_url}")
        session["oauth_state"] = state
        session["oauth_auth_url"] = auth_url
        set_json_setting(config.admin_db_file, "oauth_pending_state", state)
        set_json_setting(config.admin_db_file, "oauth_auth_url", auth_url)
        return redirect(url_for("index"))

    @app.get("/oauth/callback")
    def oauth_callback():
        print(f"[OAuth Callback] ALL args from Google: {dict(request.args)}")
        code = request.args.get("code") or ""
        state = request.args.get("state") or ""
        google_error = request.args.get("error") or ""
        if google_error:
            print(f"[OAuth Callback] Google returned ERROR: {google_error}")
        expected_session = session.get("oauth_state") or ""
        expected_store = str(
            get_json_setting(config.admin_db_file, "oauth_pending_state", "")
        ).strip()
        state_ok = bool(state) and (state == expected_session or state == expected_store)
        if not code or not state_ok:
            error_detail = google_error or ("missing code" if not code else "state mismatch")
            print(f"[OAuth Callback] Failing: code={'present' if code else 'MISSING'}, state_ok={state_ok}, google_error={google_error!r}")
            return _page(f"""
            <div class="min-h-screen flex items-center justify-center bg-gray-50">
              <div class="bg-white rounded-2xl shadow p-8 max-w-md w-full text-center">
                <p class="text-red-600 font-medium mb-2">OAuth callback failed.</p>
                <p class="text-gray-700 text-sm mb-2">Google said: <strong>{error_detail}</strong></p>
                <p class="text-gray-500 text-sm mb-4">code={'present' if code else 'missing'} &nbsp;|&nbsp; state_ok={state_ok}</p>
                <a href="/" class="text-indigo-600 hover:underline text-sm">Back to dashboard</a>
              </div>
            </div>
            """), 400
        creds = oauth_exchange_code(config, state=state, code=code)
        save_admin_credentials(config, creds)
        session["logged_in"] = True
        session.pop("oauth_state", None)
        set_json_setting(config.admin_db_file, "oauth_pending_state", "")
        session["save_notice"] = "success:Google connected successfully."
        session.pop("oauth_auth_url", None)
        set_json_setting(config.admin_db_file, "oauth_auth_url", "")
        return redirect(url_for("index"))

    @app.get("/")
    @require_login
    def index():
        creds = load_admin_credentials(config)
        active = set(get_active_calendars(config.admin_db_file, config.google_calendar_id))
        stats_selected = set(get_stats_fields(config.admin_db_file))
        target = get_sheet_target(config.admin_db_file)
        sheets_ok, sheets_msg = _sheets_status_data(config, creds, target)
        xero_ok, xero_msg, xero_tenant = _xero_status_data(config)
        google_ok = creds is not None
        save_notice = session.pop("save_notice", "")
        creds_meta = get_json_setting(
            config.admin_db_file,
            "google_credentials_meta",
            {"uploaded_name": "", "stored_path": config.google_credentials_file},
        )
        seen_submitters = get_seen_submitters(config.admin_db_file)
        submitter_aliases = get_submitter_aliases(config.admin_db_file)
        client_id = _oauth_client_id(config)
        creds_file_exists = Path(config.google_credentials_file).exists()
        stored_xero_id, stored_xero_secret = _get_xero_creds(config)
        xero_has_creds = bool(stored_xero_id and stored_xero_secret)
        # Read once and clear (like save_notice) — shows after Connect click, gone on next refresh
        xero_pending_auth_url = session.pop("xero_auth_url", None) or "" if not xero_ok else ""
        pending_auth_url = (
            session.get("oauth_auth_url")
            or str(get_json_setting(config.admin_db_file, "oauth_auth_url", "")).strip()
        ) if not google_ok else ""

        calendars = []
        spreadsheets = []
        calendar_error = ""
        if creds:
            try:
                calendars = list_calendars(creds)
            except Exception as exc:
                calendar_error = f"Failed to load calendars: {exc}"
            try:
                spreadsheets = list_spreadsheets(creds)
            except Exception:
                pass

        # --- Notice banner ---
        notice_html = ""
        if save_notice:
            is_error = save_notice.startswith("error:")
            msg = save_notice[6:] if save_notice.startswith(("error:", "success:")) else save_notice
            if is_error:
                notice_html = f"""
                <div class="mb-6 flex items-start gap-3 p-4 bg-red-50 border border-red-200 rounded-xl text-red-800 text-sm">
                  <svg class="w-5 h-5 mt-0.5 shrink-0" fill="currentColor" viewBox="0 0 20 20">
                    <path fill-rule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zM8.707 7.293a1 1 0 00-1.414 1.414L8.586 10l-1.293 1.293a1 1 0 101.414 1.414L10 11.414l1.293 1.293a1 1 0 001.414-1.414L11.414 10l1.293-1.293a1 1 0 00-1.414-1.414L10 8.586 8.707 7.293z" clip-rule="evenodd"/>
                  </svg>
                  <span>{escape(msg)}</span>
                </div>"""
            else:
                notice_html = f"""
                <div class="mb-6 flex items-start gap-3 p-4 bg-green-50 border border-green-200 rounded-xl text-green-800 text-sm">
                  <svg class="w-5 h-5 mt-0.5 shrink-0" fill="currentColor" viewBox="0 0 20 20">
                    <path fill-rule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zm3.707-9.293a1 1 0 00-1.414-1.414L9 10.586 7.707 9.293a1 1 0 00-1.414 1.414l2 2a1 1 0 001.414 0l4-4z" clip-rule="evenodd"/>
                  </svg>
                  <span>{escape(msg)}</span>
                </div>"""

        # --- Calendars ---
        if calendars:
            my_rows = []
            other_rows = []
            for c in calendars:
                cid = c["id"] or ""
                checked = "checked" if cid in active else ""
                title = escape(c.get("summary_display") or c.get("summary") or cid)
                primary_badge = ' <span class="text-xs bg-indigo-100 text-indigo-700 px-1.5 py-0.5 rounded font-medium">Primary</span>' if c.get("primary") else ""
                hints = []
                if c.get("hidden"):
                    hints.append("hidden")
                if not c.get("selected", True):
                    hints.append("not selected")
                hint = f' <span class="text-gray-400 text-xs">({"; ".join(hints)})</span>' if hints else ""
                row = (
                    f'<label class="flex items-center gap-3 p-3 rounded-lg hover:bg-gray-50 cursor-pointer">'
                    f'<input type="checkbox" name="active_calendars" value="{escape(cid)}" {checked} '
                    f'class="w-4 h-4 text-indigo-600 rounded border-gray-300 focus:ring-indigo-500">'
                    f'<span class="text-sm text-gray-800">{title}{primary_badge}{hint}</span>'
                    f'</label>'
                )
                is_my = (
                    c.get("primary") or c.get("is_birthdays")
                    or (c.get("access_role") in {"owner", "writer"} and not c.get("is_holiday"))
                )
                if is_my:
                    my_rows.append(row)
                else:
                    other_rows.append(row)

            cal_html = ""
            if my_rows:
                cal_html += '<p class="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-2">My Calendars</p>'
                cal_html += "".join(my_rows)
            if other_rows:
                if my_rows:
                    cal_html += '<div class="my-3 border-t border-gray-100"></div>'
                cal_html += '<p class="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-2">Other Calendars</p>'
                cal_html += "".join(other_rows)
        elif calendar_error:
            cal_html = f'<p class="text-sm text-red-600">{escape(calendar_error)}</p>'
        else:
            cal_html = '<p class="text-sm text-gray-500">No calendars loaded. Connect Google first.</p>'

        # --- Stats fields ---
        stats_html = ""
        for key, label in STAT_OPTIONS:
            checked = "checked" if key in stats_selected else ""
            stats_html += (
                f'<label class="flex items-center gap-3 p-2.5 rounded-lg hover:bg-gray-50 cursor-pointer">'
                f'<input type="checkbox" name="stats_fields" value="{key}" {checked} '
                f'class="w-4 h-4 text-indigo-600 rounded border-gray-300 focus:ring-indigo-500">'
                f'<span class="text-sm text-gray-800">{escape(label)}</span>'
                f'</label>'
            )

        # --- Submitter aliases ---
        alias_rows_html = ""
        for email in seen_submitters:
            alias = submitter_aliases.get(email, "")
            alias_rows_html += (
                f'<div class="flex items-center gap-3 py-2">'
                f'<span class="text-sm text-gray-600 w-64 truncate" title="{escape(email)}">{escape(email)}</span>'
                f'<input name="submitter_alias__{escape(email)}" value="{escape(alias)}" '
                f'placeholder="Display name" '
                f'class="flex-1 px-3 py-1.5 border border-gray-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500">'
                f'</div>'
            )
        if not alias_rows_html:
            alias_rows_html = '<p class="text-sm text-gray-500">No submitters seen yet — process some events first.</p>'

        # --- Spreadsheet options ---
        sheet_options = '<option value="">-- Select a spreadsheet --</option>'
        sheet_current_id = target.get("spreadsheet_id", "")
        sheet_name_val = target.get("sheet_name", "Sheet1")
        for s in spreadsheets:
            sel = 'selected' if s.get("id") == sheet_current_id else ""
            sheet_options += f'<option value="{escape(s["id"])}" {sel}>{escape(s["name"])}</option>'
        if sheet_current_id and all(s.get("id") != sheet_current_id for s in spreadsheets):
            sheet_options += f'<option value="{escape(sheet_current_id)}" selected>Current saved ({escape(sheet_current_id)})</option>'

        # --- Xero credential hints ---
        xero_redirect = escape(config.xero_redirect_uri)
        google_redirect = escape(config.google_oauth_redirect_uri)

        # --- Pending Google auth URL block ---
        if pending_auth_url:
            _esc_url = escape(pending_auth_url)
            _js_url = pending_auth_url.replace("'", "\\'")
            _preview = escape(pending_auth_url[:72]) + "..."
            pending_auth_url_html = (
                f'<div class="mt-3 p-3 bg-blue-50 border border-blue-200 rounded-xl">'
                f'<p class="text-xs font-semibold text-blue-800 mb-1">&#128279; Open this link in a new tab to authorise Google:</p>'
                f'<a href="{_esc_url}" target="_blank" class="block text-xs text-blue-700 underline break-all hover:text-blue-900 mb-2">{_preview}</a>'
                f'<button type="button" onclick="navigator.clipboard.writeText(\'{_js_url}\');this.textContent=\'Copied!\';setTimeout(()=>this.textContent=\'Copy link\',2000)" '
                f'class="px-2 py-1 text-xs font-medium bg-blue-600 text-white rounded hover:bg-blue-700 transition-colors">Copy link</button>'
                f'<p class="text-xs text-blue-600 mt-1.5">After approving in Google, come back here — the status will update automatically.</p>'
                f'</div>'
            )
        else:
            pending_auth_url_html = ""

        # --- Pending Xero auth URL block ---
        if xero_pending_auth_url:
            _xesc = escape(xero_pending_auth_url)
            _xjs = xero_pending_auth_url.replace("'", "\\'")
            _xprev = escape(xero_pending_auth_url[:72]) + "..."
            xero_pending_auth_url_html = (
                f'<div class="mt-3 p-3 bg-blue-50 border border-blue-200 rounded-xl">'
                f'<p class="text-xs font-semibold text-blue-800 mb-1">&#128279; Open this link in a new tab to authorise Xero:</p>'
                f'<a href="{_xesc}" target="_blank" class="block text-xs text-blue-700 underline break-all hover:text-blue-900 mb-2">{_xprev}</a>'
                f'<button type="button" onclick="navigator.clipboard.writeText(\'{_xjs}\');this.textContent=\'Copied!\';setTimeout(()=>this.textContent=\'Copy link\',2000)" '
                f'class="px-2 py-1 text-xs font-medium bg-blue-600 text-white rounded hover:bg-blue-700 transition-colors">Copy link</button>'
                f'<p class="text-xs text-blue-600 mt-1.5">After approving in Xero, come back here — the status will update automatically.</p>'
                f'</div>'
            )
        else:
            xero_pending_auth_url_html = ""

        return _page(f"""
        <div class="max-w-4xl mx-auto px-4 py-8">

          <!-- Header -->
          <div class="flex items-center justify-between mb-8">
            <div>
              <h1 class="text-2xl font-bold text-gray-900">Powwash Integration</h1>
              <p class="text-gray-500 text-sm mt-0.5">Google Calendar → Xero invoice automation</p>
            </div>
            <a href="/logout" class="text-sm text-gray-500 hover:text-gray-700 flex items-center gap-1.5">
              <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2"
                  d="M17 16l4-4m0 0l-4-4m4 4H7m6 4v1a3 3 0 01-3 3H6a3 3 0 01-3-3V7a3 3 0 013-3h4a3 3 0 013 3v1"/>
              </svg>
              Sign out
            </a>
          </div>

          {notice_html}

          <!-- Setup Steps Overview -->
          <div class="bg-indigo-50 border border-indigo-100 rounded-2xl p-6 mb-6">
            <h2 class="text-sm font-semibold text-indigo-900 mb-3 flex items-center gap-2">
              <svg class="w-4 h-4" fill="currentColor" viewBox="0 0 20 20">
                <path fill-rule="evenodd" d="M18 10a8 8 0 11-16 0 8 8 0 0116 0zm-7-4a1 1 0 11-2 0 1 1 0 012 0zM9 9a1 1 0 000 2v3a1 1 0 001 1h1a1 1 0 100-2v-3a1 1 0 00-1-1H9z" clip-rule="evenodd"/>
              </svg>
              Quick Setup Guide
            </h2>
            <ol class="space-y-2 text-sm text-indigo-800">
              <li class="flex gap-2"><span class="font-bold shrink-0">1.</span>
                <span>Create a <a href="https://console.cloud.google.com/apis/credentials" target="_blank" class="underline font-medium hover:text-indigo-600">Google Cloud OAuth 2.0 client</a> (type: <em>Web application</em>), add <code class="bg-indigo-100 px-1 rounded text-xs">{google_redirect}</code> as an authorised redirect URI, then download the JSON and upload it in the Google section below.</span>
              </li>
              <li class="flex gap-2"><span class="font-bold shrink-0">2.</span>
                <span>Enable the <a href="https://console.cloud.google.com/apis/library/calendar-json.googleapis.com" target="_blank" class="underline font-medium hover:text-indigo-600">Google Calendar API</a> and <a href="https://console.cloud.google.com/apis/library/sheets.googleapis.com" target="_blank" class="underline font-medium hover:text-indigo-600">Google Sheets API</a> in your Google Cloud project.</span>
              </li>
              <li class="flex gap-2"><span class="font-bold shrink-0">3.</span>
                <span>Create a <a href="https://developer.xero.com/app/manage" target="_blank" class="underline font-medium hover:text-indigo-600">Xero app</a> (type: <em>Web app</em>), set the redirect URI to <code class="bg-indigo-100 px-1 rounded text-xs">{xero_redirect}</code>, and add <code class="bg-indigo-100 px-1 rounded text-xs">XERO_CLIENT_ID</code> / <code class="bg-indigo-100 px-1 rounded text-xs">XERO_CLIENT_SECRET</code> as environment variables.</span>
              </li>
              <li class="flex gap-2"><span class="font-bold shrink-0">4.</span>
                <span>Click <strong>Connect Google</strong> and <strong>Connect Xero</strong> below, then select your calendars and save.</span>
              </li>
            </ol>
          </div>

          <form method="post" action="/save" enctype="multipart/form-data" class="space-y-6">

            <!-- Connection Status Row -->
            <div class="grid grid-cols-1 sm:grid-cols-2 gap-4">

              <!-- Google Card -->
              <div class="bg-white rounded-2xl shadow-sm border border-gray-200 p-5">
                <div class="flex items-start justify-between mb-3">
                  <div class="flex items-center gap-3">
                    <div class="w-10 h-10 bg-blue-50 rounded-xl flex items-center justify-center shrink-0">
                      <svg class="w-5 h-5 text-blue-600" viewBox="0 0 24 24" fill="currentColor">
                        <path d="M12.545 10.239v3.821h5.445c-.712 2.315-2.647 3.972-5.445 3.972a6.033 6.033 0 110-12.064c1.498 0 2.866.549 3.921 1.453l2.814-2.814A9.969 9.969 0 0012.545 2C7.021 2 2.543 6.477 2.543 12s4.478 10 10.002 10c8.396 0 10.249-7.85 9.426-11.748l-9.426-.013z"/>
                      </svg>
                    </div>
                    <div>
                      <h3 class="font-semibold text-gray-900 text-sm">Google</h3>
                      <p class="text-xs text-gray-500">Calendar &amp; Sheets</p>
                    </div>
                  </div>
                  {_status_badge(google_ok, "Connected" if google_ok else "Not connected")}
                </div>

                {"" if not client_id else f'<p class="text-xs text-gray-400 mb-3 font-mono truncate" title="{escape(client_id)}">Client: {escape(client_id[:40])}{"..." if len(client_id) > 40 else ""}</p>'}

                <div class="space-y-3">
                  <div>
                    <p class="text-xs text-gray-500 mb-1.5 font-medium">Upload OAuth credentials JSON</p>
                    <input type="file" name="google_credentials" accept=".json,application/json"
                      class="block w-full text-xs text-gray-500 file:mr-3 file:py-1.5 file:px-3 file:rounded-lg file:border-0 file:text-xs file:font-medium file:bg-indigo-50 file:text-indigo-700 hover:file:bg-indigo-100">
                    {"" if not (creds_meta or {}).get("uploaded_name") else f'<p class="text-xs text-gray-400 mt-1">Last upload: {escape((creds_meta or {{}}).get("uploaded_name", ""))}</p>'}
                  </div>

                  <div class="mb-3">
                    {"" if creds_file_exists else '<p class="text-xs text-amber-700 bg-amber-50 border border-amber-200 rounded-lg px-3 py-2">No credentials file uploaded yet. Select your JSON file and click <strong>Upload JSON</strong> first.</p>'}
                    {"" if not creds_file_exists else '<p class="text-xs text-green-700 bg-green-50 border border-green-200 rounded-lg px-3 py-2">&#10003; Credentials file uploaded. Click <strong>Connect Google</strong> to generate your authorisation link.</p>'}
                  </div>

                  {pending_auth_url_html}

                  <div class="flex gap-2 flex-wrap mt-3">
                    <button type="submit" formaction="/upload-google-credentials"
                      class="px-3 py-1.5 text-xs font-medium text-gray-700 bg-gray-100 hover:bg-gray-200 rounded-lg transition-colors">
                      Upload JSON
                    </button>
                    <a href="/connect-google"
                      class="px-3 py-1.5 text-xs font-medium text-white {"bg-blue-600 hover:bg-blue-700" if creds_file_exists else "bg-gray-300 cursor-not-allowed"} rounded-lg transition-colors">
                      {"Reconnect" if google_ok else ("New link" if pending_auth_url else "Connect Google")}
                    </a>
                  </div>
                </div>
              </div>

              <!-- Xero Card -->
              <div class="bg-white rounded-2xl shadow-sm border border-gray-200 p-5">
                <div class="flex items-start justify-between mb-3">
                  <div class="flex items-center gap-3">
                    <div class="w-10 h-10 bg-blue-50 rounded-xl flex items-center justify-center shrink-0">
                      <svg class="w-5 h-5 text-blue-700" viewBox="0 0 24 24" fill="currentColor">
                        <path d="M12 2C6.477 2 2 6.477 2 12s4.477 10 10 10 10-4.477 10-10S17.523 2 12 2zm4.5 13.5h-9v-1.5h9v1.5zm0-3h-9V11h9v1.5zm0-3h-9V8h9v1.5z"/>
                      </svg>
                    </div>
                    <div>
                      <h3 class="font-semibold text-gray-900 text-sm">Xero</h3>
                      <p class="text-xs text-gray-500">Invoice automation</p>
                    </div>
                  </div>
                  {_status_badge(xero_ok, "Connected" if xero_ok else "Not connected")}
                </div>

                <div class="text-xs text-gray-500 mb-3 space-y-1">
                  <p>{escape(xero_msg)}</p>
                  {"" if not xero_tenant else f'<p class="font-mono text-gray-400 truncate" title="{escape(xero_tenant)}">Tenant: {escape(xero_tenant[:32])}{"..." if len(xero_tenant) > 32 else ""}</p>'}
                  <p>Redirect URI: <code class="bg-gray-100 px-1 py-0.5 rounded text-xs">{xero_redirect}</code></p>
                </div>

                <div class="space-y-2 mb-3">
                  <div>
                    <label class="block text-xs font-medium text-gray-600 mb-1">Client ID</label>
                    <input name="xero_client_id" value="{escape(stored_xero_id)}"
                      placeholder="Paste your Xero Client ID"
                      class="w-full px-3 py-1.5 border border-gray-300 rounded-lg text-xs focus:outline-none focus:ring-2 focus:ring-blue-500 font-mono">
                  </div>
                  <div>
                    <label class="block text-xs font-medium text-gray-600 mb-1">Client Secret</label>
                    <input name="xero_client_secret" type="password"
                      placeholder="{"••••••••  (saved)" if stored_xero_secret else "Paste your Xero Client Secret"}"
                      class="w-full px-3 py-1.5 border border-gray-300 rounded-lg text-xs focus:outline-none focus:ring-2 focus:ring-blue-500 font-mono">
                  </div>
                  <button type="submit" formaction="/save-xero-creds"
                    class="px-3 py-1.5 text-xs font-medium text-gray-700 bg-gray-100 hover:bg-gray-200 rounded-lg transition-colors">
                    Save Credentials
                  </button>
                </div>

                {xero_pending_auth_url_html}

                <a href="/connect-xero"
                  class="inline-block mt-3 px-3 py-1.5 text-xs font-medium text-white bg-blue-700 hover:bg-blue-800 rounded-lg transition-colors {"opacity-50 pointer-events-none" if not xero_has_creds else ""}">
                  {"Reconnect Xero" if xero_ok else ("New link" if xero_pending_auth_url else "Connect Xero")}
                </a>
              </div>
            </div>

            <!-- Active Calendars -->
            <div class="bg-white rounded-2xl shadow-sm border border-gray-200 p-6">
              <div class="flex items-center justify-between mb-1">
                <h2 class="font-semibold text-gray-900">Active Calendars</h2>
                <span class="text-xs text-gray-400">{len(active)} selected</span>
              </div>
              <p class="text-sm text-gray-500 mb-4">Select which calendars to monitor for events marked with <strong>DONE</strong>.</p>
              <div class="divide-y divide-gray-50">
                {cal_html}
              </div>
            </div>

            <!-- Google Sheets -->
            <div class="bg-white rounded-2xl shadow-sm border border-gray-200 p-6">
              <div class="flex items-center justify-between mb-1">
                <h2 class="font-semibold text-gray-900">Google Sheets Target</h2>
                {_status_badge(sheets_ok, "Ready" if sheets_ok else "Not ready")}
              </div>
              <p class="text-sm text-gray-500 mb-4">{escape(sheets_msg)}</p>

              <div class="space-y-4">
                {"" if not spreadsheets else f"""
                <div>
                  <label class="block text-sm font-medium text-gray-700 mb-1.5">Pick from your spreadsheets</label>
                  <select name="spreadsheet_pick" class="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500">
                    {sheet_options}
                  </select>
                </div>
                """}
                <div>
                  <label class="block text-sm font-medium text-gray-700 mb-1.5">Spreadsheet URL or ID</label>
                  <input name="spreadsheet_input" value="{escape(sheet_current_id)}"
                    placeholder="Paste a Google Sheets URL or spreadsheet ID"
                    class="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500">
                </div>
                <div>
                  <label class="block text-sm font-medium text-gray-700 mb-1.5">Sheet tab name</label>
                  <input name="sheet_name" value="{escape(sheet_name_val)}"
                    placeholder="Sheet1"
                    class="w-48 px-3 py-2 border border-gray-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500">
                </div>
              </div>
            </div>

            <!-- Stats Fields -->
            <div class="bg-white rounded-2xl shadow-sm border border-gray-200 p-6">
              <h2 class="font-semibold text-gray-900 mb-1">Stats to Post to Sheets</h2>
              <p class="text-sm text-gray-500 mb-4">Choose which data columns are written to your Google Sheet when an invoice is processed.</p>
              <div class="grid grid-cols-1 sm:grid-cols-2 gap-1">
                {stats_html}
              </div>
            </div>

            <!-- Submitter Names -->
            <div class="bg-white rounded-2xl shadow-sm border border-gray-200 p-6">
              <h2 class="font-semibold text-gray-900 mb-1">Submitter Display Names</h2>
              <p class="text-sm text-gray-500 mb-4">Map a submitter's email address to a friendly display name used in calendar entries and sheet rows.</p>
              <div class="divide-y divide-gray-100">
                {alias_rows_html}
              </div>
              <div class="mt-4">
                <button type="submit" formaction="/apply-submitter-aliases"
                  class="px-4 py-2 bg-white hover:bg-gray-50 text-gray-700 text-sm font-medium rounded-lg border border-gray-300 transition-colors">
                  Save Names &amp; Apply to Existing Entries
                </button>
              </div>
            </div>

            <!-- Save -->
            <div class="flex justify-end pb-4">
              <button type="submit"
                class="px-6 py-2.5 bg-indigo-600 hover:bg-indigo-700 text-white font-medium rounded-lg text-sm transition-colors shadow-sm">
                Save Settings
              </button>
            </div>

          </form>
        </div>
        """)

    @app.post("/save")
    @require_login
    def save():
        calendars = request.form.getlist("active_calendars")
        if not calendars:
            calendars = [config.google_calendar_id]
        set_active_calendars(config.admin_db_file, calendars)

        stats_fields = request.form.getlist("stats_fields")
        valid = [k for k in stats_fields if k in {k for k, _ in STAT_OPTIONS}]
        if not valid:
            valid = DEFAULT_STATS_FIELDS
        set_stats_fields(config.admin_db_file, valid)

        picked = (request.form.get("spreadsheet_pick") or "").strip()
        entered = (request.form.get("spreadsheet_input") or "").strip()
        spreadsheet_id = _extract_spreadsheet_id(picked or entered)
        sheet_name = (request.form.get("sheet_name") or "Sheet1").strip()
        set_sheet_target(config.admin_db_file, spreadsheet_id=spreadsheet_id, sheet_name=sheet_name)
        target = {"spreadsheet_id": spreadsheet_id, "sheet_name": sheet_name}
        creds = load_admin_credentials(config)
        ok, _ = _sheets_status_data(config, creds, target)
        aliases = _save_submitter_aliases_from_form(config, request.form)
        msg = "Settings saved. Sheets connection is ready." if ok else "Settings saved."
        if aliases:
            msg += " Submitter names saved."
        session["save_notice"] = f"success:{msg}"

        set_json_setting(config.admin_db_file, "settings_version", {"updated": True})
        return redirect(url_for("index"))

    @app.post("/apply-submitter-aliases")
    @require_login
    def apply_submitter_aliases():
        aliases = _save_submitter_aliases_from_form(config, request.form)
        if not aliases:
            session["save_notice"] = "error:No submitter name mappings set."
            return redirect(url_for("index"))

        updated, errors = _backfill_submitter_aliases(config, aliases)
        msg = f"Submitter names saved. Updated {updated} existing calendar entries."
        if errors:
            msg += f" ({errors} updates failed.)"
        session["save_notice"] = f"success:{msg}"
        set_json_setting(config.admin_db_file, "settings_version", {"updated": True})
        return redirect(url_for("index"))

    return app


def run_web() -> None:
    config = load_config()
    app = create_app()
    app.run(host=config.web_host, port=config.web_port)


if __name__ == "__main__":
    run_web()
