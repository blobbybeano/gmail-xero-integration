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
from .xero_client import load_xero_token, save_xero_token, token_is_expired, refresh_xero_token
from .google_sheets import backfill_submitter_in_sheet, update_invoice_paid_in_sheet
from .google_calendar import register_calendar_watch, stop_calendar_watch
from .admin_store import (
    get_enabled,
    set_enabled,
    get_google_watches,
    set_google_watch,
    delete_google_watch,
    get_xero_webhook_key,
    set_xero_webhook_key,
    get_xero_webhook_verified,
    set_xero_webhook_verified,
    get_xero_tenants,
    set_xero_tenants,
    upsert_xero_tenant,
)
from .trigger import trigger_poll
from .state import load_state, get_last_sync
from .log_feed import feed as _feed


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


def _current_base_url() -> str:
    """Return the public HTTPS base URL from the current request — works on any domain."""
    base = request.host_url.rstrip("/")
    return base.replace("http://", "https://", 1)


def _url_row(label: str, url: str, hint: str) -> str:
    """Render a copy-able URL row for the Deployment URLs panel."""
    url_esc = escape(url)
    url_js = url.replace("'", "\\'")
    return (
        f'<div class="flex items-center gap-3 px-5 py-3">'
        f'<div class="min-w-0 flex-1">'
        f'<p class="text-xs font-medium text-gray-700">{label}</p>'
        f'<p class="text-xs text-gray-400 mt-0.5">{escape(hint)}</p>'
        f'</div>'
        f'<div class="flex items-center gap-2 shrink-0">'
        f'<code class="text-xs font-mono text-indigo-700 bg-indigo-50 px-2 py-1 rounded truncate max-w-xs hidden sm:block">{url_esc}</code>'
        f'<button type="button" '
        f'onclick="navigator.clipboard.writeText(\'{url_js}\');this.textContent=\'Copied!\';setTimeout(()=>this.textContent=\'Copy\',1500)" '
        f'class="shrink-0 px-2.5 py-1 text-xs font-medium bg-gray-100 hover:bg-gray-200 text-gray-700 rounded-lg transition-colors">'
        f'Copy'
        f'</button>'
        f'</div>'
        f'</div>'
    )


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


def _xero_authorization_url(
    config: AppConfig, state: str, redirect_uri: str | None = None
) -> str:
    client_id, _ = _get_xero_creds(config)
    uri = redirect_uri or config.xero_redirect_uri
    # Build manually so spaces in scope are encoded as %20 (not +) — Xero requires %20
    scope_str = _xero_scope_string(config.xero_scopes)
    base_params = urllib.parse.urlencode({
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": uri,
        "state": state,
    })
    scope_param = "scope=" + urllib.parse.quote(scope_str, safe="")
    return f"{XERO_AUTH_URL}?{base_params}&{scope_param}"


def _exchange_xero_code(
    config: AppConfig, code: str, redirect_uri: str | None = None
) -> dict:
    client_id, client_secret = _get_xero_creds(config)
    uri = redirect_uri or config.xero_redirect_uri
    basic = base64.b64encode(
        f"{client_id}:{client_secret}".encode()
    ).decode()
    headers = {"Authorization": f"Basic {basic}"}
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": uri,
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


def _get_all_xero_connections(access_token: str) -> list[dict]:
    """Return all authorised Xero connections as [{tenantId, tenantName}]."""
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
    response = requests.get(XERO_CONNECTIONS_URL, headers=headers, timeout=30)
    response.raise_for_status()
    return [
        {"tenantId": c.get("tenantId", ""), "tenantName": c.get("tenantName", c.get("tenantId", ""))}
        for c in response.json()
        if c.get("tenantId")
    ]


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


def _xero_tenant_cards(
    xero_ok: bool,
    tenant_accounts: list[dict],
) -> str:
    """Render per-tenant cards with enable toggle and separate account mapping.

    tenant_accounts: list of {tenantId, tenantName, enabled, invoiceAccount,
                               paymentAccount, revenueAccounts, bankAccounts}
    """
    if not xero_ok:
        return (
            '<div class="bg-white rounded-2xl shadow-sm border border-gray-200 p-6 opacity-50">'
            '<h2 class="font-semibold text-gray-900 mb-1">Xero Organisations</h2>'
            '<p class="text-sm text-gray-400">Connect Xero first to configure organisations and account mapping.</p>'
            '</div>'
        )

    if not tenant_accounts:
        return (
            '<div class="bg-white rounded-2xl shadow-sm border border-gray-200 p-6">'
            '<h2 class="font-semibold text-gray-900 mb-1">Xero Organisations</h2>'
            '<p class="text-sm text-gray-400">No organisations found — reconnect Xero to discover them.</p>'
            '</div>'
        )

    def _find_name(accounts, code):
        for a in accounts:
            if a.get("Code", "") == code:
                return a.get("Name", code)
        return None

    def _opts(accounts, saved):
        out = '<option value="">— select account —</option>'
        for a in sorted(accounts, key=lambda x: x.get("Name", "")):
            code = escape(a.get("Code", ""))
            name = escape(a.get("Name", ""))
            sel = ' selected' if a.get("Code", "") == saved else ""
            out += f'<option value="{code}"{sel}>{name} ({code})</option>'
        return out

    def _saved_badge(accounts, code):
        if not code:
            return '<p class="text-xs text-gray-400 mt-1">No account saved yet.</p>'
        name = _find_name(accounts, code)
        label = escape(f"{name} ({code})") if name else escape(code)
        return f'<p class="text-xs text-emerald-600 mt-1">&#10003; Saved: <span class="font-medium">{label}</span></p>'

    cards_html = ""
    for t in tenant_accounts:
        tid = escape(t["tenantId"])
        tname = escape(t.get("tenantName") or t["tenantId"])
        enabled = t.get("enabled", True)
        rev_accounts = t.get("revenueAccounts", [])
        bank_accounts = t.get("bankAccounts", [])
        saved_inv = t.get("invoiceAccount", "")
        saved_pay = t.get("paymentAccount", "")

        toggle_color = "bg-emerald-500" if enabled else "bg-gray-300"
        toggle_label = "Active" if enabled else "Paused"
        toggle_dot = "translate-x-5" if enabled else "translate-x-0"
        status_text = (
            '<span class="text-emerald-600 text-xs font-medium">● Active — invoices will be created here</span>'
            if enabled else
            '<span class="text-gray-400 text-xs font-medium">○ Paused — this organisation is skipped</span>'
        )
        border_cls = "border-emerald-200" if enabled else "border-gray-200"

        rev_opts = _opts(rev_accounts, saved_inv)
        bank_opts = _opts(bank_accounts, saved_pay)
        rev_badge = _saved_badge(rev_accounts, saved_inv)
        bank_badge = _saved_badge(bank_accounts, saved_pay)

        cards_html += f"""
        <div class="bg-white rounded-2xl shadow-sm border {border_cls} p-6">
          <div class="flex items-start justify-between mb-4">
            <div>
              <h3 class="font-semibold text-gray-900 text-base">{tname}</h3>
              <p class="text-xs text-gray-400 font-mono mt-0.5">{tid}</p>
              <div class="mt-1">{status_text}</div>
            </div>
            <form method="post" action="/toggle-xero-tenant/{tid}">
              <button type="submit" title="Toggle {tname}"
                class="flex items-center gap-2 px-3 py-1.5 text-xs font-medium rounded-lg border transition-colors
                       {'border-emerald-300 bg-emerald-50 text-emerald-700 hover:bg-emerald-100' if enabled else 'border-gray-300 bg-gray-50 text-gray-600 hover:bg-gray-100'}">
                <span class="inline-flex w-9 h-5 rounded-full {toggle_color} relative transition-colors">
                  <span class="absolute top-0.5 left-0.5 w-4 h-4 bg-white rounded-full shadow transform transition-transform {toggle_dot}"></span>
                </span>
                {toggle_label}
              </button>
            </form>
          </div>
          <form method="post" action="/save-xero-tenant/{tid}" class="space-y-4">
            <div>
              <label class="block text-xs font-medium text-gray-700 mb-1">Invoice income account <span class="text-gray-400 font-normal">(revenue / sales)</span></label>
              <select name="invoice_account_code"
                class="w-full text-sm border border-gray-200 rounded-lg px-3 py-2 focus:outline-none focus:ring-2 focus:ring-indigo-300">
                {rev_opts}
              </select>
              {rev_badge}
            </div>
            <div>
              <label class="block text-xs font-medium text-gray-700 mb-1">Payment bank account <span class="text-gray-400 font-normal">(where payments land)</span></label>
              <select name="payment_account_code"
                class="w-full text-sm border border-gray-200 rounded-lg px-3 py-2 focus:outline-none focus:ring-2 focus:ring-indigo-300">
                {bank_opts}
              </select>
              {bank_badge}
            </div>
            <button type="submit"
              class="px-4 py-2 bg-indigo-600 hover:bg-indigo-700 text-white text-sm font-medium rounded-lg transition-colors">
              Save Mapping
            </button>
          </form>
        </div>"""

    return f"""
    <div class="space-y-4">
      <div class="flex items-center justify-between">
        <h2 class="font-semibold text-gray-900">Xero Organisations</h2>
        <p class="text-xs text-gray-400">{len(tenant_accounts)} organisation{"s" if len(tenant_accounts) != 1 else ""} connected</p>
      </div>
      {cards_html}
    </div>"""


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


