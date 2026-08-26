"""SQLite storage for the bulk "Receipt Dump" feature.

A *dump batch* is a single bulk upload of many past receipts. Each uploaded
file becomes a *dump item* that is OCR'd, AI-categorised and classified
(new / duplicate / possible_duplicate / suspicious / needs_account / imported /
ignored) before the admin reviews the batch and imports the clean items into the
normal ``expense_receipts`` table.

Kept deliberately separate from ``expense_store`` so the (large) feature is
self-contained and never destabilises the existing single-receipt flow.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from typing import Any

# Item lifecycle statuses.
STATUS_NEW = "new"  # clean, ready to import
STATUS_DUPLICATE = "duplicate"  # exact/confirmed duplicate -> ignored
STATUS_POSSIBLE_DUP = "possible_duplicate"  # same merchant+date+amount, needs a look
STATUS_SUSPICIOUS = "suspicious"  # looks like another person's receipt
STATUS_NEEDS_ACCOUNT = "needs_account"  # not in the card feed -> pick an account
STATUS_IMPORTED = "imported"  # pushed into expense_receipts
STATUS_IGNORED = "ignored"  # admin (or dedupe) chose to drop it

ACTIVE_STATUSES = {
    STATUS_NEW,
    STATUS_POSSIBLE_DUP,
    STATUS_SUSPICIOUS,
    STATUS_NEEDS_ACCOUNT,
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_tables(db_path: str) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS expense_dump_batches (
                id TEXT PRIMARY KEY,
                label TEXT NOT NULL DEFAULT '',
                engineer_id INTEGER,
                subcontractor_account TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'processing',
                total_count INTEGER NOT NULL DEFAULT 0,
                summary_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS expense_dump_items (
                id TEXT PRIMARY KEY,
                batch_id TEXT NOT NULL,
                seq INTEGER NOT NULL DEFAULT 0,
                content_sha256 TEXT NOT NULL DEFAULT '',
                merchant TEXT NOT NULL DEFAULT '',
                purchased_on TEXT NOT NULL DEFAULT '',
                amount_inc REAL,
                amount_ex REAL,
                vat_amount REAL,
                currency TEXT NOT NULL DEFAULT 'GBP',
                is_split INTEGER NOT NULL DEFAULT 0,
                segments_json TEXT NOT NULL DEFAULT '[]',
                category_account_code TEXT NOT NULL DEFAULT '',
                category_account_name TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'new',
                dup_reason TEXT NOT NULL DEFAULT '',
                match_receipt_id TEXT NOT NULL DEFAULT '',
                match_engineer_id INTEGER,
                image_check_json TEXT NOT NULL DEFAULT '{}',
                card_feed_status TEXT NOT NULL DEFAULT '',
                xero_bank_transaction_id TEXT NOT NULL DEFAULT '',
                assigned_engineer_id INTEGER,
                stored_file TEXT NOT NULL DEFAULT '',
                filename TEXT NOT NULL DEFAULT '',
                mime_type TEXT NOT NULL DEFAULT '',
                ocr_raw TEXT NOT NULL DEFAULT '',
                ocr_error TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_dump_items_batch "
            "ON expense_dump_items (batch_id, seq)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_dump_items_hash "
            "ON expense_dump_items (content_sha256)"
        )
        # Lightweight migrations for columns added after the first release.
        existing_cols = {
            r[1] for r in conn.execute("PRAGMA table_info(expense_dump_batches)")
        }
        if "is_test" not in existing_cols:
            conn.execute(
                "ALTER TABLE expense_dump_batches "
                "ADD COLUMN is_test INTEGER NOT NULL DEFAULT 0"
            )
        if "card_account" not in existing_cols:
            conn.execute(
                "ALTER TABLE expense_dump_batches "
                "ADD COLUMN card_account TEXT NOT NULL DEFAULT ''"
            )
        if "supplier_profile" not in existing_cols:
            conn.execute(
                "ALTER TABLE expense_dump_batches "
                "ADD COLUMN supplier_profile TEXT NOT NULL DEFAULT ''"
            )
        item_cols = {
            r[1] for r in conn.execute("PRAGMA table_info(expense_dump_items)")
        }
        if "xero_bank_transaction_id" not in item_cols:
            conn.execute(
                "ALTER TABLE expense_dump_items "
                "ADD COLUMN xero_bank_transaction_id TEXT NOT NULL DEFAULT ''"
            )
        conn.commit()


def _conn(db_path: str) -> sqlite3.Connection:
    _ensure_tables(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


# ── Batches ────────────────────────────────────────────────────────────────

def create_batch(
    db_path: str,
    *,
    label: str = "",
    engineer_id: int | None = None,
    subcontractor_account: str = "",
    card_account: str = "",
    supplier_profile: str = "",
    is_test: bool = False,
) -> dict[str, Any]:
    bid = f"dump-{uuid.uuid4().hex[:12]}"
    now = _now_iso()
    with _conn(db_path) as conn:
        conn.execute(
            """
            INSERT INTO expense_dump_batches
                (id, label, engineer_id, subcontractor_account, status,
                 total_count, summary_json, is_test, card_account, supplier_profile,
                 created_at, updated_at)
            VALUES (?, ?, ?, ?, 'processing', 0, '{}', ?, ?, ?, ?, ?)
            """,
            (bid, label, engineer_id, subcontractor_account,
             1 if is_test else 0, card_account, supplier_profile, now, now),
        )
        conn.commit()
    return get_batch(db_path, bid)


def update_batch(db_path: str, batch_id: str, **fields) -> dict[str, Any] | None:
    allowed = {"label", "engineer_id", "subcontractor_account", "card_account",
               "supplier_profile", "status", "total_count"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if "summary" in fields:
        sets["summary_json"] = json.dumps(fields["summary"])
    if not sets:
        return get_batch(db_path, batch_id)
    sets["updated_at"] = _now_iso()
    cols = ", ".join(f"{k} = ?" for k in sets)
    with _conn(db_path) as conn:
        conn.execute(
            f"UPDATE expense_dump_batches SET {cols} WHERE id = ?",
            (*sets.values(), batch_id),
        )
        conn.commit()
    return get_batch(db_path, batch_id)


def get_batch(db_path: str, batch_id: str) -> dict[str, Any] | None:
    with _conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM expense_dump_batches WHERE id = ?", (batch_id,)
        ).fetchone()
    rec = _row(row)
    if rec is not None:
        try:
            rec["summary"] = json.loads(rec.get("summary_json") or "{}")
        except (ValueError, TypeError):
            rec["summary"] = {}
    return rec


def list_batches(db_path: str, *, limit: int = 50) -> list[dict[str, Any]]:
    with _conn(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM expense_dump_batches ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [_row(r) for r in rows]


# ── Items ──────────────────────────────────────────────────────────────────

def create_item(
    db_path: str,
    *,
    batch_id: str,
    seq: int = 0,
    content_sha256: str = "",
    merchant: str = "",
    purchased_on: str = "",
    amount_inc: float | None = None,
    amount_ex: float | None = None,
    vat_amount: float | None = None,
    currency: str = "GBP",
    is_split: bool = False,
    segments: list | None = None,
    category_account_code: str = "",
    category_account_name: str = "",
    status: str = STATUS_NEW,
    dup_reason: str = "",
    match_receipt_id: str = "",
    match_engineer_id: int | None = None,
    image_check: dict | None = None,
    card_feed_status: str = "",
    xero_bank_transaction_id: str = "",
    assigned_engineer_id: int | None = None,
    stored_file: str = "",
    filename: str = "",
    mime_type: str = "",
    ocr_raw: str = "",
    ocr_error: str = "",
    notes: str = "",
) -> dict[str, Any]:
    iid = f"di-{uuid.uuid4().hex[:12]}"
    now = _now_iso()
    with _conn(db_path) as conn:
        conn.execute(
            """
            INSERT INTO expense_dump_items
                (id, batch_id, seq, content_sha256, merchant, purchased_on,
                 amount_inc, amount_ex, vat_amount, currency, is_split,
                 segments_json, category_account_code, category_account_name,
                 status, dup_reason, match_receipt_id, match_engineer_id,
                 image_check_json, card_feed_status, xero_bank_transaction_id,
                 assigned_engineer_id,
                 stored_file, filename, mime_type, ocr_raw, ocr_error, notes,
                 created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                iid, batch_id, seq, content_sha256, merchant, purchased_on,
                amount_inc, amount_ex, vat_amount, currency, 1 if is_split else 0,
                json.dumps(segments or []), category_account_code,
                category_account_name, status, dup_reason, match_receipt_id,
                match_engineer_id, json.dumps(image_check or {}), card_feed_status,
                xero_bank_transaction_id.strip(), assigned_engineer_id,
                stored_file, filename, mime_type, ocr_raw,
                ocr_error, notes, now, now,
            ),
        )
        conn.commit()
    return get_item(db_path, iid)


