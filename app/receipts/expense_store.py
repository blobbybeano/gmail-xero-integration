"""SQLite-backed store for the Field Expenses feature.

This is deliberately separate from the calendar-event receipt scaffold
(``receipts_store.json``).  It models the people who submit expense receipts
(engineers / subcontractors), each receipt claim they upload, and the
settlement batches used to pay subcontractors back.

All tables live in the same admin SQLite DB used by ``admin_store`` so there
is a single source of truth for runtime data.
"""

from __future__ import annotations

import secrets
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

ENGINEER_KINDS = ("company_card", "subcontractor")
PAYMENT_SOURCES = ("company_card", "owner_paid")

# Statuses a single receipt claim moves through.
RECEIPT_STATUSES = (
    "pending_review",  # uploaded + OCR'd, waiting for the engineer to confirm
    "approved",        # engineer confirmed the details, ready for Xero
    "submitted",       # successfully recorded in Xero
    "failed",          # Xero submission failed
    "settled",         # subcontractor: paid back and reconciled
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today_iso() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _ensure_tables(db_path: str) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS expense_engineers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT 'company_card',
                xero_contact_id TEXT NOT NULL DEFAULT '',
                xero_contact_name TEXT NOT NULL DEFAULT '',
                expense_account_code TEXT NOT NULL DEFAULT '',
                payment_account_code TEXT NOT NULL DEFAULT '',
                customer_calendar_id TEXT NOT NULL DEFAULT '',
                allow_owner_paid INTEGER NOT NULL DEFAULT 0,
                owner_paid_account_code TEXT NOT NULL DEFAULT '',
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS expense_receipts (
                id TEXT PRIMARY KEY,
                engineer_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending_review',
                merchant TEXT NOT NULL DEFAULT '',
                purchased_on TEXT NOT NULL DEFAULT '',
                amount_inc REAL,
                amount_ex REAL,
                vat_amount REAL,
                currency TEXT NOT NULL DEFAULT 'GBP',
                ocr_merchant TEXT NOT NULL DEFAULT '',
                ocr_amount REAL,
                ocr_date TEXT NOT NULL DEFAULT '',
                ocr_raw TEXT NOT NULL DEFAULT '',
                ocr_error TEXT NOT NULL DEFAULT '',
                stored_file TEXT NOT NULL DEFAULT '',
                filename TEXT NOT NULL DEFAULT '',
                mime_type TEXT NOT NULL DEFAULT '',
                category_account_code TEXT NOT NULL DEFAULT '',
                category_account_name TEXT NOT NULL DEFAULT '',
                payment_source TEXT NOT NULL DEFAULT 'company_card',
                owner_paid_account_code TEXT NOT NULL DEFAULT '',
                xero_type TEXT NOT NULL DEFAULT '',
                xero_id TEXT NOT NULL DEFAULT '',
                xero_error TEXT NOT NULL DEFAULT '',
                settlement_id INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS expense_settlements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                engineer_id INTEGER NOT NULL,
                reference TEXT NOT NULL DEFAULT '',
                amount REAL,
                paid_on TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                plaid_tx_id TEXT NOT NULL DEFAULT '',
                note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_exp_receipts_eng "
            "ON expense_receipts (engineer_id, created_at)"
        )
        # Migrations: add expense-category columns to pre-existing DBs.
        _cols = {r[1] for r in conn.execute("PRAGMA table_info(expense_receipts)").fetchall()}
        if "category_account_code" not in _cols:
            conn.execute(
                "ALTER TABLE expense_receipts "
                "ADD COLUMN category_account_code TEXT NOT NULL DEFAULT ''"
            )
        if "category_account_name" not in _cols:
            conn.execute(
                "ALTER TABLE expense_receipts "
                "ADD COLUMN category_account_name TEXT NOT NULL DEFAULT ''"
            )
        if "payment_source" not in _cols:
            conn.execute(
                "ALTER TABLE expense_receipts "
                "ADD COLUMN payment_source TEXT NOT NULL DEFAULT 'company_card'"
            )
        if "owner_paid_account_code" not in _cols:
            conn.execute(
                "ALTER TABLE expense_receipts "
                "ADD COLUMN owner_paid_account_code TEXT NOT NULL DEFAULT ''"
            )
        # Migrations: per-engineer login credentials + linked bank card.
        _eng_cols = {
            r[1] for r in conn.execute(
                "PRAGMA table_info(expense_engineers)"
            ).fetchall()
        }
        for _col, _ddl in (
            ("username",
             "ALTER TABLE expense_engineers ADD COLUMN username TEXT NOT NULL DEFAULT ''"),
            ("password_hash",
             "ALTER TABLE expense_engineers ADD COLUMN password_hash TEXT NOT NULL DEFAULT ''"),
            ("plaid_account_id",
             "ALTER TABLE expense_engineers ADD COLUMN plaid_account_id TEXT NOT NULL DEFAULT ''"),
            ("customer_calendar_id",
             "ALTER TABLE expense_engineers ADD COLUMN customer_calendar_id TEXT NOT NULL DEFAULT ''"),
            ("allow_owner_paid",
             "ALTER TABLE expense_engineers ADD COLUMN allow_owner_paid INTEGER NOT NULL DEFAULT 0"),
            ("owner_paid_account_code",
             "ALTER TABLE expense_engineers ADD COLUMN owner_paid_account_code TEXT NOT NULL DEFAULT ''"),
        ):
            if _col not in _eng_cols:
                conn.execute(_ddl)
        # Migration: idempotency key for auto-recognised subcontractor payments.
        _set_cols = {
            r[1] for r in conn.execute(
                "PRAGMA table_info(expense_settlements)"
            ).fetchall()
        }
        if "plaid_tx_id" not in _set_cols:
            conn.execute(
                "ALTER TABLE expense_settlements "
                "ADD COLUMN plaid_tx_id TEXT NOT NULL DEFAULT ''"
            )
        if "note" not in _set_cols:
            conn.execute(
                "ALTER TABLE expense_settlements "
                "ADD COLUMN note TEXT NOT NULL DEFAULT ''"
            )
        # Migration: link a settlement to the Xero bill it raised (+ any error).
        if "xero_bill_id" not in _set_cols:
            conn.execute(
                "ALTER TABLE expense_settlements "
                "ADD COLUMN xero_bill_id TEXT NOT NULL DEFAULT ''"
            )
        if "xero_error" not in _set_cols:
            conn.execute(
                "ALTER TABLE expense_settlements "
                "ADD COLUMN xero_error TEXT NOT NULL DEFAULT ''"
            )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_exp_settle_txid "
            "ON expense_settlements (plaid_tx_id) WHERE plaid_tx_id <> ''"
        )
        conn.commit()


