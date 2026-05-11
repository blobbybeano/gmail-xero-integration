from __future__ import annotations

from pathlib import Path
from threading import Lock
from datetime import datetime, timezone
import json

from .models import ReceiptRecord


_STORE_LOCK = Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_receipts(store_file: str) -> list[ReceiptRecord]:
    path = Path(store_file)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    items = data.get("receipts") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return []
    out: list[ReceiptRecord] = []
    for row in items:
        if isinstance(row, dict):
            try:
                out.append(ReceiptRecord.from_dict(row))
            except Exception:
                continue
    return out


def save_receipts(store_file: str, records: list[ReceiptRecord]) -> None:
    path = Path(store_file)
    payload = {"receipts": [r.to_dict() for r in records], "updated_at": _now_iso()}
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def append_receipt(store_file: str, record: ReceiptRecord) -> ReceiptRecord:
    with _STORE_LOCK:
        records = load_receipts(store_file)
        records.append(record)
        save_receipts(store_file, records)
    return record


def list_receipts(store_file: str, *, limit: int = 200) -> list[ReceiptRecord]:
    with _STORE_LOCK:
        records = load_receipts(store_file)
    records.sort(key=lambda r: (r.created_at, r.id), reverse=True)
    return records[: max(limit, 1)]


def update_receipt(store_file: str, receipt_id: str, **fields) -> ReceiptRecord | None:
    with _STORE_LOCK:
        records = load_receipts(store_file)
        for idx, rec in enumerate(records):
            if rec.id != receipt_id:
                continue
            for key, value in fields.items():
                if hasattr(rec, key):
                    setattr(rec, key, value)
            rec.updated_at = _now_iso()
            records[idx] = rec
            save_receipts(store_file, records)
            return rec
    return None

