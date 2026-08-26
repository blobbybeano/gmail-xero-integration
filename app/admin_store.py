from __future__ import annotations

import json
import os
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any


DEFAULT_STATS_FIELDS = [
    "slot_datetime",
    "diary_entry_name",
    "customer",
    "invoice_number",
    "payment_method",
    "receipt_details",
    "paid_status",
    "payment_datetime",
    "job_cost_ex_vat",
    "job_cost_inc_vat",
    "submitter",
]

DEFAULT_SALES_STATS_FIELDS = [
    "slot_datetime",
    "customer",
    "invoice_number",
    "payment_method",
    "sales_item_desc",
    "sales_total_ex_vat",
    "submitter",
]

_ALLOWED_SALES_STATS_FIELDS = {
    "submitter",
    "customer",
    "slot_datetime",
    "payment_method",
    "invoice_number",
    "sales_item_desc",
    "sales_total_ex_vat",
}


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


def get_cashflows_correlation_sheet_id(db_path: str) -> str:
    """Public Google Sheet ID used to distinguish CARD from INVOICE payments."""
    return str(get_json_setting(db_path, "cashflows_correlation_sheet_id", "")).strip()


def set_cashflows_correlation_sheet_id(db_path: str, sheet_id: str) -> None:
    set_json_setting(db_path, "cashflows_correlation_sheet_id", sheet_id.strip())


def get_cashflows_reconciled_refs(db_path: str) -> dict[str, Any]:
    """Map of Cashflows payout csv_ref -> {date, amount, reconciled_at}.

    Secondary dedup guard for CSV reconciliation: payouts this app has already
    reconciled are skipped on later uploads. Populated by Phase 2 (writes); read
    by Phase 1 preview so re-uploaded files ignore already-done batches.
    """
    value = get_json_setting(db_path, "cashflows_csv_reconciled", {})
    return value if isinstance(value, dict) else {}


def add_cashflows_reconciled_refs(db_path: str, refs: dict[str, Any]) -> None:
    existing = get_cashflows_reconciled_refs(db_path)
    existing.update(refs)
    set_json_setting(db_path, "cashflows_csv_reconciled", existing)


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
    fields = [str(v) for v in value]
    if "diary_entry_name" not in fields:
        insert_at = 1 if "slot_datetime" in fields else 0
        fields.insert(insert_at, "diary_entry_name")
    return fields


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


def get_sales_sheet_target(db_path: str) -> dict[str, str]:
    target = get_json_setting(
        db_path,
        "sales_sheet_target",
        {"spreadsheet_id": "", "sheet_name": "Sales"},
    )
    return {
        "spreadsheet_id": str(target.get("spreadsheet_id", "")).strip(),
        "sheet_name": str(target.get("sheet_name", "Sales")).strip() or "Sales",
    }


def get_cash_sheet_target(db_path: str) -> dict[str, str]:
    target = get_json_setting(
        db_path,
        "cash_sheet_target",
        {"spreadsheet_id": "", "sheet_name": "Cash"},
    )
    return {
        "spreadsheet_id": str(target.get("spreadsheet_id", "")).strip(),
        "sheet_name": str(target.get("sheet_name", "Cash")).strip() or "Cash",
    }


def set_sales_sheet_target(db_path: str, spreadsheet_id: str, sheet_name: str) -> None:
    set_json_setting(
        db_path,
        "sales_sheet_target",
        {
            "spreadsheet_id": spreadsheet_id.strip(),
            "sheet_name": sheet_name.strip() or "Sales",
        },
    )


def set_cash_sheet_target(db_path: str, spreadsheet_id: str, sheet_name: str) -> None:
    set_json_setting(
        db_path,
        "cash_sheet_target",
        {
            "spreadsheet_id": spreadsheet_id.strip(),
            "sheet_name": sheet_name.strip() or "Cash",
        },
    )


def get_sales_stats_fields(db_path: str) -> list[str]:
    value = get_json_setting(db_path, "sales_stats_fields", DEFAULT_SALES_STATS_FIELDS)
    if not value:
        return []
    cleaned: list[str] = []
    for v in value:
        key = str(v)
        if key in _ALLOWED_SALES_STATS_FIELDS and key not in cleaned:
            cleaned.append(key)
    return cleaned