def _bootstrap_credentials(config) -> None:
    """Write credentials from env vars to disk on first boot (Fly.io / Docker)."""
    import os
    creds_b64 = os.getenv("GOOGLE_CREDENTIALS_JSON", "")
    if creds_b64:
        creds_path = Path(config.google_credentials_file)
        if not creds_path.exists():
            creds_path.parent.mkdir(parents=True, exist_ok=True)
            creds_path.write_bytes(base64.b64decode(creds_b64))


def create_app() -> Flask:
    config = load_config()
    _bootstrap_credentials(config)
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
            return redirect(url_for("dashboard"))
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

    @app.post("/save-xero-accounts")
    @require_login
    def save_xero_accounts():
        invoice_code = (request.form.get("invoice_account_code") or "").strip()
        payment_code = (request.form.get("payment_account_code") or "").strip()
        set_json_setting(config.admin_db_file, "xero_invoice_account_code", invoice_code)
        set_json_setting(config.admin_db_file, "xero_payment_account_code", payment_code)
        session["save_notice"] = "success:Xero account mapping saved."
        return redirect(url_for("index"))

    @app.post("/save-xero-tenant/<tenant_id>")
    @require_login
    def save_xero_tenant(tenant_id: str):
        invoice_code = (request.form.get("invoice_account_code") or "").strip()
        payment_code = (request.form.get("payment_account_code") or "").strip()
        upsert_xero_tenant(
            config.admin_db_file,
            tenant_id,
            invoice_account=invoice_code,
            payment_account=payment_code,
        )
        session["save_notice"] = "success:Account mapping saved."
        return redirect(url_for("index"))

    @app.post("/toggle-xero-tenant/<tenant_id>")
    @require_login
    def toggle_xero_tenant(tenant_id: str):
        tenants = get_xero_tenants(config.admin_db_file)
        current = next((t for t in tenants if t["tenantId"] == tenant_id), {})
        new_enabled = not current.get("enabled", True)
        upsert_xero_tenant(config.admin_db_file, tenant_id, enabled=new_enabled)
        label = "enabled" if new_enabled else "paused"
        session["save_notice"] = f"success:Organisation {label}."
        return redirect(url_for("index"))

    @app.get("/connect-xero")
    @require_login
    def connect_xero():
        xero_client_id, xero_client_secret = _get_xero_creds(config)
        if not xero_client_id or not xero_client_secret:
            session["save_notice"] = "error:Xero connect failed: enter your Client ID and Secret in the Xero card first."
            return redirect(url_for("index"))
        state = secrets.token_urlsafe(24)
        dynamic_xero_redirect = _current_base_url() + "/xero/callback"
        xero_auth_url = _xero_authorization_url(config, state, redirect_uri=dynamic_xero_redirect)
        session["xero_oauth_state"] = state
        session["xero_auth_url"] = xero_auth_url
        session["xero_redirect_uri"] = dynamic_xero_redirect
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
            dynamic_xero_redirect = (
                session.get("xero_redirect_uri")
                or _current_base_url() + "/xero/callback"
            )
            token = _exchange_xero_code(config, code, redirect_uri=dynamic_xero_redirect)
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
        # Register every authorised connection as a per-tenant config entry
        try:
            all_conns = _get_all_xero_connections(token.get("access_token", ""))
            for conn in all_conns:
                upsert_xero_tenant(config.admin_db_file, conn["tenantId"], tenant_name=conn["tenantName"])
        except Exception:
            pass
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
            dynamic_google_redirect = _current_base_url() + "/oauth/callback"
            auth_url, state = oauth_authorization_url(config, redirect_uri=dynamic_google_redirect)
        except Exception as exc:
            session["save_notice"] = f"error:Could not start Google OAuth: {exc}"
            return redirect(url_for("index"))
        print(f"[Google OAuth] redirect_uri={dynamic_google_redirect}")
        print(f"[Google OAuth] auth_url={auth_url}")
        session["oauth_state"] = state
        session["oauth_auth_url"] = auth_url
        session["google_redirect_uri"] = dynamic_google_redirect
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
        dynamic_google_redirect = (
            session.get("google_redirect_uri")
            or _current_base_url() + "/oauth/callback"
        )
        try:
            creds = oauth_exchange_code(config, state=state, code=code, redirect_uri=dynamic_google_redirect)
        except Exception as exc:
            print(f"[OAuth Callback] token exchange failed: {exc}")
            return _page(f"""
            <div class="min-h-screen flex items-center justify-center bg-gray-50">
              <div class="bg-white rounded-2xl shadow p-8 max-w-md w-full text-center">
                <p class="text-red-600 font-medium mb-2">Google token exchange failed.</p>
                <p class="text-gray-700 text-sm mb-2">{escape(str(exc))}</p>
                <p class="text-gray-500 text-sm mb-4">Make sure <strong>{escape(dynamic_google_redirect)}</strong> is listed
                as an authorised redirect URI in your Google Cloud Console OAuth client.</p>
                <a href="/settings" class="text-indigo-600 hover:underline text-sm">Back to settings</a>
              </div>
            </div>
            """), 400
        save_admin_credentials(config, creds)
        session["logged_in"] = True
        session.pop("oauth_state", None)
        set_json_setting(config.admin_db_file, "oauth_pending_state", "")
        session["save_notice"] = "success:Google connected successfully."
        session.pop("oauth_auth_url", None)
        set_json_setting(config.admin_db_file, "oauth_auth_url", "")
        return redirect(url_for("index"))

    # ── Dashboard (main page) ──────────────────────────────────────────────────

    @app.get("/")
    @require_login
    def dashboard():
        state = load_state(config.state_file)
        inv_map = state.get("event_invoice_map", {})
        total_invoices = len(inv_map)
        watches = get_google_watches(config.admin_db_file)
        xero_tok = load_xero_token(config.xero_token_file)
        google_ok = bool(load_admin_credentials(config))
        xero_ok = bool(xero_tok and not token_is_expired(xero_tok))
        watch_count = len(watches)
        enabled = get_enabled(config.admin_db_file)

        recent_logs = _feed.recent(500)
        last_seq_val = recent_logs[-1]["seq"] if recent_logs else 0

        # Scan feed for meaningful status signals
        last_event_entry = None
        last_contact_entry = None
        last_invoice_entry = None
        for entry in reversed(recent_logs):
            msg = entry.get("msg", "")
            level = entry.get("level", "")
            if last_event_entry is None and level == "event" and "DONE event detected:" in msg:
                last_event_entry = entry
            if last_contact_entry is None and level == "event" and msg.startswith("New contact:"):
                last_contact_entry = entry
            if last_invoice_entry is None and level == "success" and "Invoice created" in msg:
                last_invoice_entry = entry
            if last_event_entry and last_contact_entry and last_invoice_entry:
                break

        def _ago(ts_f: float) -> str:
            diff = time.time() - ts_f
            if diff < 5:
                return "just now"
            if diff < 60:
                return f"{int(diff)}s ago"
            if diff < 3600:
                return f"{int(diff // 60)}m ago"
            if diff < 86400:
                return f"{int(diff // 3600)}h ago"
            return f"{int(diff // 86400)}d ago"

        def _signal_card(label: str, value: str, sub: str, color: str) -> str:
            return (
                f'<div class="bg-neutral-900 border border-neutral-800 rounded-xl p-4 min-w-0">'
                f'<p class="text-xs text-neutral-500 mb-1.5 uppercase tracking-wider">{label}</p>'
                f'<p class="text-sm font-semibold {color} truncate" title="{escape(value)}">{escape(value)}</p>'
                f'<p class="text-xs text-neutral-600 mt-0.5">{escape(sub)}</p>'
                f'</div>'
            )

        # Webhook signal card
        if watch_count:
            wh_value = f"{watch_count} active webhook{'s' if watch_count != 1 else ''}"
            wh_sub = "Google push notifications on"
            wh_color = "text-indigo-300"
        else:
            wh_value = "Polling only"
            wh_sub = "No webhooks registered"
            wh_color = "text-neutral-400"
        webhook_card = _signal_card("Webhooks", wh_value, wh_sub, wh_color)

        # Last DONE event card
        if last_event_entry:
            ev_msg = last_event_entry["msg"].replace("DONE event detected: ", "").strip('"')
            ev_sub = _ago(last_event_entry["ts"])
            event_card = _signal_card("Last event formatted", ev_msg, ev_sub, "text-cyan-300")
        else:
            event_card = _signal_card("Last event formatted", "None yet", "Waiting for a DONE event…", "text-neutral-500")

        # Last new contact card
        if last_contact_entry:
            ct_name = last_contact_entry["msg"].replace("New contact: ", "").strip()
            ct_sub = _ago(last_contact_entry["ts"])
            contact_card = _signal_card("Last new contact", ct_name, ct_sub, "text-emerald-300")
        else:
            contact_card = _signal_card("Last new contact", "None yet", "New submitters appear here", "text-neutral-500")

        # Last invoice card
        if last_invoice_entry:
            inv_msg = last_invoice_entry["msg"].replace("Invoice created in Xero — ", "").strip()
            inv_sub = _ago(last_invoice_entry["ts"])
            invoice_card = _signal_card("Last invoice", inv_msg[:40], inv_sub, "text-blue-300")
        else:
            invoice_card = _signal_card("Last invoice", f"{total_invoices} total" if total_invoices else "None yet", "Session history", "text-neutral-400")

        # Connection badges
        google_badge = (
            '<span class="flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium bg-emerald-900/60 text-emerald-300 border border-emerald-700/50">'
            '<span class="w-1.5 h-1.5 rounded-full bg-emerald-400 shrink-0"></span>Google</span>'
            if google_ok else
            '<span class="flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium bg-neutral-800 text-neutral-400 border border-neutral-700">'
            '<span class="w-1.5 h-1.5 rounded-full bg-neutral-500 shrink-0"></span>Google</span>'
        )
        xero_badge = (
            '<span class="flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium bg-blue-900/60 text-blue-300 border border-blue-700/50">'
            '<span class="w-1.5 h-1.5 rounded-full bg-blue-400 shrink-0"></span>Xero</span>'
            if xero_ok else
            '<span class="flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium bg-neutral-800 text-neutral-400 border border-neutral-700">'
            '<span class="w-1.5 h-1.5 rounded-full bg-neutral-500 shrink-0"></span>Xero</span>'
        )

        # On/Off toggle state
        enabled_js = "true" if enabled else "false"
        toggle_label = "Running" if enabled else "Paused"
        toggle_cls = (
            "flex items-center gap-2 px-4 py-1.5 rounded-full text-sm font-semibold border transition-all "
            + ("bg-emerald-900/50 text-emerald-300 border-emerald-600/60 hover:bg-emerald-800/60"
               if enabled else
               "bg-neutral-800 text-neutral-400 border-neutral-600 hover:bg-neutral-700")
        )
        dot_cls = "w-2 h-2 rounded-full " + ("bg-emerald-400 animate-pulse" if enabled else "bg-neutral-500")

        def _render_line(entry):
            ts = dt.datetime.fromtimestamp(entry["ts"], tz=dt.timezone.utc).strftime("%H:%M:%S")
            level = entry.get("level", "info")
            msg = entry.get("msg", "")
            from html import escape as _esc
            color_map = {
                "event":   "text-cyan-400",
                "success": "text-emerald-400",
                "warn":    "text-amber-400",
                "error":   "text-red-400",
                "paid":    "text-emerald-300",
                "system":  "text-neutral-500",
                "info":    "text-neutral-300",
            }
            prefix_map = {
                "event":   "◆",
                "success": "✓",
                "warn":    "⚠",
                "error":   "✗",
                "paid":    "£",
                "system":  "·",
                "info":    "›",
            }
            col = color_map.get(level, "text-neutral-300")
            pre = prefix_map.get(level, "›")
            return (
                f'<div class="term-line flex gap-3 leading-relaxed">'
                f'<span class="text-neutral-600 shrink-0 select-none">{_esc(ts)}</span>'
                f'<span class="{col} shrink-0 select-none">{pre}</span>'
                f'<span class="{col}">{_esc(msg)}</span>'
                f'</div>'
            )

        history_html = "\n".join(_render_line(e) for e in recent_logs) if recent_logs else (
            '<div class="text-neutral-600 text-sm italic">No activity yet — waiting for events…</div>'
        )

        return (
            f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Powwash Bridge</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <style>
    body {{ background: #0a0a0f; }}
    #terminal {{ scroll-behavior: smooth; }}
    .term-line {{ padding: 1px 0; }}
  </style>
</head>
<body class="min-h-screen text-neutral-100 font-sans">

  <!-- Header -->
  <header class="flex items-center justify-between px-6 py-3 border-b border-neutral-800 bg-neutral-900/80 backdrop-blur sticky top-0 z-10">
    <div class="flex items-center gap-3">
      <div class="w-8 h-8 bg-indigo-600 rounded-lg flex items-center justify-center shrink-0">
        <svg class="w-4 h-4 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 10V3L4 14h7v7l9-11h-7z"/>
        </svg>
      </div>
      <div>
        <h1 class="text-sm font-semibold text-white tracking-tight">Powwash Bridge</h1>
        <p class="text-xs text-neutral-500">Calendar → Xero automation</p>
      </div>
    </div>
    <div class="flex items-center gap-2">
      {google_badge}
      {xero_badge}
      <div class="w-px h-5 bg-neutral-700 mx-1"></div>
      <!-- On/Off Toggle -->
      <button id="toggle-btn" onclick="toggleEnabled()" class="{toggle_cls}">
        <span id="toggle-dot" class="{dot_cls}"></span>
        <span id="toggle-label">{toggle_label}</span>
      </button>
      <div class="w-px h-5 bg-neutral-700 mx-1"></div>
      <a href="/settings" class="px-3 py-1.5 text-xs font-medium text-neutral-300 hover:text-white bg-neutral-800 hover:bg-neutral-700 rounded-lg border border-neutral-700 transition-colors">
        Settings
      </a>
      <a href="/logout" class="px-3 py-1.5 text-xs font-medium text-neutral-400 hover:text-neutral-300 transition-colors">
        Sign out
      </a>
    </div>
  </header>

  <!-- Status signals -->
  <div class="grid grid-cols-2 sm:grid-cols-4 gap-3 px-6 pt-5 pb-4">
    {webhook_card}
    {event_card}
    {contact_card}
    {invoice_card}
  </div>

  <!-- Terminal -->
  <div class="mx-6 mb-6 rounded-xl overflow-hidden border border-neutral-800 shadow-2xl">
    <!-- Terminal chrome -->
    <div class="flex items-center justify-between px-4 py-2.5 bg-neutral-900 border-b border-neutral-800">
      <div class="flex items-center gap-1.5">
        <div class="w-3 h-3 rounded-full bg-red-500/80"></div>
        <div class="w-3 h-3 rounded-full bg-amber-400/80"></div>
        <div class="w-3 h-3 rounded-full bg-emerald-500/80"></div>
      </div>
      <span class="text-xs font-mono text-neutral-500">live feed</span>
      <div class="flex items-center gap-2">
        <span id="conn-dot" class="w-2 h-2 rounded-full bg-neutral-600"></span>
        <span id="conn-label" class="text-xs text-neutral-600">connecting…</span>
        <button id="scroll-btn" onclick="toggleScroll()"
          class="text-xs text-neutral-500 hover:text-neutral-300 px-2 py-0.5 rounded border border-neutral-700 transition-colors">
          ↓ Auto-scroll
        </button>
      </div>
    </div>
    <!-- Terminal body -->
    <div id="terminal"
      class="h-[55vh] overflow-y-auto bg-neutral-950 p-4 font-mono text-sm"
      onscroll="onUserScroll()">
      {history_html}
    </div>
  </div>

  <!-- Legend -->
  <div class="flex flex-wrap gap-4 px-6 pb-6 text-xs font-mono">
    <span class="text-cyan-400">◆ event / new contact</span>
    <span class="text-emerald-400">✓ created / logged</span>
    <span class="text-emerald-300">£ payment received</span>
    <span class="text-amber-400">⚠ needs attention</span>
    <span class="text-red-400">✗ error</span>
    <span class="text-neutral-500">· system</span>
  </div>

<script>
const term = document.getElementById('terminal');
const connDot = document.getElementById('conn-dot');
const connLabel = document.getElementById('conn-label');
const scrollBtn = document.getElementById('scroll-btn');
let autoScroll = true;

function scrollToBottom() {{
  term.scrollTop = term.scrollHeight;
}}
scrollToBottom();

function toggleScroll() {{
  autoScroll = !autoScroll;
  scrollBtn.textContent = autoScroll ? '↓ Auto-scroll' : '⏸ Paused';
  scrollBtn.classList.toggle('text-indigo-400', autoScroll);
  scrollBtn.classList.toggle('text-neutral-500', !autoScroll);
  if (autoScroll) scrollToBottom();
}}

function onUserScroll() {{
  const atBottom = term.scrollTop + term.clientHeight >= term.scrollHeight - 40;
  if (!atBottom && autoScroll) {{
    autoScroll = false;
    scrollBtn.textContent = '⏸ Paused';
    scrollBtn.classList.remove('text-indigo-400');
    scrollBtn.classList.add('text-neutral-500');
  }}
}}

const levelColor = {{
  event: 'text-cyan-400', success: 'text-emerald-400', warn: 'text-amber-400',
  error: 'text-red-400', paid: 'text-emerald-300', system: 'text-neutral-500', info: 'text-neutral-300',
}};
const levelPrefix = {{
  event: '◆', success: '✓', warn: '⚠', error: '✗', paid: '£', system: '·', info: '›',
}};

function escHtml(s) {{
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}}

function appendLine(entry) {{
  const ts = new Date(entry.ts * 1000).toLocaleTimeString('en-GB', {{hour:'2-digit',minute:'2-digit',second:'2-digit'}});
  const lvl = entry.level || 'info';
  const col = levelColor[lvl] || 'text-neutral-300';
  const pre = levelPrefix[lvl] || '›';
  const div = document.createElement('div');
  div.className = 'term-line flex gap-3 leading-relaxed';
  div.innerHTML = `<span class="text-neutral-600 shrink-0 select-none">${{ts}}</span>`
    + `<span class="${{col}} shrink-0 select-none">${{pre}}</span>`
    + `<span class="${{col}}">${{escHtml(entry.msg)}}</span>`;
  term.appendChild(div);
  if (autoScroll) scrollToBottom();
}}

let lastSeq = {last_seq_val};
const es = new EventSource('/feed?since=' + lastSeq);

es.onopen = () => {{
  connDot.className = 'w-2 h-2 rounded-full bg-emerald-400';
  connLabel.textContent = 'live';
  connLabel.className = 'text-xs text-emerald-400';
}};

es.onmessage = (e) => {{
  try {{
    const data = JSON.parse(e.data);
    if (data.type !== 'log' && !data.seq) return;
    appendLine(data);
    lastSeq = data.seq;
  }} catch (err) {{}}
}};

es.onerror = () => {{
  connDot.className = 'w-2 h-2 rounded-full bg-red-400';
  connLabel.textContent = 'reconnecting…';
  connLabel.className = 'text-xs text-red-400';
}};

// On/Off toggle
let _enabled = {enabled_js};
const toggleBtn = document.getElementById('toggle-btn');
const toggleDot = document.getElementById('toggle-dot');
const toggleLbl = document.getElementById('toggle-label');

function applyToggleState(on) {{
  _enabled = on;
  toggleLbl.textContent = on ? 'Running' : 'Paused';
  if (on) {{
    toggleBtn.className = 'flex items-center gap-2 px-4 py-1.5 rounded-full text-sm font-semibold border transition-all bg-emerald-900/50 text-emerald-300 border-emerald-600/60 hover:bg-emerald-800/60';
    toggleDot.className = 'w-2 h-2 rounded-full bg-emerald-400 animate-pulse';
  }} else {{
    toggleBtn.className = 'flex items-center gap-2 px-4 py-1.5 rounded-full text-sm font-semibold border transition-all bg-neutral-800 text-neutral-400 border-neutral-600 hover:bg-neutral-700';
    toggleDot.className = 'w-2 h-2 rounded-full bg-neutral-500';
  }}
}}

function toggleEnabled() {{
  toggleBtn.disabled = true;
  fetch('/toggle-enabled', {{method: 'POST'}})
    .then(r => r.json())
    .then(d => {{ applyToggleState(d.enabled); toggleBtn.disabled = false; }})
    .catch(() => {{ toggleBtn.disabled = false; }});
}}
</script>

</body>
</html>"""
        )

    @app.get("/feed")
    def feed_stream():
        if not session.get("logged_in"):
            return "", 401
        last_seq = int(request.args.get("since", 0))
        from flask import Response, stream_with_context
        def generate():
            yield from _feed.stream(last_seq)
        return Response(
            stream_with_context(generate()),
            content_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/poll-now")
    @require_login
    def poll_now():
        _feed.push("Manual poll triggered", "system")
        trigger_poll()
        return "", 204

    @app.post("/toggle-enabled")
    @require_login
    def toggle_enabled():
        current = get_enabled(config.admin_db_file)
        set_enabled(config.admin_db_file, not current)
        new_state = not current
        import flask as _flask
        return _flask.jsonify({"enabled": new_state})

    @app.get("/settings")
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

        # Fetch Xero accounts per tenant for account-mapping UI
        xero_tenant_account_data: list[dict] = []
        if xero_ok:
            try:
                _tok = load_xero_token(config.xero_token_file)
                if token_is_expired(_tok) and _tok.get("refresh_token"):
                    _cid, _csec = _get_xero_creds(config)
                    if _cid and _csec:
                        _refreshed = refresh_xero_token(_cid, _csec, _tok["refresh_token"])
                        _tok = {**_tok, **_refreshed}
                        save_xero_token(config.xero_token_file, _tok)
                _at = _tok.get("access_token", "")
                _all_conns = _get_all_xero_connections(_at)
                _saved_tenants = {t["tenantId"]: t for t in get_xero_tenants(config.admin_db_file)}
                for _conn in _all_conns:
                    _tid = _conn["tenantId"]
                    _tname = _conn.get("tenantName", _tid)
                    _rev: list = []
                    _bank: list = []
                    try:
                        _ar = requests.get(
                            "https://api.xero.com/api.xro/2.0/Accounts",
                            headers={
                                "Authorization": f"Bearer {_at}",
                                "Xero-tenant-id": _tid,
                                "Accept": "application/json",
                            },
                            timeout=10,
                        )
                        for _a in _ar.json().get("Accounts", []):
                            if _a.get("Status") != "ACTIVE":
                                continue
                            _typ = _a.get("Type", "")
                            if _typ in ("REVENUE", "SALES", "OTHERINCOME"):
                                _rev.append(_a)
                            elif _typ == "BANK":
                                _bank.append(_a)
                    except Exception:
                        pass
                    _cfg = _saved_tenants.get(_tid, {})
                    xero_tenant_account_data.append({
                        "tenantId": _tid,
                        "tenantName": _tname,
                        "enabled": _cfg.get("enabled", True),
                        "invoiceAccount": _cfg.get("invoiceAccount", ""),
                        "paymentAccount": _cfg.get("paymentAccount", ""),
                        "revenueAccounts": _rev,
                        "bankAccounts": _bank,
                    })
            except Exception:
                pass
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

        # --- Webhook state ---
        watches = get_google_watches(config.admin_db_file)
        xero_wh_key = get_xero_webhook_key(config.admin_db_file)
        xero_wh_verified = get_xero_webhook_verified(config.admin_db_file)
        base_url = request.host_url.rstrip("/")
        # Replit proxies HTTPS → HTTP internally; always show the public HTTPS URL
        base_url = base_url.replace("http://", "https://", 1)
        gcal_webhook_url = f"{base_url}/webhooks/google-calendar"
        xero_webhook_url = f"{base_url}/webhooks/xero"

        import time as _time
        now_ms = int(_time.time() * 1000)
        active_cals_for_wh = get_active_calendars(config.admin_db_file, config.google_calendar_id)
        watch_rows_html = ""
        for cal_id in active_cals_for_wh:
            winfo = watches.get(cal_id)
            if winfo:
                exp_ms = int(winfo.get("expiration_ms") or 0)
                if exp_ms > now_ms:
                    exp_dt = dt.datetime.fromtimestamp(exp_ms / 1000, tz=dt.timezone.utc)
                    exp_str = exp_dt.strftime("%d/%m/%Y %H:%M UTC")
                    badge = f'<span class="text-xs font-medium text-emerald-700 bg-emerald-50 border border-emerald-200 px-2 py-0.5 rounded-full">Active — expires {escape(exp_str)}</span>'
                else:
                    badge = '<span class="text-xs font-medium text-amber-700 bg-amber-50 border border-amber-200 px-2 py-0.5 rounded-full">Expired — re-register</span>'
            else:
                badge = '<span class="text-xs font-medium text-gray-500 bg-gray-100 px-2 py-0.5 rounded-full">Not registered</span>'
            cal_name = next((c.get("summary_display") or c.get("summary") or cal_id for c in calendars if c.get("id") == cal_id), cal_id)
            watch_rows_html += f'<div class="flex items-center justify-between py-2"><span class="text-sm text-gray-700 truncate">{escape(cal_name)}</span>{badge}</div>'
        if not watch_rows_html:
            watch_rows_html = '<p class="text-sm text-gray-400">No active calendars selected yet.</p>'

        # Google Calendar watch badge
        if watches and active_cals_for_wh and all(
            int((watches.get(c) or {}).get("expiration_ms") or 0) > now_ms
            for c in active_cals_for_wh if watches.get(c)
        ) and all(watches.get(c) for c in active_cals_for_wh):
            gcal_watch_badge = '<span class="text-xs font-medium text-emerald-700 bg-emerald-50 border border-emerald-200 px-2 py-0.5 rounded-full">&#10003; Registered</span>'
        elif watches:
            gcal_watch_badge = '<span class="text-xs font-medium text-amber-700 bg-amber-50 border border-amber-200 px-2 py-0.5 rounded-full">Some watches expired</span>'
        else:
            gcal_watch_badge = '<span class="text-xs font-medium text-gray-500 bg-gray-100 px-2 py-0.5 rounded-full">Polling only</span>'

        # --- Deployment URLs (always computed from live request so they auto-update with domain) ---
        _req_base = _current_base_url()
        google_redirect = escape(_req_base + "/oauth/callback")
        xero_redirect = escape(_req_base + "/xero/callback")

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
            <div class="flex items-center gap-3">
              <a href="/" class="text-sm text-indigo-600 hover:text-indigo-700 flex items-center gap-1.5 font-medium">
                <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 10V3L4 14h7v7l9-11h-7z"/>
                </svg>
                Live Feed
              </a>
              <a href="/logout" class="text-sm text-gray-500 hover:text-gray-700 flex items-center gap-1.5">
                <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2"
                    d="M17 16l4-4m0 0l-4-4m4 4H7m6 4v1a3 3 0 01-3 3H6a3 3 0 01-3-3V7a3 3 0 013-3h4a3 3 0 013 3v1"/>
                </svg>
                Sign out
              </a>
            </div>
          </div>

          {notice_html}

          <!-- Deployment URLs Panel -->
          <div class="bg-white border border-gray-200 rounded-2xl shadow-sm mb-6 overflow-hidden">
            <div class="flex items-center justify-between px-5 py-3 border-b border-gray-100 bg-gray-50">
              <div class="flex items-center gap-2">
                <svg class="w-4 h-4 text-indigo-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13.828 10.172a4 4 0 00-5.656 0l-4 4a4 4 0 105.656 5.656l1.102-1.101m-.758-4.899a4 4 0 005.656 0l4-4a4 4 0 00-5.656-5.656l-1.1 1.1"/>
                </svg>
                <span class="text-sm font-semibold text-gray-800">Deployment URLs</span>
                <span class="text-xs text-gray-400 font-normal ml-1">— auto-updates with your domain</span>
              </div>
              <span class="text-xs font-mono text-indigo-600 truncate max-w-xs">{escape(_req_base)}</span>
            </div>
            <div class="divide-y divide-gray-100">
              {_url_row("Google OAuth redirect URI", _req_base + "/oauth/callback", "Register in Google Cloud Console → Credentials → OAuth client → Authorised redirect URIs")}
              {_url_row("Xero OAuth redirect URI", _req_base + "/xero/callback", "Register in Xero Developer Portal → Your App → Redirect URIs")}
              {_url_row("Google Calendar webhook", _req_base + "/webhooks/google-calendar", "Used automatically when you register watches below")}
              {_url_row("Xero webhook", _req_base + "/webhooks/xero", "Paste into Xero Developer Portal → Your App → Webhooks")}
            </div>
          </div>

          <!-- Setup Steps Overview (collapsible) -->
          <details class="bg-indigo-50 border border-indigo-100 rounded-2xl mb-6 group">
            <summary class="flex items-center justify-between cursor-pointer px-6 py-4 list-none select-none">
              <h2 class="text-sm font-semibold text-indigo-900 flex items-center gap-2">
                <svg class="w-4 h-4" fill="currentColor" viewBox="0 0 20 20">
                  <path fill-rule="evenodd" d="M18 10a8 8 0 11-16 0 8 8 0 0116 0zm-7-4a1 1 0 11-2 0 1 1 0 012 0zM9 9a1 1 0 000 2v3a1 1 0 001 1h1a1 1 0 100-2v-3a1 1 0 00-1-1H9z" clip-rule="evenodd"/>
                </svg>
                Quick Setup Guide
              </h2>
              <svg class="w-4 h-4 text-indigo-400 transition-transform duration-200 group-open:rotate-180" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"/>
              </svg>
            </summary>
            <div class="px-6 pb-5">
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
          </details>

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

            <!-- Xero Account Mapping -->
            {_xero_tenant_cards(xero_ok, xero_tenant_account_data)}

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

          </form>

          <!-- Google Sheets - standalone form -->
          <form method="post" action="/save-sheet-target">
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
                  {f'<p class="text-xs text-emerald-600 mt-1">&#10003; Currently saved: <span class="font-medium">{escape(sheet_current_id)}</span></p>' if sheet_current_id else '<p class="text-xs text-gray-400 mt-1">No spreadsheet saved yet.</p>'}
                </div>
                <div>
                  <label class="block text-sm font-medium text-gray-700 mb-1.5">Sheet tab name</label>
                  <input name="sheet_name" value="{escape(sheet_name_val)}"
                    placeholder="Sheet1"
                    class="w-48 px-3 py-2 border border-gray-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500">
                  {f'<p class="text-xs text-emerald-600 mt-1">&#10003; Currently saved: <span class="font-medium">{escape(sheet_name_val)}</span></p>' if sheet_current_id else ""}
                </div>
                <div>
                  <button type="submit"
                    class="px-4 py-2 bg-indigo-600 hover:bg-indigo-700 text-white text-sm font-medium rounded-lg transition-colors">
                    Save Spreadsheet Settings
                  </button>
                </div>
              </div>
            </div>
          </form>

          <!-- Webhooks Card -->
          <div class="bg-white rounded-2xl shadow-sm border border-gray-200 p-6 space-y-6">
            <div>
              <h2 class="font-semibold text-gray-900 mb-1">Webhooks &amp; Real-Time Updates</h2>
              <p class="text-sm text-gray-500">Instead of waiting for the next poll, connect Google Calendar and Xero to push changes to this app the moment they happen.</p>
            </div>

            <!-- Google Calendar Push Notifications -->
            <div class="border border-gray-100 rounded-xl p-4">
              <div class="flex items-center justify-between mb-3">
                <h3 class="text-sm font-semibold text-gray-800">Google Calendar — Push Notifications</h3>
                {gcal_watch_badge}
              </div>
              <p class="text-xs text-gray-500 mb-3">Click <strong>Register</strong> to tell Google to call this app whenever a calendar event changes. Watches auto-renew every 7 days.</p>
              <div class="mb-3">
                <label class="block text-xs font-medium text-gray-600 mb-1">Your webhook URL (set automatically — no action needed)</label>
                <div class="flex gap-2">
                  <input readonly value="{escape(gcal_webhook_url)}"
                    class="flex-1 px-3 py-1.5 border border-gray-200 rounded-lg text-xs font-mono bg-gray-50 text-gray-600">
                  <button type="button"
                    onclick="navigator.clipboard.writeText('{gcal_webhook_url.replace(chr(39), chr(92)+chr(39))}');this.textContent='Copied!';setTimeout(()=>this.textContent='Copy',2000)"
                    class="px-3 py-1.5 text-xs font-medium bg-gray-100 hover:bg-gray-200 text-gray-700 rounded-lg transition-colors">Copy</button>
                </div>
              </div>
              <div class="divide-y divide-gray-50 mb-3">
                {watch_rows_html}
              </div>
              <div class="flex gap-2">
                <form method="post" action="/setup/register-google-watches">
                  <button type="submit"
                    class="px-4 py-2 text-xs font-medium text-white bg-indigo-600 hover:bg-indigo-700 rounded-lg transition-colors {"opacity-50 pointer-events-none" if not google_ok else ""}">
                    {"Register / Refresh Watches" if not watches else "Re-register Watches"}
                  </button>
                </form>
                {"" if not watches else """<form method="post" action="/setup/stop-google-watches">
                  <button type="submit" class="px-4 py-2 text-xs font-medium text-red-600 bg-red-50 hover:bg-red-100 rounded-lg border border-red-200 transition-colors">Stop Watches</button>
                </form>"""}
              </div>
            </div>

            <!-- Xero Webhooks -->
            <div class="border border-gray-100 rounded-xl p-4">
              <div class="flex items-center justify-between mb-3">
                <h3 class="text-sm font-semibold text-gray-800">Xero — Invoice Payment Webhooks</h3>
                {('<span class="text-xs font-medium text-emerald-700 bg-emerald-50 border border-emerald-200 px-2 py-0.5 rounded-full">&#10003; Verified</span>' if xero_wh_verified else ('<span class="text-xs font-medium text-amber-700 bg-amber-50 border border-amber-200 px-2 py-0.5 rounded-full">Key saved — awaiting verification</span>' if xero_wh_key else '<span class="text-xs font-medium text-gray-500 bg-gray-100 px-2 py-0.5 rounded-full">Not configured</span>'))}
              </div>
              <p class="text-xs text-gray-500 mb-3">When an invoice is paid in Xero, Xero will call this app and the sheet row will be updated to <strong>Paid</strong> automatically.</p>

              <div class="mb-3 p-3 bg-blue-50 border border-blue-200 rounded-lg text-xs text-blue-800 space-y-1">
                <p class="font-semibold">You need to do this once in Xero:</p>
                <ol class="list-decimal list-inside space-y-1 text-blue-700">
                  <li>Go to <strong>developer.xero.com</strong> → your app → <strong>Webhooks</strong></li>
                  <li>Add a new webhook, paste the URL below, tick <strong>Invoices</strong></li>
                  <li>Copy the <strong>Webhooks Key</strong> shown and paste it below</li>
                  <li>Click Save below — Xero will send a test ping to verify</li>
                </ol>
              </div>

              <div class="mb-3">
                <label class="block text-xs font-medium text-gray-600 mb-1">Webhook URL — paste this into the Xero Developer portal</label>
                <div class="flex gap-2">
                  <input readonly value="{escape(xero_webhook_url)}"
                    class="flex-1 px-3 py-1.5 border border-gray-200 rounded-lg text-xs font-mono bg-gray-50 text-gray-600">
                  <button type="button"
                    onclick="navigator.clipboard.writeText('{xero_webhook_url.replace(chr(39), chr(92)+chr(39))}');this.textContent='Copied!';setTimeout(()=>this.textContent='Copy',2000)"
                    class="px-3 py-1.5 text-xs font-medium bg-gray-100 hover:bg-gray-200 text-gray-700 rounded-lg transition-colors">Copy</button>
                </div>
              </div>

              <form method="post" action="/save-xero-webhook-key">
                <div class="mb-3">
                  <label class="block text-xs font-medium text-gray-600 mb-1">Xero Webhook Signing Key</label>
                  <input name="xero_webhook_key" type="password"
                    placeholder="{"••••••••  (saved)" if xero_wh_key else "Paste the key from the Xero Developer portal"}"
                    class="w-full px-3 py-1.5 border border-gray-300 rounded-lg text-xs font-mono focus:outline-none focus:ring-2 focus:ring-indigo-500">
                </div>
                <button type="submit"
                  class="px-4 py-2 text-xs font-medium text-white bg-indigo-600 hover:bg-indigo-700 rounded-lg transition-colors">
                  Save Webhook Key
                </button>
              </form>
            </div>
          </div>

          <form method="post" action="/save" enctype="multipart/form-data" class="space-y-6">

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

        aliases = _save_submitter_aliases_from_form(config, request.form)
        msg = "Settings saved."
        if aliases:
            msg += " Submitter names saved."
        session["save_notice"] = f"success:{msg}"

        set_json_setting(config.admin_db_file, "settings_version", {"updated": True})
        return redirect(url_for("index"))

    @app.post("/save-sheet-target")
    @require_login
    def save_sheet_target():
        picked = (request.form.get("spreadsheet_pick") or "").strip()
        entered = (request.form.get("spreadsheet_input") or "").strip()
        spreadsheet_id = _extract_spreadsheet_id(picked or entered)
        sheet_name = (request.form.get("sheet_name") or "Sheet1").strip() or "Sheet1"
        set_sheet_target(config.admin_db_file, spreadsheet_id=spreadsheet_id, sheet_name=sheet_name)
        target = {"spreadsheet_id": spreadsheet_id, "sheet_name": sheet_name}
        creds = load_admin_credentials(config)
        ok, _ = _sheets_status_data(config, creds, target)
        msg = "Spreadsheet settings saved. Connection is ready." if ok else "Spreadsheet settings saved."
        session["save_notice"] = f"success:{msg}"
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

        creds = load_admin_credentials(config)
        sheet_target = get_sheet_target(config.admin_db_file)
        spreadsheet_id = (sheet_target.get("spreadsheet_id") or "").strip()
        sheet_name = (sheet_target.get("sheet_name") or "Sheet1").strip() or "Sheet1"
        sheet_updated = 0
        if creds and spreadsheet_id:
            try:
                sheet_updated = backfill_submitter_in_sheet(
                    creds,
                    spreadsheet_id=spreadsheet_id,
                    sheet_name=sheet_name,
                    aliases=aliases,
                )
            except Exception:
                pass
        if sheet_updated:
            msg += f" Updated {sheet_updated} sheet row(s)."

        session["save_notice"] = f"success:{msg}"
        set_json_setting(config.admin_db_file, "settings_version", {"updated": True})
        return redirect(url_for("index"))

    # ── Webhook endpoints ──────────────────────────────────────────────────────

    @app.post("/webhooks/google-calendar")
    def webhook_google_calendar():
        """Receive Google Calendar push notifications and wake the poller."""
        state_header = request.headers.get("X-Goog-Resource-State", "")
        token = request.headers.get("X-Goog-Channel-Token", "")
        if token != "gcal-bridge":
            return "", 200
        if state_header == "exists":
            _feed.push("Google Calendar change detected — scanning calendars now", "event")
            trigger_poll()
            print("[webhook] Google Calendar event changed — poll triggered", flush=True)
        return "", 200

    @app.post("/webhooks/xero")
    def webhook_xero():
        """Receive Xero webhooks (invoice paid etc.) and update the sheet."""
        import hmac as _hmac
        import hashlib as _hashlib
        import base64 as _base64

        raw_body = request.get_data()
        webhook_key = get_xero_webhook_key(config.admin_db_file)

        sig = request.headers.get("x-xero-signature", "")
        if webhook_key:
            expected = _base64.b64encode(
                _hmac.new(webhook_key.encode("utf-8"), raw_body, _hashlib.sha256).digest()
            ).decode()
            if not _hmac.compare_digest(sig, expected):
                return "", 401

        try:
            payload = request.get_json(force=True, silent=True) or {}
        except Exception:
            return "", 200

        events = payload.get("events", [])
        if not events:
            # This is an intent-to-receive verification ping from Xero — mark as verified
            set_xero_webhook_verified(config.admin_db_file, True)
            print("[webhook] Xero intent-to-receive verified successfully", flush=True)
            return "", 200

        app_state = load_state(config.state_file)
        inv_map = app_state.get("event_invoice_map", {})
        inv_id_to_key = {v: k for k, v in inv_map.items()}

        creds = load_admin_credentials(config)
        sheet_target = get_sheet_target(config.admin_db_file)
        spreadsheet_id = (sheet_target.get("spreadsheet_id") or "").strip()
        sheet_name = (sheet_target.get("sheet_name") or "Sheet1").strip() or "Sheet1"

        xero_tok = load_xero_token(config.xero_token_file)
        xero_at = (xero_tok or {}).get("access_token", "")
        xero_tenant = (xero_tok or {}).get("tenant_id", "")

        for ev in events:
            if ev.get("eventCategory") != "INVOICE":
                continue
            invoice_id = ev.get("resourceId", "")
            if not invoice_id:
                continue
            print(f"[webhook] Xero invoice event: {ev.get('eventType')} {invoice_id}", flush=True)

            if xero_at and xero_tenant:
                try:
                    resp = requests.get(
                        f"https://api.xero.com/api.xro/2.0/Invoices/{invoice_id}",
                        headers={
                            "Authorization": f"Bearer {xero_at}",
                            "Xero-tenant-id": xero_tenant,
                            "Accept": "application/json",
                        },
                        timeout=10,
                    )
                    invoices = resp.json().get("Invoices", [])
                    if invoices and invoices[0].get("Status") == "PAID":
                        inv_number = invoices[0].get("InvoiceNumber", "")
                        print(f"[webhook] Invoice {inv_number} is PAID — updating sheet", flush=True)
                        _feed.push(f"Invoice {inv_number} paid — marking sheet row as Paid", "paid")
                        if creds and spreadsheet_id and inv_number:
                            try:
                                updated = update_invoice_paid_in_sheet(
                                    creds,
                                    spreadsheet_id=spreadsheet_id,
                                    sheet_name=sheet_name,
                                    invoice_number=inv_number,
                                )
                                if updated:
                                    print(f"[webhook] Sheet row updated for {inv_number}", flush=True)
                                    _feed.push(f"Sheet updated: {inv_number} marked as Paid", "paid")
                            except Exception as exc:
                                print(f"[webhook] Sheet update failed: {exc}", flush=True)
                except Exception as exc:
                    print(f"[webhook] Xero invoice fetch failed: {exc}", flush=True)

        trigger_poll()
        return "", 200

    @app.post("/setup/register-google-watches")
    @require_login
    def register_google_watches():
        creds = load_admin_credentials(config)
        if not creds:
            session["save_notice"] = "error:Google is not connected — please connect Google first."
            return redirect(url_for("index"))
        active_cals = get_active_calendars(config.admin_db_file, config.google_calendar_id)
        if not active_cals:
            session["save_notice"] = "error:No calendars are selected. Tick at least one calendar in the Active Calendars section first."
            return redirect(url_for("index"))
        base_url = request.host_url.rstrip("/").replace("http://", "https://", 1)
        webhook_url = f"{base_url}/webhooks/google-calendar"
        ok = 0
        errors = []
        for cal_id in active_cals:
            try:
                resp = register_calendar_watch(config, cal_id, webhook_url)
                set_google_watch(
                    config.admin_db_file, cal_id,
                    resp["id"], resp["resourceId"],
                    int(resp.get("expiration") or 0),
                    webhook_url=webhook_url,
                )
                ok += 1
                print(f"[watch] Registered Google Calendar watch for {cal_id}: channel {resp['id']}", flush=True)
            except Exception as exc:
                err_msg = str(exc)
                errors.append(err_msg)
                print(f"[watch] Failed to register watch for {cal_id}: {err_msg}", flush=True)
        if errors:
            first_err = errors[0][:200]
            session["save_notice"] = (
                f"error:Registered {ok} watch(es) but {len(errors)} failed. "
                f"Google said: {first_err}"
            )
        else:
            session["save_notice"] = f"success:Registered {ok} Google Calendar watch(es). Events will now be processed in real time."
        return redirect(url_for("index"))

    @app.post("/setup/stop-google-watches")
    @require_login
    def stop_google_watches():
        watches = get_google_watches(config.admin_db_file)
        for cal_id, winfo in list(watches.items()):
            try:
                stop_calendar_watch(config, winfo["channel_id"], winfo["resource_id"])
            except Exception:
                pass
            delete_google_watch(config.admin_db_file, cal_id)
        session["save_notice"] = "success:All Google Calendar watches stopped. Polling mode resumed."
        return redirect(url_for("index"))

    @app.post("/save-xero-webhook-key")
    @require_login
    def save_xero_webhook_key():
        key = (request.form.get("xero_webhook_key") or "").strip()
        set_xero_webhook_key(config.admin_db_file, key)
        set_xero_webhook_verified(config.admin_db_file, False)
        session["save_notice"] = "success:Xero webhook key saved. Now click \"Send intent to receive\" in the Xero Developer portal to verify."
        return redirect(url_for("index"))

    return app


def run_web() -> None:
    config = load_config()
    app = create_app()
    app.run(host=config.web_host, port=config.web_port)


if __name__ == "__main__":
    run_web()