def _conn(db_path: str) -> sqlite3.Connection:
    _ensure_tables(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


# ---------------------------------------------------------------------------
# Engineers
# ---------------------------------------------------------------------------

def create_engineer(
    db_path: str,
    *,
    name: str,
    kind: str = "company_card",
    xero_contact_id: str = "",
    xero_contact_name: str = "",
    expense_account_code: str = "",
    payment_account_code: str = "",
    customer_calendar_id: str = "",
    allow_owner_paid: bool | int = False,
    owner_paid_account_code: str = "",
) -> dict[str, Any]:
    name = (name or "").strip()
    if not name:
        raise ValueError("Engineer name is required")
    if kind not in ENGINEER_KINDS:
        kind = "company_card"
    token = secrets.token_urlsafe(8)
    with _conn(db_path) as conn:
        # Guarantee token uniqueness even on the astronomically unlikely clash.
        while conn.execute(
            "SELECT 1 FROM expense_engineers WHERE token = ?", (token,)
        ).fetchone():
            token = secrets.token_urlsafe(8)
        cur = conn.execute(
            """
            INSERT INTO expense_engineers
                (token, name, kind, xero_contact_id, xero_contact_name,
                 expense_account_code, payment_account_code, customer_calendar_id,
                 allow_owner_paid,
                 owner_paid_account_code, active, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
            """,
            (
                token, name, kind, xero_contact_id.strip(),
                xero_contact_name.strip(), expense_account_code.strip(),
                payment_account_code.strip(), customer_calendar_id.strip(),
                1 if allow_owner_paid else 0, owner_paid_account_code.strip(),
                _now_iso(),
            ),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM expense_engineers WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
    return _row_to_dict(row)


def update_engineer(db_path: str, engineer_id: int, **fields) -> dict[str, Any] | None:
    allowed = {
        "name", "kind", "xero_contact_id", "xero_contact_name",
        "expense_account_code", "payment_account_code", "customer_calendar_id",
        "active",
        "username", "password_hash", "plaid_account_id",
        "allow_owner_paid", "owner_paid_account_code",
    }
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return get_engineer(db_path, engineer_id)
    if "kind" in sets and sets["kind"] not in ENGINEER_KINDS:
        sets["kind"] = "company_card"
    if "active" in sets:
        sets["active"] = 1 if sets["active"] else 0
    if "allow_owner_paid" in sets:
        sets["allow_owner_paid"] = 1 if sets["allow_owner_paid"] else 0
    cols = ", ".join(f"{k} = ?" for k in sets)
    with _conn(db_path) as conn:
        conn.execute(
            f"UPDATE expense_engineers SET {cols} WHERE id = ?",
            (*sets.values(), engineer_id),
        )
        conn.commit()
    return get_engineer(db_path, engineer_id)


def get_engineer(db_path: str, engineer_id: int) -> dict[str, Any] | None:
    with _conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM expense_engineers WHERE id = ?", (engineer_id,)
        ).fetchone()
    return _row_to_dict(row)


def get_engineer_by_token(db_path: str, token: str) -> dict[str, Any] | None:
    token = (token or "").strip()
    if not token:
        return None
    with _conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM expense_engineers WHERE token = ?", (token,)
        ).fetchone()
    return _row_to_dict(row)