def set_sales_stats_fields(db_path: str, fields: list[str]) -> None:
    cleaned: list[str] = []
    for field in fields:
        key = str(field)
        if key in _ALLOWED_SALES_STATS_FIELDS and key not in cleaned:
            cleaned.append(key)
    if not cleaned:
        cleaned = list(DEFAULT_SALES_STATS_FIELDS)
    set_json_setting(db_path, "sales_stats_fields", cleaned)


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


def get_xero_webhook_verified(db_path: str) -> bool:
    return bool(get_json_setting(db_path, "xero_webhook_verified", False))


def set_xero_webhook_verified(db_path: str, verified: bool) -> None:
    set_json_setting(db_path, "xero_webhook_verified", verified)


def get_enabled(db_path: str) -> bool:
    # Default is False — Calendar→Xero sync must be explicitly turned ON in
    # Live View.  This prevents accidental invoice creation when Xero is first
    # connected.  Field Expenses, Email Invoices, and Cashflows work
    # independently of this toggle.
    return bool(get_json_setting(db_path, "system_enabled", False))


def set_enabled(db_path: str, enabled: bool) -> None:
    set_json_setting(db_path, "system_enabled", enabled)


def get_receipts_settings(db_path: str) -> dict[str, Any]:
    raw = get_json_setting(
        db_path,
        "receipts_settings",
        {
            "enabled": False,
            "document_ai_project_id": "",
            "document_ai_location": "us",
            "document_ai_processor_id": "",
            "document_ai_project_name": "",
            "google_service_account_file": "",
            "retention_days": 2,
            "sheet_spreadsheet_id": "",
            "sheet_name": "Receipt_Reconciliation",
        },
    )
    if not isinstance(raw, dict):
        raw = {}
    sa_file = str(raw.get("google_service_account_file", "")).strip()
    return {
        "enabled": bool(raw.get("enabled", False)),
        "document_ai_project_id": str(raw.get("document_ai_project_id", "")).strip(),
        "document_ai_location": str(raw.get("document_ai_location", "us")).strip() or "us",
        "document_ai_processor_id": str(raw.get("document_ai_processor_id", "")).strip(),
        "document_ai_project_name": str(raw.get("document_ai_project_name", "")).strip(),
        "google_service_account_file": sa_file,
        "retention_days": max(int(raw.get("retention_days", 2) or 2), 1),
        "sheet_spreadsheet_id": str(raw.get("sheet_spreadsheet_id", "")).strip(),
        "sheet_name": str(raw.get("sheet_name", "Receipt_Reconciliation")).strip() or "Receipt_Reconciliation",
    }


def get_expense_settings(db_path: str) -> dict[str, Any]:
    """Settings for the Field Expenses feature (separate from receipts_settings)."""
    raw = get_json_setting(
        db_path,
        "expense_settings",
        {
            "default_expense_account": "",
            "default_payment_account": "",
            "vat_rate": 20.0,
            "xero_submission_mode": "scheduled",
            "xero_submission_time": "17:00",
            "bank_feed_reminder_day": 0,
        },
    )
    if not isinstance(raw, dict):
        raw = {}
    try:
        vat_rate = float(raw.get("vat_rate", 20.0))
    except (TypeError, ValueError):
        vat_rate = 20.0
    if vat_rate < 0 or vat_rate > 100:
        vat_rate = 20.0
    mode = str(raw.get("xero_submission_mode", "scheduled")).strip().lower()
    if mode not in {"scheduled", "manual", "immediate"}:
        mode = "scheduled"
    submit_time = _clean_hhmm(raw.get("xero_submission_time"), "17:00")
    try:
        reminder_day = int(raw.get("bank_feed_reminder_day", 0))
    except (TypeError, ValueError):
        reminder_day = 0
    if reminder_day < 0 or reminder_day > 6:
        reminder_day = 0
    return {
        "default_expense_account": str(raw.get("default_expense_account", "")).strip(),
        "default_payment_account": str(raw.get("default_payment_account", "")).strip(),
        "vat_rate": vat_rate,
        "xero_submission_mode": mode,
        "xero_submission_time": submit_time,
        "bank_feed_reminder_day": reminder_day,
    }


