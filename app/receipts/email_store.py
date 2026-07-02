"""SQLite-backed store for the Email Invoice Importer.

Tables live in the same admin SQLite DB (admin_db_file) used by admin_store /
expense_store, keeping a single source of truth.

email_scan_batches   — one row per scan run (manual or daily scheduled)
email_scan_items     — one row per invoice attachment found in a batch
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

# ── Status constants ──────────────────────────────────────────────────────────

STATUS_NEW         = "new"           # invoice to import
STATUS_DUPLICATE   = "duplicate"     # exact image match already in system
STATUS_POSSIBLE_DUP = "possible_dup" # same merchant/date/amount — needs review
STATUS_SUSPICIOUS  = "suspicious"    # vision check inconclusive
STATUS_IGNORED     = "ignored"       # manually dismissed by admin
STATUS_IMPORTED    = "imported"      # written to expense_receipts
STATUS_OWN_COMPANY = "own_company"   # filtered: from our own company
STATUS_NO_ACCOUNT  = "no_account"    # could not assign an account code
STATUS_NOT_INVOICE = "not_invoice"   # AI: not a supplier invoice to Power Wash
STATUS_SKIPPED_EMAIL = "skipped_email"  # email skipped before OCR — reviewable placeholder

IMPORTABLE_STATUSES = {STATUS_NEW}
REVIEW_STATUSES     = {STATUS_POSSIBLE_DUP, STATUS_SUSPICIOUS, STATUS_NO_ACCOUNT}
SKIP_STATUSES       = {STATUS_DUPLICATE, STATUS_OWN_COMPANY, STATUS_IGNORED, STATUS_NOT_INVOICE}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_tables(db_path: str) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS email_scan_batches (
                id          TEXT PRIMARY KEY,
                label       TEXT    NOT NULL DEFAULT '',
                date_from   TEXT    NOT NULL DEFAULT '',
                date_to     TEXT    NOT NULL DEFAULT '',
                status      TEXT    NOT NULL DEFAULT 'processing',
                is_test     INTEGER NOT NULL DEFAULT 0,
                total_found INTEGER NOT NULL DEFAULT 0,
                summary_json TEXT   NOT NULL DEFAULT '{}',
                created_at  TEXT    NOT NULL,
                updated_at  TEXT    NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS email_scan_items (
                id                   TEXT PRIMARY KEY,
                batch_id             TEXT NOT NULL,
                seq                  INTEGER NOT NULL DEFAULT 0,
                message_id           TEXT NOT NULL DEFAULT '',
                thread_id            TEXT NOT NULL DEFAULT '',
                sender_from          TEXT NOT NULL DEFAULT '',
                sender_name          TEXT NOT NULL DEFAULT '',
                subject              TEXT NOT NULL DEFAULT '',
                email_date           TEXT NOT NULL DEFAULT '',
                attachment_name      TEXT NOT NULL DEFAULT '',
                attachment_mime      TEXT NOT NULL DEFAULT '',
                status               TEXT NOT NULL DEFAULT 'new',
                merchant             TEXT NOT NULL DEFAULT '',
                purchased_on         TEXT NOT NULL DEFAULT '',
                amount_inc           REAL,
                amount_ex            REAL,
                vat_amount           REAL,
                currency             TEXT NOT NULL DEFAULT 'GBP',
                is_split             INTEGER NOT NULL DEFAULT 0,
                segments_json        TEXT NOT NULL DEFAULT '[]',
                category_account_code  TEXT NOT NULL DEFAULT '',
                category_account_name  TEXT NOT NULL DEFAULT '',
                dup_reason           TEXT NOT NULL DEFAULT '',
                match_receipt_id     TEXT NOT NULL DEFAULT '',
                stored_file          TEXT NOT NULL DEFAULT '',
                ocr_raw              TEXT NOT NULL DEFAULT '',
                ocr_error            TEXT NOT NULL DEFAULT '',
                notes                TEXT NOT NULL DEFAULT '',
                image_check_json     TEXT NOT NULL DEFAULT '{}',
                created_at           TEXT NOT NULL,
                updated_at           TEXT NOT NULL
            )
        """)
        # Older installs predate the card_account column (which card/bank
        # account this batch's invoices should be checked against).
        try:
            conn.execute(
                "ALTER TABLE email_scan_batches "
                "ADD COLUMN card_account TEXT NOT NULL DEFAULT ''"
            )
        except sqlite3.OperationalError:
            pass  # column already exists
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_escan_items_batch  "
            "ON email_scan_items (batch_id, seq)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_escan_items_msgid  "
            "ON email_scan_items (message_id)"
        )
        conn.commit()


def _conn(db_path: str) -> sqlite3.Connection:
    _ensure_tables(db_path)
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    return c


def _row(row: Any) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    for jkey in ("summary_json", "segments_json", "image_check_json"):
        if jkey in d:
            try:
                parsed = json.loads(d[jkey] or "null")
            except Exception:
                parsed = None
            plain = jkey.replace("_json", "")
            d[plain] = parsed if parsed is not None else ([] if "segments" in jkey else {})
    return d


# ── Batches ───────────────────────────────────────────────────────────────────

def create_batch(
    db_path: str,
    *,
    label: str = "",
    date_from: str = "",
    date_to: str = "",
    is_test: bool = False,
    card_account: str = "",
) -> dict:
    bid = f"escan-{uuid.uuid4().hex[:12]}"
    now = _now_iso()
    with _conn(db_path) as conn:
        conn.execute(
            "INSERT INTO email_scan_batches "
            "(id, label, date_from, date_to, status, is_test, total_found, summary_json, card_account, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'processing', ?, 0, '{}', ?, ?, ?)",
            (bid, label, date_from, date_to, 1 if is_test else 0,
             card_account or "", now, now),
        )
        conn.commit()
    return get_batch(db_path, bid)


