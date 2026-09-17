from __future__ import annotations

import datetime as dt
import io
import json
import re
import secrets
import time
from pathlib import Path
from typing import Any

from googleapiclient.http import MediaIoBaseUpload

from .admin_store import get_json_setting, set_json_setting, get_job_photo_settings
from .config import AppConfig
from .event_processor import parse_app_ledger, parse_customer_fields
from .google_admin import load_admin_credentials, build_drive_service_from_creds
from .google_calendar import build_calendar_service

_EVENT_LINKS_KEY = "job_photo_event_links"
_LINKS_KEY = "job_photo_short_links"
_FILES_KEY = "job_photo_files_by_event"

CATEGORY_CUSTOMER = "customer"
CATEGORY_BEFORE_AFTER = "before_after"
CATEGORY_UPDATE = "update"

CATEGORY_LABELS = {
    CATEGORY_CUSTOMER: "Customer provided photos",
    CATEGORY_BEFORE_AFTER: "Before / after photos",
    CATEGORY_UPDATE: "Technical photos / videos",
}


def drive_scope_configured(config: AppConfig) -> bool:
    token_path = Path(config.google_admin_token_file)
    try:
        raw = json.loads(token_path.read_text()) if token_path.exists() else {}
    except Exception:
        raw = {}
    scopes = set(raw.get("scopes") or [])
    return (
        "https://www.googleapis.com/auth/drive.file" in scopes
        or "https://www.googleapis.com/auth/drive" in scopes
    )


def extract_drive_folder_id(value: str | None) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    patterns = [
        r"/folders/([A-Za-z0-9_-]+)",
        r"[?&]id=([A-Za-z0-9_-]+)",
    ]
    for pat in patterns:
        m = re.search(pat, raw)
        if m:
            return m.group(1)
    return raw


def _now_ts() -> int:
    return int(time.time())


def _safe_name(value: str, *, fallback: str = "Job") -> str:
    text = " ".join((value or "").strip().split())
    text = re.sub(r"[\\/:*?\"<>|]+", " ", text)
    text = " ".join(text.split())
    return (text or fallback)[:120]


def _event_datetime_label(event: dict[str, Any]) -> str:
    start = (event.get("start") or {}).get("dateTime") or (event.get("start") or {}).get("date") or ""
    if not start:
        return ""
    try:
        return dt.datetime.fromisoformat(start.replace("Z", "+00:00")).strftime("%Y-%m-%d")
    except Exception:
        return start[:10]


def _invoice_number(description: str | None) -> str:
    text = description or ""
    m = re.search(r"\b(INV-\d+)\b", text, flags=re.I)
    return m.group(1).upper() if m else ""


def event_is_processed(description: str | None) -> bool:
    text = (description or "").lower()
    ledger = parse_app_ledger(description)
    ledger_state = (ledger.get("s") or "").strip().lower()
    ledger_reason = (ledger.get("r") or "").strip().lower()
    if ledger_state in {"complete", "sent"}:
        return True
    if ledger_reason in {"cash", "ok"} and ledger_state not in {"needs_input", "error", "failed"}:
        return True
    return (
        "payment type (card/invoice)" in text
        or ("payment type" in text and ("card" in text or "invoice" in text or "cash" in text))
        or "invoice sent" in text
        or "invoice link:" in text
        or "entry complete" in text
    )


def ensure_job_photo_code(
    db_path: str,
    event_key: str,
    *,
    ttl_days: int = 0,
) -> str:
    event_key = (event_key or "").strip()
    if not event_key:
        raise ValueError("missing event key")
    event_links = get_json_setting(db_path, _EVENT_LINKS_KEY, {}) or {}
    existing = str(event_links.get(event_key) or "").strip()
    links = get_json_setting(db_path, _LINKS_KEY, {}) or {}
    if existing and isinstance(links.get(existing), dict):
        return existing

    code = secrets.token_urlsafe(6)
    while code in links:
        code = secrets.token_urlsafe(6)
    exp = 0
    if ttl_days > 0:
        exp = _now_ts() + ttl_days * 86400
    links[code] = {"event_key": event_key, "created_at": _now_ts(), "exp": exp}
    event_links[event_key] = code
    set_json_setting(db_path, _LINKS_KEY, links)
    set_json_setting(db_path, _EVENT_LINKS_KEY, event_links)
    return code


def resolve_job_photo_code(db_path: str, code: str) -> str:
    links = get_json_setting(db_path, _LINKS_KEY, {}) or {}
    row = links.get((code or "").strip())
    if not isinstance(row, dict):
        raise ValueError("photo link not found")
    exp = int(row.get("exp") or 0)
    if exp and exp < _now_ts():
        raise ValueError("photo link expired")
    event_key = str(row.get("event_key") or "").strip()
    if not event_key:
        raise ValueError("photo link is incomplete")
    return event_key


def create_job_photo_links(db_path: str, event_key: str, base_url: str) -> tuple[str, str]:
    settings = get_job_photo_settings(db_path)
    code = ensure_job_photo_code(
        db_path,
        event_key,
        ttl_days=int(settings.get("link_ttl_days") or 0),
    )
    base = base_url.rstrip("/")
    return f"{base}/j/{code}", f"{base}/jp/{code}"


def list_job_photos(db_path: str, event_key: str) -> list[dict[str, Any]]:
    rows = get_json_setting(db_path, _FILES_KEY, {}) or {}
    files = rows.get(event_key) or []
    return files if isinstance(files, list) else []