def set_expense_settings(db_path: str, settings: dict[str, Any]) -> None:
    current = get_expense_settings(db_path)
    current.update(
        {k: v for k, v in (settings or {}).items() if k in current}
    )
    mode = str(current.get("xero_submission_mode", "scheduled")).strip().lower()
    if mode not in {"scheduled", "manual", "immediate"}:
        mode = "scheduled"
    current["xero_submission_mode"] = mode
    current["xero_submission_time"] = _clean_hhmm(
        current.get("xero_submission_time"), "17:00"
    )
    try:
        reminder_day = int(current.get("bank_feed_reminder_day", 0))
    except (TypeError, ValueError):
        reminder_day = 0
    if reminder_day < 0 or reminder_day > 6:
        reminder_day = 0
    current["bank_feed_reminder_day"] = reminder_day
    set_json_setting(db_path, "expense_settings", current)


def _clean_hhmm(value: Any, default: str) -> str:
    text = str(value or "").strip()
    parts = text.split(":", 1)
    if len(parts) != 2:
        return default
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except (TypeError, ValueError):
        return default
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        return default
    return f"{hour:02d}:{minute:02d}"


def set_receipts_settings(db_path: str, settings: dict[str, Any]) -> None:
    sa_file = str(settings.get("google_service_account_file", "")).strip()
    cleaned = {
        "enabled": bool(settings.get("enabled", False)),
        "document_ai_project_id": str(settings.get("document_ai_project_id", "")).strip(),
        "document_ai_location": str(settings.get("document_ai_location", "us")).strip() or "us",
        "document_ai_processor_id": str(settings.get("document_ai_processor_id", "")).strip(),
        "document_ai_project_name": str(settings.get("document_ai_project_name", "")).strip(),
        "google_service_account_file": sa_file,
        "retention_days": max(int(settings.get("retention_days", 2) or 2), 1),
        "sheet_spreadsheet_id": str(settings.get("sheet_spreadsheet_id", "")).strip(),
        "sheet_name": str(settings.get("sheet_name", "Receipt_Reconciliation")).strip() or "Receipt_Reconciliation",
    }
    set_json_setting(db_path, "receipts_settings", cleaned)


def get_cashflows_settings(db_path: str) -> dict[str, Any]:
    raw = get_json_setting(
        db_path,
        "cashflows_settings",
        {
            "enabled": False,
            "environment": "integration",
            "base_url": "https://gateway-int.cashflows.com/api/gateway",
            "configuration_id": "",
            "api_key": "",
            "timeout_seconds": 15,
            "settlements_action": "GetSettlementPayouts",
        },
    )
    if not isinstance(raw, dict):
        raw = {}
    env = str(raw.get("environment", "integration")).strip().lower()
    if env not in {"integration", "production"}:
        env = "integration"
    return {
        "enabled": bool(raw.get("enabled", False)),
        "environment": env,
        "base_url": str(raw.get("base_url", "")).strip(),
        "configuration_id": str(raw.get("configuration_id", "")).strip(),
        "api_key": str(raw.get("api_key", "")).strip(),
        "timeout_seconds": max(int(raw.get("timeout_seconds", 15) or 15), 5),
        "settlements_action": str(
            raw.get("settlements_action")
            or os.getenv("CASHFLOWS_SETTLEMENTS_ACTION")
            or "GetSettlementPayouts"
        ).strip()
        or "GetSettlementPayouts",
    }


def set_cashflows_settings(db_path: str, settings: dict[str, Any]) -> None:
    env = str(settings.get("environment", "integration")).strip().lower()
    if env not in {"integration", "production"}:
        env = "integration"
    default_base = (
        "https://gateway-int.cashflows.com/api/gateway"
        if env == "integration"
        else "https://gateway.cashflows.com/api/gateway"
    )
    cleaned = {
        "enabled": bool(settings.get("enabled", False)),
        "environment": env,
        "base_url": str(settings.get("base_url", "")).strip() or default_base,
        "configuration_id": str(settings.get("configuration_id", "")).strip(),
        "api_key": str(settings.get("api_key", "")).strip(),
        "timeout_seconds": max(int(settings.get("timeout_seconds", 15) or 15), 5),
        "settlements_action": str(
            settings.get("settlements_action")
            or os.getenv("CASHFLOWS_SETTLEMENTS_ACTION")
            or "GetSettlementPayouts"
        ).strip()
        or "GetSettlementPayouts",
    }
    set_json_setting(db_path, "cashflows_settings", cleaned)


