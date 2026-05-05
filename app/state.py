from __future__ import annotations

import datetime as dt
import json
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
        "event_xero_retry_after",
        "event_sheet_log_updates",
        "event_sales_log_updates",
        "event_cash_log_updates",
        "event_cash_global_log_updates",
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
            state[field] = {k: v for k, v in value.items() if k in keep_keys}

    for field in list_fields:
        value = state.get(field)
        if isinstance(value, list):
            state[field] = [k for k in value if k in keep_keys]

    return state
