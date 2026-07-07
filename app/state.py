from __future__ import annotations

import datetime as dt
import json
import time
from pathlib import Path
from typing import Dict


def load_state(state_file: str) -> Dict:
    path = Path(state_file)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def save_state(state_file: str, state: Dict) -> None:
    path = Path(state_file)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(path)


def _max_iso(left: str | None, right: str | None) -> str:
    left_s = str(left or "")
    right_s = str(right or "")
    return max(left_s, right_s)


def _max_number(left, right):
    try:
        return max(float(left or 0), float(right or 0))
    except Exception:
        return right if right not in (None, "") else left


def merge_state_for_save(latest: Dict, incoming: Dict) -> Dict:
    """
    Merge a worker's in-memory state with the latest on-disk state.

    The poller and webhook can run at the same time. Final facts from either
    side, especially paid/sent invoice state, must survive whichever save
    happens last.
    """
    merged = dict(latest or {})
    merged.update(incoming or {})

    union_list_fields = [
        "processed_event_ids",
        "prefilled_event_ids",
        "invoice_sent_event_ids",
        "invoice_paid_event_ids",
    ]
    for field in union_list_fields:
        values = set()
        for source in (latest, incoming):
            raw = (source or {}).get(field)
            if isinstance(raw, list):
                values.update(str(item) for item in raw if str(item))
        if values:
            merged[field] = sorted(values)

    newest_timestamp_maps = [
        "event_contact_updates",
        "event_processed_updates",
        "event_invoice_updates",
    ]
    for field in newest_timestamp_maps:
        out = {}
        for source in (latest, incoming):
            raw = (source or {}).get(field)
            if isinstance(raw, dict):
                for key, value in raw.items():
                    out[str(key)] = _max_iso(out.get(str(key)), str(value or ""))
        if out:
            merged[field] = out

    max_number_maps = [
        "event_draft_sync_attempted_at",
        "event_xero_retry_after",
        "event_xero_retry_backoff",
        "event_xero_action_attempts",
    ]
    for field in max_number_maps:
        out = {}
        for source in (latest, incoming):
            raw = (source or {}).get(field)
            if isinstance(raw, dict):
                for key, value in raw.items():
                    out[str(key)] = _max_number(out.get(str(key)), value)
        if out:
            merged[field] = out

    max_value_fields = [
        "xero_lockout_until_ts",
        "xero_lockout_updated_at_ts",
        "xero_pressure_last_notice_ts",
    ]
    for field in max_value_fields:
        if field in latest or field in incoming:
            merged[field] = _max_number((latest or {}).get(field), (incoming or {}).get(field))

    additive_maps = [
        "event_contact_map",
        "event_contact_fingerprints",
        "event_draft_sync_fingerprints",
        "draft_cleanup_queue",
        "event_sheet_log_updates",
        "event_sales_log_updates",
        "event_cash_log_updates",
        "event_cash_global_log_updates",
        "recent_xero_webhook_events",
        "deferred_xero_event_targets",
    ]
    for field in additive_maps:
        out = {}
        latest_raw = (latest or {}).get(field)
        incoming_raw = (incoming or {}).get(field)
        if isinstance(latest_raw, dict):
            out.update(latest_raw)
        if isinstance(incoming_raw, dict):
            out.update(incoming_raw)
        if out:
            merged[field] = out

    # Invoice mappings are additive unless the incoming state deliberately
    # clears a specific mapping by setting it to an empty string.
    invoice_map = {}
    latest_invoice_map = (latest or {}).get("event_invoice_map")
    incoming_invoice_map = (incoming or {}).get("event_invoice_map")
    if isinstance(latest_invoice_map, dict):
        invoice_map.update(latest_invoice_map)
    if isinstance(incoming_invoice_map, dict):
        for key, value in incoming_invoice_map.items():
            invoice_map[str(key)] = value
    if invoice_map:
        merged["event_invoice_map"] = invoice_map

    return merged


def save_state_merged(
    state_file: str,
    state: Dict,
    *,
    prune_keep_recent_events: int | None = None,
) -> Dict:
    latest = load_state(state_file)
    merged = merge_state_for_save(latest, state)
    if prune_keep_recent_events is not None:
        merged = prune_state(merged, keep_recent_events=prune_keep_recent_events)
    save_state(state_file, merged)
    return merged


def get_last_sync(state: Dict) -> dt.datetime:
    raw = state.get("last_sync")
    if not raw:
        # default to 7 days ago to avoid missing recent items
        return dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=7)
    return dt.datetime.fromisoformat(raw)


def set_last_sync(state: Dict, when: dt.datetime) -> Dict:
    state["last_sync"] = when.isoformat()
    return state


def mark_processed(state: Dict, event_id: str) -> Dict:
    processed = set(state.get("processed_event_ids", []))
    processed.add(event_id)
    state["processed_event_ids"] = sorted(processed)
    return state


def mark_prefilled(state: Dict, event_id: str) -> Dict:
    prefills = set(state.get("prefilled_event_ids", []))
    prefills.add(event_id)
    state["prefilled_event_ids"] = sorted(prefills)
    return state