def get_openai_settings(db_path: str) -> dict[str, Any]:
    raw = get_json_setting(db_path, "openai_settings", {})
    if not isinstance(raw, dict):
        raw = {}
    return {
        "api_key": str(raw.get("api_key", "")).strip(),
        "model": str(raw.get("model", "")).strip() or "gpt-4o-mini",
    }


def set_openai_settings(db_path: str, settings: dict[str, Any]) -> None:
    cleaned = {
        "api_key": str(settings.get("api_key", "")).strip(),
        "model": str(settings.get("model", "")).strip() or "gpt-4o-mini",
    }
    set_json_setting(db_path, "openai_settings", cleaned)


# ---------------------------------------------------------------------------
# Field Expenses — live parser test sessions
#
# A test session lets an admin generate a QR code; a tester opens it on their
# phone, photographs a real receipt, and the app shows what the parser
# extracted and which Xero account it WOULD choose — submitting NOTHING.
# Sessions are short-lived and stored per-token so they survive across worker
# processes (autoscale) without an in-memory store.
# ---------------------------------------------------------------------------

_EXPENSE_TEST_TTL = 1800  # seconds (30 minutes)
_EXPENSE_TEST_PREFIX = "expense_test_session:"


def _expense_test_prune(db_path: str, now: int) -> None:
    """Best-effort removal of expired test sessions to keep the table tidy."""
    try:
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                "SELECT key, value FROM settings WHERE key LIKE ?",
                (_EXPENSE_TEST_PREFIX + "%",),
            ).fetchall()
            stale: list[str] = []
            for key, raw in rows:
                try:
                    created = int((json.loads(raw) or {}).get("created_at", 0))
                except Exception:
                    created = 0
                if now - created > _EXPENSE_TEST_TTL:
                    stale.append(key)
            for key in stale:
                conn.execute("DELETE FROM settings WHERE key = ?", (key,))
            if stale:
                conn.commit()
    except Exception:
        pass


def create_expense_test_session(
    db_path: str, *, engineer_id: int | None = None
) -> str:
    """Create a new test session and return its opaque token."""
    now = int(time.time())
    _expense_test_prune(db_path, now)
    token = secrets.token_urlsafe(9)
    while _get_raw(db_path, _EXPENSE_TEST_PREFIX + token) is not None:
        token = secrets.token_urlsafe(9)
    set_json_setting(
        db_path,
        _EXPENSE_TEST_PREFIX + token,
        {
            "created_at": now,
            "engineer_id": int(engineer_id) if engineer_id else None,
            "status": "waiting",
            "result": None,
        },
    )
    return token


def get_expense_test_session(db_path: str, token: str) -> dict[str, Any] | None:
    """Return the session dict, or None if missing/expired."""
    token = (token or "").strip()
    if not token:
        return None
    raw = get_json_setting(db_path, _EXPENSE_TEST_PREFIX + token, None)
    if not isinstance(raw, dict):
        return None
    if int(time.time()) - int(raw.get("created_at", 0)) > _EXPENSE_TEST_TTL:
        return None
    return raw


def set_expense_test_result(
    db_path: str, token: str, *, status: str, result: dict[str, Any] | None
) -> bool:
    """Update a session's status/result. Returns False if it has expired."""
    session = get_expense_test_session(db_path, token)
    if session is None:
        return False
    session["status"] = status
    session["result"] = result
    session["updated_at"] = int(time.time())
    set_json_setting(db_path, _EXPENSE_TEST_PREFIX + token.strip(), session)
    return True


def get_xero_tenants(db_path: str) -> list[dict]:
    """Return list of per-tenant configs: {tenantId, tenantName, enabled, invoiceAccount, paymentAccount}"""
    raw = get_json_setting(db_path, "xero_tenants", [])
    if not isinstance(raw, list):
        return []
    return raw


def get_cash_submitter_sheets(db_path: str) -> dict[str, dict[str, str]]:
    """
    Per-submitter cash sheet routing.
    Shape:
      {
        "<submitter_email>": {"spreadsheet_id": "...", "sheet_name": "..."},
        ...
      }
    """
    raw = get_json_setting(db_path, "cash_submitter_sheets", {})
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict[str, str]] = {}
    for email, cfg in raw.items():
        clean_email = _clean_email(str(email))
        if not clean_email or not isinstance(cfg, dict):
            continue
        sid = str(cfg.get("spreadsheet_id", "")).strip()
        sname = str(cfg.get("sheet_name", "Sheet1")).strip() or "Sheet1"
        out[clean_email] = {
            "spreadsheet_id": sid,
            "sheet_name": sname,
        }
    return out


