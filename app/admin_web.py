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


def _sheets_status_html(
    config: AppConfig, creds, target: dict[str, str]
) -> tuple[bool, str]:
    if not creds:
        return False, (
            "<p><b>Not connected:</b> click <i>Connect / Reconnect Google</i> first.</p>"
        )

    spreadsheet_id = (target.get("spreadsheet_id") or "").strip()
    sheet_name = (target.get("sheet_name") or "Sheet1").strip() or "Sheet1"
    if not spreadsheet_id:
        return False, "<p><b>Not ready:</b> add a Spreadsheet URL/ID and click Save Settings.</p>"

    scopes = set(getattr(creds, "scopes", []) or [])
    if "https://www.googleapis.com/auth/spreadsheets" not in scopes:
        return False, (
            "<p><b>Not ready:</b> token is missing spreadsheets scope. "
            "Reconnect Google from this page.</p>"
        )

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
            return False, (
                "<p><b>Not ready:</b> Google Sheets API is disabled in your Google Cloud project.</p>"
                f"<p>Enable it here: <a href='{escape(url)}' target='_blank'>{escape(url)}</a></p>"
                "<p>Then wait 2-5 minutes and retry.</p>"
            )
        if exc.resp and exc.resp.status == 404:
            return False, (
                "<p><b>Not ready:</b> spreadsheet not found. Check URL/ID and make sure "
                "you connected the same Google account that owns the sheet.</p>"
            )
        if exc.resp and exc.resp.status == 403:
            return False, (
                "<p><b>Not ready:</b> no access to this spreadsheet for the connected Google account.</p>"
                "<p>Share the sheet with that account, then save again.</p>"
                f"<p><small>{escape(message)}</small></p>"
            )
        return False, (
            "<p><b>Not ready:</b> failed to access spreadsheet.</p>"
            f"<p><small>{escape(message)}</small></p>"
        )

    workbook = (meta.get("properties", {}) or {}).get("title", "")
    tabs = {
        ((s.get("properties", {}) or {}).get("title") or "").strip()
        for s in (meta.get("sheets") or [])
    }
    if sheet_name not in tabs:
        return False, (
            "<p><b>Partially ready:</b> spreadsheet is reachable, but the tab name was not found.</p>"
            f"<p>Create a tab named <code>{escape(sheet_name)}</code> in "
            f"<b>{escape(workbook or spreadsheet_id)}</b>, then retry.</p>"
        )

    return True, (
        "<p><b>Connected:</b> Sheets target is valid and ready.</p>"
        f"<p>Workbook: <b>{escape(workbook or spreadsheet_id)}</b> | "
        f"Tab: <b>{escape(sheet_name)}</b></p>"
    )


def _oauth_debug_html(config: AppConfig) -> str:
    path = Path(config.google_credentials_file)
    client_id = ""
    if path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            section = payload.get("web") or payload.get("installed") or {}
            client_id = str(section.get("client_id") or "")
        except Exception:
            client_id = ""
    return (
        "<p><small>"
        f"OAuth client_id in use: <code>{escape(client_id or 'unknown')}</code><br>"
        f"OAuth redirect URI in use: <code>{escape(config.google_oauth_redirect_uri)}</code>"
        "</small></p>"
    )


def _xero_scope_string(scopes: list[str]) -> str:
    parts = [s.strip() for s in scopes if s and s.strip()]
    return " ".join(parts)


def _xero_authorization_url(config: AppConfig, state: str) -> str:
    params = {
        "response_type": "code",
        "client_id": config.xero_client_id,
        "redirect_uri": config.xero_redirect_uri,
        "scope": _xero_scope_string(config.xero_scopes),
        "state": state,
    }
    return f"{XERO_AUTH_URL}?{urllib.parse.urlencode(params)}"


