"""Uploaded Xero statement/reconciliation report cache.

The normal Xero Accounting API does not reliably expose every row from the
Xero UI's Bank statements tab.  When the office exports a Xero statement or
reconciliation report, this module stores the reconciled rows so Field Expenses
can mark matching bank CSV lines as already handled without guessing and
without making more live Xero requests.
"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import re
from typing import Any

from .admin_store import get_json_setting, set_json_setting

_ROWS_KEY = "xero_statement_reconciled_rows"
_META_KEY = "xero_statement_reconciled_meta"
RETENTION_DAYS = 550


def _decode(data: bytes | str) -> str:
    if isinstance(data, str):
        return data
    for enc in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, ValueError):
            continue
    raise ValueError("The file doesn't look like a text CSV export.")


def _norm_header(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").strip().lower()).strip()


def _parse_date(raw: str) -> str:
    raw = (raw or "").strip()
    # Xero exports often include a timestamp; keep the date part.
    raw = raw.split(" ", 1)[0]
    for fmt in ("%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d", "%d-%m-%Y", "%d %b %Y"):
        try:
            return dt.datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    raise ValueError(f"Unrecognised date: {raw!r}")


def _parse_amount(raw: str) -> float:
    s = (raw or "").strip()
    if not s:
        return 0.0
    neg = s.startswith("(") and s.endswith(")")
    s = (
        s.replace("£", "")
        .replace(",", "")
        .replace("(", "")
        .replace(")", "")
        .strip()
    )
    if not s:
        return 0.0
    value = float(s)
    return -value if neg else value


def _pick(row: dict[str, str], names: tuple[str, ...]) -> str:
    for name in names:
        if name in row and str(row.get(name) or "").strip():
            return str(row.get(name) or "").strip()
    return ""


def _is_reconciled(row: dict[str, str]) -> bool:
    status = _pick(
        row,
        (
            "status",
            "reconciliation status",
            "reconciled status",
            "statement status",
            "bank status",
        ),
    ).lower()
    if any(word in status for word in ("unreconciled", "not reconciled")):
        return False
    if "reconciled" in status:
        return True
    flag = _pick(row, ("reconciled", "is reconciled")).lower()
    return flag in {"yes", "y", "true", "1"}


def _row_description(row: dict[str, str]) -> str:
    parts = [
        _pick(row, ("description", "details", "transaction description", "narrative")),
        _pick(row, ("contact", "payee", "who")),
        _pick(row, ("reference", "payment ref", "payment reference", "bank reference")),
    ]
    return re.sub(r"\s+", " ", " ".join(p for p in parts if p)).strip()


def parse_csv(
    data: bytes | str,
    *,
    xero_account_id: str = "",
    xero_account_name: str = "",
) -> list[dict[str, Any]]:
    """Parse a Xero CSV export into reconciled statement rows.

    Supports the common Xero Bank statements / Account transactions report
    shapes: Date + Description/Reference + Status/Reconciled + Amount, or
    Spent/Received, or Debit/Credit columns.
    """
    text = _decode(data)
    rows = [r for r in csv.reader(io.StringIO(text)) if any((c or "").strip() for c in r)]
    if not rows:
        raise ValueError("The file is empty.")

    header_idx = -1
    header: list[str] = []
    for i, raw in enumerate(rows[:12]):
        normal = [_norm_header(c) for c in raw]
        has_date = any(c in normal for c in ("date", "transaction date", "posted date"))
        if has_date and (
            "status" in normal
            or "reconciled" in normal
            or "reconciliation status" in normal
            or "reconciled status" in normal
        ):
            header_idx = i
            header = normal
            break
    if header_idx < 0:
        raise ValueError(
            "Couldn't find Xero statement columns. Export a Xero report that "
            "includes Date, Amount/Spent/Debit and Status/Reconciled."
        )

    out: list[dict[str, Any]] = []
    bad_rows = 0
    for raw in rows[header_idx + 1:]:
        row = {
            header[col_i]: (raw[col_i] if col_i < len(raw) else "")
            for col_i in range(len(header))
            if header[col_i]
        }
        if not _is_reconciled(row):
            continue
        try:
            date_iso = _parse_date(_pick(row, ("date", "transaction date", "posted date")))
        except ValueError:
            bad_rows += 1
            continue

        direction = "unknown"
        spent = _parse_amount(_pick(row, ("spent", "debit", "debit amount", "money out")))
        received = _parse_amount(_pick(row, ("received", "credit", "credit amount", "money in")))
        if spent:
            amount = abs(spent)
            direction = "spent"
        elif received:
            amount = abs(received)
            direction = "received"
        else:
            amount_raw = _parse_amount(_pick(row, ("amount", "value", "total")))
            if amount_raw < 0:
                direction = "spent"
            amount = abs(amount_raw)
        amount = round(float(amount or 0), 2)
        if amount <= 0:
            bad_rows += 1
            continue

        account_name = _pick(row, ("account", "bank account", "account name")) or xero_account_name
        description = _row_description(row)
        seed = "|".join(
            [
                xero_account_id,
                account_name,
                date_iso,
                f"{amount:.2f}",
                direction,
                description,
            ]
        )
        out.append(
            {
                "id": "xstmt_" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24],
                "date": date_iso,
                "amount": amount,
                "direction": direction,
                "description": description,
                "xero_account_id": xero_account_id,
                "xero_account_name": account_name,
                "status": "reconciled",
            }
        )

    if not out:
        raise ValueError(
            "No reconciled rows were found in that Xero CSV"
            + (f" ({bad_rows} row(s) couldn't be read)." if bad_rows else ".")
        )
    return out


def _cutoff() -> str:
    return (
        dt.datetime.now(dt.timezone.utc).date()
        - dt.timedelta(days=RETENTION_DAYS)
    ).isoformat()


def _load(db_path: str) -> dict[str, dict[str, Any]]:
    raw = get_json_setting(db_path, _ROWS_KEY, {}) or {}
    return raw if isinstance(raw, dict) else {}


def ingest_csv(
    db_path: str,
    data: bytes | str,
    *,
    xero_account_id: str = "",
    xero_account_name: str = "",
) -> dict[str, Any]:
    parsed = parse_csv(
        data,
        xero_account_id=xero_account_id,
        xero_account_name=xero_account_name,
    )
    cutoff = _cutoff()
    store = {k: v for k, v in _load(db_path).items() if (v.get("date") or "") >= cutoff}
    added = 0
    skipped_dup = 0
    too_old = 0
    for row in parsed:
        if row["date"] < cutoff:
            too_old += 1
            continue
        if row["id"] in store:
            skipped_dup += 1
            continue
        store[row["id"]] = row
        added += 1
    set_json_setting(db_path, _ROWS_KEY, store)
    dates = sorted(v.get("date") or "" for v in store.values())
    set_json_setting(
        db_path,
        _META_KEY,
        {
            "last_upload_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "last_upload_added": added,
        },
    )
    return {
        "parsed": len(parsed),
        "added": added,
        "skipped_duplicates": skipped_dup,
        "too_old": too_old,
        "total": len(store),
        "date_from": dates[0] if dates else "",
        "date_to": dates[-1] if dates else "",
    }


def get_reconciled_rows(db_path: str) -> list[dict[str, Any]]:
    cutoff = _cutoff()
    rows = [v for v in _load(db_path).values() if (v.get("date") or "") >= cutoff]
    rows.sort(key=lambda r: (r.get("date") or "", r.get("id") or ""), reverse=True)
    return rows


def status(db_path: str) -> dict[str, Any]:
    rows = get_reconciled_rows(db_path)
    meta = get_json_setting(db_path, _META_KEY, {}) or {}
    dates = [r.get("date") or "" for r in rows if r.get("date")]
    return {
        "has_data": bool(rows),
        "row_count": len(rows),
        "date_from": min(dates) if dates else "",
        "date_to": max(dates) if dates else "",
        "last_upload_at": meta.get("last_upload_at") or "",
    }


def clear(db_path: str) -> None:
    set_json_setting(db_path, _ROWS_KEY, {})
    set_json_setting(db_path, _META_KEY, {})


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def _names_match(a: str = "", b: str = "") -> bool:
    a_norm = _norm(a)
    b_norm = _norm(b)
    if not a_norm or not b_norm:
        return False
    return a_norm in b_norm or b_norm in a_norm


def find_reconciled_match(
    rows: list[dict[str, Any]],
    *,
    amount: float,
    date: dt.date,
    xero_account_id: str = "",
    xero_account_name: str = "",
    description: str = "",
) -> dict[str, Any] | None:
    try:
        amount_f = round(float(amount or 0), 2)
    except (TypeError, ValueError):
        return None
    if amount_f <= 0 or not isinstance(date, dt.date):
        return None
    candidates = []
    for row in rows or []:
        if str(row.get("direction") or "unknown") == "received":
            continue
        try:
            row_amount = round(float(row.get("amount") or 0), 2)
            row_date = dt.date.fromisoformat(str(row.get("date") or "")[:10])
        except (TypeError, ValueError):
            continue
        if abs(row_amount - amount_f) > 0.02 or row_date != date:
            continue
        row_acct_id = str(row.get("xero_account_id") or "").strip()
        row_acct_name = str(row.get("xero_account_name") or "").strip()
        if xero_account_id and row_acct_id and row_acct_id != xero_account_id:
            continue
        if (
            not xero_account_id
            and xero_account_name
            and row_acct_name
            and not _names_match(xero_account_name, row_acct_name)
        ):
            continue
        desc_score = 0
        if description and row.get("description"):
            desc_score = 0 if _names_match(description, str(row.get("description") or "")) else 1
        candidates.append((desc_score, row))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0])
    return candidates[0][1]
