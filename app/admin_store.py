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

DEFAULT_SALES_STATS_FIELDS = [
    "submitter",
    "customer",
    "slot_datetime",
    "payment_method",
    "invoice_number",
    "sales_item_desc",
    "sales_item_ex_vat",
    "sales_item_inc_vat",
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


def set_sales_sheet_target(db_path: str, spreadsheet_id: str, sheet_name: str) -> None:
    set_json_setting(
        db_path,
        "sales_sheet_target",
        {
            "spreadsheet_id": spreadsheet_id.strip(),
            "sheet_name": sheet_name.strip() or "Sales",
        },
    )


def get_sales_stats_fields(db_path: str) -> list[str]:
    value = get_json_setting(db_path, "sales_stats_fields", DEFAULT_SALES_STATS_FIELDS)
    if not value:
        return []
    return [str(v) for v in value]


def set_sales_stats_fields(db_path: str, fields: list[str]) -> None:
    set_json_setting(db_path, "sales_stats_fields", fields)


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
    return bool(get_json_setting(db_path, "system_enabled", True))


def set_enabled(db_path: str, enabled: bool) -> None:
    set_json_setting(db_path, "system_enabled", enabled)


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
