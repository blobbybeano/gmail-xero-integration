from __future__ import annotations

import time
from typing import Any

from .admin_store import get_json_setting, set_json_setting

_KEY = "xero_busy_gate"
_DEFAULT_TTL_SECONDS = 15 * 60


def mark_xero_busy(
    admin_db_file: str,
    *,
    owner: str,
    reason: str,
    ttl_seconds: int = _DEFAULT_TTL_SECONDS,
) -> None:
    until_ts = time.time() + max(60, int(ttl_seconds or _DEFAULT_TTL_SECONDS))
    set_json_setting(
        admin_db_file,
        _KEY,
        {
            "owner": str(owner or "unknown"),
            "reason": str(reason or "Xero work in progress"),
            "until_ts": until_ts,
            "updated_at_ts": time.time(),
        },
    )


def clear_xero_busy(admin_db_file: str, *, owner: str = "") -> None:
    current = get_json_setting(admin_db_file, _KEY, {}) or {}
    if owner and str(current.get("owner") or "") not in {"", owner}:
        return
    set_json_setting(admin_db_file, _KEY, {})


def xero_busy_status(admin_db_file: str) -> dict[str, Any]:
    current = get_json_setting(admin_db_file, _KEY, {}) or {}
    try:
        until_ts = float(current.get("until_ts") or 0.0)
    except Exception:
        until_ts = 0.0
    active = until_ts > time.time()
    return {
        "active": active,
        "owner": str(current.get("owner") or ""),
        "reason": str(current.get("reason") or ""),
        "until_ts": until_ts if active else 0.0,
        "seconds_left": max(0, int(until_ts - time.time())) if active else 0,
    }