def set_cash_submitter_sheets(
    db_path: str,
    mapping: dict[str, dict[str, str]],
) -> None:
    cleaned: dict[str, dict[str, str]] = {}
    for email, cfg in (mapping or {}).items():
        clean_email = _clean_email(str(email))
        if not clean_email or not isinstance(cfg, dict):
            continue
        sid = str(cfg.get("spreadsheet_id", "")).strip()
        sname = str(cfg.get("sheet_name", "Sheet1")).strip() or "Sheet1"
        cleaned[clean_email] = {
            "spreadsheet_id": sid,
            "sheet_name": sname,
        }
    set_json_setting(db_path, "cash_submitter_sheets", cleaned)


def get_cash_backlog(db_path: str) -> list[dict]:
    """
    Pending cash entries waiting for submitter-specific sheet routing.
    """
    raw = get_json_setting(db_path, "cash_backlog", [])
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for row in raw:
        if isinstance(row, dict):
            out.append(row)
    return out


def set_cash_backlog(db_path: str, rows: list[dict]) -> None:
    safe_rows = [r for r in (rows or []) if isinstance(r, dict)]
    set_json_setting(db_path, "cash_backlog", safe_rows)


def get_sheet_backlog(db_path: str) -> list[dict]:
    """
    Pending universal sheet rows waiting for sheet routing or transient retries.
    """
    raw = get_json_setting(db_path, "sheet_backlog", [])
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for row in raw:
        if isinstance(row, dict):
            out.append(row)
    return out


def set_sheet_backlog(db_path: str, rows: list[dict]) -> None:
    safe_rows = [r for r in (rows or []) if isinstance(r, dict)]
    set_json_setting(db_path, "sheet_backlog", safe_rows)


def get_sales_submitter_sheets(db_path: str) -> dict[str, dict[str, str]]:
    """
    Per-submitter sales sheet routing.
    Shape:
      {
        "<submitter_email>": {"spreadsheet_id": "...", "sheet_name": "..."},
        ...
      }
    """
    raw = get_json_setting(db_path, "sales_submitter_sheets", {})
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict[str, str]] = {}
    for email, cfg in raw.items():
        clean_email = _clean_email(str(email))
        if not clean_email or not isinstance(cfg, dict):
            continue
        sid = str(cfg.get("spreadsheet_id", "")).strip()
        sname = str(cfg.get("sheet_name", "Sales")).strip() or "Sales"
        out[clean_email] = {
            "spreadsheet_id": sid,
            "sheet_name": sname,
        }
    return out


def set_sales_submitter_sheets(
    db_path: str,
    mapping: dict[str, dict[str, str]],
) -> None:
    cleaned: dict[str, dict[str, str]] = {}
    for email, cfg in (mapping or {}).items():
        clean_email = _clean_email(str(email))
        if not clean_email or not isinstance(cfg, dict):
            continue
        sid = str(cfg.get("spreadsheet_id", "")).strip()
        sname = str(cfg.get("sheet_name", "Sales")).strip() or "Sales"
        cleaned[clean_email] = {
            "spreadsheet_id": sid,
            "sheet_name": sname,
        }
    set_json_setting(db_path, "sales_submitter_sheets", cleaned)


def get_sales_backlog(db_path: str) -> list[dict]:
    """
    Pending sales entries waiting for submitter-specific sheet routing.
    """
    raw = get_json_setting(db_path, "sales_backlog", [])
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for row in raw:
        if isinstance(row, dict):
            out.append(row)
    return out


def set_sales_backlog(db_path: str, rows: list[dict]) -> None:
    safe_rows = [r for r in (rows or []) if isinstance(r, dict)]
    set_json_setting(db_path, "sales_backlog", safe_rows)


