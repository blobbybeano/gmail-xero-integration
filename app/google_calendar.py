from __future__ import annotations

import datetime as dt
import json
import time
from pathlib import Path
from typing import Dict, List

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .config import AppConfig


def _load_credentials(config: AppConfig) -> Credentials:
    token_path = Path(config.google_token_file)
    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(
            config.google_token_file, config.google_scopes
        )

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                config.google_credentials_file, config.google_scopes
            )
            creds = flow.run_local_server(port=0)

        token_path.write_text(creds.to_json())

    return creds


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