def _exchange_xero_code(config: AppConfig, code: str) -> dict:
    basic = base64.b64encode(
        f"{config.xero_client_id}:{config.xero_client_secret}".encode()
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


def _xero_status_html(config: AppConfig) -> str:
    token = load_xero_token(config.xero_token_file)
    has_credentials = bool(config.xero_client_id and config.xero_client_secret)
    scope_text = _xero_scope_string(config.xero_scopes)
    if not has_credentials:
        return (
            "<p><b>Not ready:</b> missing XERO_CLIENT_ID / XERO_CLIENT_SECRET in environment.</p>"
            f"<p><small>Callback URI to set in Xero app: <code>{escape(config.xero_redirect_uri)}</code></small></p>"
        )

    lines = [
        f"<p><small>Callback URI to set in Xero app: <code>{escape(config.xero_redirect_uri)}</code><br>",
        f"Scopes in use: <code>{escape(scope_text or 'none')}</code></small></p>",
    ]
    access = token.get("access_token")
    tenant = token.get("tenant_id")
    if access and tenant:
        exp = "expired" if token_is_expired(token) else "valid"
        lines.append(
            f"<p><b>Connected:</b> tenant <code>{escape(tenant)}</code> (token {exp}).</p>"
        )
    else:
        lines.append("<p><b>Not connected:</b> click <i>Connect / Reconnect Xero</i>.</p>")
    return "".join(lines)


def _save_submitter_aliases_from_form(config: AppConfig, form) -> dict[str, str]:
    current = get_submitter_aliases(config.admin_db_file)
    next_aliases = dict(current)
    prefix = "submitter_alias__"
    for k, v in form.items():
        if not k.startswith(prefix):
            continue
        email = k[len(prefix) :].strip().lower()
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
    new_description = description[: m.start()] + start + new_block + end + description[m.end() :]
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
        return """
        <h2>Powwash Admin Login</h2>
        <form method="post">
          <label>Username</label><br>
          <input name="username"><br><br>
          <label>Password</label><br>
          <input name="password" type="password"><br><br>
          <button type="submit">Log in</button>
        </form>
        """

    @app.post("/login")
    def login_post():
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        if username == config.admin_username and password == config.admin_password:
            session["logged_in"] = True
            return redirect(url_for("index"))
        return "<p>Invalid login.</p><a href='/login'>Try again</a>", 401

    @app.get("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.get("/connect-xero")
    @require_login
    def connect_xero():
        if not config.xero_client_id or not config.xero_client_secret:
            session["save_notice"] = "Xero connect failed: missing XERO_CLIENT_ID/XERO_CLIENT_SECRET."
            return redirect(url_for("index"))
        state = secrets.token_urlsafe(24)
        session["xero_oauth_state"] = state
        set_json_setting(config.admin_db_file, "xero_oauth_pending_state", state)
        return redirect(_xero_authorization_url(config, state))

    @app.get("/xero/callback")
    def xero_callback():
        code = request.args.get("code") or ""
        state = request.args.get("state") or ""
        err = request.args.get("error") or ""
        if err:
            return f"<p>Xero OAuth error: {escape(err)}</p>", 400
        expected_session = session.get("xero_oauth_state") or ""
        expected_store = str(
            get_json_setting(config.admin_db_file, "xero_oauth_pending_state", "")
        ).strip()
        state_ok = bool(state) and (state == expected_session or state == expected_store)
        if not code or not state_ok:
            return (
                "<p>Xero callback invalid state/code.</p>"
                "<p>Use one host consistently for login + callback and retry.</p>",
                400,
            )
        try:
            token = _exchange_xero_code(config, code)
            tenant_id = _get_xero_tenant_id(token.get("access_token", ""))
        except Exception as exc:  # noqa: BLE001
            return f"<p>Xero connect failed: {escape(str(exc))}</p>", 400

        token["tenant_id"] = tenant_id
        save_xero_token(config.xero_token_file, token)
        session["logged_in"] = True
        session.pop("xero_oauth_state", None)
        set_json_setting(config.admin_db_file, "xero_oauth_pending_state", "")
        session["save_notice"] = "Xero connected successfully."
        return redirect(url_for("index"))

    @app.post("/upload-google-credentials")
    @require_login
    def upload_google_credentials():
        f = request.files.get("google_credentials")
        if not f or not f.filename:
            session["save_notice"] = "No credentials file selected."
            return redirect(url_for("index"))
        raw = f.read()
        ok, err = _validate_google_credentials_json(raw)
        if not ok:
            session["save_notice"] = f"Credentials upload failed: {err}"
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
            f"Credentials uploaded to {path}. Reconnect Google to apply."
        )
        return redirect(url_for("index"))

    @app.get("/connect-google")
    @require_login
    def connect_google():
        auth_url, state = oauth_authorization_url(config)
        session["oauth_state"] = state
        set_json_setting(config.admin_db_file, "oauth_pending_state", state)
        return redirect(auth_url)

    @app.get("/oauth/callback")
    def oauth_callback():
        code = request.args.get("code") or ""
        state = request.args.get("state") or ""
        expected_session = session.get("oauth_state") or ""
        expected_store = str(
            get_json_setting(config.admin_db_file, "oauth_pending_state", "")
        ).strip()
        state_ok = bool(state) and (state == expected_session or state == expected_store)
        if not code or not state_ok:
            return (
                "<p>OAuth callback invalid state/code.</p>"
                "<p>Tip: use one host consistently for login + callback "
                "(for example, both on localhost or both on 127.0.0.1).</p>",
                400,
            )
        creds = oauth_exchange_code(config, state=state, code=code)
        save_admin_credentials(config, creds)
        session["logged_in"] = True
        session.pop("oauth_state", None)
        set_json_setting(config.admin_db_file, "oauth_pending_state", "")
        return redirect(url_for("index"))

    @app.get("/")
    @require_login
    def index():
        creds = load_admin_credentials(config)
        active = set(get_active_calendars(config.admin_db_file, config.google_calendar_id))
        stats_selected = set(get_stats_fields(config.admin_db_file))
        target = get_sheet_target(config.admin_db_file)
        sheets_ok, sheets_status = _sheets_status_html(config, creds, target)
        oauth_debug = _oauth_debug_html(config)
        xero_status = _xero_status_html(config)
        save_notice = session.pop("save_notice", "")
        creds_meta = get_json_setting(
            config.admin_db_file,
            "google_credentials_meta",
            {"uploaded_name": "", "stored_path": config.google_credentials_file},
        )
        seen_submitters = get_seen_submitters(config.admin_db_file)
        submitter_aliases = get_submitter_aliases(config.admin_db_file)

        calendars = []
        spreadsheets = []
        oauth_note = ""
        if creds:
            try:
                calendars = list_calendars(creds)
            except Exception as exc:  # noqa: BLE001
                oauth_note = f"Connected, but failed to load calendars: {exc}"
            try:
                spreadsheets = list_spreadsheets(creds)
            except Exception:
                pass
        else:
            oauth_note = "Google not connected yet."

        cal_html = ""
        if calendars:
            my_rows = []
            other_rows = []
            for c in calendars:
                cid = c["id"] or ""
                checked = "checked" if cid in active else ""
                title = escape(c.get("summary_display") or c.get("summary") or cid)
                primary_badge = " <b>(Primary)</b>" if c.get("primary") else ""
                hint_bits = []
                if c.get("hidden"):
                    hint_bits.append("hidden")
                if not c.get("selected", True):
                    hint_bits.append("not selected")
                hint = f" <small>({'; '.join(hint_bits)})</small>" if hint_bits else ""
                row = (
                    f"<label><input type='checkbox' name='active_calendars' "
                    f"value='{escape(cid)}' {checked}> {title}{primary_badge}{hint}</label>"
                )
                is_my_calendar = (
                    c.get("primary")
                    or c.get("is_birthdays")
                    or (
                        c.get("access_role") in {"owner", "writer"}
                        and not c.get("is_holiday")
                    )
                )
                if is_my_calendar:
                    my_rows.append(row)
                else:
                    other_rows.append(row)
            parts = []
            if my_rows:
                parts.append("<b>My calendars</b><br>" + "<br>".join(my_rows))
            if other_rows:
                parts.append("<b>Other calendars</b><br>" + "<br>".join(other_rows))
            cal_html = "<br><br>".join(parts)
        else:
            cal_html = "<p>No calendars loaded yet. Connect Google first.</p>"

        stats_html = "<br>".join(
            [
                (
                    f"<label><input type='checkbox' name='stats_fields' value='{key}' "
                    f"{'checked' if key in stats_selected else ''}> {label}</label>"
                )
                for key, label in STAT_OPTIONS
            ]
        )
        alias_rows = []
        for email in seen_submitters:
            alias = submitter_aliases.get(email, "")
            alias_rows.append(
                "<div style='margin-bottom:6px'>"
                f"<label style='display:inline-block;min-width:320px'>{escape(email)}</label> "
                f"<input name='submitter_alias__{escape(email)}' value='{escape(alias)}' "
                "placeholder='Display name'>"
                "</div>"
            )
        submitter_alias_html = (
            "".join(alias_rows)
            if alias_rows
            else "<p><small>No submitters seen yet. Process some events first.</small></p>"
        )

        sheet_options = "".join(
            [
                f"<option value='{escape(s['id'])}'>{escape(s['name'])}</option>"
                for s in spreadsheets
            ]
        )
        sheet_current = escape(target.get("spreadsheet_id", ""))
        sheet_name = escape(target.get("sheet_name", "Sheet1"))
        if sheet_current and all(s.get("id") != sheet_current for s in spreadsheets):
            sheet_options = (
                f"<option value='{sheet_current}'>Current saved target ({sheet_current})</option>"
                + sheet_options
            )

        return f"""
        <h2>Powwash Integration Admin</h2>
        <p><a href='/logout'>Logout</a></p>
        <p><a href='/connect-google'>Connect / Reconnect Google</a></p>
        <p><a href='/connect-xero'>Connect / Reconnect Xero</a></p>
        <p>{escape(save_notice)}</p>
        <p>{escape(oauth_note)}</p>
        <h3>Xero Connection</h3>
        {xero_status}
        <hr>
        <form method="post" action="/save" enctype="multipart/form-data">
          <h3>Active Calendars</h3>
          <p><small>Only calendars returned by the Google Calendar API are shown here. Google Tasks is not a Calendar API calendar.</small></p>
          {cal_html}
          <hr>
          <h3>Google Sheets Target</h3>
          {oauth_debug}
          <p><small>Google credentials path: <code>{escape(config.google_credentials_file)}</code> | Last upload: <code>{escape((creds_meta or {}).get("uploaded_name", "") or "none")}</code></small></p>
          <input type="file" name="google_credentials" accept=".json,application/json">
          <button type="submit" formaction="/upload-google-credentials">Upload Credentials JSON</button>
          <p><small>After upload, click <i>Connect / Reconnect Google</i> to issue a fresh token.</small></p>
          <label>Spreadsheet URL or ID</label><br>
          <input name="spreadsheet_input" style="width:680px" value="{sheet_current}"><br><br>
          <label>Sheet tab name</label><br>
          <input name="sheet_name" value="{sheet_name}"><br><br>
          <label>Recent spreadsheets (optional)</label><br>
          <select name="spreadsheet_pick">
            <option value="">-- optional quick pick --</option>
            {sheet_options}
          </select>
          <br><br>
          <h3>Sheets Connection Check</h3>
          {sheets_status}
          <hr>
          <h3>Stats to Post to Sheets</h3>
          {stats_html}
          <hr>
          <h3>Submitter Display Names</h3>
          <p><small>Map submitter e-mail to a display name used in calendar status and sheet rows.</small></p>
          {submitter_alias_html}
          <button type="submit" formaction="/apply-submitter-aliases">Save Names + Apply To Existing Entries</button>
          <br><br>
          <button type="submit">Save Settings</button>
        </form>
        """

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
        ok, _ = _sheets_status_html(config, creds, target)
        aliases = _save_submitter_aliases_from_form(config, request.form)
        session["save_notice"] = (
            "Settings saved. Sheets connection is ready."
            if ok
            else "Settings saved. Follow the Sheets Connection Check steps below."
        )
        if aliases:
            session["save_notice"] += " Submitter names saved."

        # Ensure worker can quickly detect settings refresh.
        set_json_setting(config.admin_db_file, "settings_version", {"updated": True})

        return redirect(url_for("index"))

    @app.post("/apply-submitter-aliases")
    @require_login
    def apply_submitter_aliases():
        aliases = _save_submitter_aliases_from_form(config, request.form)
        if not aliases:
            session["save_notice"] = "No submitter name mappings set."
            return redirect(url_for("index"))

        updated, errors = _backfill_submitter_aliases(config, aliases)
        session["save_notice"] = (
            f"Submitter names saved. Updated {updated} existing calendar entries."
        )
        if errors:
            session["save_notice"] += f" ({errors} updates failed.)"
        set_json_setting(config.admin_db_file, "settings_version", {"updated": True})
        return redirect(url_for("index"))

    return app


def run_web() -> None:
    config = load_config()
    app = create_app()
    app.run(host=config.web_host, port=config.web_port)


if __name__ == "__main__":
    run_web()