def get_calendar_sales_sheets(db_path: str) -> dict[str, dict[str, str]]:
    """
    Per-calendar sales sheet routing.
    Shape:
      {
        "<calendar_id>": {"spreadsheet_id": "...", "sheet_name": "..."},
        ...
      }
    """
    raw = get_json_setting(db_path, "calendar_sales_sheets", {})
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict[str, str]] = {}
    for cal_id, cfg in raw.items():
        cid = str(cal_id).strip()
        if not cid or not isinstance(cfg, dict):
            continue
        sid = str(cfg.get("spreadsheet_id", "")).strip()
        sname = str(cfg.get("sheet_name", "Sales")).strip() or "Sales"
        out[cid] = {"spreadsheet_id": sid, "sheet_name": sname}
    return out


def set_calendar_sales_sheets(db_path: str, mapping: dict[str, dict[str, str]]) -> None:
    cleaned: dict[str, dict[str, str]] = {}
    for cal_id, cfg in (mapping or {}).items():
        cid = str(cal_id).strip()
        if not cid or not isinstance(cfg, dict):
            continue
        sid = str(cfg.get("spreadsheet_id", "")).strip()
        sname = str(cfg.get("sheet_name", "Sales")).strip() or "Sales"
        cleaned[cid] = {"spreadsheet_id": sid, "sheet_name": sname}
    set_json_setting(db_path, "calendar_sales_sheets", cleaned)


def get_calendar_cash_sheets(db_path: str) -> dict[str, dict[str, str]]:
    """
    Per-calendar cash sheet routing.
    Shape:
      {
        "<calendar_id>": {"spreadsheet_id": "...", "sheet_name": "..."},
        ...
      }
    """
    raw = get_json_setting(db_path, "calendar_cash_sheets", {})
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict[str, str]] = {}
    for cal_id, cfg in raw.items():
        cid = str(cal_id).strip()
        if not cid or not isinstance(cfg, dict):
            continue
        sid = str(cfg.get("spreadsheet_id", "")).strip()
        sname = str(cfg.get("sheet_name", "Sheet1")).strip() or "Sheet1"
        out[cid] = {"spreadsheet_id": sid, "sheet_name": sname}
    return out


def set_calendar_cash_sheets(db_path: str, mapping: dict[str, dict[str, str]]) -> None:
    cleaned: dict[str, dict[str, str]] = {}
    for cal_id, cfg in (mapping or {}).items():
        cid = str(cal_id).strip()
        if not cid or not isinstance(cfg, dict):
            continue
        sid = str(cfg.get("spreadsheet_id", "")).strip()
        sname = str(cfg.get("sheet_name", "Sheet1")).strip() or "Sheet1"
        cleaned[cid] = {"spreadsheet_id": sid, "sheet_name": sname}
    set_json_setting(db_path, "calendar_cash_sheets", cleaned)


def set_xero_tenants(db_path: str, tenants: list[dict]) -> None:
    set_json_setting(db_path, "xero_tenants", tenants)


def upsert_xero_tenant(
    db_path: str,
    tenant_id: str,
    tenant_name: str = "",
    enabled: bool | None = None,
    invoice_account: str | None = None,
    payment_account: str | None = None,
    branding_theme_id: str | None = None,
    premium_theme_id: str | None = None,
    premium_threshold: float | None = None,
) -> None:
    """Create or update a single tenant's config without touching other tenants."""
    tenants = get_xero_tenants(db_path)
    for t in tenants:
        if t.get("tenantId") == tenant_id:
            if tenant_name:
                t["tenantName"] = tenant_name
            if enabled is not None:
                t["enabled"] = enabled
            if invoice_account is not None:
                t["invoiceAccount"] = invoice_account
            if payment_account is not None:
                t["paymentAccount"] = payment_account
            if branding_theme_id is not None:
                t["brandingThemeId"] = branding_theme_id
            if premium_theme_id is not None:
                t["premiumThemeId"] = premium_theme_id
            if premium_threshold is not None:
                t["premiumThreshold"] = premium_threshold
            set_xero_tenants(db_path, tenants)
            return
    entry: dict = {
        "tenantId": tenant_id,
        "tenantName": tenant_name,
        "enabled": True if enabled is None else enabled,
        "invoiceAccount": invoice_account or "",
        "paymentAccount": payment_account or "",
        "brandingThemeId": branding_theme_id or "",
        "premiumThemeId": premium_theme_id or "",
        "premiumThreshold": premium_threshold,
    }
    tenants.append(entry)
    set_xero_tenants(db_path, tenants)
