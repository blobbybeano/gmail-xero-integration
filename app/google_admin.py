from __future__ import annotations

import secrets
from pathlib import Path
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

from .config import AppConfig


def _scopes(config: AppConfig) -> list[str]:
    return config.google_admin_scopes or config.google_scopes


def load_admin_credentials(config: AppConfig) -> Credentials | None:
    token_path = Path(config.google_admin_token_file)
    if not token_path.exists():
        return None
    creds = Credentials.from_authorized_user_file(token_path.as_posix(), _scopes(config))
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token_path.write_text(creds.to_json())
    if creds and creds.valid:
        return creds
    return None


def save_admin_credentials(config: AppConfig, creds: Credentials) -> None:
    Path(config.google_admin_token_file).write_text(creds.to_json())


def build_calendar_service_from_creds(creds: Credentials):
    return build("calendar", "v3", credentials=creds)


def build_drive_service_from_creds(creds: Credentials):
    return build("drive", "v3", credentials=creds)


def build_sheets_service_from_creds(creds: Credentials):
    return build("sheets", "v4", credentials=creds)


def oauth_authorization_url(
    config: AppConfig,
    redirect_uri: str | None = None,
) -> tuple[str, str]:
    uri = redirect_uri or config.google_oauth_redirect_uri
    flow = Flow.from_client_secrets_file(
        config.google_credentials_file,
        scopes=_scopes(config),
        redirect_uri=uri,
    )
    state = secrets.token_urlsafe(24)
    auth_url, returned_state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        state=state,
        prompt="consent",
    )
    return auth_url, returned_state


def oauth_exchange_code(
    config: AppConfig,
    state: str,
    code: str,
    redirect_uri: str | None = None,
) -> Credentials:
    uri = redirect_uri or config.google_oauth_redirect_uri
    flow = Flow.from_client_secrets_file(
        config.google_credentials_file,
        scopes=_scopes(config),
        state=state,
        redirect_uri=uri,
    )
    flow.fetch_token(code=code)
    return flow.credentials


def list_calendars(creds: Credentials) -> list[dict[str, Any]]:
    service = build_calendar_service_from_creds(creds)
    items: list[dict[str, Any]] = []
    page_token = None
    while True:
        resp = (
            service.calendarList()
            .list(showHidden=True, pageToken=page_token)
            .execute()
        )
        items.extend(resp.get("items", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    calendars = [
        {
            "id": c.get("id"),
            "summary": c.get("summary"),
            "summary_display": c.get("summaryOverride") or c.get("summary") or c.get("id"),
            "primary": bool(c.get("primary")),
            "access_role": c.get("accessRole", ""),
            "selected": bool(c.get("selected", True)),
            "hidden": bool(c.get("hidden", False)),
            "is_holiday": "#holiday@group.v.calendar.google.com" in (c.get("id") or ""),
            "is_birthdays": "#contacts@group.v.calendar.google.com" in (c.get("id") or ""),
        }
        for c in items
        if c.get("id")
    ]
    calendars.sort(
        key=lambda c: (
            0 if c.get("primary") else 1,
            0 if c.get("access_role") in {"owner", "writer"} else 1,
            (c.get("summary") or "").lower(),
        )
    )
    return calendars


def list_spreadsheets(creds: Credentials, limit: int = 25) -> list[dict[str, str]]:
    drive = build_drive_service_from_creds(creds)
    resp = (
        drive.files()
        .list(
            q="mimeType='application/vnd.google-apps.spreadsheet' and trashed=false",
            pageSize=limit,
            fields="files(id,name,webViewLink)",
            orderBy="modifiedTime desc",
        )
        .execute()
    )
    files = resp.get("files", [])
    return [
        {"id": f.get("id", ""), "name": f.get("name", ""), "url": f.get("webViewLink", "")}
        for f in files
    ]
