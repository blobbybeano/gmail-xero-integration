from __future__ import annotations

import datetime as dt
import base64
import json
import os
import re
import secrets
import time
import uuid
import urllib.parse
from pathlib import Path
from functools import wraps
from html import escape

import requests
from flask import Flask, redirect, request, session, url_for
from googleapiclient.errors import HttpError

from .admin_store import (
    DEFAULT_SALES_STATS_FIELDS,
    DEFAULT_STATS_FIELDS,
    get_active_calendars,
    get_calendar_cash_sheets,
    get_cash_sheet_target,
    get_calendar_sales_sheets,
    get_cash_backlog,
    get_cash_submitter_sheets,
    get_json_setting,
    get_sales_backlog,
    get_sales_sheet_target,
    get_sales_stats_fields,
    get_sales_submitter_sheets,
    get_sheet_target,
    get_seen_submitters,
    get_stats_fields,
    get_submitter_aliases,
    init_admin_store,
    set_calendar_cash_sheets,
    set_cash_sheet_target,
    set_calendar_sales_sheets,
    set_submitter_aliases,
    set_active_calendars,
    set_cash_submitter_sheets,
    set_json_setting,
    set_sales_sheet_target,
    set_sales_stats_fields,
    set_sales_submitter_sheets,
    set_sheet_target,
    set_stats_fields,
)
from .config import load_config
from .event_processor import set_title_status_emoji, set_title_mail_emoji
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
from .google_calendar import update_event_description, build_calendar_service
from .config import AppConfig
from .xero_client import (
    load_xero_token,
    save_xero_token,
    token_is_expired,
    refresh_xero_token,
    build_xero_client,
)
from .google_sheets import backfill_submitter_in_sheet, update_invoice_paid_in_sheet
from .google_sheets import ensure_header, append_stats_row
from .event_processor import extract_sales_lines, parse_customer_fields, payment_choice
from .admin_store import (
    get_enabled,
    set_enabled,
    get_google_watches,
    get_xero_webhook_key,
    set_xero_webhook_key,
    get_xero_webhook_verified,
    set_xero_webhook_verified,
    get_xero_tenants,
    set_xero_tenants,
    upsert_xero_tenant,
)
from .trigger import (
    trigger_poll,
    trigger_watch_check,
    queue_calendar_target,
    queue_event_target,
)
from .state import (
    load_state,
    save_state,
    get_last_sync,
    get_sales_log_marker,
    set_sales_log_marker,
)
from .log_feed import feed as _feed


XERO_AUTH_URL = "https://login.xero.com/identity/connect/authorize"
XERO_TOKEN_URL = "https://identity.xero.com/connect/token"
XERO_CONNECTIONS_URL = "https://api.xero.com/connections"
REQUIRED_XERO_SCOPES = (
    "offline_access",
    "accounting.invoices",
    "accounting.contacts",
    "accounting.settings",
    "accounting.payments",
)


STAT_OPTIONS = [
    ("diary_entry_name", "Diary entry name"),
    ("submitter", "Person who submitted invoice"),
    ("customer", "Customer name"),
    ("invoice_number", "Invoice number"),
    ("receipt_details", "Receipt details (when implemented)"),
    ("slot_datetime", "Diary slot date/time"),
    ("payment_datetime", "Payment date/time"),
    ("payment_method", "Payment method"),
    ("paid_status", "Payment status (Paid/Pending)"),
    ("job_cost_ex_vat", "Job cost (ex VAT)"),
    ("job_cost_inc_vat", "Job cost (inc VAT)"),
]

SALES_STAT_OPTIONS = [
    ("submitter", "Person who submitted"),
    ("customer", "Customer name"),
    ("slot_datetime", "Diary slot date/time"),
    ("payment_method", "Payment method"),
    ("invoice_number", "Invoice number"),
    ("sales_item_desc", "Sales item + value"),
    ("sales_total_ex_vat", "Sales total"),
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


def _assistant_config(db_path: str) -> dict:
    raw = get_json_setting(
        db_path,
        "assistant_config",
        {
            "write_enabled": False,
            "always_confirm": True,
        },
    )
    if not isinstance(raw, dict):
        raw = {}
    return {
        "write_enabled": bool(raw.get("write_enabled", False)),
        "always_confirm": True,  # enforced
    }


def _set_assistant_config(db_path: str, *, write_enabled: bool) -> None:
    set_json_setting(
        db_path,
        "assistant_config",
        {"write_enabled": bool(write_enabled), "always_confirm": True},
    )


def _append_assistant_chat(role: str, text: str) -> None:
    history = session.get("assistant_chat_history") or []
    if not isinstance(history, list):
        history = []
    history.append(
        {
            "role": role,
            "text": text,
            "ts": dt.datetime.now(dt.timezone.utc).astimezone().strftime("%H:%M"),
        }
    )
    session["assistant_chat_history"] = history[-30:]


def _active_calendar_ids(config: AppConfig) -> list[str]:
    return get_active_calendars(config.admin_db_file, config.google_calendar_id)


def _find_events_by_title(config: AppConfig, phrase: str, *, days_back: int = 180, days_forward: int = 60) -> list[dict]:
    creds = load_admin_credentials(config)
    if not creds:
        return []
    svc = build_calendar_service(config)
    now = dt.datetime.now(dt.timezone.utc)
    tmin = (now - dt.timedelta(days=days_back)).isoformat().replace("+00:00", "Z")
    tmax = (now + dt.timedelta(days=days_forward)).isoformat().replace("+00:00", "Z")
    target = (phrase or "").strip().lower()
    if not target:
        return []
    hits: list[dict] = []
    for cal_id in _active_calendar_ids(config):
        page_token = None
        while True:
            resp = (
                svc.events()
                .list(
                    calendarId=cal_id,
                    timeMin=tmin,
                    timeMax=tmax,
                    singleEvents=True,
                    orderBy="startTime",
                    maxResults=250,
                    pageToken=page_token,
                )
                .execute()
            )
            for ev in (resp.get("items") or []):
                summary = str(ev.get("summary") or "")
                if target in summary.lower():
                    hits.append({"calendar_id": cal_id, "event": ev})
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
    return hits


def _list_today_created_event_titles(config: AppConfig) -> list[str]:
    creds = load_admin_credentials(config)
    if not creds:
        return []
    svc = build_calendar_service(config)
    tz = dt.datetime.now().astimezone().tzinfo or dt.timezone.utc
    today = dt.datetime.now(tz).date()
    out: list[tuple[dt.datetime, str]] = []
    for cal_id in _active_calendar_ids(config):
        page_token = None
        while True:
            resp = (
                svc.events()
                .list(
                    calendarId=cal_id,
                    singleEvents=True,
                    orderBy="updated",
                    maxResults=250,
                    pageToken=page_token,
                )
                .execute()
            )
            for ev in (resp.get("items") or []):
                created = str(ev.get("created") or "").strip()
                if not created:
                    continue
                try:
                    created_dt = dt.datetime.fromisoformat(created.replace("Z", "+00:00")).astimezone(tz)
                except Exception:
                    continue
                if created_dt.date() == today:
                    out.append((created_dt, str(ev.get("summary") or "(no title)")))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
    out.sort(key=lambda x: x[0])
    return [title for _, title in out]


def _xero_fetch_draft_invoices(config: AppConfig) -> list[dict]:
    tok = load_xero_token(config.xero_token_file)
    access_token = str(tok.get("access_token") or "").strip()
    tenant_id = str(tok.get("tenant_id") or "").strip()
    if not (access_token and tenant_id):
        return []
    url = "https://api.xero.com/api.xro/2.0/Invoices"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Xero-tenant-id": tenant_id,
        "Accept": "application/json",
    }
    results: list[dict] = []
    for page in range(1, 8):
        resp = requests.get(
            url,
            headers=headers,
            params={"where": 'Status=="DRAFT"', "page": page},
            timeout=20,
        )
        if not resp.ok:
            break
        rows = (resp.json() or {}).get("Invoices") or []
        if not rows:
            break
        results.extend(rows)
        if len(rows) < 100:
            break
    return results


def _openai_assistant_reply(prompt: str) -> tuple[str | None, str | None]:
    """
    Return (reply, error_code). error_code is one of:
    - missing_key
    - api_error
    """
    api_key = (os.getenv("OPENAI_API_KEY") or "").strip()
    if not api_key:
        return None, "missing_key"

    model = (os.getenv("OPENAI_MODEL") or "gpt-5-mini").strip()
    system_prompt = (
        "You are an assistant for a field-service calendar and invoicing app. "
        "Be concise and practical. If a user asks for risky write actions, tell them "
        "the app requires explicit approval before making changes."
    )
    payload = {
        "model": model,
        "input": [
            {
                "role": "system",
                "content": [{"type": "input_text", "text": system_prompt}],
            },
            {
                "role": "user",
                "content": [{"type": "input_text", "text": prompt}],
            },
        ],
        "max_output_tokens": 500,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    try:
        resp = requests.post(
            "https://api.openai.com/v1/responses",
            headers=headers,
            json=payload,
            timeout=45,
        )
    except Exception:
        return None, "api_error"

    if not resp.ok:
        return None, "api_error"

    try:
        data = resp.json()
    except Exception:
        return None, "api_error"

    out = str(data.get("output_text") or "").strip()
    if out:
        return out, None

    # Fallback parse for response content structure.
    parts: list[str] = []
    for item in (data.get("output") or []):
        if str(item.get("type") or "") != "message":
            continue
        for c in (item.get("content") or []):
            ctype = str(c.get("type") or "").lower()
            if ctype in {"output_text", "text", "message_text"}:
                txt = str(c.get("text") or "").strip()
                if txt:
                    parts.append(txt)
    merged = "\n".join(parts).strip()
    if merged:
        return merged, None
    return None, "api_error"


_xero_acct_cache: "dict[str, tuple[float, list, list, list, str]]" = {}
_xero_conn_cache: "dict[str, tuple[float, list[dict]]]" = {}
_XERO_CACHE_TTL = 300  # seconds (5 min)
_XERO_CONN_CACHE_TTL = 300  # seconds


def _get_tenant_acct_themes(at: str, tid: str) -> "tuple[list, list, list, str]":
    """Return (revenue_accounts, bank_accounts, branding_themes) for a Xero tenant.
    Results are cached for _XERO_CACHE_TTL seconds so the settings page doesn't
    make live API calls on every load."""
    # Cache by tenant only (not access token) so the settings page stays fast
    # across normal token refreshes.
    key = tid
    cached = _xero_acct_cache.get(key)
    if cached:
        ts, rev, bank, themes, warning = cached
        if time.time() - ts < _XERO_CACHE_TTL:
            return rev, bank, themes, warning
    hdrs = {
        "Authorization": f"Bearer {at}",
        "Xero-tenant-id": tid,
        "Accept": "application/json",
    }
    rev: list = []
    bank: list = []
    warnings: list[str] = []
    try:
        _ar = requests.get(
            "https://api.xero.com/api.xro/2.0/Accounts",
            headers=hdrs,
            timeout=3,
        )
        if _ar.ok:
            for _a in _ar.json().get("Accounts", []):
                if _a.get("Status") != "ACTIVE":
                    continue
                _typ = _a.get("Type", "")
                if _typ in ("REVENUE", "SALES", "OTHERINCOME"):
                    rev.append(_a)
                elif _typ == "BANK":
                    bank.append(_a)
        else:
            if _ar.status_code == 403:
                warnings.append("Cannot load account list (missing accounting.settings scope).")
            elif _ar.status_code == 401:
                warnings.append("Cannot load account list (token unauthorised: reconnect Xero with accounting.settings scope).")
            else:
                warnings.append(f"Cannot load account list (HTTP {_ar.status_code}).")
    except Exception:
        warnings.append("Cannot load account list (request failed).")
    themes: list = []
    try:
        _tr = requests.get(
            "https://api.xero.com/api.xro/2.0/BrandingThemes",
            headers=hdrs,
            timeout=3,
        )
        if _tr.ok:
            themes = sorted(
                _tr.json().get("BrandingThemes", []),
                key=lambda x: (x.get("SortOrder", 999), x.get("Name", "")),
            )
        else:
            if _tr.status_code == 403:
                warnings.append("Cannot load branding themes (missing accounting.settings scope).")
            elif _tr.status_code == 401:
                warnings.append("Cannot load branding themes (token unauthorised: reconnect Xero with accounting.settings scope).")
            else:
                warnings.append(f"Cannot load branding themes (HTTP {_tr.status_code}).")
    except Exception:
        warnings.append("Cannot load branding themes (request failed).")
    warning = " ".join(dict.fromkeys([w.strip() for w in warnings if w.strip()]))
    # If live fetch fails but we have stale cache, use stale data immediately so
    # Settings remains responsive instead of blocking on repeated API failures.
    if warning and cached:
        _, c_rev, c_bank, c_themes, c_warning = cached
        if c_rev or c_bank or c_themes:
            _warn = warning
            if c_warning and c_warning not in _warn:
                _warn = f"{_warn} Using cached options."
            return c_rev, c_bank, c_themes, _warn

    # Avoid caching empty+failed responses for long periods; this keeps recovery fast
    # after a reconnect/refresh.
    if warning and not (rev or bank or themes):
        # short-lived cache for failed empty responses, so UI recovers quickly
        _xero_acct_cache[key] = (time.time() - (_XERO_CACHE_TTL - 20), rev, bank, themes, warning)
    else:
        _xero_acct_cache[key] = (time.time(), rev, bank, themes, warning)
    return rev, bank, themes, warning


def _get_tenant_cached_only(tid: str) -> "tuple[list, list, list, str]":
    """Return cached tenant options only (never perform network I/O)."""
    cached = _xero_acct_cache.get(tid)
    if not cached:
        return [], [], [], ""
    _, rev, bank, themes, warning = cached
    return rev, bank, themes, warning


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
    seen: set[str] = set()
    ordered: list[str] = []
    for scope in [*REQUIRED_XERO_SCOPES, *(scopes or [])]:
        s = str(scope).strip()
        if not s or s in seen:
            continue
        seen.add(s)
        ordered.append(s)
    return " ".join(ordered)


def _parse_scope_value(value) -> set[str]:
    if not value:
        return set()
    if isinstance(value, list):
        return {str(v).strip() for v in value if str(v).strip()}
    if isinstance(value, str):
        return {v.strip() for v in value.replace(",", " ").split() if v.strip()}
    return set()


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
    config: AppConfig,
    state: str,
    redirect_uri: str | None = None,
    *,
    force_consent: bool = False,
) -> str:
    client_id, _ = _get_xero_creds(config)
    uri = redirect_uri or config.xero_redirect_uri
    # Build manually so spaces in scope are encoded as %20 (not +) — Xero requires %20
    scope_str = _xero_scope_string(config.xero_scopes)
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": uri,
        "state": state,
    }
    if force_consent:
        # Ensures Xero shows the consent/org-selection screen again so users
        # can add missing organisations to the app connection.
        params["prompt"] = "consent"
    base_params = urllib.parse.urlencode(params)
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
    if not access_token:
        return []
    cache_key = access_token[-24:]
    cached = _xero_conn_cache.get(cache_key)
    if cached:
        ts, rows = cached
        if time.time() - ts < _XERO_CONN_CACHE_TTL:
            return rows
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
    response = requests.get(XERO_CONNECTIONS_URL, headers=headers, timeout=4)
    response.raise_for_status()
    rows = [
        {"tenantId": c.get("tenantId", ""), "tenantName": c.get("tenantName", c.get("tenantId", ""))}
        for c in response.json()
        if c.get("tenantId")
    ]
    _xero_conn_cache[cache_key] = (time.time(), rows)
    return rows