def update_item(db_path: str, item_id: str, **fields) -> dict[str, Any] | None:
    allowed = {
        "merchant", "purchased_on", "amount_inc", "amount_ex", "vat_amount",
        "currency", "is_split", "category_account_code", "category_account_name",
        "status", "dup_reason", "match_receipt_id", "match_engineer_id",
        "card_feed_status", "xero_bank_transaction_id",
        "assigned_engineer_id", "notes",
    }
    sets: dict[str, Any] = {}
    for k, v in fields.items():
        if k in allowed:
            sets[k] = (1 if v else 0) if k == "is_split" else v
    if "segments" in fields:
        sets["segments_json"] = json.dumps(fields["segments"] or [])
    if "image_check" in fields:
        sets["image_check_json"] = json.dumps(fields["image_check"] or {})
    if not sets:
        return get_item(db_path, item_id)
    sets["updated_at"] = _now_iso()
    cols = ", ".join(f"{k} = ?" for k in sets)
    with _conn(db_path) as conn:
        conn.execute(
            f"UPDATE expense_dump_items SET {cols} WHERE id = ?",
            (*sets.values(), item_id),
        )
        conn.commit()
    return get_item(db_path, item_id)


def _hydrate(rec: dict[str, Any] | None) -> dict[str, Any] | None:
    if rec is None:
        return None
    try:
        rec["segments"] = json.loads(rec.get("segments_json") or "[]")
    except (ValueError, TypeError):
        rec["segments"] = []
    try:
        rec["image_check"] = json.loads(rec.get("image_check_json") or "{}")
    except (ValueError, TypeError):
        rec["image_check"] = {}
    return rec