def is_prefilled(state: Dict, event_id: str) -> bool:
    return event_id in set(state.get("prefilled_event_ids", []))


def set_contact_for_event(state: Dict, event_id: str, contact_id: str) -> Dict:
    mapping = state.get("event_contact_map", {})
    mapping[event_id] = contact_id
    state["event_contact_map"] = mapping
    return state


def get_contact_for_event(state: Dict, event_id: str) -> str | None:
    return state.get("event_contact_map", {}).get(event_id)


def get_contact_update_marker(state: Dict, event_id: str) -> str | None:
    return state.get("event_contact_updates", {}).get(event_id)


def set_contact_update_marker(state: Dict, event_id: str, updated_at: str) -> Dict:
    mapping = state.get("event_contact_updates", {})
    mapping[event_id] = updated_at
    state["event_contact_updates"] = mapping
    return state


def get_contact_fingerprint(state: Dict, event_id: str) -> str | None:
    return state.get("event_contact_fingerprints", {}).get(event_id)


def set_contact_fingerprint(state: Dict, event_id: str, fingerprint: str) -> Dict:
    mapping = state.get("event_contact_fingerprints", {})
    mapping[event_id] = fingerprint
    state["event_contact_fingerprints"] = mapping
    return state


def is_processed(state: Dict, event_id: str) -> bool:
    if event_id in state.get("event_processed_updates", {}):
        return True
    return event_id in set(state.get("processed_event_ids", []))


def get_processed_update_marker(state: Dict, event_id: str) -> str | None:
    return state.get("event_processed_updates", {}).get(event_id)


def set_processed_update_marker(state: Dict, event_id: str, updated_at: str) -> Dict:
    mapping = state.get("event_processed_updates", {})
    mapping[event_id] = updated_at
    state["event_processed_updates"] = mapping
    return state


def mark_invoice_sent(state: Dict, event_id: str) -> Dict:
    sent = set(state.get("invoice_sent_event_ids", []))
    sent.add(event_id)
    state["invoice_sent_event_ids"] = sorted(sent)
    return state


def is_invoice_sent(state: Dict, event_id: str) -> bool:
    return event_id in set(state.get("invoice_sent_event_ids", []))


def mark_invoice_paid(state: Dict, event_id: str) -> Dict:
    paid = set(state.get("invoice_paid_event_ids", []))
    paid.add(event_id)
    state["invoice_paid_event_ids"] = sorted(paid)
    return state


def is_invoice_paid(state: Dict, event_id: str) -> bool:
    return event_id in set(state.get("invoice_paid_event_ids", []))


def mark_recent_xero_webhook(
    state: Dict,
    event_id: str,
    invoice_id: str,
    *,
    when_ts: float | None = None,
) -> Dict:
    if not event_id:
        return state
    mapping = state.get("recent_xero_webhook_events", {})
    mapping[event_id] = {
        "invoice_id": str(invoice_id or ""),
        "handled_at_ts": float(when_ts if when_ts is not None else time.time()),
    }
    state["recent_xero_webhook_events"] = mapping
    return state


def was_recent_xero_webhook(
    state: Dict,
    event_id: str,
    invoice_id: str,
    *,
    now_ts: float | None = None,
    within_seconds: int = 600,
) -> bool:
    row = (state.get("recent_xero_webhook_events", {}) or {}).get(event_id)
    if not isinstance(row, dict):
        return False
    if invoice_id and str(row.get("invoice_id") or "") not in {"", str(invoice_id)}:
        return False
    try:
        handled_at = float(row.get("handled_at_ts") or 0.0)
    except Exception:
        return False
    return handled_at > 0 and (float(now_ts if now_ts is not None else time.time()) - handled_at) <= within_seconds


def set_invoice_for_event(state: Dict, event_id: str, invoice_id: str) -> Dict:
    mapping = state.get("event_invoice_map", {})
    mapping[event_id] = invoice_id
    state["event_invoice_map"] = mapping
    return state


def get_invoice_for_event(state: Dict, event_id: str) -> str | None:
    return state.get("event_invoice_map", {}).get(event_id)


def get_invoice_update_marker(state: Dict, event_id: str) -> str | None:
    return state.get("event_invoice_updates", {}).get(event_id)


def set_invoice_update_marker(state: Dict, event_id: str, updated_at: str) -> Dict:
    mapping = state.get("event_invoice_updates", {})
    mapping[event_id] = updated_at
    state["event_invoice_updates"] = mapping
    return state


def get_draft_sync_fingerprint(state: Dict, event_id: str) -> str | None:
    return state.get("event_draft_sync_fingerprints", {}).get(event_id)


def set_draft_sync_fingerprint(state: Dict, event_id: str, fingerprint: str) -> Dict:
    mapping = state.get("event_draft_sync_fingerprints", {})
    mapping[event_id] = fingerprint
    state["event_draft_sync_fingerprints"] = mapping
    return state