def get_engineer_by_username(db_path: str, username: str) -> dict[str, Any] | None:
    """Look up an engineer by their (case-insensitive) login username."""
    username = (username or "").strip()
    if not username:
        return None
    with _conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM expense_engineers "
            "WHERE username != '' AND lower(username) = lower(?)",
            (username,),
        ).fetchone()
    return _row_to_dict(row)


def username_taken(db_path: str, username: str, *, exclude_id: int = 0) -> bool:
    """True if another engineer already uses this username (case-insensitive)."""
    username = (username or "").strip()
    if not username:
        return False
    with _conn(db_path) as conn:
        row = conn.execute(
            "SELECT 1 FROM expense_engineers "
            "WHERE username != '' AND lower(username) = lower(?) AND id != ? LIMIT 1",
            (username, exclude_id),
        ).fetchone()
    return row is not None


def list_engineers(db_path: str, *, include_inactive: bool = True) -> list[dict[str, Any]]:
    sql = "SELECT * FROM expense_engineers"
    if not include_inactive:
        sql += " WHERE active = 1"
    sql += " ORDER BY active DESC, name COLLATE NOCASE ASC"
    with _conn(db_path) as conn:
        rows = conn.execute(sql).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Receipts
# ---------------------------------------------------------------------------

def create_receipt(
    db_path: str,
    *,
    engineer_id: int,
    merchant: str = "",
    purchased_on: str = "",
    amount_inc: float | None = None,
    amount_ex: float | None = None,
    vat_amount: float | None = None,
    currency: str = "GBP",
    ocr_merchant: str = "",
    ocr_amount: float | None = None,
    ocr_date: str = "",
    ocr_raw: str = "",
    ocr_error: str = "",
    stored_file: str = "",
    filename: str = "",
    mime_type: str = "",
    category_account_code: str = "",
    category_account_name: str = "",
    payment_source: str = "company_card",
    owner_paid_account_code: str = "",
    status: str = "pending_review",
) -> dict[str, Any]:
    rid = f"exp-{uuid.uuid4().hex[:12]}"
    now = _now_iso()
    if payment_source not in PAYMENT_SOURCES:
        payment_source = "company_card"
    with _conn(db_path) as conn:
        conn.execute(
            """
            INSERT INTO expense_receipts
                (id, engineer_id, status, merchant, purchased_on, amount_inc,
                 amount_ex, vat_amount, currency, ocr_merchant, ocr_amount,
                 ocr_date, ocr_raw, ocr_error, stored_file, filename,
                 mime_type, category_account_code, category_account_name,
                 payment_source, owner_paid_account_code, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                rid, engineer_id, status, merchant, purchased_on, amount_inc,
                amount_ex, vat_amount, currency, ocr_merchant, ocr_amount,
                ocr_date, ocr_raw, ocr_error, stored_file, filename,
                mime_type, category_account_code, category_account_name,
                payment_source, owner_paid_account_code.strip(),
                now, now,
            ),
        )
        conn.commit()
    return get_receipt(db_path, rid)


def update_receipt(db_path: str, receipt_id: str, **fields) -> dict[str, Any] | None:
    allowed = {
        "status", "merchant", "purchased_on", "amount_inc", "amount_ex",
        "vat_amount", "currency", "xero_type", "xero_id", "xero_error",
        "settlement_id", "category_account_code", "category_account_name",
        "payment_source", "owner_paid_account_code",
    }
    sets = {k: v for k, v in fields.items() if k in allowed}
    if "payment_source" in sets and sets["payment_source"] not in PAYMENT_SOURCES:
        sets["payment_source"] = "company_card"
    if not sets:
        return get_receipt(db_path, receipt_id)
    sets["updated_at"] = _now_iso()
    cols = ", ".join(f"{k} = ?" for k in sets)
    with _conn(db_path) as conn:
        conn.execute(
            f"UPDATE expense_receipts SET {cols} WHERE id = ?",
            (*sets.values(), receipt_id),
        )
        conn.commit()
    return get_receipt(db_path, receipt_id)


def get_receipt(db_path: str, receipt_id: str) -> dict[str, Any] | None:
    with _conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM expense_receipts WHERE id = ?", (receipt_id,)
        ).fetchone()
    return _row_to_dict(row)


def list_receipts_for_engineer(
    db_path: str,
    engineer_id: int,
    *,
    limit: int = 500,
) -> list[dict[str, Any]]:
    with _conn(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM expense_receipts WHERE engineer_id = ? "
            "ORDER BY created_at DESC LIMIT ?",
            (engineer_id, max(limit, 1)),
        ).fetchall()
    return [dict(r) for r in rows]


def list_all_receipts(db_path: str, *, limit: int = 1000) -> list[dict[str, Any]]:
    with _conn(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM expense_receipts ORDER BY created_at DESC LIMIT ?",
            (max(limit, 1),),
        ).fetchall()
    return [dict(r) for r in rows]


def stored_file_in_use(db_path: str, stored_file: str) -> bool:
    """True if any Field Expenses receipt references this image file path."""
    if not stored_file:
        return False
    with _conn(db_path) as conn:
        row = conn.execute(
            "SELECT 1 FROM expense_receipts WHERE stored_file = ? LIMIT 1",
            (stored_file,),
        ).fetchone()
    return row is not None


def stored_file_in_use_by_others(
    db_path: str, stored_file: str, exclude_receipt_id: str
) -> bool:
    """True if any receipt *other than exclude_receipt_id* references the file."""
    if not stored_file:
        return False
    with _conn(db_path) as conn:
        row = conn.execute(
            "SELECT 1 FROM expense_receipts "
            "WHERE stored_file = ? AND id != ? LIMIT 1",
            (stored_file, exclude_receipt_id),
        ).fetchone()
    return row is not None


def delete_receipt(db_path: str, receipt_id: str) -> str:
    """Delete a receipt row and return the stored_file path it held (may be '')."""
    rec = get_receipt(db_path, receipt_id)
    if not rec:
        return ""
    stored_file = rec.get("stored_file") or ""
    with _conn(db_path) as conn:
        conn.execute("DELETE FROM expense_receipts WHERE id = ?", (receipt_id,))
        conn.commit()
    return stored_file


def clear_receipt_stored_file(db_path: str, receipt_id: str) -> str:
    """Set stored_file='' on a receipt and return the previous path."""
    rec = get_receipt(db_path, receipt_id)
    if not rec:
        return ""
    old_path = rec.get("stored_file") or ""
    if not old_path:
        return ""
    with _conn(db_path) as conn:
        conn.execute(
            "UPDATE expense_receipts SET stored_file = '', updated_at = ? WHERE id = ?",
            (_now_iso(), receipt_id),
        )
        conn.commit()
    return old_path


def list_receipts_with_images(db_path: str) -> list[dict[str, Any]]:
    """All receipts that still have a stored image file, newest first."""
    with _conn(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM expense_receipts "
            "WHERE stored_file != '' "
            "ORDER BY created_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def amount_owed_to_engineer(db_path: str, engineer_id: int) -> float:
    """Sum of approved/submitted, not-yet-settled receipts for a subcontractor."""
    with _conn(db_path) as conn:
        row = conn.execute(
            """
            SELECT COALESCE(SUM(COALESCE(amount_inc, 0)), 0) AS total
            FROM expense_receipts
            WHERE engineer_id = ?
              AND status IN ('approved', 'submitted')
              AND settlement_id IS NULL
            """,
            (engineer_id,),
        ).fetchone()
    return float(row["total"] or 0.0)


# ---------------------------------------------------------------------------
# Settlements (subcontractor payments) — used in a later phase
# ---------------------------------------------------------------------------

def create_settlement(
    db_path: str,
    *,
    engineer_id: int,
    reference: str = "",
    amount: float | None = None,
    paid_on: str = "",
    status: str = "pending",
    plaid_tx_id: str = "",
    note: str = "",
) -> dict[str, Any]:
    with _conn(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO expense_settlements
                (engineer_id, reference, amount, paid_on, status, plaid_tx_id,
                 note, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (engineer_id, reference.strip(), amount, paid_on.strip(),
             (status or "pending").strip(), (plaid_tx_id or "").strip(),
             (note or "").strip(), _now_iso()),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM expense_settlements WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
    return _row_to_dict(row)


def list_settlements_for_engineer(db_path: str, engineer_id: int) -> list[dict[str, Any]]:
    with _conn(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM expense_settlements WHERE engineer_id = ? "
            "ORDER BY created_at DESC",
            (engineer_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def update_settlement(db_path: str, settlement_id: int, **fields) -> dict[str, Any] | None:
    allowed = {"status", "note", "xero_bill_id", "xero_error", "paid_on"}
    sets = {k: (v if v is not None else "") for k, v in fields.items() if k in allowed}
    if not sets:
        with _conn(db_path) as conn:
            row = conn.execute(
                "SELECT * FROM expense_settlements WHERE id = ?", (settlement_id,)
            ).fetchone()
        return _row_to_dict(row)
    cols = ", ".join(f"{k} = ?" for k in sets)
    with _conn(db_path) as conn:
        conn.execute(
            f"UPDATE expense_settlements SET {cols} WHERE id = ?",
            (*sets.values(), settlement_id),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM expense_settlements WHERE id = ?", (settlement_id,)
        ).fetchone()
    return _row_to_dict(row)