def get_item(db_path: str, item_id: str) -> dict[str, Any] | None:
    with _conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM expense_dump_items WHERE id = ?", (item_id,)
        ).fetchone()
    return _hydrate(_row(row))


def list_items(db_path: str, batch_id: str) -> list[dict[str, Any]]:
    with _conn(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM expense_dump_items WHERE batch_id = ? ORDER BY seq, created_at",
            (batch_id,),
        ).fetchall()
    return [_hydrate(_row(r)) for r in rows]


def hashes_in_other_batches(db_path: str, batch_id: str) -> set[str]:
    """Content hashes of receipts from *other* batches that actually reached Xero.

    Only items with status ``imported`` count. A receipt that was merely
    uploaded/reviewed in a previous batch but never imported has not been
    reconciled against anything in Xero, so re-uploading it (e.g. while testing,
    or to actually reconcile it this time) must NOT be treated as a duplicate.
    The point of the dump is to reconcile the bank feed — something is only a
    genuine duplicate if it has already been pushed into Xero.

    Test batches are excluded too: a dry run imports nothing.
    """
    with _conn(db_path) as conn:
        rows = conn.execute(
            "SELECT DISTINCT i.content_sha256 "
            "FROM expense_dump_items i "
            "JOIN expense_dump_batches b ON b.id = i.batch_id "
            "WHERE i.batch_id != ? AND i.content_sha256 != '' "
            "AND i.status = ? "
            "AND COALESCE(b.is_test, 0) = 0",
            (batch_id, STATUS_IMPORTED),
        ).fetchall()
    return {r[0] for r in rows}


# ── Deletion ─────────────────────────────────────────────────────────────────

def stored_files_for_batch(db_path: str, batch_id: str) -> list[str]:
    """Distinct non-empty ``stored_file`` paths used by a batch's items."""
    with _conn(db_path) as conn:
        rows = conn.execute(
            "SELECT DISTINCT stored_file FROM expense_dump_items "
            "WHERE batch_id = ? AND stored_file != ''",
            (batch_id,),
        ).fetchall()
    return [r[0] for r in rows]


def stored_file_in_use(db_path: str, stored_file: str) -> bool:
    """True if any (remaining) dump item still references this file path."""
    if not stored_file:
        return False
    with _conn(db_path) as conn:
        row = conn.execute(
            "SELECT 1 FROM expense_dump_items WHERE stored_file = ? LIMIT 1",
            (stored_file,),
        ).fetchone()
    return row is not None


def delete_batch(db_path: str, batch_id: str) -> bool:
    """Delete a batch and all of its items. Returns True if the batch existed.

    Image files on disk are NOT touched here — the caller owns file cleanup so
    it can guard against files still referenced by other batches or by imported
    Field Expenses receipts.
    """
    with _conn(db_path) as conn:
        existed = conn.execute(
            "SELECT 1 FROM expense_dump_batches WHERE id = ?", (batch_id,)
        ).fetchone() is not None
        conn.execute("DELETE FROM expense_dump_items WHERE batch_id = ?", (batch_id,))
        conn.execute("DELETE FROM expense_dump_batches WHERE id = ?", (batch_id,))
        conn.commit()
    return existed


def test_batch_ids(db_path: str) -> list[str]:
    """IDs of every batch flagged as a test (dry) run."""
    with _conn(db_path) as conn:
        rows = conn.execute(
            "SELECT id FROM expense_dump_batches WHERE COALESCE(is_test, 0) = 1"
        ).fetchall()
    return [r[0] for r in rows]