def _xero_status_data(config: AppConfig) -> tuple[bool, str, str]:
    """Returns (connected, status_text, tenant_id)"""
    token = load_xero_token(config.xero_token_file)
    client_id, client_secret = _get_xero_creds(config)
    has_credentials = bool(client_id and client_secret)
    if not has_credentials:
        return False, "Enter your Xero Client ID and Secret below, then click Save.", ""
    access = token.get("access_token", "")
    tenant = token.get("tenant_id", "")
    refresh_token = token.get("refresh_token", "")

    if not access:
        return False, "Not connected — click Connect Xero below.", ""

    if token_is_expired(token):
        if not (client_id and client_secret and refresh_token):
            return False, "Xero token expired — reconnect Xero.", ""
        try:
            refreshed = refresh_xero_token(client_id, client_secret, refresh_token)
            token = {**token, **refreshed}
            save_xero_token(config.xero_token_file, token)
            access = token.get("access_token", "")
            tenant = token.get("tenant_id", tenant)
        except Exception as exc:
            short = str(exc).splitlines()[0][:180]
            return False, f"Xero token refresh failed: {short}", ""

    try:
        connections = _get_all_xero_connections(access)
    except Exception as exc:
        short = str(exc).splitlines()[0][:180]
        return False, f"Connected token, but organisation lookup failed: {short}", tenant

    if not connections:
        return False, "Connected token, but no organisations are authorised. Reconnect Xero and choose an organisation.", ""

    valid_tenants = {c.get("tenantId", "") for c in connections}
    if not tenant or tenant not in valid_tenants:
        tenant = next(iter(valid_tenants))
        token["tenant_id"] = tenant
        save_xero_token(config.xero_token_file, token)

    count = len(connections)
    return True, f"Connected ({count} organisation{'s' if count != 1 else ''})", tenant


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


def _save_cash_submitter_sheets_from_form(config: AppConfig, form) -> dict[str, dict[str, str]]:
    current = get_cash_submitter_sheets(config.admin_db_file)
    next_map = dict(current)
    prefix_id = "cash_sheet_id__"
    prefix_name = "cash_sheet_name__"

    candidate_emails: set[str] = set()
    for k in form.keys():
        if k.startswith(prefix_id):
            candidate_emails.add(k[len(prefix_id):].strip().lower())
        elif k.startswith(prefix_name):
            candidate_emails.add(k[len(prefix_name):].strip().lower())

    for email in candidate_emails:
        raw_id = (form.get(f"{prefix_id}{email}") or "").strip()
        sid = _extract_spreadsheet_id(raw_id)
        sname = (form.get(f"{prefix_name}{email}") or "Sheet1").strip() or "Sheet1"
        if sid:
            next_map[email] = {"spreadsheet_id": sid, "sheet_name": sname}
        else:
            next_map.pop(email, None)

    set_cash_submitter_sheets(config.admin_db_file, next_map)
    return next_map


def _save_sales_submitter_sheets_from_form(config: AppConfig, form) -> dict[str, dict[str, str]]:
    current = get_sales_submitter_sheets(config.admin_db_file)
    next_map = dict(current)
    prefix_id = "sales_sheet_id__"
    prefix_name = "sales_sheet_name__"

    candidate_emails: set[str] = set()
    for k in form.keys():
        if k.startswith(prefix_id):
            candidate_emails.add(k[len(prefix_id):].strip().lower())
        elif k.startswith(prefix_name):
            candidate_emails.add(k[len(prefix_name):].strip().lower())

    for email in candidate_emails:
        raw_id = (form.get(f"{prefix_id}{email}") or "").strip()
        sid = _extract_spreadsheet_id(raw_id)
        sname = (form.get(f"{prefix_name}{email}") or "Sales").strip() or "Sales"
        if sid:
            next_map[email] = {"spreadsheet_id": sid, "sheet_name": sname}
        else:
            next_map.pop(email, None)

    set_sales_submitter_sheets(config.admin_db_file, next_map)
    return next_map


def _save_calendar_sales_sheets_from_form(config: AppConfig, form) -> dict[str, dict[str, str]]:
    current = get_calendar_sales_sheets(config.admin_db_file)
    next_map = dict(current)
    prefix_id = "cal_sales_sheet_id__"
    prefix_name = "cal_sales_sheet_name__"

    candidate_cals: set[str] = set()
    for k in form.keys():
        if k.startswith(prefix_id):
            candidate_cals.add(k[len(prefix_id):].strip())
        elif k.startswith(prefix_name):
            candidate_cals.add(k[len(prefix_name):].strip())

    for cal_id in candidate_cals:
        raw_id = (form.get(f"{prefix_id}{cal_id}") or "").strip()
        sid = _extract_spreadsheet_id(raw_id)
        sname = (form.get(f"{prefix_name}{cal_id}") or "Sales").strip() or "Sales"
        if sid:
            next_map[cal_id] = {"spreadsheet_id": sid, "sheet_name": sname}
        else:
            next_map.pop(cal_id, None)

    set_calendar_sales_sheets(config.admin_db_file, next_map)
    return next_map