def _save_job_photo(db_path: str, event_key: str, row: dict[str, Any]) -> None:
    rows = get_json_setting(db_path, _FILES_KEY, {}) or {}
    files = rows.get(event_key) or []
    if not isinstance(files, list):
        files = []
    files.append(row)
    rows[event_key] = files
    set_json_setting(db_path, _FILES_KEY, rows)


def _quote_drive(value: str) -> str:
    return (value or "").replace("\\", "\\\\").replace("'", "\\'")


def _find_or_create_folder(drive, name: str, parent_id: str) -> str:
    q = (
        "mimeType='application/vnd.google-apps.folder' "
        "and trashed=false "
        f"and name='{_quote_drive(name)}' "
        f"and '{_quote_drive(parent_id)}' in parents"
    )
    resp = drive.files().list(q=q, fields="files(id,name)", pageSize=1).execute()
    files = resp.get("files") or []
    if files:
        return files[0]["id"]
    meta = {
        "name": name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id],
    }
    created = drive.files().create(body=meta, fields="id").execute()
    return created["id"]


def _event_from_key(config: AppConfig, event_key: str) -> dict[str, Any]:
    if ":" not in event_key:
        raise ValueError("invalid event key")
    calendar_id, event_id = event_key.split(":", 1)
    svc = build_calendar_service(config)
    return svc.events().get(calendarId=calendar_id, eventId=event_id).execute()


def _job_folder_name(event: dict[str, Any]) -> str:
    desc = event.get("description") or ""
    customer = parse_customer_fields(desc).get("name") or ""
    invoice_no = _invoice_number(desc)
    job_date = _event_datetime_label(event)
    summary = event.get("summary") or "Job"
    parts = [_safe_name(customer or summary)]
    if invoice_no:
        parts.append(invoice_no)
    if job_date:
        parts.append(job_date)
    return _safe_name(" - ".join(parts), fallback="Job")


def ensure_event_drive_folder(config: AppConfig, event_key: str, category: str) -> tuple[str, dict[str, Any]]:
    return ensure_event_drive_folder_for_upload(config, event_key, category, "")


def ensure_event_drive_folder_for_upload(
    config: AppConfig,
    event_key: str,
    category: str,
    category_detail: str = "",
) -> tuple[str, dict[str, Any]]:
    settings = get_job_photo_settings(config.admin_db_file)
    parent = str(settings.get("drive_parent_folder_id") or "").strip() or "root"
    creds = load_admin_credentials(config)
    if not creds:
        raise ValueError("Google is not connected.")
    drive = build_drive_service_from_creds(creds)
    event = _event_from_key(config, event_key)
    root_id = _find_or_create_folder(drive, "Customer pictures", parent)
    customers_id = _find_or_create_folder(drive, "Customers", root_id)
    job_id = _find_or_create_folder(drive, _job_folder_name(event), customers_id)
    category_name = CATEGORY_LABELS.get(category, CATEGORY_LABELS[CATEGORY_CUSTOMER])
    category_id = _find_or_create_folder(drive, category_name, job_id)
    detail = " ".join((category_detail or "").strip().lower().split())
    if category == CATEGORY_BEFORE_AFTER and detail in {"before", "after"}:
        category_id = _find_or_create_folder(drive, detail.title(), category_id)
    return category_id, event


def upload_job_photo(
    config: AppConfig,
    *,
    event_key: str,
    file_bytes: bytes,
    filename: str,
    mime_type: str,
    category: str,
    category_detail: str = "",
) -> dict[str, Any]:
    category = category if category in CATEGORY_LABELS else CATEGORY_CUSTOMER
    detail = " ".join((category_detail or "").strip().lower().split())
    if category != CATEGORY_BEFORE_AFTER or detail not in {"before", "after"}:
        detail = ""
    folder_id, event = ensure_event_drive_folder_for_upload(config, event_key, category, detail)
    creds = load_admin_credentials(config)
    if not creds:
        raise ValueError("Google is not connected.")
    drive = build_drive_service_from_creds(creds)
    safe_filename = Path(filename or "photo.jpg").name or "photo.jpg"
    media = MediaIoBaseUpload(io.BytesIO(file_bytes), mimetype=mime_type or "application/octet-stream", resumable=False)
    created = (
        drive.files()
        .create(
            body={"name": safe_filename, "parents": [folder_id]},
            media_body=media,
            fields="id,name,mimeType,webViewLink,thumbnailLink,createdTime",
        )
        .execute()
    )
    row = {
        "id": created.get("id", ""),
        "file_id": created.get("id", ""),
        "name": created.get("name", safe_filename),
        "mime_type": created.get("mimeType", mime_type or ""),
        "web_view_link": created.get("webViewLink", ""),
        "thumbnail_link": created.get("thumbnailLink", ""),
        "created_time": created.get("createdTime", ""),
        "uploaded_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "category": category,
        "category_detail": detail,
        "category_label": CATEGORY_LABELS.get(category, CATEGORY_LABELS[CATEGORY_CUSTOMER]),
    }
    _save_job_photo(config.admin_db_file, event_key, row)
    return row


def get_drive_file_media(config: AppConfig, file_id: str) -> tuple[bytes, str, str]:
    creds = load_admin_credentials(config)
    if not creds:
        raise ValueError("Google is not connected.")
    drive = build_drive_service_from_creds(creds)
    meta = drive.files().get(fileId=file_id, fields="name,mimeType").execute()
    data = drive.files().get_media(fileId=file_id).execute()
    return data, str(meta.get("mimeType") or "application/octet-stream"), str(meta.get("name") or "photo")
