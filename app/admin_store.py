from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any


DEFAULT_STATS_FIELDS = [
    "submitter",
    "customer",
    "invoice_number",
    "receipt_details",
    "slot_datetime",
    "payment_datetime",
    "payment_method",
    "paid_status",
    "job_cost_ex_vat",
    "job_cost_inc_vat",
]


def _clean_email(value: str) -> str:
    return (value or "").strip().lower()


def init_admin_store(db_path: str) -> None:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        conn.commit()


def _get_raw(db_path: str, key: str) -> str | None:
    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def _set_raw(db_path: str, key: str, value: str) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO settings (key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )
        conn.commit()


def get_json_setting(db_path: str, key: str, default: Any) -> Any:
    raw = _get_raw(db_path, key)
    if raw is None:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return default


def set_json_setting(db_path: str, key: str, value: Any) -> None:
    _set_raw(db_path, key, json.dumps(value))


def get_active_calendars(db_path: str, fallback_calendar: str) -> list[str]:
    value = get_json_setting(db_path, "active_calendars", [])
    if not value:
        return [fallback_calendar]
    return [str(v) for v in value if str(v).strip()]


def set_active_calendars(db_path: str, calendar_ids: list[str]) -> None:
    cleaned = [c.strip() for c in calendar_ids if c and c.strip()]
    set_json_setting(db_path, "active_calendars", cleaned)


def get_stats_fields(db_path: str) -> list[str]:
    value = get_json_setting(db_path, "stats_fields", DEFAULT_STATS_FIELDS)
    if not value:
        return []
    return [str(v) for v in value]


def set_stats_fields(db_path: str, fields: list[str]) -> None:
    set_json_setting(db_path, "stats_fields", fields)


def get_sheet_target(db_path: str) -> dict[str, str]:
    target = get_json_setting(
        db_path,
        "sheet_target",
        {"spreadsheet_id": "", "sheet_name": "Sheet1"},
    )
    return {
        "spreadsheet_id": str(target.get("spreadsheet_id", "")).strip(),
        "sheet_name": str(target.get("sheet_name", "Sheet1")).strip() or "Sheet1",
    }


def set_sheet_target(db_path: str, spreadsheet_id: str, sheet_name: str) -> None:
    set_json_setting(
        db_path,
        "sheet_target",
        {
            "spreadsheet_id": spreadsheet_id.strip(),
            "sheet_name": sheet_name.strip() or "Sheet1",
        },
    )


def get_submitter_aliases(db_path: str) -> dict[str, str]:
    raw = get_json_setting(db_path, "submitter_aliases", {})
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for k, v in raw.items():
        email = _clean_email(str(k))
        name = str(v).strip()
        if email and name:
            out[email] = name
    return out


def set_submitter_aliases(db_path: str, aliases: dict[str, str]) -> None:
    cleaned: dict[str, str] = {}
    for k, v in aliases.items():
        email = _clean_email(str(k))
        name = str(v).strip()
        if email and name:
            cleaned[email] = name
    set_json_setting(db_path, "submitter_aliases", cleaned)


def get_seen_submitters(db_path: str) -> list[str]:
    raw = get_json_setting(db_path, "seen_submitters", [])
    if not isinstance(raw, list):
        return []
    emails = {_clean_email(str(v)) for v in raw}
    return sorted([e for e in emails if e])


def add_seen_submitter(db_path: str, email: str) -> None:
    clean = _clean_email(email)
    if not clean:
        return
    seen = set(get_seen_submitters(db_path))
    if clean in seen:
        return
    seen.add(clean)
    set_json_setting(db_path, "seen_submitters", sorted(seen))


def get_google_watches(db_path: str) -> dict:
    return get_json_setting(db_path, "google_watches", {})


def set_google_watch(
    db_path: str,
    calendar_id: str,
    channel_id: str,
    resource_id: str,
    expiration_ms: int,
    webhook_url: str = "",
) -> None:
    watches = get_google_watches(db_path)
    watches[calendar_id] = {
        "channel_id": channel_id,
        "resource_id": resource_id,
        "expiration_ms": expiration_ms,
        "webhook_url": webhook_url,
    }
    set_json_setting(db_path, "google_watches", watches)


def delete_google_watch(db_path: str, calendar_id: str) -> None:
    watches = get_google_watches(db_path)
    watches.pop(calendar_id, None)
    set_json_setting(db_path, "google_watches", watches)


def get_xero_webhook_key(db_path: str) -> str:
    return str(get_json_setting(db_path, "xero_webhook_key", "") or "").strip()


def set_xero_webhook_key(db_path: str, key: str) -> None:
    set_json_setting(db_path, "xero_webhook_key", key.strip())