def get_batch(db_path: str, batch_id: str) -> dict | None:
    with _conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM email_scan_batches WHERE id = ?", (batch_id,)
        ).fetchone()
    return _row(row)


def update_batch(db_path: str, batch_id: str, **fields) -> dict | None:
    allowed = {"label", "status", "is_test", "total_found", "summary_json",
               "card_account"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return get_batch(db_path, batch_id)
    now = _now_iso()
    clauses = ", ".join(f"{k} = ?" for k in sets)
    vals = list(sets.values()) + [now, batch_id]
    with _conn(db_path) as conn:
        conn.execute(
            f"UPDATE email_scan_batches SET {clauses}, updated_at = ? WHERE id = ?",
            vals,
        )
        conn.commit()
    return get_batch(db_path, batch_id)


def list_batches(db_path: str, limit: int = 50) -> list[dict]:
    with _conn(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM email_scan_batches ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [_row(r) for r in rows]


def delete_batch(db_path: str, batch_id: str) -> None:
    with _conn(db_path) as conn:
        conn.execute("DELETE FROM email_scan_items WHERE batch_id = ?", (batch_id,))
        conn.execute("DELETE FROM email_scan_batches WHERE id = ?", (batch_id,))
        conn.commit()


# ── Items ─────────────────────────────────────────────────────────────────────

def create_item(db_path: str, *, batch_id: str, seq: int = 0, **fields) -> dict:
    iid = f"esi-{uuid.uuid4().hex[:12]}"
    now = _now_iso()
    row = dict(
        id=iid,
        batch_id=batch_id,
        seq=seq,
        message_id=fields.get("message_id", ""),
        thread_id=fields.get("thread_id", ""),
        sender_from=fields.get("sender_from", ""),
        sender_name=fields.get("sender_name", ""),
        subject=fields.get("subject", ""),
        email_date=fields.get("email_date", ""),
        attachment_name=fields.get("attachment_name", ""),
        attachment_mime=fields.get("attachment_mime", ""),
        status=fields.get("status", STATUS_NEW),
        merchant=fields.get("merchant", ""),
        purchased_on=fields.get("purchased_on", ""),
        amount_inc=fields.get("amount_inc"),
        amount_ex=fields.get("amount_ex"),
        vat_amount=fields.get("vat_amount"),
        currency=fields.get("currency", "GBP"),
        is_split=1 if fields.get("is_split") else 0,
        segments_json=json.dumps(fields.get("segments") or []),
        category_account_code=fields.get("category_account_code", ""),
        category_account_name=fields.get("category_account_name", ""),
        dup_reason=fields.get("dup_reason", ""),
        match_receipt_id=fields.get("match_receipt_id", ""),
        stored_file=fields.get("stored_file", ""),
        ocr_raw=fields.get("ocr_raw", ""),
        ocr_error=fields.get("ocr_error", ""),
        notes=fields.get("notes", ""),
        image_check_json=json.dumps(fields.get("image_check") or {}),
        created_at=now,
        updated_at=now,
    )
    cols = ", ".join(row.keys())
    placeholders = ", ".join("?" * len(row))
    with _conn(db_path) as conn:
        conn.execute(
            f"INSERT INTO email_scan_items ({cols}) VALUES ({placeholders})",
            list(row.values()),
        )
        conn.commit()
    return get_item(db_path, iid)


def get_item(db_path: str, item_id: str) -> dict | None:
    with _conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM email_scan_items WHERE id = ?", (item_id,)
        ).fetchone()
    return _row(row)


def update_item(db_path: str, item_id: str, **fields) -> dict | None:
    allowed = {
        "status", "merchant", "purchased_on", "amount_inc", "amount_ex",
        "vat_amount", "category_account_code", "category_account_name",
        "segments_json", "is_split", "dup_reason", "match_receipt_id",
        "stored_file", "ocr_raw", "ocr_error", "notes", "image_check_json",
        "attachment_name", "attachment_mime",
    }
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return get_item(db_path, item_id)
    now = _now_iso()
    clauses = ", ".join(f"{k} = ?" for k in sets)
    vals = list(sets.values()) + [now, item_id]
    with _conn(db_path) as conn:
        conn.execute(
            f"UPDATE email_scan_items SET {clauses}, updated_at = ? WHERE id = ?",
            vals,
        )
        conn.commit()
    return get_item(db_path, item_id)


def list_items(db_path: str, batch_id: str) -> list[dict]:
    with _conn(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM email_scan_items WHERE batch_id = ? ORDER BY seq ASC, created_at ASC",
            (batch_id,),
        ).fetchall()
    return [_row(r) for r in rows]


def message_already_scanned(
    db_path: str, message_id: str, attachment_name: str
) -> bool:
    """True if this exact Gmail message + attachment was already IMPORTED.

    Intentionally only blocks on imported items — not on previous scan
    results that were never actioned (no_account / not_invoice / ignored).
    This lets old unimported scan batches coexist without blocking a fresh
    re-scan of the same date range.
    """
    with _conn(db_path) as conn:
        row = conn.execute(
            "SELECT id FROM email_scan_items "
            "WHERE message_id = ? AND attachment_name = ? AND status = 'imported' LIMIT 1",
            (message_id, attachment_name),
        ).fetchone()
    return row is not None