def _save_calendar_cash_sheets_from_form(config: AppConfig, form) -> dict[str, dict[str, str]]:
    current = get_calendar_cash_sheets(config.admin_db_file)
    next_map = dict(current)
    prefix_id = "cal_cash_sheet_id__"
    prefix_name = "cal_cash_sheet_name__"

    candidate_cals: set[str] = set()
    for k in form.keys():
        if k.startswith(prefix_id):
            candidate_cals.add(k[len(prefix_id):].strip())
        elif k.startswith(prefix_name):
            candidate_cals.add(k[len(prefix_name):].strip())

    for cal_id in candidate_cals:
        raw_id = (form.get(f"{prefix_id}{cal_id}") or "").strip()
        sid = _extract_spreadsheet_id(raw_id)
        sname = (form.get(f"{prefix_name}{cal_id}") or "Sheet1").strip() or "Sheet1"
        if sid:
            next_map[cal_id] = {"spreadsheet_id": sid, "sheet_name": sname}
        else:
            next_map.pop(cal_id, None)

    set_calendar_cash_sheets(config.admin_db_file, next_map)
    return next_map


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
    warning_message: str = "",
) -> str:
    """Render per-tenant cards with enable toggle and separate account mapping.

    tenant_accounts: list of {tenantId, tenantName, enabled, invoiceAccount,
                               paymentAccount, revenueAccounts, bankAccounts}
    """
    if not xero_ok and not tenant_accounts:
        return (
            '<div class="bg-white rounded-2xl shadow-sm border border-gray-200 p-6 opacity-50 mt-4">'
            '<h2 class="font-semibold text-gray-900 mb-1">Xero Organisations</h2>'
            '<p class="text-sm text-gray-400">Connect Xero first to configure organisations and account mapping.</p>'
            '</div>'
        )

    if not tenant_accounts:
        warning_html = (
            f'<p class="text-xs text-amber-700 bg-amber-50 border border-amber-200 rounded-lg px-3 py-2 mb-3">{escape(warning_message)}</p>'
            if warning_message
            else ""
        )
        return (
            '<div class="bg-white rounded-2xl shadow-sm border border-gray-200 p-6 mt-4">'
            '<h2 class="font-semibold text-gray-900 mb-1">Xero Organisations</h2>'
            f'{warning_html}'
            '<p class="text-sm text-gray-400">No organisations found — reconnect Xero to discover them.</p>'
            '</div>'
        )

    def _account_value(account: dict, *, allow_id_fallback: bool = False) -> str:
        code = str(account.get("Code") or "").strip()
        if code:
            return code
        if allow_id_fallback:
            aid = str(account.get("AccountID") or "").strip()
            if aid:
                return f"id:{aid}"
        return ""

    def _find_name(accounts, saved_value, *, allow_id_fallback: bool = False):
        for a in accounts:
            if _account_value(a, allow_id_fallback=allow_id_fallback) == str(saved_value or "").strip():
                return a.get("Name", saved_value)
        return None

    def _opts(accounts, saved, *, allow_id_fallback: bool = False):
        out = '<option value="">— select account —</option>'
        found_saved = False
        for a in sorted(accounts, key=lambda x: x.get("Name", "")):
            raw_value = _account_value(a, allow_id_fallback=allow_id_fallback)
            if not raw_value:
                continue
            code = escape(raw_value)
            name = escape(a.get("Name", ""))
            code_display = escape(str(a.get("Code") or "").strip() or "no-code")
            sel = ' selected' if raw_value == saved else ""
            if sel:
                found_saved = True
            out += f'<option value="{code}"{sel}>{name} ({code_display})</option>'
        if saved and not found_saved:
            out += f'<option value="{escape(saved)}" selected>Saved account ({escape(saved)})</option>'
        return out

    def _theme_opts(themes, saved, blank_label="— use Xero default —"):
        out = f'<option value="">{blank_label}</option>'
        for th in themes:
            tid_val = escape(th.get("BrandingThemeID", ""))
            tname_val = escape(th.get("Name", tid_val))
            sel = ' selected' if th.get("BrandingThemeID", "") == saved else ""
            out += f'<option value="{tid_val}"{sel}>{tname_val}</option>'
        return out

    def _saved_badge(accounts, saved_value, *, allow_id_fallback: bool = False):
        if not saved_value:
            return '<p class="text-xs text-gray-400 mt-1">No account saved yet.</p>'
        name = _find_name(accounts, saved_value, allow_id_fallback=allow_id_fallback)
        label = escape(f"{name} ({saved_value})") if name else escape(saved_value)
        return f'<p class="text-xs text-emerald-600 mt-1">&#10003; Saved: <span class="font-medium">{label}</span></p>'

    def _theme_saved_badge(themes, theme_id):
        if not theme_id:
            return '<p class="text-xs text-gray-400 mt-1">Using Xero default template.</p>'
        name = next((escape(th.get("Name", theme_id)) for th in themes if th.get("BrandingThemeID") == theme_id), escape(theme_id))
        return f'<p class="text-xs text-emerald-600 mt-1">&#10003; Saved: <span class="font-medium">{name}</span></p>'

    cards_html = ""
    for t in tenant_accounts:
        tid = escape(t["tenantId"])
        tname = escape(t.get("tenantName") or t["tenantId"])
        enabled = t.get("enabled", True)
        rev_accounts = t.get("revenueAccounts", [])
        bank_accounts = t.get("bankAccounts", [])
        themes = t.get("brandingThemes", [])
        saved_inv = t.get("invoiceAccount", "")
        saved_pay = t.get("paymentAccount", "")
        saved_theme = t.get("brandingThemeId", "")
        saved_premium_theme = t.get("premiumThemeId", "")
        saved_threshold = t.get("premiumThreshold")
        load_warning = str(t.get("loadWarning", "") or "").strip()
        threshold_val = f'{saved_threshold:g}' if saved_threshold is not None else ""
        missing_bits: list[str] = []
        if enabled and not saved_pay:
            missing_bits.append("Payment bank account")
        if enabled and not saved_theme:
            missing_bits.append("Invoice template")
        setup_notice = (
            '<div class="mb-3 text-xs text-amber-700 bg-amber-50 border border-amber-200 rounded-lg px-3 py-2">'
            f"Action needed: set {escape(', '.join(missing_bits))} before full automation can run."
            "</div>"
            if missing_bits
            else ""
        )
        load_warning_html = (
            '<div class="mb-3 text-xs text-amber-700 bg-amber-50 border border-amber-200 rounded-lg px-3 py-2">'
            f"{escape(load_warning)}</div>"
            if load_warning
            else ""
        )

        toggle_color = "bg-emerald-500" if enabled else "bg-gray-300"
        toggle_label = "Active" if enabled else "Paused"
        border_cls = "border-emerald-200" if enabled else "border-gray-200"

        rev_opts = _opts(rev_accounts, saved_inv)
        bank_opts = _opts(bank_accounts, saved_pay, allow_id_fallback=True)
        rev_badge = _saved_badge(rev_accounts, saved_inv)
        bank_badge = _saved_badge(bank_accounts, saved_pay, allow_id_fallback=True)

        _input_cls = "w-full text-sm border border-gray-200 rounded-lg px-3 py-2 focus:outline-none focus:ring-2 focus:ring-indigo-300"
        _manual_note = '<p class="text-xs text-amber-600 mt-1">Account list unavailable — enter the Xero account code directly (e.g. 200).</p>'
        if rev_accounts:
            rev_field = f'<select name="invoice_account_code" class="{_input_cls}">{rev_opts}</select>'
        else:
            rev_field = (
                f'<input type="text" name="invoice_account_code" value="{escape(saved_inv)}" '
                f'placeholder="Enter account code (e.g. 200)" class="{_input_cls}">'
                f'{_manual_note}'
            )
        if bank_accounts:
            bank_field = f'<select name="payment_account_code" class="{_input_cls}">{bank_opts}</select>'
        else:
            bank_field = (
                f'<input type="text" name="payment_account_code" value="{escape(saved_pay)}" '
                f'placeholder="Enter account code (e.g. 090)" class="{_input_cls}">'
                f'{_manual_note}'
            )
        theme_opts = _theme_opts(themes, saved_theme)
        theme_badge = _theme_saved_badge(themes, saved_theme)

        premium_theme_opts = _theme_opts(themes, saved_premium_theme, blank_label="— same as default —")
        premium_theme_badge = _theme_saved_badge(themes, saved_premium_theme)

        no_themes_msg = (
            '' if themes or load_warning else
            '<p class="text-xs text-amber-700 bg-amber-50 border border-amber-100 rounded-lg px-3 py-2 mt-1">'
            'No branding themes found — create one in Xero first (Settings → Invoice Settings → New Style).'
            '</p>'
        )
        advanced_open = ' open' if (saved_premium_theme or saved_threshold) else ''

        cards_html += f"""
        <details class="bg-white rounded-2xl shadow-sm border {border_cls} overflow-hidden group/org">
          <summary class="flex items-center justify-between px-5 py-4 cursor-pointer list-none select-none hover:bg-gray-50 transition-colors">
            <div class="flex items-center gap-3">
              <div class="w-2.5 h-2.5 rounded-full {"bg-emerald-400" if enabled else "bg-gray-300"} shrink-0 mt-0.5"></div>
              <div>
                <span class="font-semibold text-gray-900 text-sm">{tname}</span>
                <p class="text-xs {"text-emerald-600" if enabled else "text-gray-400"} mt-0.5">{"Active" if enabled else "Paused"}</p>
              </div>
            </div>
            <div class="flex items-center gap-2">
              <form method="post" action="/toggle-xero-tenant/{tid}" onclick="event.stopPropagation()">
                <button type="submit" title="Toggle {tname}"
                  class="flex items-center gap-1.5 px-2.5 py-1 text-xs font-medium rounded-lg border transition-colors
                         {'border-emerald-300 bg-emerald-50 text-emerald-700 hover:bg-emerald-100' if enabled else 'border-gray-300 bg-gray-50 text-gray-600 hover:bg-gray-100'}">
                  <span class="inline-flex w-7 h-4 rounded-full {toggle_color} relative transition-colors">
                    <span class="absolute top-0.5 left-0.5 w-3 h-3 bg-white rounded-full shadow transform transition-transform {"translate-x-3" if enabled else "translate-x-0"}"></span>
                  </span>
                  {toggle_label}
                </button>
              </form>
              <svg class="w-4 h-4 text-gray-400 transition-transform duration-200 group-open/org:rotate-180 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"/>
              </svg>
            </div>
          </summary>
          <div class="px-5 pb-5 pt-4 border-t border-gray-100">
            <p class="text-xs text-gray-400 font-mono mb-4">{tid}</p>
            {setup_notice}
            {load_warning_html}
            <form method="post" action="/save-xero-tenant/{tid}" class="space-y-4">

              <!-- Account mapping -->
              <div>
                <label class="block text-xs font-medium text-gray-700 mb-1">Invoice income account <span class="text-gray-400 font-normal">(revenue / sales)</span></label>
                {rev_field}
                {rev_badge}
              </div>
              <div>
                <label class="block text-xs font-medium text-gray-700 mb-1">Payment bank account <span class="text-red-500">*</span> <span class="text-gray-400 font-normal">(where payments land)</span></label>
                {bank_field}
                {bank_badge}
              </div>

              <!-- Invoice template -->
              <div class="pt-1 border-t border-gray-100">
                <label class="block text-xs font-medium text-gray-700 mb-1">Invoice template <span class="text-red-500">*</span> <span class="text-gray-400 font-normal">(branding theme)</span></label>
                <select name="branding_theme_id"
                  class="w-full text-sm border border-gray-200 rounded-lg px-3 py-2 focus:outline-none focus:ring-2 focus:ring-indigo-300 {"opacity-40" if not themes else ""}">
                  {theme_opts}
                </select>
                {theme_badge}
                {no_themes_msg}
              </div>

              <!-- Advanced -->
              <details class="border border-gray-100 rounded-xl overflow-hidden group/adv"{advanced_open}>
                <summary class="flex items-center justify-between px-4 py-3 bg-gray-50 cursor-pointer list-none select-none hover:bg-gray-100 transition-colors">
                  <div class="flex items-center gap-2">
                    <svg class="w-3.5 h-3.5 text-gray-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 6V4m0 2a2 2 0 100 4m0-4a2 2 0 110 4m-6 8a2 2 0 100-4m0 4a2 2 0 110-4m0 4v2m0-6V4m6 6v10m6-2a2 2 0 100-4m0 4a2 2 0 110-4m0 4v2m0-6V4"/>
                    </svg>
                    <span class="text-xs font-semibold text-gray-700">Advanced — premium template rules</span>
                  </div>
                  <svg class="w-3.5 h-3.5 text-gray-400 transition-transform duration-200 group-open/adv:rotate-180 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"/>
                  </svg>
                </summary>
                <div class="px-4 py-3 space-y-3">
                  <p class="text-xs text-gray-500">Use a different invoice template when the job total is above a set amount — handy for VIP or large-spend clients.</p>
                  <div>
                    <label class="block text-xs font-medium text-gray-700 mb-1">Premium template <span class="text-gray-400 font-normal">(used when spend is above threshold)</span></label>
                    <select name="premium_theme_id"
                      class="w-full text-sm border border-gray-200 rounded-lg px-3 py-2 focus:outline-none focus:ring-2 focus:ring-indigo-300 {"opacity-40" if not themes else ""}">
                      {premium_theme_opts}
                    </select>
                    {premium_theme_badge}
                  </div>
                  <div>
                    <label class="block text-xs font-medium text-gray-700 mb-1">Spend threshold <span class="text-gray-400 font-normal">(£ ex-VAT — premium template used at or above this amount)</span></label>
                    <div class="relative">
                      <span class="absolute left-3 top-1/2 -translate-y-1/2 text-sm text-gray-400 pointer-events-none">£</span>
                      <input type="number" name="premium_threshold" value="{threshold_val}" min="0" step="0.01"
                        placeholder="e.g. 500"
                        class="w-full pl-7 pr-3 py-2 text-sm border border-gray-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-indigo-300">
                    </div>
                    {f'<p class="text-xs text-emerald-600 mt-1">&#10003; Premium template used when job is £{threshold_val}+ ex-VAT</p>' if threshold_val else '<p class="text-xs text-gray-400 mt-1">No threshold set — premium template unused.</p>'}
                  </div>
                </div>
              </details>

              <button type="submit"
                class="px-4 py-2 bg-indigo-600 hover:bg-indigo-700 text-white text-sm font-medium rounded-lg transition-colors">
                Save
              </button>
            </form>
          </div>
        </details>"""

    warning_html = (
        f'<div class="text-xs text-amber-700 bg-amber-50 border border-amber-200 rounded-lg px-3 py-2">{escape(warning_message)}</div>'
        if warning_message
        else ""
    )

    return f"""
    <div class="space-y-4 mt-4">
      <div class="flex items-center justify-between">
        <h2 class="font-semibold text-gray-900">Xero Organisations</h2>
        <p class="text-xs text-gray-400">{len(tenant_accounts)} organisation{"s" if len(tenant_accounts) != 1 else ""} connected</p>
      </div>
      {warning_html}
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


def _save_admin_auth_file(config: AppConfig, username: str, password: str) -> None:
    path = Path(config.admin_auth_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "username": username.strip(),
        "password": password,
        "updated_at": int(time.time()),
    }
    path.write_text(json.dumps(payload, indent=2))


def _load_admin_auth(config: AppConfig) -> tuple[str, str]:
    """
    Read admin auth from persistent file first, env fallback second.
    """
    path = Path(config.admin_auth_file)
    if path.exists():
        try:
            raw = json.loads(path.read_text())
            username = str(raw.get("username", "")).strip()
            password = str(raw.get("password", ""))
            if username and password:
                return username, password
        except Exception:
            pass
    return config.admin_username, config.admin_password


def _bootstrap_admin_auth(config: AppConfig) -> None:
    """
    Ensure a persistent admin auth file exists.
    """
    username, password = _load_admin_auth(config)
    if not username or not password:
        return
    if not Path(config.admin_auth_file).exists():
        _save_admin_auth_file(config, username, password)


def create_app() -> Flask:
    config = load_config()
    _bootstrap_credentials(config)
    _bootstrap_admin_auth(config)
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
              <p class="text-center mt-4 text-xs text-gray-500">
                Can't sign in? <a href="/admin/reset-login" class="text-indigo-600 hover:text-indigo-700">Reset admin login</a>
              </p>
            </div>
          </div>
        </div>
        """)

    @app.post("/login")
    def login_post():
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        expected_user, expected_pass = _load_admin_auth(config)
        if username == expected_user and password == expected_pass:
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

    @app.get("/assistant")
    @require_login
    def assistant_page():
        cfg = _assistant_config(config.admin_db_file)
        openai_configured = bool((os.getenv("OPENAI_API_KEY") or "").strip())
        openai_model = (os.getenv("OPENAI_MODEL") or "gpt-5-mini").strip()
        history = session.get("assistant_chat_history") or []
        if not isinstance(history, list):
            history = []
        pending = session.get("assistant_pending_action") or {}
        pending_html = ""
        if isinstance(pending, dict) and pending.get("id") and pending.get("summary"):
            pending_html = f"""
              <div class="rounded-xl border border-amber-300 bg-amber-50 p-4">
                <p class="text-sm font-semibold text-amber-800">Pending write action</p>
                <p class="text-sm text-amber-900 mt-1">{escape(str(pending.get("summary") or ""))}</p>
                <form method="post" action="/assistant/confirm" class="mt-3 flex flex-wrap gap-2">
                  <input type="hidden" name="action_id" value="{escape(str(pending.get("id")))}">
                  <button name="decision" value="approve" class="px-3 py-1.5 text-sm rounded-lg bg-emerald-600 text-white hover:bg-emerald-700">Approve & Run</button>
                  <button name="decision" value="reject" class="px-3 py-1.5 text-sm rounded-lg bg-gray-200 text-gray-800 hover:bg-gray-300">Reject</button>
                </form>
              </div>
            """

        lines: list[str] = []
        for row in history[-20:]:
            role = "You" if row.get("role") == "user" else "Assistant"
            role_cls = "text-indigo-700" if role == "You" else "text-emerald-700"
            lines.append(
                f'<div class="py-2 border-b border-gray-100">'
                f'<p class="text-xs {role_cls} font-semibold">{role} · {escape(str(row.get("ts") or ""))}</p>'
                f'<p class="text-sm text-gray-800 whitespace-pre-wrap">{escape(str(row.get("text") or ""))}</p>'
                f"</div>"
            )
        chat_html = "".join(lines) or '<p class="text-sm text-gray-500">No messages yet.</p>'

        return _page(
            f"""
            <div class="max-w-5xl mx-auto py-6 px-4 sm:px-6">
              <div class="flex items-center justify-between mb-4">
                <h1 class="text-2xl font-bold text-gray-900">Assistant</h1>
                <div class="flex items-center gap-2">
                  <a href="/" class="px-3 py-1.5 text-sm rounded-lg border border-gray-300 text-gray-700 hover:bg-gray-50">Dashboard</a>
                  <a href="/settings" class="px-3 py-1.5 text-sm rounded-lg border border-gray-300 text-gray-700 hover:bg-gray-50">Settings</a>
                </div>
              </div>

              <div class="rounded-xl border border-gray-200 bg-white p-4 mb-4">
                <p class="text-sm text-gray-700 mb-3">Write mode controls whether the assistant can execute changes after your approval.</p>
                <form method="post" action="/assistant/config" class="flex flex-wrap items-center gap-4">
                  <label class="inline-flex items-center gap-2 text-sm text-gray-800">
                    <input type="checkbox" name="write_enabled" value="1" {"checked" if cfg.get("write_enabled") else ""}>
                    Enable write actions
                  </label>
                  <span class="text-xs text-gray-500">Always confirm is enforced.</span>
                  <button class="px-3 py-1.5 text-sm rounded-lg bg-indigo-600 text-white hover:bg-indigo-700">Save</button>
                </form>
                <div class="mt-3 text-xs text-gray-600">
                  <p>OpenAI: <span class="font-semibold {"text-emerald-700" if openai_configured else "text-amber-700"}">{"Connected" if openai_configured else "Not configured"}</span> · model <code>{escape(openai_model)}</code></p>
                  <p class="mt-1">API keys: <a class="text-indigo-600 hover:underline" href="https://platform.openai.com/api-keys" target="_blank" rel="noopener">platform.openai.com/api-keys</a></p>
                </div>
              </div>

              {pending_html}

              <div class="grid grid-cols-1 lg:grid-cols-2 gap-4 mt-4">
                <div class="rounded-xl border border-gray-200 bg-white p-4">
                  <h2 class="text-sm font-semibold text-gray-900 mb-2">Ask Assistant</h2>
                  <form method="post" action="/assistant/ask">
                    <textarea name="prompt" rows="7" class="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm" placeholder="Ask a calendar/Xero question..."></textarea>
                    <button class="mt-3 px-3 py-1.5 text-sm rounded-lg bg-gray-900 text-white hover:bg-black">Send</button>
                  </form>
                  <p class="text-xs text-gray-500 mt-3">Examples: "what new entries were created today", "who booked W5 D.S John O'Discoll", "delete orphan drafts".</p>
                </div>
                <div class="rounded-xl border border-gray-200 bg-white p-4 max-h-[480px] overflow-y-auto">
                  <h2 class="text-sm font-semibold text-gray-900 mb-2">Conversation</h2>
                  {chat_html}
                </div>
              </div>
            </div>
            """
        )

    @app.post("/assistant/config")
    @require_login
    def assistant_config_save():
        write_enabled = (request.form.get("write_enabled") or "").strip() in {"1", "true", "on", "yes", "y"}
        _set_assistant_config(config.admin_db_file, write_enabled=write_enabled)
        _append_assistant_chat("assistant", f"Write mode {'enabled' if write_enabled else 'disabled'}.")
        return redirect(url_for("assistant_page"))

    @app.post("/assistant/ask")
    @require_login
    def assistant_ask():
        prompt = (request.form.get("prompt") or "").strip()
        if not prompt:
            return redirect(url_for("assistant_page"))
        _append_assistant_chat("user", prompt)
        cfg = _assistant_config(config.admin_db_file)
        low = prompt.lower()

        # Read-only: list today's new entries.
        if (
            ("new" in low and "entr" in low and "today" in low)
            or ("created today" in low and "calendar" in low)
        ):
            titles = _list_today_created_event_titles(config)
            if not titles:
                _append_assistant_chat("assistant", "No new entries found today.")
            else:
                _append_assistant_chat("assistant", "New entries today:\n- " + "\n- ".join(titles))
            return redirect(url_for("assistant_page"))

        # Read-only: who booked <title>.
        if "who booked" in low:
            phrase = re.sub(r"^.*who booked\s*", "", prompt, flags=re.I).strip(" ?\"'")
            hits = _find_events_by_title(config, phrase)
            if not hits:
                _append_assistant_chat("assistant", f"No matching event found for: {phrase}")
                return redirect(url_for("assistant_page"))
            ev = hits[0]["event"]
            created = ev.get("created") or ""
            creator = ((ev.get("creator") or {}).get("email") or "").strip() or "unknown"
            summary = str(ev.get("summary") or "(no title)")
            _append_assistant_chat(
                "assistant",
                f'Booked by: {creator}\nEvent: {summary}\nCreated: {created}',
            )
            return redirect(url_for("assistant_page"))

        # Write intent: remove orphan drafts.
        if ("delete" in low or "remove" in low) and "draft" in low and "xero" in low:
            if not cfg.get("write_enabled"):
                _append_assistant_chat("assistant", "Write mode is OFF. Enable write actions first, then ask again.")
                return redirect(url_for("assistant_page"))
            state = load_state(config.state_file)
            mapped = {str(v).strip() for v in (state.get("event_invoice_map") or {}).values() if str(v).strip()}
            drafts = _xero_fetch_draft_invoices(config)
            orphan = [d for d in drafts if str(d.get("InvoiceID") or "").strip() and str(d.get("InvoiceID") or "").strip() not in mapped]
            preview = [str(d.get("InvoiceNumber") or d.get("InvoiceID") or "") for d in orphan[:20]]
            action_id = str(uuid.uuid4())
            session["assistant_pending_action"] = {
                "id": action_id,
                "type": "delete_orphan_drafts",
                "invoice_ids": [str(d.get("InvoiceID")) for d in orphan],
                "summary": f"Delete {len(orphan)} orphan draft invoice(s): {', '.join(preview) if preview else 'none'}",
            }
            _append_assistant_chat(
                "assistant",
                f"Ready to run: delete {len(orphan)} orphan draft invoice(s). Please approve below.",
            )
            return redirect(url_for("assistant_page"))

        # Write intent: void app-tracked invoices by contact name fragment.
        if "void" in low and ("invoice" in low or "payment" in low):
            if not cfg.get("write_enabled"):
                _append_assistant_chat("assistant", "Write mode is OFF. Enable write actions first, then ask again.")
                return redirect(url_for("assistant_page"))
            m = re.search(r"under\s+(.+)$", prompt, flags=re.I)
            if not m:
                _append_assistant_chat("assistant", "Please include a name fragment, e.g. 'void invoices under Test Name'.")
                return redirect(url_for("assistant_page"))
            needle = m.group(1).strip(" .\"'")
            xc = build_xero_client(config)
            if not xc:
                _append_assistant_chat("assistant", "Xero client is not connected.")
                return redirect(url_for("assistant_page"))
            state = load_state(config.state_file)
            invoice_ids = sorted({str(v).strip() for v in (state.get("event_invoice_map") or {}).values() if str(v).strip()})
            matches: list[dict] = []
            for inv_id in invoice_ids[:500]:
                try:
                    inv = xc.get_invoice(inv_id)
                except Exception:
                    continue
                cname = str(((inv.get("Contact") or {}).get("Name") or "")).strip()
                if needle.lower() in cname.lower():
                    status = str(inv.get("Status") or "").upper()
                    if status not in {"VOIDED", "DELETED"}:
                        matches.append(inv)
            action_id = str(uuid.uuid4())
            preview = [str(inv.get("InvoiceNumber") or inv.get("InvoiceID") or "") for inv in matches[:20]]
            session["assistant_pending_action"] = {
                "id": action_id,
                "type": "void_invoices_by_name",
                "invoice_ids": [str(inv.get("InvoiceID")) for inv in matches],
                "summary": f'Void {len(matches)} invoice(s) for name containing "{needle}": {", ".join(preview) if preview else "none"}',
            }
            _append_assistant_chat(
                "assistant",
                f'Ready to run: void {len(matches)} invoice(s) for "{needle}". Please approve below.',
            )
            return redirect(url_for("assistant_page"))

        ai_reply, ai_err = _openai_assistant_reply(prompt)
        if ai_reply:
            _append_assistant_chat("assistant", ai_reply)
            return redirect(url_for("assistant_page"))
        if ai_err == "missing_key":
            _append_assistant_chat(
                "assistant",
                "OpenAI API key is not configured. Add `OPENAI_API_KEY` to Fly secrets. "
                "Key page: https://platform.openai.com/api-keys",
            )
            return redirect(url_for("assistant_page"))

        _append_assistant_chat(
            "assistant",
            "I couldn't reach OpenAI right now. Try again shortly.",
        )
        return redirect(url_for("assistant_page"))

    @app.post("/assistant/confirm")
    @require_login
    def assistant_confirm():
        decision = (request.form.get("decision") or "").strip().lower()
        action_id = (request.form.get("action_id") or "").strip()
        pending = session.get("assistant_pending_action") or {}
        if not pending or pending.get("id") != action_id:
            _append_assistant_chat("assistant", "No matching pending action found.")
            return redirect(url_for("assistant_page"))
        if decision != "approve":
            session.pop("assistant_pending_action", None)
            _append_assistant_chat("assistant", "Pending action cancelled.")
            return redirect(url_for("assistant_page"))

        cfg = _assistant_config(config.admin_db_file)
        if not cfg.get("write_enabled"):
            _append_assistant_chat("assistant", "Write mode is OFF; action not executed.")
            session.pop("assistant_pending_action", None)
            return redirect(url_for("assistant_page"))

        xc = build_xero_client(config)
        if not xc:
            _append_assistant_chat("assistant", "Xero not connected; action not executed.")
            session.pop("assistant_pending_action", None)
            return redirect(url_for("assistant_page"))

        invoice_ids = [str(v).strip() for v in (pending.get("invoice_ids") or []) if str(v).strip()]
        max_ops = 50
        invoice_ids = invoice_ids[:max_ops]
        done = 0
        failed = 0
        for inv_id in invoice_ids:
            try:
                xc.delete_draft_invoice(inv_id)
                done += 1
            except Exception:
                failed += 1
        session.pop("assistant_pending_action", None)
        _append_assistant_chat(
            "assistant",
            f"Write action completed. Success: {done}, Failed: {failed}, Attempted: {len(invoice_ids)}.",
        )
        return redirect(url_for("assistant_page"))

    @app.get("/admin/reset-login")
    def reset_login_page():
        token_set = bool((config.admin_reset_token or "").strip())
        hint = (
            ""
            if token_set
            else '<p class="text-sm text-amber-700 bg-amber-50 border border-amber-200 rounded-lg px-3 py-2 mb-4">ADMIN_RESET_TOKEN is not set in environment/secrets yet.</p>'
        )
        return _page(
            f"""
        <div class="min-h-screen flex items-center justify-center bg-gradient-to-br from-indigo-50 to-blue-100 px-4">
          <div class="w-full max-w-md">
            <div class="bg-white rounded-2xl shadow-xl p-8">
              <h1 class="text-2xl font-bold text-gray-900 mb-2">Reset Admin Login</h1>
              <p class="text-gray-500 text-sm mb-6">Use your reset token to replace admin username/password stored on volume.</p>
              {hint}
              <form method="post" class="space-y-4">
                <div>
                  <label class="block text-sm font-medium text-gray-700 mb-1.5">Reset token</label>
                  <input name="reset_token" type="password" class="w-full px-4 py-2.5 border border-gray-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500" placeholder="ADMIN_RESET_TOKEN">
                </div>
                <div>
                  <label class="block text-sm font-medium text-gray-700 mb-1.5">New username</label>
                  <input name="new_username" class="w-full px-4 py-2.5 border border-gray-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500" placeholder="admin@example.com">
                </div>
                <div>
                  <label class="block text-sm font-medium text-gray-700 mb-1.5">New password</label>
                  <input name="new_password" type="password" class="w-full px-4 py-2.5 border border-gray-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500" placeholder="New password">
                </div>
                <div>
                  <label class="block text-sm font-medium text-gray-700 mb-1.5">Confirm password</label>
                  <input name="confirm_password" type="password" class="w-full px-4 py-2.5 border border-gray-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500" placeholder="Repeat password">
                </div>
                <button type="submit" class="w-full py-2.5 px-4 bg-indigo-600 hover:bg-indigo-700 text-white font-medium rounded-lg text-sm transition-colors">Reset Login</button>
              </form>
              <a href="/login" class="block text-center mt-4 text-sm text-gray-500 hover:text-gray-700">Back to login</a>
            </div>
          </div>
        </div>
        """
        )

    @app.post("/admin/reset-login")
    def reset_login_post():
        configured_token = (config.admin_reset_token or "").strip()
        provided_token = (request.form.get("reset_token") or "").strip()
        new_username = (request.form.get("new_username") or "").strip()
        new_password = request.form.get("new_password") or ""
        confirm_password = request.form.get("confirm_password") or ""

        if not configured_token:
            return _page(
                """
            <div class="min-h-screen flex items-center justify-center bg-gradient-to-br from-indigo-50 to-blue-100 px-4">
              <div class="bg-white rounded-2xl shadow-xl p-8 w-full max-w-md">
                <h1 class="text-2xl font-bold text-red-700 mb-2">Reset Unavailable</h1>
                <p class="text-sm text-gray-600">ADMIN_RESET_TOKEN is not configured on this deployment.</p>
                <a href="/login" class="inline-block mt-4 px-4 py-2 bg-indigo-600 hover:bg-indigo-700 text-white rounded-lg text-sm">Back to login</a>
              </div>
            </div>
            """
            ), 400

        if not provided_token or not secrets.compare_digest(provided_token, configured_token):
            return _page(
                """
            <div class="min-h-screen flex items-center justify-center bg-gradient-to-br from-indigo-50 to-blue-100 px-4">
              <div class="bg-white rounded-2xl shadow-xl p-8 w-full max-w-md">
                <h1 class="text-2xl font-bold text-red-700 mb-2">Invalid Reset Token</h1>
                <p class="text-sm text-gray-600">The reset token is invalid.</p>
                <a href="/admin/reset-login" class="inline-block mt-4 px-4 py-2 bg-indigo-600 hover:bg-indigo-700 text-white rounded-lg text-sm">Try again</a>
              </div>
            </div>
            """
            ), 401

        if not new_username or not new_password:
            return _page(
                """
            <div class="min-h-screen flex items-center justify-center bg-gradient-to-br from-indigo-50 to-blue-100 px-4">
              <div class="bg-white rounded-2xl shadow-xl p-8 w-full max-w-md">
                <h1 class="text-2xl font-bold text-red-700 mb-2">Missing Fields</h1>
                <p class="text-sm text-gray-600">Username and password are required.</p>
                <a href="/admin/reset-login" class="inline-block mt-4 px-4 py-2 bg-indigo-600 hover:bg-indigo-700 text-white rounded-lg text-sm">Try again</a>
              </div>
            </div>
            """
            ), 400

        if new_password != confirm_password:
            return _page(
                """
            <div class="min-h-screen flex items-center justify-center bg-gradient-to-br from-indigo-50 to-blue-100 px-4">
              <div class="bg-white rounded-2xl shadow-xl p-8 w-full max-w-md">
                <h1 class="text-2xl font-bold text-red-700 mb-2">Password Mismatch</h1>
                <p class="text-sm text-gray-600">New password and confirmation do not match.</p>
                <a href="/admin/reset-login" class="inline-block mt-4 px-4 py-2 bg-indigo-600 hover:bg-indigo-700 text-white rounded-lg text-sm">Try again</a>
              </div>
            </div>
            """
            ), 400

        _save_admin_auth_file(config, new_username, new_password)
        session.clear()
        return _page(
            """
        <div class="min-h-screen flex items-center justify-center bg-gradient-to-br from-indigo-50 to-blue-100 px-4">
          <div class="bg-white rounded-2xl shadow-xl p-8 w-full max-w-md">
            <h1 class="text-2xl font-bold text-emerald-700 mb-2">Login Reset Complete</h1>
            <p class="text-sm text-gray-600">Admin credentials were saved to persistent storage.</p>
            <a href="/login" class="inline-block mt-4 px-4 py-2 bg-indigo-600 hover:bg-indigo-700 text-white rounded-lg text-sm">Go to login</a>
          </div>
        </div>
        """
        )

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
        branding_theme_id = (request.form.get("branding_theme_id") or "").strip()
        premium_theme_id = (request.form.get("premium_theme_id") or "").strip()
        premium_threshold_raw = (request.form.get("premium_threshold") or "").strip()
        try:
            premium_threshold = float(premium_threshold_raw) if premium_threshold_raw else None
        except ValueError:
            premium_threshold = None
        upsert_xero_tenant(
            config.admin_db_file,
            tenant_id,
            invoice_account=invoice_code,
            payment_account=payment_code,
            branding_theme_id=branding_theme_id,
            premium_theme_id=premium_theme_id if premium_theme_id else None,
            premium_threshold=premium_threshold,
        )
        if not payment_code:
            session["save_notice"] = (
                "error:Saved, but Payment bank account is missing. "
                "Card payments will fail until it is set."
            )
        elif not branding_theme_id:
            session["save_notice"] = (
                "success:Saved. Invoice template not set; Xero default template will be used."
            )
        else:
            session["save_notice"] = "success:Account mapping and invoice template saved."
        return redirect(url_for("index"))

    @app.post("/toggle-xero-tenant/<tenant_id>")
    @require_login
    def toggle_xero_tenant(tenant_id: str):
        tenants = get_xero_tenants(config.admin_db_file)
        current = next((t for t in tenants if t["tenantId"] == tenant_id), {})
        if not current:
            session["save_notice"] = "error:Organisation not found."
            return redirect(url_for("index"))

        # Single-active-tenant model: selecting one tenant enables it and pauses others.
        if current.get("enabled", True):
            session["save_notice"] = "success:Organisation already active."
            return redirect(url_for("index"))

        for t in tenants:
            t["enabled"] = (t.get("tenantId") == tenant_id)
        set_xero_tenants(config.admin_db_file, tenants)

        # Keep token tenant in sync with selected active tenant.
        try:
            tok = load_xero_token(config.xero_token_file)
            if tok:
                tok["tenant_id"] = tenant_id
                save_xero_token(config.xero_token_file, tok)
        except Exception:
            pass

        session["save_notice"] = "success:Active organisation switched."
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
        choose_orgs = (request.args.get("choose_orgs") or "").strip().lower() in {"1", "true", "yes", "y"}
        xero_auth_url = _xero_authorization_url(
            config,
            state,
            redirect_uri=dynamic_xero_redirect,
            force_consent=choose_orgs,
        )
        session["xero_oauth_state"] = state
        session["xero_auth_url"] = xero_auth_url
        session["xero_redirect_uri"] = dynamic_xero_redirect
        # Persist to DB so the callback works even when opened in a separate browser tab
        set_json_setting(config.admin_db_file, "xero_oauth_pending_state", state)
        set_json_setting(config.admin_db_file, "xero_oauth_pending_redirect_uri", dynamic_xero_redirect)
        wants_json = (
            (request.args.get("mode") or "").strip().lower() == "json"
            or request.headers.get("X-Requested-With", "") == "XMLHttpRequest"
            or "application/json" in (request.headers.get("Accept", "") or "")
        )
        if wants_json:
            import flask as _flask
            return _flask.jsonify({"url": xero_auth_url})
        session["save_notice"] = "success:Xero authorisation link generated below."
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
                or str(get_json_setting(config.admin_db_file, "xero_oauth_pending_redirect_uri", "")).strip()
                or _current_base_url() + "/xero/callback"
            )
            token = _exchange_xero_code(config, code, redirect_uri=dynamic_xero_redirect)
            all_conns = _get_all_xero_connections(token.get("access_token", ""))
            if not all_conns:
                raise RuntimeError("No Xero organisation connections returned.")
        except Exception as exc:
            return _page(f"""
            <div class="min-h-screen flex items-center justify-center bg-gray-50">
              <div class="bg-white rounded-2xl shadow p-8 max-w-md w-full text-center">
                <p class="text-red-600 font-medium mb-4">Xero connect failed: {escape(str(exc))}</p>
                <a href="/" class="text-indigo-600 hover:underline text-sm">Back to dashboard</a>
              </div>
            </div>
            """), 400

        existing_tenants = get_xero_tenants(config.admin_db_file)
        existing_by_id = {t.get("tenantId"): t for t in existing_tenants if t.get("tenantId")}
        existing_active = next(
            (t.get("tenantId") for t in existing_tenants if t.get("enabled", True) and t.get("tenantId")),
            "",
        )
        previous_token = str(load_xero_token(config.xero_token_file).get("tenant_id", "") or "").strip()
        conn_ids = {c["tenantId"] for c in all_conns}
        tenant_id = (
            existing_active
            if existing_active in conn_ids
            else (previous_token if previous_token in conn_ids else all_conns[0]["tenantId"])
        )

        rebuilt_tenants: list[dict] = []
        for conn in all_conns:
            tid = conn["tenantId"]
            saved = existing_by_id.get(tid, {})
            rebuilt_tenants.append(
                {
                    "tenantId": tid,
                    "tenantName": conn.get("tenantName", tid),
                    "enabled": tid == tenant_id,
                    "invoiceAccount": saved.get("invoiceAccount", ""),
                    "paymentAccount": saved.get("paymentAccount", ""),
                    "brandingThemeId": saved.get("brandingThemeId", ""),
                    "premiumThemeId": saved.get("premiumThemeId", ""),
                    "premiumThreshold": saved.get("premiumThreshold"),
                }
            )
        set_xero_tenants(config.admin_db_file, rebuilt_tenants)

        token["tenant_id"] = tenant_id
        save_xero_token(config.xero_token_file, token)
        session["logged_in"] = True
        session.pop("xero_oauth_state", None)
        session.pop("xero_auth_url", None)
        session.pop("xero_redirect_uri", None)
        set_json_setting(config.admin_db_file, "xero_oauth_pending_state", "")
        set_json_setting(config.admin_db_file, "xero_oauth_pending_redirect_uri", "")
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
        xero_lockout_until_ts = float(state.get("xero_lockout_until_ts") or 0.0)
        xero_lockout_active = xero_lockout_until_ts > time.time()
        xero_lockout_reason = str(state.get("xero_lockout_reason") or "").strip()
        xero_lockout_banner = ""
        if xero_lockout_active:
            _mins = int(max(1, (xero_lockout_until_ts - time.time()) // 60))
            _until_local = dt.datetime.fromtimestamp(
                xero_lockout_until_ts, tz=dt.timezone.utc
            ).astimezone().strftime("%d %b %Y %H:%M")
            _reason_txt = xero_lockout_reason or "Xero is temporarily rate-limited."
            xero_lockout_banner = (
                '<div class="mx-6 mt-4 flex items-start gap-3 px-4 py-3 rounded-lg border '
                'border-red-700/50 bg-red-950/40 text-red-200 text-sm">'
                '<span class="shrink-0 mt-0.5">🔒</span>'
                f'<span><strong>Xero lockout active.</strong> {_reason_txt} '
                f'No Xero requests will be sent until unlock. '
                f'Estimated unlock: {_until_local} '
                f'(<span id="xero-lockout-countdown" data-until="{int(xero_lockout_until_ts)}">{_mins}m</span>).</span>'
                '</div>'
            )

        # Build home-page warnings
        _dash_warnings: list[str] = []
        if not google_ok:
            _dash_warnings.append(
                "Google not connected \u2014 calendar events cannot be read and webhooks cannot be "
                "registered. Go to Settings to reconnect."
            )
        if not xero_ok:
            _dash_warnings.append(
                "Xero not connected \u2014 invoices cannot be created. Go to Settings to reconnect."
            )
        _now_ms = int(time.time() * 1000)
        _active_cals = get_active_calendars(config.admin_db_file, config.google_calendar_id)
        for _cal_id in _active_cals:
            _winfo = watches.get(_cal_id) or {}
            _exp_ms = int(_winfo.get("expiration_ms") or 0)
            if not _winfo or not _winfo.get("channel_id"):
                _dash_warnings.append(
                    f"No webhook registered for calendar {_cal_id} \u2014 events will only be "
                    "caught by the polling fallback (check Settings \u2192 Calendars)."
                )
            elif _exp_ms and _exp_ms < _now_ms:
                _dash_warnings.append(
                    f"Google webhook expired for {_cal_id} \u2014 automatic renewal will be "
                    "attempted on the next poll cycle."
                )

        def _warning_banner(msg: str) -> str:
            return (
                f'<div class="flex items-start gap-3 px-4 py-3 rounded-lg border '
                f'border-amber-600/40 bg-amber-950/40 text-amber-300 text-sm">'
                f'<span class="shrink-0 text-amber-400 mt-0.5">\u26a0</span>'
                f'<span>{escape(msg)}</span>'
                f'</div>'
            )

        warnings_html = (
            '<div class="mx-6 mt-4 flex flex-col gap-2">'
            + "".join(_warning_banner(w) for w in _dash_warnings)
            + "</div>"
            if _dash_warnings
            else ""
        )

        # Keep dashboard rendering snappy even when the ring buffer is large.
        recent_logs = _feed.recent(30)
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
      <!-- On/Off Toggle Switch -->
      <label for="toggle-switch" class="flex items-center gap-2 px-2 py-1 rounded-lg border border-neutral-700 bg-neutral-800/70">
        <span id="toggle-label" class="text-xs font-semibold {'text-emerald-300' if enabled else 'text-neutral-400'}">{toggle_label}</span>
        <input id="toggle-switch" type="checkbox" class="sr-only" {'checked' if enabled else ''} onchange="toggleEnabled(this.checked)">
        <span id="toggle-track" class="relative inline-flex h-5 w-9 items-center rounded-full transition-colors {'bg-emerald-500' if enabled else 'bg-neutral-600'}">
          <span id="toggle-knob" class="inline-block h-4 w-4 transform rounded-full bg-white transition-transform {'translate-x-4' if enabled else 'translate-x-1'}"></span>
        </span>
      </label>
      <div class="w-px h-5 bg-neutral-700 mx-1"></div>
      <a href="/settings" class="px-3 py-1.5 text-xs font-medium text-neutral-300 hover:text-white bg-neutral-800 hover:bg-neutral-700 rounded-lg border border-neutral-700 transition-colors">
        Settings
      </a>
      <a href="/assistant" class="px-3 py-1.5 text-xs font-medium text-neutral-300 hover:text-white bg-neutral-800 hover:bg-neutral-700 rounded-lg border border-neutral-700 transition-colors">
        Assistant
      </a>
      <a href="/logout" class="px-3 py-1.5 text-xs font-medium text-neutral-400 hover:text-neutral-300 transition-colors">
        Sign out
      </a>
    </div>
  </header>

  {warnings_html}
  {xero_lockout_banner}

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
const MAX_TERM_LINES = 30;
let autoScroll = true;

function _fmtRemaining(seconds) {{
  const s = Math.max(0, Math.floor(seconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (h > 0) return `${{h}}h ${{m}}m`;
  return `${{m}}m`;
}}

function updateXeroLockoutCountdowns() {{
  const ids = ['xero-lockout-countdown'];
  let hasActive = false;
  ids.forEach((id) => {{
    const el = document.getElementById(id);
    if (!el) return;
    const until = parseInt(el.dataset.until || '0', 10);
    if (!until) return;
    const now = Math.floor(Date.now() / 1000);
    const remaining = until - now;
    if (remaining > 0) {{
      hasActive = true;
      el.textContent = _fmtRemaining(remaining);
    }} else {{
      el.textContent = '0m';
    }}
  }});
}}
updateXeroLockoutCountdowns();
setInterval(updateXeroLockoutCountdowns, 1000);

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
  // Keep auto-scroll sticky unless user explicitly toggles pause.
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
  while (term.children.length > MAX_TERM_LINES) {{
    term.removeChild(term.firstElementChild);
  }}
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
const toggleSwitch = document.getElementById('toggle-switch');
const toggleTrack = document.getElementById('toggle-track');
const toggleKnob = document.getElementById('toggle-knob');
const toggleLbl = document.getElementById('toggle-label');

function applyToggleState(on) {{
  _enabled = on;
  toggleSwitch.checked = !!on;
  toggleLbl.textContent = on ? 'Running' : 'Paused';
  if (on) {{
    toggleLbl.className = 'text-xs font-semibold text-emerald-300';
    toggleTrack.className = 'relative inline-flex h-5 w-9 items-center rounded-full transition-colors bg-emerald-500';
    toggleKnob.className = 'inline-block h-4 w-4 transform rounded-full bg-white transition-transform translate-x-4';
  }} else {{
    toggleLbl.className = 'text-xs font-semibold text-neutral-400';
    toggleTrack.className = 'relative inline-flex h-5 w-9 items-center rounded-full transition-colors bg-neutral-600';
    toggleKnob.className = 'inline-block h-4 w-4 transform rounded-full bg-white transition-transform translate-x-1';
  }}
}}

function toggleEnabled(requested) {{
  toggleSwitch.disabled = true;
  const previous = _enabled;
  applyToggleState(!!requested);
  fetch('/toggle-enabled', {{method: 'POST'}})
    .then(r => {{
      if (!r.ok) throw new Error('toggle failed');
      return r.json();
    }})
    .then(d => {{ applyToggleState(!!d.enabled); toggleSwitch.disabled = false; }})
    .catch(() => {{
      applyToggleState(previous);
      toggleSwitch.disabled = false;
      alert('Toggle failed. Please refresh and try again.');
    }});
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
        new_state = not current
        set_enabled(config.admin_db_file, new_state)
        # Wake worker immediately so pause/resume takes effect now, not after
        # the next poll timeout.
        trigger_poll()
        if new_state:
            _feed.push("System resumed from Live View toggle", "system")
        else:
            _feed.push("System paused from Live View toggle", "system")
        import flask as _flask
        return _flask.jsonify({"enabled": new_state})

    @app.get("/settings")
    @require_login
    def index():
        # Persist the currently used public base URL so the worker can always
        # register/renew Google watches using the same domain.
        current_base = _current_base_url()
        stored_base = str(get_json_setting(config.admin_db_file, "public_base_url", "") or "").strip()
        if current_base and current_base != stored_base:
            set_json_setting(config.admin_db_file, "public_base_url", current_base)
            trigger_watch_check()
            trigger_poll()

        creds = load_admin_credentials(config)
        active = set(get_active_calendars(config.admin_db_file, config.google_calendar_id))
        stats_selected = set(get_stats_fields(config.admin_db_file))
        target = get_sheet_target(config.admin_db_file)
        sales_target = get_sales_sheet_target(config.admin_db_file)
        sales_stats_selected = set(get_sales_stats_fields(config.admin_db_file))
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
        cash_submitter_sheets = get_cash_submitter_sheets(config.admin_db_file)
        cash_backlog = get_cash_backlog(config.admin_db_file)
        sales_submitter_sheets = get_sales_submitter_sheets(config.admin_db_file)
        sales_backlog = get_sales_backlog(config.admin_db_file)
        calendar_sales_sheets = get_calendar_sales_sheets(config.admin_db_file)
        calendar_cash_sheets = get_calendar_cash_sheets(config.admin_db_file)
        client_id = _oauth_client_id(config)
        creds_file_exists = Path(config.google_credentials_file).exists()
        stored_xero_id, stored_xero_secret = _get_xero_creds(config)
        xero_has_creds = bool(stored_xero_id and stored_xero_secret)
        # Read once and clear (like save_notice) — shows after Connect click, gone on next refresh
        xero_pending_auth_url = session.pop("xero_auth_url", None) or "" if not xero_ok else ""

        # Fetch Xero accounts per tenant for account-mapping UI
        xero_tenant_account_data: list[dict] = []
        xero_tenant_warning = ""
        _xero_tok_raw = load_xero_token(config.xero_token_file)
        if xero_ok or _xero_tok_raw:
            try:
                _tok = load_xero_token(config.xero_token_file)
                _granted_scopes = _parse_scope_value(_tok.get("scope"))
                _missing_scopes = [s for s in REQUIRED_XERO_SCOPES if s not in _granted_scopes]
                if _missing_scopes:
                    _miss = " ".join(_missing_scopes)
                    _warn_scopes = (
                        f"Xero token missing scopes ({_miss}). "
                        "Click Reconnect Xero so account/theme lists and card payments can work."
                    )
                    xero_tenant_warning = (
                        f"{xero_tenant_warning} | {_warn_scopes}" if xero_tenant_warning else _warn_scopes
                    )
                if token_is_expired(_tok) and _tok.get("refresh_token"):
                    _cid, _csec = _get_xero_creds(config)
                    if _cid and _csec:
                        _refreshed = refresh_xero_token(_cid, _csec, _tok["refresh_token"])
                        _tok = {**_tok, **_refreshed}
                        save_xero_token(config.xero_token_file, _tok)
                _at = _tok.get("access_token", "")
                _all_conns = _get_all_xero_connections(_at)
                _acct_load_warnings: list[str] = []
                _saved_tenants = {t["tenantId"]: t for t in get_xero_tenants(config.admin_db_file)}
                for _conn in _all_conns:
                    _tid = _conn["tenantId"]
                    _tname = _conn.get("tenantName", _tid)
                    _cfg = _saved_tenants.get(_tid, {})
                    _enabled = _cfg.get("enabled", _tid == xero_tenant)
                    try:
                        # Keep settings snappy: fetch live options for the active tenant,
                        # use cached options only for inactive tenants.
                        if _enabled:
                            _rev, _bank, _themes, _load_warn = _get_tenant_acct_themes(_at, _tid)
                        else:
                            _rev, _bank, _themes, _load_warn = _get_tenant_cached_only(_tid)
                            if not (_rev or _bank or _themes):
                                _load_warn = (
                                    "Options load for the active organisation to keep settings fast. "
                                    "Set this organisation Active to load its account/theme lists."
                                )
                    except Exception:
                        _rev, _bank, _themes, _load_warn = [], [], [], "Cannot load Xero account/theme options."
                    if _load_warn:
                        _acct_load_warnings.append(f"{_tname}: {_load_warn}")
                    xero_tenant_account_data.append({
                        "tenantId": _tid,
                        "tenantName": _tname,
                        "enabled": _enabled,
                        "invoiceAccount": _cfg.get("invoiceAccount", ""),
                        "paymentAccount": _cfg.get("paymentAccount", ""),
                        "revenueAccounts": _rev,
                        "bankAccounts": _bank,
                        "brandingThemes": _themes,
                        "loadWarning": _load_warn,
                        "brandingThemeId": _cfg.get("brandingThemeId", ""),
                        "premiumThemeId": _cfg.get("premiumThemeId", ""),
                        "premiumThreshold": _cfg.get("premiumThreshold"),
                    })
                if _acct_load_warnings:
                    _warn = " | ".join(_acct_load_warnings[:3])
                    if len(_acct_load_warnings) > 3:
                        _warn += f" | +{len(_acct_load_warnings)-3} more"
                    xero_tenant_warning = (
                        f"{xero_tenant_warning} | {_warn}" if xero_tenant_warning else _warn
                    )
            except Exception as exc:
                _saved_only = get_xero_tenants(config.admin_db_file)
                for _cfg in _saved_only:
                    _tid = _cfg.get("tenantId") or ""
                    if not _tid:
                        continue
                    xero_tenant_account_data.append(
                        {
                            "tenantId": _tid,
                            "tenantName": _cfg.get("tenantName") or _tid,
                            "enabled": _cfg.get("enabled", _tid == xero_tenant),
                            "invoiceAccount": _cfg.get("invoiceAccount", ""),
                            "paymentAccount": _cfg.get("paymentAccount", ""),
                            "revenueAccounts": [],
                            "bankAccounts": [],
                            "brandingThemes": [],
                            "loadWarning": "Reconnect Xero to load live account and branding theme options.",
                            "brandingThemeId": _cfg.get("brandingThemeId", ""),
                            "premiumThemeId": _cfg.get("premiumThemeId", ""),
                            "premiumThreshold": _cfg.get("premiumThreshold"),
                        }
                    )
                xero_tenant_warning = (
                    f"Could not refresh organisation list from Xero: {str(exc).splitlines()[0][:240]}"
                )
        setup_issues: list[str] = []
        for _t in xero_tenant_account_data:
            if not _t.get("enabled", True):
                continue
            _missing: list[str] = []
            if not str(_t.get("paymentAccount", "")).strip():
                _missing.append("Payment bank account")
            if not str(_t.get("brandingThemeId", "")).strip():
                _missing.append("Invoice template")
            if _missing:
                _setup_name = str(_t.get("tenantName") or _t.get("tenantId") or "Organisation")
                setup_issues.append(f"{_setup_name}: missing {', '.join(_missing)}")
        if setup_issues:
            setup_msg = "Setup required — " + " | ".join(setup_issues[:4])
            if len(setup_issues) > 4:
                setup_msg += f" | +{len(setup_issues) - 4} more"
            xero_tenant_warning = (
                f"{xero_tenant_warning} | {setup_msg}" if xero_tenant_warning else setup_msg
            )
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
        my_cal_ids: set[str] = set()
        if calendars:
            my_rows = []
            other_rows = []
            for c in calendars:
                cid = c["id"] or ""
                checked = "checked" if (cid in active or (c.get("primary") and "primary" in active)) else ""
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
                    if cid:
                        my_cal_ids.add(cid)
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

        # --- Calendar routing UI ---
        cal_id_to_name: dict[str, str] = {
            c["id"]: (c.get("summary_display") or c.get("summary") or c["id"])
            for c in calendars
            if c.get("id")
        }
        # Also include any active calendar IDs that may not be in the fetched list
        for cid in active:
            if cid not in cal_id_to_name:
                cal_id_to_name[cid] = cid

        sales_backlog_by_cal: dict[str, int] = {}
        for row in sales_backlog:
            cid = str(row.get("calendar_id", "")).strip()
            if cid:
                sales_backlog_by_cal[cid] = sales_backlog_by_cal.get(cid, 0) + 1

        cash_backlog_by_cal: dict[str, int] = {}
        for row in cash_backlog:
            cid = str(row.get("calendar_id", "")).strip()
            if cid:
                cash_backlog_by_cal[cid] = cash_backlog_by_cal.get(cid, 0) + 1

        # Show all "My Calendars" + active + any previously configured — so unticking never loses config
        configured_cal_ids = set(calendar_sales_sheets.keys()) | set(calendar_cash_sheets.keys())
        all_routing_cals = sorted(my_cal_ids | active | configured_cal_ids)

        # Build per-calendar dropdown HTML — one block for sales, one for cash
        def _cal_dropdown_html(prefix_select: str, prefix_panel: str, kind: str) -> str:
            """Return a dropdown + hidden panels for per-calendar sheet routing."""
            no_cal_msg = '<p class="text-sm text-gray-500">No active calendars found. Tick calendars in Active Calendars and save first.</p>'
            cals = [c for c in all_routing_cals if c in active or c in configured_cal_ids]
            if not cals:
                return no_cal_msg
            is_sales = kind == "sales"
            mapping = calendar_sales_sheets if is_sales else calendar_cash_sheets
            backlog_by_cal = sales_backlog_by_cal if is_sales else cash_backlog_by_cal
            default_tab = "Sales" if is_sales else "Cash"
            default_cid = next(
                (c for c in cals if c in mapping),
                next((c for c in cals if c in active), cals[0]),
            )
            # Dropdown
            opts = ""
            for cid in cals:
                label = cal_id_to_name.get(cid, cid)
                status = "● " if cid in active else "○ "
                opts += f'<option value="{escape(cid)}"{" selected" if cid == default_cid else ""}>{escape(status + label)}</option>'
            # Panels
            panels = ""
            for cid in cals:
                mapped = mapping.get(cid, {})
                sid = escape(mapped.get("spreadsheet_id", ""))
                sname = escape(mapped.get("sheet_name", default_tab))
                pending = backlog_by_cal.get(cid, 0)
                pending_badge = (
                    f'<span class="text-xs text-amber-700 bg-amber-50 border border-amber-200 px-2 py-0.5 rounded-full ml-2">{pending} pending</span>'
                    if pending else ""
                )
                display = "block" if cid == default_cid else "none"
                panels += (
                    f'<div id="{prefix_panel}{escape(cid)}" style="display:{display}" class="space-y-3 pt-1">'
                    f'<p class="text-xs text-gray-500">Spreadsheet URL or ID {pending_badge}</p>'
                    f'<input name="cal_{kind}_sheet_id__{escape(cid)}" value="{sid}" '
                    f'placeholder="Paste a Google Sheets URL or spreadsheet ID" '
                    f'class="w-full px-3 py-1.5 border border-gray-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500">'
                    f'<p class="text-xs text-gray-500 mt-2">Tab name</p>'
                    f'<input name="cal_{kind}_sheet_name__{escape(cid)}" value="{sname}" '
                    f'placeholder="{default_tab}" '
                    f'class="w-48 px-3 py-1.5 border border-gray-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500">'
                    f'</div>'
                )
            js = (
                f'<script>'
                f'(function(){{'
                f'function _show_{kind}(cid){{'
                f'document.querySelectorAll("[id^=\'{prefix_panel}\']").forEach(function(el){{el.style.display="none";}});'
                f'var p=document.getElementById("{prefix_panel}"+cid);'
                f'if(p)p.style.display="block";'
                f'}}'
                f'var sel=document.getElementById("{prefix_select}");'
                f'if(sel)sel.addEventListener("change",function(){{_show_{kind}(this.value);}});'
                f'}})();'
                f'</script>'
            )
            return (
                f'<div>'
                f'<label class="block text-sm font-medium text-gray-700 mb-1.5">Calendar / Person</label>'
                f'<select id="{prefix_select}" '
                f'class="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500">'
                f'{opts}'
                f'</select>'
                f'</div>'
                f'{panels}'
                f'{js}'
            )

        cal_sales_routing_html = _cal_dropdown_html("cal_sales_select", "cal_sp__", "sales")
        cal_cash_routing_html = _cal_dropdown_html("cal_cash_select", "cal_cp__", "cash")

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
        sales_stats_html = ""
        for key, label in SALES_STAT_OPTIONS:
            checked = "checked" if key in sales_stats_selected else ""
            sales_stats_html += (
                f'<label class="flex items-center gap-3 p-2 rounded-lg hover:bg-gray-50 cursor-pointer">'
                f'<input type="checkbox" name="sales_stats_fields" value="{key}" {checked} '
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

        sales_backlog_counts: dict[str, int] = {}
        for row in sales_backlog:
            email = str(row.get("submitter_email", "")).strip().lower()
            if not email:
                continue
            sales_backlog_counts[email] = sales_backlog_counts.get(email, 0) + 1

        sales_rows_html = ""
        for email in seen_submitters:
            mapped = sales_submitter_sheets.get(email, {})
            sid = mapped.get("spreadsheet_id", "")
            sname = mapped.get("sheet_name", "Sales")
            pending = sales_backlog_counts.get(email, 0)
            pending_badge = (
                f'<span class="text-xs text-amber-700 bg-amber-50 border border-amber-200 px-2 py-0.5 rounded-full">{pending} pending</span>'
                if pending
                else '<span class="text-xs text-gray-400">No backlog</span>'
            )
            sales_rows_html += (
                f'<div class="py-3 border-b border-gray-100 last:border-b-0">'
                f'<div class="flex items-center justify-between gap-3 mb-2">'
                f'<span class="text-sm text-gray-700 truncate" title="{escape(email)}">{escape(email)}</span>'
                f'{pending_badge}'
                f'</div>'
                f'<div class="grid grid-cols-1 sm:grid-cols-3 gap-2">'
                f'<input name="sales_sheet_id__{escape(email)}" value="{escape(sid)}" '
                f'placeholder="Spreadsheet URL or ID" '
                f'class="sm:col-span-2 px-3 py-1.5 border border-gray-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500">'
                f'<input name="sales_sheet_name__{escape(email)}" value="{escape(sname)}" '
                f'placeholder="Sheet tab (e.g. Sales)" '
                f'class="px-3 py-1.5 border border-gray-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500">'
                f'</div>'
                f'</div>'
            )
        if not sales_rows_html:
            sales_rows_html = '<p class="text-sm text-gray-500">No submitters seen yet — process some events first.</p>'

        cash_backlog_counts: dict[str, int] = {}
        for row in cash_backlog:
            email = str(row.get("submitter_email", "")).strip().lower()
            if not email:
                continue
            cash_backlog_counts[email] = cash_backlog_counts.get(email, 0) + 1

        cash_rows_html = ""
        for email in seen_submitters:
            mapped = cash_submitter_sheets.get(email, {})
            sid = mapped.get("spreadsheet_id", "")
            sname = mapped.get("sheet_name", "Sheet1")
            pending = cash_backlog_counts.get(email, 0)
            pending_badge = (
                f'<span class="text-xs text-amber-700 bg-amber-50 border border-amber-200 px-2 py-0.5 rounded-full">{pending} pending</span>'
                if pending
                else '<span class="text-xs text-gray-400">No backlog</span>'
            )
            cash_rows_html += (
                f'<div class="py-3 border-b border-gray-100 last:border-b-0">'
                f'<div class="flex items-center justify-between gap-3 mb-2">'
                f'<span class="text-sm text-gray-700 truncate" title="{escape(email)}">{escape(email)}</span>'
                f'{pending_badge}'
                f'</div>'
                f'<div class="grid grid-cols-1 sm:grid-cols-3 gap-2">'
                f'<input name="cash_sheet_id__{escape(email)}" value="{escape(sid)}" '
                f'placeholder="Spreadsheet URL or ID" '
                f'class="sm:col-span-2 px-3 py-1.5 border border-gray-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500">'
                f'<input name="cash_sheet_name__{escape(email)}" value="{escape(sname)}" '
                f'placeholder="Sheet tab (e.g. Cash)" '
                f'class="px-3 py-1.5 border border-gray-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500">'
                f'</div>'
                f'</div>'
            )
        if not cash_rows_html:
            cash_rows_html = '<p class="text-sm text-gray-500">No submitters seen yet — process some events first.</p>'

        # --- Spreadsheet options ---
        sheet_options = '<option value="">-- Select a spreadsheet --</option>'
        sheet_current_id = target.get("spreadsheet_id", "")
        sheet_name_val = target.get("sheet_name", "Sheet1")
        sales_sheet_current_id = sales_target.get("spreadsheet_id", "")
        sales_sheet_name_val = sales_target.get("sheet_name", "Sales")
        cash_target = get_cash_sheet_target(config.admin_db_file)
        cash_sheet_current_id = cash_target.get("spreadsheet_id", "")
        cash_sheet_name_val = cash_target.get("sheet_name", "Cash")
        for s in spreadsheets:
            sel = 'selected' if s.get("id") == sheet_current_id else ""
            sheet_options += f'<option value="{escape(s["id"])}" {sel}>{escape(s["name"])}</option>'
        if sheet_current_id and all(s.get("id") != sheet_current_id for s in spreadsheets):
            sheet_options += f'<option value="{escape(sheet_current_id)}" selected>Current saved ({escape(sheet_current_id)})</option>'
        sales_sheet_options = '<option value="">-- Select a spreadsheet --</option>'
        for s in spreadsheets:
            sel = 'selected' if s.get("id") == sales_sheet_current_id else ""
            sales_sheet_options += f'<option value="{escape(s["id"])}" {sel}>{escape(s["name"])}</option>'
        if sales_sheet_current_id and all(s.get("id") != sales_sheet_current_id for s in spreadsheets):
            sales_sheet_options += f'<option value="{escape(sales_sheet_current_id)}" selected>Current saved ({escape(sales_sheet_current_id)})</option>'
        cash_sheet_options = '<option value="">-- Select a spreadsheet --</option>'
        for s in spreadsheets:
            sel = 'selected' if s.get("id") == cash_sheet_current_id else ""
            cash_sheet_options += f'<option value="{escape(s["id"])}" {sel}>{escape(s["name"])}</option>'
        if cash_sheet_current_id and all(s.get("id") != cash_sheet_current_id for s in spreadsheets):
            cash_sheet_options += f'<option value="{escape(cash_sheet_current_id)}" selected>Current saved ({escape(cash_sheet_current_id)})</option>'

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
              <a href="/assistant" class="text-sm text-indigo-600 hover:text-indigo-700 flex items-center gap-1.5 font-medium">
                <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 10h.01M12 10h.01M16 10h.01M9 16h6M4 7a2 2 0 012-2h12a2 2 0 012 2v7a2 2 0 01-2 2H8l-4 4V7z"/>
                </svg>
                Assistant
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
          <details class="bg-white border border-gray-200 rounded-2xl shadow-sm mb-6 overflow-hidden group">
            <summary class="flex items-center justify-between px-5 py-3 border-b border-gray-100 bg-gray-50 cursor-pointer list-none select-none hover:bg-gray-100 transition-colors">
              <div class="flex items-center gap-2">
                <svg class="w-4 h-4 text-indigo-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13.828 10.172a4 4 0 00-5.656 0l-4 4a4 4 0 105.656 5.656l1.102-1.101m-.758-4.899a4 4 0 005.656 0l4-4a4 4 0 00-5.656-5.656l-1.1 1.1"/>
                </svg>
                <span class="text-sm font-semibold text-gray-800">Deployment URLs</span>
                <span class="text-xs text-gray-400 font-normal ml-1">— tap to expand</span>
              </div>
              <div class="flex items-center gap-2">
                <span class="text-xs font-mono text-indigo-600 truncate max-w-xs hidden sm:block">{escape(_req_base)}</span>
                <svg class="w-4 h-4 text-gray-400 transition-transform duration-200 group-open:rotate-180" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"/>
                </svg>
              </div>
            </summary>
            <div class="divide-y divide-gray-100">
              {_url_row("Google OAuth redirect URI", _req_base + "/oauth/callback", "Register in Google Cloud Console → Credentials → OAuth client → Authorised redirect URIs")}
              {_url_row("Xero OAuth redirect URI", _req_base + "/xero/callback", "Register in Xero Developer Portal → Your App → Redirect URIs")}
              {_url_row("Google Calendar webhook", _req_base + "/webhooks/google-calendar", "Auto-managed for active calendars (created and renewed automatically)")}
              {_url_row("Xero webhook", _req_base + "/webhooks/xero", "Paste into Xero Developer Portal → Your App → Webhooks")}
            </div>
          </details>

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

          <!-- Connection Status Row -->
          <form method="post" action="/save" enctype="multipart/form-data" id="conn-form">
            <div class="grid grid-cols-1 sm:grid-cols-2 gap-4">

              <!-- Google Card -->
              <details class="bg-white rounded-2xl shadow-sm border border-gray-200 group {'border-green-200' if google_ok else ''}">
                <summary class="flex items-center justify-between p-5 cursor-pointer list-none select-none hover:bg-gray-50 rounded-2xl transition-colors">
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
                  <div class="flex items-center gap-2">
                    {_status_badge(google_ok, "Connected" if google_ok else "Not connected")}
                    <svg class="w-4 h-4 text-gray-400 transition-transform duration-200 group-open:rotate-180 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"/>
                    </svg>
                  </div>
                </summary>

                <div class="px-5 pb-5 border-t border-gray-100 pt-4 space-y-3">
                  {"" if not client_id else f'<p class="text-xs text-gray-400 mb-2 font-mono truncate" title="{escape(client_id)}">Client: {escape(client_id[:40])}{"..." if len(client_id) > 40 else ""}</p>'}
                  <div>
                    <p class="text-xs text-gray-500 mb-1.5 font-medium">Upload OAuth credentials JSON</p>
                    <input type="file" name="google_credentials" accept=".json,application/json"
                      class="block w-full text-xs text-gray-500 file:mr-3 file:py-1.5 file:px-3 file:rounded-lg file:border-0 file:text-xs file:font-medium file:bg-indigo-50 file:text-indigo-700 hover:file:bg-indigo-100">
                    {"" if not (creds_meta or {}).get("uploaded_name") else f'<p class="text-xs text-gray-400 mt-1">Last upload: {escape((creds_meta or {{}}).get("uploaded_name", ""))}</p>'}
                  </div>
                  <div>
                    {"" if creds_file_exists else '<p class="text-xs text-amber-700 bg-amber-50 border border-amber-200 rounded-lg px-3 py-2">No credentials file uploaded yet. Select your JSON file and click <strong>Upload JSON</strong> first.</p>'}
                    {"" if not creds_file_exists else '<p class="text-xs text-green-700 bg-green-50 border border-green-200 rounded-lg px-3 py-2">&#10003; Credentials file uploaded. Click <strong>Connect Google</strong> to authorise.</p>'}
                  </div>
                  {pending_auth_url_html}
                  <div class="flex gap-2 flex-wrap pt-1">
                    <button type="submit" formaction="/upload-google-credentials"
                      class="px-3 py-1.5 text-xs font-medium text-gray-700 bg-gray-100 hover:bg-gray-200 rounded-lg transition-colors">
                      Upload JSON
                    </button>
                    <a href="/connect-google"
                      class="px-3 py-1.5 text-xs font-medium text-white {"bg-blue-600 hover:bg-blue-700" if creds_file_exists else "bg-gray-300 cursor-not-allowed"} rounded-lg transition-colors">
                      {"Reconnect" if google_ok else ("New link" if pending_auth_url else "Connect Google")}
                    </a>
                  </div>

                  <!-- Calendar Push Notifications (nested) -->
                  <details class="mt-2 border border-gray-100 rounded-xl overflow-hidden group/gcal">
                    <summary class="flex items-center justify-between px-4 py-3 bg-gray-50 cursor-pointer list-none select-none hover:bg-gray-100 transition-colors">
                      <div class="flex items-center gap-2">
                        <svg class="w-3.5 h-3.5 text-blue-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 17h5l-1.405-1.405A2.032 2.032 0 0118 14.158V11a6.002 6.002 0 00-4-5.659V5a2 2 0 10-4 0v.341C7.67 6.165 6 8.388 6 11v3.159c0 .538-.214 1.055-.595 1.436L4 17h5m6 0v1a3 3 0 11-6 0v-1m6 0H9"/>
                        </svg>
                        <span class="text-xs font-semibold text-gray-700">Calendar push notifications</span>
                      </div>
                      <div class="flex items-center gap-2">
                        {gcal_watch_badge}
                        <svg class="w-3.5 h-3.5 text-gray-400 transition-transform duration-200 group-open/gcal:rotate-180 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"/>
                        </svg>
                      </div>
                    </summary>
                    <div class="px-4 py-3 space-y-3">
                      <p class="text-xs text-gray-500">Instant notifications when calendar events change. Watches are auto-created and auto-renewed for active calendars.</p>
                      <div>
                        <label class="block text-xs font-medium text-gray-600 mb-1">Webhook URL <span class="text-gray-400 font-normal">(set automatically)</span></label>
                        <div class="flex gap-2">
                          <input readonly value="{escape(gcal_webhook_url)}"
                            class="flex-1 px-3 py-1.5 border border-gray-200 rounded-lg text-xs font-mono bg-gray-50 text-gray-600">
                          <button type="button"
                            onclick="navigator.clipboard.writeText('{gcal_webhook_url.replace(chr(39), chr(92)+chr(39))}');this.textContent='Copied!';setTimeout(()=>this.textContent='Copy',2000)"
                            class="px-3 py-1.5 text-xs font-medium bg-gray-100 hover:bg-gray-200 text-gray-700 rounded-lg transition-colors">Copy</button>
                        </div>
                      </div>
                      <div class="divide-y divide-gray-50">
                        {watch_rows_html}
                      </div>
                      <p class="text-xs text-gray-500">No manual step needed. Ticking or unticking calendars in <strong>Active Calendars</strong> applies automatically.</p>
                    </div>
                  </details>
                </div>
              </details>

              <!-- Xero Card -->
              <details class="bg-white rounded-2xl shadow-sm border border-gray-200 group">
                <summary class="flex items-center justify-between p-5 cursor-pointer list-none select-none hover:bg-gray-50 rounded-2xl transition-colors">
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
                  <div class="flex items-center gap-2">
                    {_status_badge(xero_ok, "Connected" if xero_ok else "Not connected")}
                    <svg class="w-4 h-4 text-gray-400 transition-transform duration-200 group-open:rotate-180 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"/>
                    </svg>
                  </div>
                </summary>

                <div class="px-5 pb-5 border-t border-gray-100 pt-4 space-y-3">
                  <div class="text-xs text-gray-500 space-y-0.5">
                    <p>{escape(xero_msg)}</p>
                    {"" if not xero_tenant else f'<p class="font-mono text-gray-400 truncate" title="{escape(xero_tenant)}">Tenant: {escape(xero_tenant[:32])}{"..." if len(xero_tenant) > 32 else ""}</p>'}
                    <p>Redirect URI: <code class="bg-gray-100 px-1 py-0.5 rounded text-xs">{xero_redirect}</code></p>
                  </div>
                  <div class="space-y-2">
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
                  <div class="flex flex-wrap gap-2">
                    <button type="button" {"disabled" if not xero_has_creds else ""}
                      onclick="fetchXeroUrl('/connect-xero?mode=json')"
                      class="mt-1 px-3 py-1.5 text-xs font-medium text-white bg-blue-700 hover:bg-blue-800 rounded-lg transition-colors disabled:opacity-50 disabled:cursor-not-allowed">
                      {"Reconnect Xero" if xero_ok else "Connect Xero"}
                    </button>
                    <button type="button" {"disabled" if not xero_has_creds else ""}
                      onclick="fetchXeroUrl('/connect-xero?choose_orgs=1&mode=json')"
                      class="mt-1 px-3 py-1.5 text-xs font-medium text-blue-700 bg-blue-50 hover:bg-blue-100 rounded-lg transition-colors disabled:opacity-50 disabled:cursor-not-allowed">
                      Choose organisations
                    </button>
                  </div>
                  <details id="xero-url-box" class="hidden mt-3 border border-blue-200 bg-blue-50 rounded-xl overflow-hidden group/xurl" open>
                    <summary class="flex items-center justify-between px-3 py-2 text-xs font-semibold text-blue-800 cursor-pointer list-none select-none">
                      <span>Authorisation Link (click to expand/collapse)</span>
                      <svg class="w-3.5 h-3.5 text-blue-500 transition-transform duration-200 group-open/xurl:rotate-180 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"/>
                      </svg>
                    </summary>
                    <div class="px-3 pb-3">
                      <p class="text-xs text-blue-700 mb-2">Open this URL in a new tab to continue Xero setup:</p>
                      <div class="flex gap-2 items-center">
                        <input id="xero-url-input" type="text" readonly
                          class="flex-1 text-xs font-mono border border-blue-200 rounded-lg px-3 py-2 bg-white focus:outline-none select-all">
                        <a id="xero-url-open" href="#" target="_blank"
                          class="px-3 py-2 text-xs font-medium text-white bg-blue-600 hover:bg-blue-700 rounded-lg transition-colors whitespace-nowrap">Open</a>
                        <button type="button"
                          onclick="navigator.clipboard.writeText(document.getElementById('xero-url-input').value).then(()=>{{this.textContent='Copied!';setTimeout(()=>this.textContent='Copy',2000)}});"
                          class="px-3 py-2 text-xs font-medium text-gray-700 bg-gray-100 hover:bg-gray-200 rounded-lg transition-colors whitespace-nowrap">
                          Copy
                        </button>
                      </div>
                    </div>
                  </details>
                  <script>
                  function fetchXeroUrl(endpoint) {{
                    fetch(endpoint, {{credentials: 'same-origin'}})
                      .then(r => r.json())
                      .then(data => {{
                        var box = document.getElementById('xero-url-box');
                        var inp = document.getElementById('xero-url-input');
                        var openLink = document.getElementById('xero-url-open');
                        inp.value = data.url;
                        openLink.href = data.url;
                        box.classList.remove('hidden');
                        box.setAttribute('open', 'open');
                        inp.select();
                      }})
                      .catch(e => alert('Could not generate link: ' + e));
                  }}
                  </script>

                  <!-- Xero Invoice Payment Webhooks (nested) -->
                  <details class="mt-2 border border-gray-100 rounded-xl overflow-hidden group/xwh">
                    <summary class="flex items-center justify-between px-4 py-3 bg-gray-50 cursor-pointer list-none select-none hover:bg-gray-100 transition-colors">
                      <div class="flex items-center gap-2">
                        <svg class="w-3.5 h-3.5 text-blue-700" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"/>
                        </svg>
                        <span class="text-xs font-semibold text-gray-700">Invoice payment webhooks</span>
                      </div>
                      <div class="flex items-center gap-2">
                        {('<span class="text-xs font-medium text-emerald-700 bg-emerald-50 border border-emerald-200 px-2 py-0.5 rounded-full">&#10003; Verified</span>' if xero_wh_verified else ('<span class="text-xs font-medium text-amber-700 bg-amber-50 border border-amber-200 px-2 py-0.5 rounded-full">Key saved</span>' if xero_wh_key else '<span class="text-xs font-medium text-gray-500 bg-gray-100 px-2 py-0.5 rounded-full">Not set up</span>'))}
                        <svg class="w-3.5 h-3.5 text-gray-400 transition-transform duration-200 group-open/xwh:rotate-180 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"/>
                        </svg>
                      </div>
                    </summary>
                    <div class="px-4 py-3 space-y-3">
                      <p class="text-xs text-gray-500">When an invoice is marked paid in Xero, this app updates the sheet row to <strong>Paid</strong> automatically.</p>
                      <div class="p-3 bg-blue-50 border border-blue-100 rounded-lg text-xs text-blue-800 space-y-1">
                        <p class="font-semibold">One-time setup in Xero:</p>
                        <ol class="list-decimal list-inside space-y-0.5 text-blue-700">
                          <li>Go to <strong>developer.xero.com</strong> → your app → <strong>Webhooks</strong></li>
                          <li>Add webhook URL below, tick <strong>Invoices</strong></li>
                          <li>Copy the <strong>Webhooks Key</strong> shown, paste it below and save</li>
                        </ol>
                      </div>
                      <div>
                        <label class="block text-xs font-medium text-gray-600 mb-1">Webhook URL <span class="text-gray-400 font-normal">(paste into Xero portal)</span></label>
                        <div class="flex gap-2">
                          <input readonly value="{escape(xero_webhook_url)}"
                            class="flex-1 px-3 py-1.5 border border-gray-200 rounded-lg text-xs font-mono bg-gray-50 text-gray-600">
                          <button type="button"
                            onclick="navigator.clipboard.writeText('{xero_webhook_url.replace(chr(39), chr(92)+chr(39))}');this.textContent='Copied!';setTimeout(()=>this.textContent='Copy',2000)"
                            class="px-3 py-1.5 text-xs font-medium bg-gray-100 hover:bg-gray-200 text-gray-700 rounded-lg transition-colors">Copy</button>
                        </div>
                      </div>
                      <div>
                        <label class="block text-xs font-medium text-gray-600 mb-1">Xero Webhooks Key</label>
                        <input name="xero_webhook_key" type="password"
                          placeholder="{"••••••••  (saved)" if xero_wh_key else "Paste the signing key from Xero"}"
                          class="w-full px-3 py-1.5 border border-gray-300 rounded-lg text-xs font-mono focus:outline-none focus:ring-2 focus:ring-indigo-500">
                      </div>
                      <div class="flex flex-wrap gap-2">
                        <button type="submit" formaction="/save-xero-webhook-key"
                          class="px-3 py-1.5 text-xs font-medium text-white bg-indigo-600 hover:bg-indigo-700 rounded-lg transition-colors">
                          Save Webhook Key
                        </button>
                        <button type="submit" formaction="/test-xero-webhook"
                          class="px-3 py-1.5 text-xs font-medium text-indigo-700 bg-indigo-50 hover:bg-indigo-100 rounded-lg transition-colors">
                          Test Webhook
                        </button>
                      </div>
                    </div>
                  </details>
                </div>
              </details>
            </div>
          </form>

          <!-- Google Sheets Configuration - collapsible standalone form - directly under connectivity -->
          <form method="post" action="/save-sheet-target" class="mt-4">
            <details class="bg-white rounded-2xl shadow-sm border border-gray-200 overflow-hidden group" {"open" if not sheets_ok else ""}>
              <summary class="flex items-center justify-between px-5 py-4 cursor-pointer list-none select-none hover:bg-gray-50 transition-colors">
                <div class="flex items-center gap-3">
                  <div class="w-9 h-9 bg-blue-50 rounded-xl flex items-center justify-center shrink-0">
                    <svg class="w-4 h-4 text-blue-600" viewBox="0 0 24 24" fill="currentColor">
                      <path d="M12.545 10.239v3.821h5.445c-.712 2.315-2.647 3.972-5.445 3.972a6.033 6.033 0 110-12.064c1.498 0 2.866.549 3.921 1.453l2.814-2.814A9.969 9.969 0 0012.545 2C7.021 2 2.543 6.477 2.543 12s4.478 10 10.002 10c8.396 0 10.249-7.85 9.426-11.748l-9.426-.013z"/>
                    </svg>
                  </div>
                  <div>
                    <h2 class="font-semibold text-gray-900 text-sm">Google Sheets Configuration</h2>
                    <p class="text-xs text-gray-500">Invoice sheet · Sales tracking · Cash tracking</p>
                  </div>
                </div>
                <div class="flex items-center gap-2">
                  {_status_badge(sheets_ok, "Ready" if sheets_ok else "Not ready")}
                  <svg class="w-4 h-4 text-gray-400 transition-transform duration-200 group-open:rotate-180 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"/>
                  </svg>
                </div>
              </summary>
              <div class="px-5 pb-5 pt-4 border-t border-gray-100 space-y-4">
                <p class="text-sm text-gray-500">{escape(sheets_msg)}</p>
                <details class="border border-gray-200 rounded-xl overflow-hidden group/master" open>
                  <summary class="flex items-center justify-between px-4 py-3 bg-gray-50 cursor-pointer list-none select-none hover:bg-gray-100 transition-colors">
                    <div>
                      <h3 class="text-sm font-semibold text-gray-900">Master Sheet</h3>
                      <p class="text-xs text-gray-500">All invoice/payment rows (universal)</p>
                    </div>
                    <svg class="w-4 h-4 text-gray-400 transition-transform duration-200 group-open/master:rotate-180 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"/>
                    </svg>
                  </summary>
                  <div class="px-4 py-4 border-t border-gray-100 space-y-3">
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
                      {f'<p class="text-xs text-emerald-600 mt-1">&#10003; Saved: <span class="font-medium">{escape(sheet_current_id)}</span></p>' if sheet_current_id else '<p class="text-xs text-gray-400 mt-1">No spreadsheet saved yet.</p>'}
                    </div>
                    <div>
                      <label class="block text-sm font-medium text-gray-700 mb-1.5">Sheet tab name</label>
                      <input name="sheet_name" value="{escape(sheet_name_val)}"
                        placeholder="Sheet1"
                        class="w-48 px-3 py-2 border border-gray-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500">
                      {f'<p class="text-xs text-emerald-600 mt-1">&#10003; Saved: <span class="font-medium">{escape(sheet_name_val)}</span></p>' if sheet_current_id else ""}
                    </div>
                  </div>
                </details>

                <details class="border border-gray-200 rounded-xl overflow-hidden group/cashall">
                  <summary class="flex items-center justify-between px-4 py-3 bg-gray-50 cursor-pointer list-none select-none hover:bg-gray-100 transition-colors">
                    <div>
                      <h3 class="text-sm font-semibold text-gray-900">Cash (All)</h3>
                      <p class="text-xs text-gray-500">All cash payments in one sheet</p>
                    </div>
                    <svg class="w-4 h-4 text-gray-400 transition-transform duration-200 group-open/cashall:rotate-180 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"/>
                    </svg>
                  </summary>
                  <div class="px-4 py-4 border-t border-gray-100 space-y-3">
                    {"" if not spreadsheets else f"""
                    <div>
                      <label class="block text-sm font-medium text-gray-700 mb-1.5">Pick from your spreadsheets</label>
                      <select name="cash_spreadsheet_pick" class="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500">
                        {cash_sheet_options}
                      </select>
                    </div>
                    """}
                    <div>
                      <label class="block text-sm font-medium text-gray-700 mb-1.5">Spreadsheet URL or ID</label>
                      <input name="cash_spreadsheet_input" value="{escape(cash_sheet_current_id)}"
                        placeholder="Paste a Google Sheets URL or spreadsheet ID"
                        class="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500">
                    </div>
                    <div>
                      <label class="block text-sm font-medium text-gray-700 mb-1.5">Sheet tab name</label>
                      <input name="cash_sheet_name" value="{escape(cash_sheet_name_val)}"
                        placeholder="Cash"
                        class="w-48 px-3 py-2 border border-gray-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500">
                      {f'<p class="text-xs text-emerald-600 mt-1">&#10003; Saved: <span class="font-medium">{escape(cash_sheet_current_id)} / {escape(cash_sheet_name_val)}</span></p>' if cash_sheet_current_id else '<p class="text-xs text-gray-400 mt-1">No global cash sheet saved yet.</p>'}
                    </div>
                  </div>
                </details>

                <details class="border border-gray-200 rounded-xl overflow-hidden group/sales" open>
                  <summary class="flex items-center justify-between px-4 py-3 bg-gray-50 cursor-pointer list-none select-none hover:bg-gray-100 transition-colors">
                    <div>
                      <h3 class="text-sm font-semibold text-gray-900">Sales Tracking</h3>
                      <p class="text-xs text-gray-500">Logs lines below <strong>⬇Sales⬇</strong> in [invoice] — per calendar</p>
                    </div>
                    <svg class="w-4 h-4 text-gray-400 transition-transform duration-200 group-open/sales:rotate-180 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"/>
                    </svg>
                  </summary>
                  <div class="px-4 py-4 border-t border-gray-100 space-y-4">
                    {cal_sales_routing_html}
                  </div>
                </details>

                <details class="border border-gray-200 rounded-xl overflow-hidden group/cash">
                  <summary class="flex items-center justify-between px-4 py-3 bg-gray-50 cursor-pointer list-none select-none hover:bg-gray-100 transition-colors">
                    <div>
                      <h3 class="text-sm font-semibold text-gray-900">Cash Tracking</h3>
                      <p class="text-xs text-gray-500">Logs cash payments to a sheet — per calendar</p>
                    </div>
                    <svg class="w-4 h-4 text-gray-400 transition-transform duration-200 group-open/cash:rotate-180 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"/>
                    </svg>
                  </summary>
                  <div class="px-4 py-4 border-t border-gray-100 space-y-4">
                    {cal_cash_routing_html}
                  </div>
                </details>

                <button type="submit"
                  class="px-4 py-2 bg-indigo-600 hover:bg-indigo-700 text-white text-sm font-medium rounded-lg transition-colors">
                  Save Sheets Configuration
                </button>
              </div>
            </details>
          </form>

          <!-- Xero Organisations -->
          {_xero_tenant_cards(xero_ok, xero_tenant_account_data, xero_tenant_warning)}

          <form method="post" action="/save" enctype="multipart/form-data" class="space-y-6">

            <!-- Active Calendars -->
            <div class="bg-white rounded-2xl shadow-sm border border-gray-200 p-6">
              <div class="flex items-center justify-between mb-1">
                <h2 class="font-semibold text-gray-900">Active Calendars</h2>
                <span class="text-xs text-gray-400">{len(active)} selected</span>
              </div>
              <p class="text-sm text-gray-500 mb-4">Select which calendars to monitor for entries finalised with <strong>Y/N = Y</strong>.</p>
              <div class="divide-y divide-gray-50">
                {cal_html}
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
        trigger_watch_check()  # immediately register/remove watches for the new selection
        trigger_poll()         # wake poller so the watch check runs without delay

        stats_fields = request.form.getlist("stats_fields")
        valid = [k for k in stats_fields if k in {k for k, _ in STAT_OPTIONS}]
        if not valid:
            valid = DEFAULT_STATS_FIELDS
        set_stats_fields(config.admin_db_file, valid)

        _save_calendar_sales_sheets_from_form(config, request.form)
        _save_calendar_cash_sheets_from_form(config, request.form)

        aliases = _save_submitter_aliases_from_form(config, request.form)
        msg = "Settings saved."
        if aliases:
            msg += " Submitter names saved."
        session["save_notice"] = f"success:{msg}"

        set_json_setting(config.admin_db_file, "settings_version", {"updated": True})
        trigger_poll()
        return redirect(url_for("index"))

    @app.post("/save-sheet-target")
    @require_login
    def save_sheet_target():
        picked = (request.form.get("spreadsheet_pick") or "").strip()
        entered = (request.form.get("spreadsheet_input") or "").strip()
        spreadsheet_id = _extract_spreadsheet_id(picked or entered)
        sheet_name = (request.form.get("sheet_name") or "Sheet1").strip() or "Sheet1"
        set_sheet_target(config.admin_db_file, spreadsheet_id=spreadsheet_id, sheet_name=sheet_name)

        sales_picked = (request.form.get("sales_spreadsheet_pick") or "").strip()
        sales_entered = (request.form.get("sales_spreadsheet_input") or "").strip()
        sales_spreadsheet_id = _extract_spreadsheet_id(sales_picked or sales_entered)
        sales_sheet_name = (request.form.get("sales_sheet_name") or "Sales").strip() or "Sales"
        set_sales_sheet_target(
            config.admin_db_file,
            spreadsheet_id=sales_spreadsheet_id,
            sheet_name=sales_sheet_name,
        )
        cash_picked = (request.form.get("cash_spreadsheet_pick") or "").strip()
        cash_entered = (request.form.get("cash_spreadsheet_input") or "").strip()
        cash_spreadsheet_id = _extract_spreadsheet_id(cash_picked or cash_entered)
        cash_sheet_name = (request.form.get("cash_sheet_name") or "Cash").strip() or "Cash"
        set_cash_sheet_target(
            config.admin_db_file,
            spreadsheet_id=cash_spreadsheet_id,
            sheet_name=cash_sheet_name,
        )
        sales_fields = request.form.getlist("sales_stats_fields")
        valid_sales = [k for k in sales_fields if k in {k for k, _ in SALES_STAT_OPTIONS}]
        if not valid_sales:
            valid_sales = DEFAULT_SALES_STATS_FIELDS
        set_sales_stats_fields(config.admin_db_file, valid_sales)

        _save_sales_submitter_sheets_from_form(config, request.form)
        _save_cash_submitter_sheets_from_form(config, request.form)
        cal_sales_routes = _save_calendar_sales_sheets_from_form(config, request.form)
        cal_cash_routes = _save_calendar_cash_sheets_from_form(config, request.form)
        target = {"spreadsheet_id": spreadsheet_id, "sheet_name": sheet_name}
        creds = None
        ok = False
        try:
            creds = load_admin_credentials(config)
            ok, _ = _sheets_status_data(config, creds, target)
        except Exception:
            ok = False
        if ok:
            msg = "Sheets configuration saved. Connection is ready."
        else:
            msg = "Sheets configuration saved."
        if cal_sales_routes or cal_cash_routes:
            msg += " Calendar routing updated."
        if sales_spreadsheet_id:
            msg += " Sales sheet updated."
        if cash_spreadsheet_id:
            msg += " Global cash sheet updated."
        session["save_notice"] = f"success:{msg}"
        trigger_poll()
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
        channel_id = request.headers.get("X-Goog-Channel-ID", "")

        # Accept notifications from currently-registered channels.
        # Some legacy channels may miss our token but still be valid until renewal.
        known_channels = {
            str((w or {}).get("channel_id") or "").strip()
            for w in (get_google_watches(config.admin_db_file) or {}).values()
        }
        known_channels.discard("")
        channel_known = bool(channel_id and channel_id in known_channels)

        if token and token != "gcal-bridge" and not channel_known:
            return "", 200
        if not token and not channel_known:
            return "", 200

        calendar_hint = ""
        if channel_id:
            for _cal_id, _w in (get_google_watches(config.admin_db_file) or {}).items():
                if str((_w or {}).get("channel_id") or "").strip() == channel_id:
                    calendar_hint = _cal_id
                    break

        if state_header in {"exists", "not_exists", "sync"}:
            _feed.push("Google Calendar change detected — scanning calendars now", "event")
            if calendar_hint:
                queue_calendar_target(calendar_hint)
            else:
                trigger_poll()
            print(
                f"[webhook] Google Calendar change ({state_header}) — poll triggered"
                f"{' for ' + calendar_hint if calendar_hint else ''}",
                flush=True,
            )
        return "", 200

    @app.post("/webhooks/xero")
    def webhook_xero():
        """Receive Xero webhooks (invoice paid etc.) and update sheets + calendar status."""
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
        sales_sheet_target = get_sales_sheet_target(config.admin_db_file)
        calendar_sales_sheets = get_calendar_sales_sheets(config.admin_db_file)
        sales_stats_fields = get_sales_stats_fields(config.admin_db_file)
        submitter_aliases = get_submitter_aliases(config.admin_db_file)
        spreadsheet_id = (sheet_target.get("spreadsheet_id") or "").strip()
        sheet_name = (sheet_target.get("sheet_name") or "Sheet1").strip() or "Sheet1"

        xero_tok = load_xero_token(config.xero_token_file)
        if token_is_expired(xero_tok) and xero_tok.get("refresh_token"):
            try:
                _cid, _csec = _get_xero_creds(config)
                if _cid and _csec:
                    refreshed = refresh_xero_token(
                        _cid,
                        _csec,
                        xero_tok["refresh_token"],
                    )
                    xero_tok = {**xero_tok, **refreshed}
                    save_xero_token(config.xero_token_file, xero_tok)
            except Exception as exc:
                print(
                    f"[webhook] Xero token refresh failed before invoice webhook handling: {exc}",
                    flush=True,
                )
        xero_at = (xero_tok or {}).get("access_token", "")
        xero_tenant = (xero_tok or {}).get("tenant_id", "")

        def _fetch_invoice(invoice_id: str):
            nonlocal xero_at, xero_tok
            if not (xero_at and xero_tenant):
                return None
            url = f"https://api.xero.com/api.xro/2.0/Invoices/{invoice_id}"
            headers = {
                "Authorization": f"Bearer {xero_at}",
                "Xero-tenant-id": xero_tenant,
                "Accept": "application/json",
            }
            resp = requests.get(url, headers=headers, timeout=10)
            if resp.status_code == 401 and xero_tok.get("refresh_token"):
                try:
                    _cid, _csec = _get_xero_creds(config)
                    if _cid and _csec:
                        refreshed = refresh_xero_token(_cid, _csec, xero_tok["refresh_token"])
                        xero_tok = {**xero_tok, **refreshed}
                        save_xero_token(config.xero_token_file, xero_tok)
                        xero_at = (xero_tok or {}).get("access_token", "")
                        headers["Authorization"] = f"Bearer {xero_at}"
                        resp = requests.get(url, headers=headers, timeout=10)
                except Exception as exc:
                    print(f"[webhook] Xero token refresh failed while fetching invoice: {exc}", flush=True)
            if not resp.ok:
                return None
            invoices = resp.json().get("Invoices", [])
            return invoices[0] if invoices else None

        for ev in events:
            if ev.get("eventCategory") != "INVOICE":
                continue
            invoice_id = ev.get("resourceId", "")
            if not invoice_id:
                continue
            print(f"[webhook] Xero invoice event: {ev.get('eventType')} {invoice_id}", flush=True)

            try:
                invoice = _fetch_invoice(invoice_id)
                if invoice:
                    status_raw = str(invoice.get("Status") or "").upper()
                    try:
                        amount_due = float(invoice.get("AmountDue") or 0.0)
                    except Exception:
                        amount_due = 0.0
                    # Treat fully settled invoices as paid even if status lags.
                    is_paid_or_settled = status_raw == "PAID" or amount_due <= 0.0001
                else:
                    is_paid_or_settled = False
                if is_paid_or_settled:
                    inv_number = invoice.get("InvoiceNumber", "")
                    print(f"[webhook] Invoice {inv_number} is PAID — updating sheet + calendar", flush=True)
                    _feed.push(f"Invoice {inv_number} paid — marking sheet row as Paid", "paid")
                    if creds and spreadsheet_id and inv_number:
                        try:
                            sheet_update_status = update_invoice_paid_in_sheet(
                                creds,
                                spreadsheet_id=spreadsheet_id,
                                sheet_name=sheet_name,
                                invoice_number=inv_number,
                            )
                            if sheet_update_status == "updated":
                                print(f"[webhook] Sheet row updated for {inv_number}", flush=True)
                                _feed.push(f"Sheet updated: {inv_number} marked as Paid", "paid")
                            elif sheet_update_status == "already_paid":
                                print(f"[webhook] Sheet row already up-to-date for {inv_number}", flush=True)
                            else:
                                print(
                                    f"[webhook] Sheet row update skipped for {inv_number}: {sheet_update_status}",
                                    flush=True,
                                )
                        except Exception as exc:
                            print(f"[webhook] Sheet update failed: {exc}", flush=True)

                    event_key = inv_id_to_key.get(invoice_id, "")
                    if event_key and ":" in event_key:
                        cal_id, event_id = event_key.split(":", 1)
                        queue_event_target(event_key)
                        try:
                            gsvc = build_calendar_service_from_creds(creds) if creds else None
                            if gsvc:
                                ge = gsvc.events().get(calendarId=cal_id, eventId=event_id).execute()
                                cur_summary = ge.get("summary")
                                updated_summary = set_title_status_emoji(cur_summary, "green")
                                updated_summary = set_title_mail_emoji(
                                    updated_summary,
                                    "invoice send failed" in (ge.get("description") or "").lower(),
                                )
                                if updated_summary != cur_summary:
                                    update_event_description(
                                        config,
                                        event_id=event_id,
                                        description=ge.get("description") or "",
                                        summary=updated_summary,
                                        calendar_id=cal_id,
                                    )
                                    print(f"[webhook] Calendar title set to green for {event_key}", flush=True)
                                    _feed.push(f"Calendar updated to paid (green): {inv_number or event_id}", "paid")

                                # Invoice-mode sales logging happens only after payment is actually made.
                                if payment_choice(ge.get("description")) == "invoice":
                                    sales_lines = extract_sales_lines(ge.get("description"))
                                    effective_sales_stats_fields = [
                                        str(s) for s in (sales_stats_fields or DEFAULT_SALES_STATS_FIELDS)
                                    ]
                                    if sales_lines:
                                        route = calendar_sales_sheets.get(cal_id) or {}
                                        sales_spreadsheet_id = (
                                            str(route.get("spreadsheet_id") or "").strip()
                                            or str(sales_sheet_target.get("spreadsheet_id") or "").strip()
                                        )
                                        sales_sheet_name = (
                                            str(route.get("sheet_name") or "").strip()
                                            or str(sales_sheet_target.get("sheet_name") or "Sales").strip()
                                            or "Sales"
                                        )
                                        if sales_spreadsheet_id:
                                            sales_total_ex = round(
                                                sum(
                                                    float(li.get("UnitAmount") or 0.0)
                                                    * float(li.get("Quantity") or 1.0)
                                                    for li in sales_lines
                                                ),
                                                2,
                                            )
                                            sales_total_inc = round(
                                                sum(
                                                    (float(li.get("UnitAmount") or 0.0)
                                                    * float(li.get("Quantity") or 1.0))
                                                    * (
                                                        1.2
                                                        if (li.get("TaxType") or "").upper() == "OUTPUT2"
                                                        else 1.0
                                                    )
                                                    for li in sales_lines
                                                ),
                                                2,
                                            )
                                            sales_marker = (
                                                f"{invoice_id}:invoice:sales:{sales_spreadsheet_id}:"
                                                f"{sales_sheet_name}:{len(sales_lines)}:"
                                                f"{sales_total_ex:.2f}:{sales_total_inc:.2f}"
                                            ).upper()
                                            if get_sales_log_marker(app_state, event_key) != sales_marker:
                                                from zoneinfo import ZoneInfo

                                                london_tz = ZoneInfo("Europe/London")

                                                def _fmt_london(iso_str: str) -> str:
                                                    if not iso_str:
                                                        return ""
                                                    try:
                                                        if "T" in iso_str:
                                                            obj = dt.datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
                                                            if obj.tzinfo is None:
                                                                obj = obj.replace(tzinfo=dt.timezone.utc)
                                                            return obj.astimezone(london_tz).strftime("%d/%m/%Y %H:%M")
                                                        obj = dt.date.fromisoformat(iso_str)
                                                        return obj.strftime("%d/%m/%Y")
                                                    except Exception:
                                                        return iso_str

                                                start = (ge.get("start", {}) or {}).get("dateTime") or (ge.get("start", {}) or {}).get("date") or ""
                                                end = (ge.get("end", {}) or {}).get("dateTime") or (ge.get("end", {}) or {}).get("date") or ""
                                                start_fmt = _fmt_london(start)
                                                end_fmt = _fmt_london(end)
                                                slot_text = f"{start_fmt} – {end_fmt}".strip(" –") if start_fmt != end_fmt else start_fmt
                                                customer_fields = parse_customer_fields(ge.get("description"))
                                                submitter_email = (
                                                    ((ge.get("creator", {}) or {}).get("email"))
                                                    or ((ge.get("organizer", {}) or {}).get("email"))
                                                    or ""
                                                ).strip().lower()
                                                submitter_name = submitter_aliases.get(submitter_email, submitter_email)
                                                event_id_raw = ge.get("id") or ""
                                                date_part = start.split("T", 1)[0].replace("-", "") if start else ""
                                                suffix = event_id_raw[-4:] if event_id_raw else "0000"
                                                event_id_display = (
                                                    f"GC-{date_part}-{suffix}" if date_part else (event_id_raw or event_key)
                                                )
                                                ensure_header(
                                                    creds,
                                                    spreadsheet_id=sales_spreadsheet_id,
                                                    sheet_name=sales_sheet_name,
                                                    stats_fields=effective_sales_stats_fields,
                                                )
                                                sales_lines_text: list[str] = []
                                                for line in sales_lines:
                                                    ex_vat = round(
                                                        float(line.get("UnitAmount") or 0.0)
                                                        * float(line.get("Quantity") or 1.0),
                                                        2,
                                                    )
                                                    sales_lines_text.append(
                                                        f"{line.get('Description') or ''} = £{ex_vat:.2f}"
                                                    )
                                                sales_row_event_id = f"{event_id_display}-S"
                                                append_stats_row(
                                                    creds,
                                                    spreadsheet_id=sales_spreadsheet_id,
                                                    sheet_name=sales_sheet_name,
                                                    event_key=f"{event_key}:sales",
                                                    stats_fields=effective_sales_stats_fields,
                                                    payload={
                                                        "submitter": submitter_name,
                                                        "customer": customer_fields.get("name") or "",
                                                        "invoice_number": inv_number,
                                                        "slot_datetime": slot_text,
                                                        "payment_method": "INVOICE",
                                                        "sales_item_desc": "\n".join(sales_lines_text),
                                                        "sales_total_ex_vat": f"{sales_total_ex:.2f}",
                                                    },
                                                    event_id_display=sales_row_event_id,
                                                    dedupe_signature={"Event ID": sales_row_event_id},
                                                )
                                                app_state = set_sales_log_marker(app_state, event_key, sales_marker)
                                                save_state(config.state_file, app_state)
                                                print(f"[webhook] Sales rows appended for paid invoice {inv_number}", flush=True)
                                                _feed.push(
                                                    f"Sales logged after payment: {inv_number or event_id}",
                                                    "success",
                                                )
                        except Exception as exc:
                            print(f"[webhook] Calendar status update failed for {event_key}: {exc}", flush=True)
            except Exception as exc:
                print(f"[webhook] Xero invoice fetch failed: {exc}", flush=True)

        trigger_poll()
        return "", 200

    @app.post("/setup/register-google-watches")
    @require_login
    def register_google_watches():
        trigger_poll()
        session["save_notice"] = (
            "success:Google Calendar watches are auto-managed now. "
            "Save Active Calendars and the app will create/renew watches automatically."
        )
        return redirect(url_for("index"))

    @app.post("/setup/stop-google-watches")
    @require_login
    def stop_google_watches():
        session["save_notice"] = (
            "success:Manual stop is disabled. "
            "Untick calendars in Active Calendars to remove their watches."
        )
        return redirect(url_for("index"))

    @app.post("/save-xero-webhook-key")
    @require_login
    def save_xero_webhook_key():
        key = (request.form.get("xero_webhook_key") or "").strip()
        set_xero_webhook_key(config.admin_db_file, key)
        set_xero_webhook_verified(config.admin_db_file, False)
        session["save_notice"] = "success:Xero webhook key saved. Now click \"Send intent to receive\" in the Xero Developer portal to verify."
        return redirect(url_for("index"))

    @app.post("/test-xero-webhook")
    @require_login
    def test_xero_webhook():
        """
        Manual test for Xero webhook handling from settings page.
        1) Sends an intent-style ping (events=[]) to verify route/signature path.
        2) Sends one sample INVOICE event using a known mapped invoice id (if available).
        """
        import hmac as _hmac
        import hashlib as _hashlib

        webhook_key = get_xero_webhook_key(config.admin_db_file)
        base_url = request.host_url.rstrip("/")  # keep local http for localhost dev
        webhook_url = f"{base_url}/webhooks/xero"

        def _post_payload(payload: dict) -> requests.Response:
            raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            headers = {"Content-Type": "application/json"}
            if webhook_key:
                sig = base64.b64encode(
                    _hmac.new(webhook_key.encode("utf-8"), raw, _hashlib.sha256).digest()
                ).decode("utf-8")
                headers["x-xero-signature"] = sig
            return requests.post(webhook_url, data=raw, headers=headers, timeout=12)

        try:
            ping_resp = _post_payload({"events": []})
            if ping_resp.status_code != 200:
                session["save_notice"] = (
                    f"error:Webhook test failed (intent ping HTTP {ping_resp.status_code})."
                )
                return redirect(url_for("index"))

            # Optional sample invoice event to exercise paid/settled path.
            app_state = load_state(config.state_file)
            inv_map = app_state.get("event_invoice_map", {}) or {}
            sample_invoice_id = next(
                (v for v in reversed(list(inv_map.values())) if str(v).strip()),
                "",
            )
            if sample_invoice_id:
                _post_payload(
                    {
                        "events": [
                            {
                                "resourceId": sample_invoice_id,
                                "eventCategory": "INVOICE",
                                "eventType": "UPDATE",
                            }
                        ]
                    }
                )
                session["save_notice"] = (
                    "success:Xero webhook test passed. Intent ping accepted and sample invoice event submitted."
                )
            else:
                session["save_notice"] = (
                    "success:Xero webhook test passed. Intent ping accepted."
                )
        except Exception as exc:
            session["save_notice"] = f"error:Xero webhook test failed: {str(exc)[:220]}"
        return redirect(url_for("index"))

    return app


def run_web() -> None:
    config = load_config()
    app = create_app()
    app.run(host=config.web_host, port=config.web_port)


if __name__ == "__main__":
    run_web()
