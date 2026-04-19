from __future__ import annotations

import datetime as dt
import json
import time
import uuid
from pathlib import Path
from typing import Dict, List

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .config import AppConfig


def _load_credentials(config: AppConfig) -> Credentials:
    # Prefer the admin token written by the web OAuth flow, fall back to legacy token
    for token_file in [config.google_admin_token_file, config.google_token_file]:
        token_path = Path(token_file)
        if not token_path.exists():
            continue
        try:
            creds = Credentials.from_authorized_user_file(
                token_path.as_posix(),
                getattr(config, "google_admin_scopes", None) or config.google_scopes,
            )
        except Exception:
            continue
        if creds and creds.valid:
            return creds
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            token_path.write_text(creds.to_json())
            if creds.valid:
                return creds

    raise RuntimeError(
        "No valid Google credentials found. "
        "Please connect Google via the admin dashboard first."
    )


def build_calendar_service(config: AppConfig):
    creds = _load_credentials(config)
    return build("calendar", "v3", credentials=creds)


def list_recent_events(
    config: AppConfig,
    updated_min: dt.datetime,
    time_min: dt.datetime,
    time_max: dt.datetime,
    calendar_id: str | None = None,
) -> List[Dict]:
    service = build_calendar_service(config)

    events_result = (
        service.events()
        .list(
            calendarId=calendar_id or config.google_calendar_id,
            singleEvents=True,
            orderBy="startTime",
            updatedMin=updated_min.isoformat(),
            timeMin=time_min.isoformat(),
            timeMax=time_max.isoformat(),
        )
        .execute()
    )
    return events_result.get("items", [])


def update_event_description(
    config: AppConfig,
    event_id: str,
    description: str,
    calendar_id: str | None = None,
) -> Dict:
    service = build_calendar_service(config)
    for attempt in range(3):
        try:
            updated = (
                service.events()
                .patch(
                    calendarId=calendar_id or config.google_calendar_id,
                    eventId=event_id,
                    body={"description": description},
                )
                .execute()
            )
            return updated
        except HttpError as exc:
            if _is_rate_limit_error(exc):
                if attempt < 2:
                    time.sleep(2**attempt)
                    continue
                raise RateLimitError("Google Calendar rate limit exceeded") from exc
            raise
    raise RateLimitError("Google Calendar rate limit exceeded")


def register_calendar_watch(
    config: AppConfig,
    calendar_id: str,
    webhook_url: str,
) -> Dict:
    """Register a Google Calendar push-notification watch. Returns the channel dict."""
    service = build_calendar_service(config)
    channel_id = str(uuid.uuid4())
    body = {
        "id": channel_id,
        "type": "web_hook",
        "address": webhook_url,
        "token": "gcal-bridge",
    }
    return service.events().watch(calendarId=calendar_id, body=body).execute()


def stop_calendar_watch(config: AppConfig, channel_id: str, resource_id: str) -> None:
    """Stop an active Google Calendar push-notification watch."""
    try:
        service = build_calendar_service(config)
        service.channels().stop(
            body={"id": channel_id, "resourceId": resource_id}
        ).execute()
    except Exception:
        pass


class RateLimitError(RuntimeError):
    pass


def _is_rate_limit_error(exc: HttpError) -> bool:
    status = getattr(exc.resp, "status", None)
    if status not in {403, 429}:
        return False
    try:
        payload = json.loads(exc.content.decode("utf-8"))
        errors = payload.get("error", {}).get("errors", [])
        if errors:
            reason = errors[0].get("reason", "")
            return reason in {"rateLimitExceeded", "userRateLimitExceeded"}
    except Exception:
        return False
    return True
