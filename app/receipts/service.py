from __future__ import annotations

import re

from ..config import AppConfig
from .models import ReceiptRecord
from .store import append_receipt, list_receipts, update_receipt


class ReceiptService:
    """
    Receipt processing scaffold service.
    Feature-flagged and isolated from live invoice/calendar processing.
    """

    def __init__(self, config: AppConfig):
        self._config = config
        self._store_file = config.receipts_store_file

    @property
    def enabled(self) -> bool:
        return bool(self._config.receipts_enabled)

    @property
    def write_confirmation_required(self) -> bool:
        return bool(self._config.receipts_require_write_confirmation)

    def create_draft(self, raw_text: str, *, source: str = "manual") -> ReceiptRecord:
        parsed = self._parse_best_effort(raw_text or "")
        record = ReceiptRecord(
            source=source,
            raw_text=raw_text or "",
            merchant=parsed.get("merchant", ""),
            amount=parsed.get("amount"),
            currency=parsed.get("currency", "GBP"),
            transaction_ref=parsed.get("transaction_ref", ""),
            status="new",
            metadata={"parser": "scaffold-v1"},
        )
        return append_receipt(self._store_file, record)

    def list_recent(self, limit: int = 100) -> list[ReceiptRecord]:
        return list_receipts(self._store_file, limit=limit)

    def link_to_event(self, receipt_id: str, event_key: str) -> ReceiptRecord | None:
        # Link only. No live calendar/Xero writes in scaffold mode.
        return update_receipt(
            self._store_file,
            receipt_id,
            event_key=(event_key or "").strip(),
            status="linked_pending",
        )

    def mark_reconciled(self, receipt_id: str, *, notes: str = "") -> ReceiptRecord | None:
        return update_receipt(
            self._store_file,
            receipt_id,
            status="reconciled",
            notes=notes or "",
        )

    def _parse_best_effort(self, raw_text: str) -> dict:
        text = (raw_text or "").strip()
        if not text:
            return {"merchant": "", "amount": None, "currency": "GBP", "transaction_ref": ""}

        # Keep parser deliberately conservative at scaffold stage.
        amount = None
        m_amt = re.search(r"£\s*(\d+(?:\.\d{1,2})?)", text, flags=re.I)
        if m_amt:
            try:
                amount = float(m_amt.group(1))
            except Exception:
                amount = None

        transaction_ref = ""
        m_ref = re.search(r"\b(?:ref|auth|txn|transaction)\s*[:#-]?\s*([A-Z0-9-]{5,})\b", text, flags=re.I)
        if m_ref:
            transaction_ref = m_ref.group(1).strip()

        merchant = text.splitlines()[0].strip()[:120]
        return {
            "merchant": merchant,
            "amount": amount,
            "currency": "GBP",
            "transaction_ref": transaction_ref,
        }

