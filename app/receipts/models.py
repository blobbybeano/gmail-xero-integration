from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
import uuid


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ReceiptRecord:
    """
    Receipt scaffold record.
    This is intentionally generic so parser/providers can evolve safely.
    """

    id: str = field(default_factory=lambda: f"rcpt-{uuid.uuid4().hex[:12]}")
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)
    source: str = "manual"
    raw_text: str = ""
    merchant: str = ""
    amount: float | None = None
    currency: str = "GBP"
    transaction_ref: str = ""
    event_key: str = ""
    status: str = "new"
    notes: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "source": self.source,
            "raw_text": self.raw_text,
            "merchant": self.merchant,
            "amount": self.amount,
            "currency": self.currency,
            "transaction_ref": self.transaction_ref,
            "event_key": self.event_key,
            "status": self.status,
            "notes": self.notes,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ReceiptRecord":
        return cls(
            id=str(data.get("id") or f"rcpt-{uuid.uuid4().hex[:12]}"),
            created_at=str(data.get("created_at") or _now_iso()),
            updated_at=str(data.get("updated_at") or _now_iso()),
            source=str(data.get("source") or "manual"),
            raw_text=str(data.get("raw_text") or ""),
            merchant=str(data.get("merchant") or ""),
            amount=(float(data["amount"]) if data.get("amount") not in (None, "") else None),
            currency=str(data.get("currency") or "GBP"),
            transaction_ref=str(data.get("transaction_ref") or ""),
            event_key=str(data.get("event_key") or ""),
            status=str(data.get("status") or "new"),
            notes=str(data.get("notes") or ""),
            metadata=dict(data.get("metadata") or {}),
        )