def get_draft_sync_attempted_at(state: Dict, event_id: str) -> float | None:
    raw = (state.get("event_draft_sync_attempted_at", {}) or {}).get(event_id)
    try:
        return float(raw)
    except Exception:
        return None


def set_draft_sync_attempted_at(state: Dict, event_id: str, when_ts: float) -> Dict:
    mapping = state.get("event_draft_sync_attempted_at", {})
    mapping[event_id] = float(when_ts)
    state["event_draft_sync_attempted_at"] = mapping
    return state


def get_xero_action_attempts(
    state: Dict,
    event_id: str,
    action: str,
    fingerprint: str,
) -> int:
    key = f"{event_id}|{action}|{fingerprint}"
    try:
        return int((state.get("event_xero_action_attempts", {}) or {}).get(key) or 0)
    except Exception:
        return 0


def bump_xero_action_attempts(
    state: Dict,
    event_id: str,
    action: str,
    fingerprint: str,
) -> tuple[Dict, int]:
    key = f"{event_id}|{action}|{fingerprint}"
    mapping = state.get("event_xero_action_attempts", {})
    try:
        attempts = int(mapping.get(key) or 0) + 1
    except Exception:
        attempts = 1
    mapping[key] = attempts
    state["event_xero_action_attempts"] = mapping
    return state, attempts


def clear_xero_action_attempts(state: Dict, event_id: str) -> Dict:
    mapping = state.get("event_xero_action_attempts", {})
    if isinstance(mapping, dict):
        prefix = f"{event_id}|"
        state["event_xero_action_attempts"] = {
            k: v for k, v in mapping.items() if not str(k).startswith(prefix)
        }
    return state


def get_sheet_log_marker(state: Dict, event_id: str) -> str | None:
    return state.get("event_sheet_log_updates", {}).get(event_id)


def set_sheet_log_marker(state: Dict, event_id: str, marker: str) -> Dict:
    mapping = state.get("event_sheet_log_updates", {})
    mapping[event_id] = marker
    state["event_sheet_log_updates"] = mapping
    return state


def get_sales_log_marker(state: Dict, event_id: str) -> str | None:
    return state.get("event_sales_log_updates", {}).get(event_id)


def set_sales_log_marker(state: Dict, event_id: str, marker: str) -> Dict:
    mapping = state.get("event_sales_log_updates", {})
    mapping[event_id] = marker
    state["event_sales_log_updates"] = mapping
    return state


def get_cash_log_marker(state: Dict, event_id: str) -> str | None:
    return state.get("event_cash_log_updates", {}).get(event_id)


def set_cash_log_marker(state: Dict, event_id: str, marker: str) -> Dict:
    mapping = state.get("event_cash_log_updates", {})
    mapping[event_id] = marker
    state["event_cash_log_updates"] = mapping
    return state


def get_cash_global_log_marker(state: Dict, event_id: str) -> str | None:
    return state.get("event_cash_global_log_updates", {}).get(event_id)


def set_cash_global_log_marker(state: Dict, event_id: str, marker: str) -> Dict:
    mapping = state.get("event_cash_global_log_updates", {})
    mapping[event_id] = marker
    state["event_cash_global_log_updates"] = mapping
    return state


def prune_state(state: Dict, keep_recent_events: int = 1500) -> Dict:
    """
    Keep state bounded so long-running deployments stay reliable.
    Older event keys are removed from per-event maps/lists.
    """
    processed_updates = state.get("event_processed_updates", {})
    if not isinstance(processed_updates, dict):
        return state

    if len(processed_updates) <= keep_recent_events:
        return state

    def _sort_key(item: tuple[str, str]) -> str:
        return str(item[1] or "")

    recent_items = sorted(processed_updates.items(), key=_sort_key, reverse=True)[
        :keep_recent_events
    ]
    keep_keys = {k for k, _ in recent_items}

    map_fields = [
        "event_contact_map",
        "event_contact_updates",
        "event_contact_fingerprints",
        "event_processed_updates",
        "event_invoice_map",
        "event_invoice_updates",
        "event_draft_sync_fingerprints",
        "event_draft_sync_attempted_at",
        "event_xero_retry_after",
        "event_xero_retry_backoff",
        "event_xero_action_attempts",
        "draft_cleanup_queue",
        "event_sheet_log_updates",
        "event_sales_log_updates",
        "event_cash_log_updates",
        "event_cash_global_log_updates",
        "recent_xero_webhook_events",
        "deferred_xero_event_targets",
    ]
    list_fields = [
        "processed_event_ids",
        "prefilled_event_ids",
        "invoice_sent_event_ids",
        "invoice_paid_event_ids",
    ]

    for field in map_fields:
        value = state.get(field)
        if isinstance(value, dict):
            if field == "event_xero_action_attempts":
                state[field] = {
                    k: v
                    for k, v in value.items()
                    if str(k).split("|", 1)[0] in keep_keys
                }
            else:
                state[field] = {k: v for k, v in value.items() if k in keep_keys}

    for field in list_fields:
        value = state.get(field)
        if isinstance(value, list):
            state[field] = [k for k in value if k in keep_keys]

    return state
