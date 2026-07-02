"""Manual CSV card feed — the always-available fallback source of the
company card's real bank transactions.

Why this exists
---------------
Every free automated route to the company's Lloyds feed is closed (Enable
Banking has no Lloyds UK production support, GoCardless is closed to new
sign-ups, Plaid/TrueLayer have no free production tier, and Xero's
BankStatementsPlus endpoint is gated behind partner certification).  Lloyds
internet banking DOES let you export the account's transactions as a CSV, so
the admin uploads that file here instead.

Transactions are persisted in the admin DB and kept for up to a year
(``RETENTION_DAYS``), so the matcher always has data to work with even if
uploads are irregular.  Re-uploading overlapping exports is safe: every row
gets a deterministic id (hash of its content + its occurrence index within the
file), so duplicates from overlapping files collapse onto the same id.

Expected format (Lloyds Business export)::

    Transaction Date,Transaction Type,Sort Code,Account Number,
    Transaction Description,Debit Amount,Credit Amount,Balance
    02/07/2026,FPI,'30-93-53,60563768,STRIPE PAYMENTS UK ...,,295.10,40755.42

Normalisation matches the rest of the card-feed plumbing: money OUT (debit)
is positive, money IN (credit) is negative, and each row becomes::

    {transaction_id, account_id, date, amount, name, pending}

``account_id`` is the bank account number from the file — the value a
company-card engineer is linked to via ``expense_engineers.plaid_account_id``.
"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import re
from typing import Any

from .admin_store import get_json_setting, set_json_setting

_TX_KEY = "csv_card_transactions"
_META_KEY = "csv_card_meta"
_LABELS_KEY = "csv_account_labels"

#: Keep roughly a year of history so the matcher always has data to work with.
RETENTION_DAYS = 366

# Lloyds bank account CSV (internet banking → Statements → Export)
_BANK_REQUIRED = ("transaction date", "transaction description",
                  "debit amount", "credit amount")

# Lloyds credit card CSV (card account statement download)
_CC_REQUIRED = ("date", "description", "amount")


# ── parsing helpers ─────────────────────────────────────────────────────────
def _decode(data: bytes | str) -> str:
    if isinstance(data, str):
        return data
    for enc in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, ValueError):
            continue
    raise ValueError("The file doesn't look like a text CSV export.")


def _parse_date(raw: str) -> str:
    s = (raw or "").strip()
    for fmt in ("%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    raise ValueError(f"Unrecognised date: {s!r}")


def _parse_amount(raw: str) -> float:
    s = (raw or "").strip().replace(",", "").replace("£", "")
    if not s:
        return 0.0
    return float(s)


def _make_tx(account: str, date_iso: str, amount: float, name: str,
             seed_extra: str, seen_seeds: dict[str, int]) -> dict[str, Any]:
    """Build a normalised transaction dict with a deterministic id."""
    seed_base = f"{account}|{date_iso}|{amount:.2f}|{name}|{seed_extra}"
    occ = seen_seeds.get(seed_base, 0)
    seen_seeds[seed_base] = occ + 1
    tx_id = "csv_" + hashlib.sha256(
        f"{seed_base}|{occ}".encode("utf-8")
    ).hexdigest()[:24]
    return {
        "transaction_id": tx_id,
        "account_id": account,
        "date": date_iso,
        "amount": amount,
        "name": name,
        "pending": False,
    }


def _parse_bank_rows(rows: list[list[str]], idx: dict[str, int]) -> list[dict[str, Any]]:
    """Parse Lloyds bank account format rows (already past the header)."""
    def _cell(row: list[str], col: str) -> str:
        i = idx.get(col)
        return (row[i] or "").strip() if i is not None and i < len(row) else ""

    out: list[dict[str, Any]] = []
    seen_seeds: dict[str, int] = {}
    bad_rows = 0
    for row in rows:
        try:
            date_iso = _parse_date(_cell(row, "transaction date"))
            debit = _parse_amount(_cell(row, "debit amount"))
            credit = _parse_amount(_cell(row, "credit amount"))
        except ValueError:
            bad_rows += 1
            continue
        amount = round(debit if debit else -credit, 2)
        if amount == 0:
            bad_rows += 1
            continue
        name = re.sub(r"\s+", " ", _cell(row, "transaction description")).strip()
        account = re.sub(r"\D", "", _cell(row, "account number")) or "csvcard"
        raw_balance = _cell(row, "balance")
        try:
            balance = f"{_parse_amount(raw_balance):.2f}" if raw_balance else ""
        except ValueError:
            balance = ""
        out.append(_make_tx(account, date_iso, amount, name, balance, seen_seeds))
    if not out:
        raise ValueError(
            "No usable transactions found in the file"
            + (f" ({bad_rows} row(s) couldn't be read)." if bad_rows else ".")
        )
    return out


def _parse_cc_rows(rows: list[list[str]], idx: dict[str, int]) -> list[dict[str, Any]]:
    """Parse Lloyds credit card statement format rows (already past the header)."""
    def _cell(row: list[str], col: str) -> str:
        i = idx.get(col)
        return (row[i] or "").strip() if i is not None and i < len(row) else ""

    # Pre-pass: find the card ending used in this file so payment rows
    # (which have an empty Card Ending) are attributed to the same account.
    file_card_ending = ""
    for row in rows:
        ce = re.sub(r"\D", "", _cell(row, "card ending"))
        if ce:
            file_card_ending = ce
            break

    out: list[dict[str, Any]] = []
    seen_seeds: dict[str, int] = {}
    bad_rows = 0
    for row in rows:
        try:
            date_iso = _parse_date(_cell(row, "date"))
            amount = round(_parse_amount(_cell(row, "amount")), 2)
        except ValueError:
            bad_rows += 1
            continue
        if amount == 0:
            bad_rows += 1
            continue
        name = re.sub(r"\s+", " ", _cell(row, "description")).strip()
        card_ending = re.sub(r"\D", "", _cell(row, "card ending"))
        # Fall back to the file's card ending for payment rows with no card column.
        account = card_ending or file_card_ending or "cccard"
        ref = _cell(row, "reference")
        out.append(_make_tx(account, date_iso, amount, name, ref, seen_seeds))
    if not out:
        raise ValueError(
            "No usable transactions found in the file"
            + (f" ({bad_rows} row(s) couldn't be read)." if bad_rows else ".")
        )
    return out


def parse_csv(data: bytes | str) -> list[dict[str, Any]]:
    """Parse a Lloyds bank account or credit card CSV export into normalised transactions.

    Auto-detects the format:
    - **Bank account** (Statements → Export): columns include
      ``Transaction Date``, ``Debit Amount``, ``Credit Amount``.
    - **Credit card statement**: columns include ``Date``, ``Description``,
      ``Amount``, ``Card Ending``.

    Raises ``ValueError`` with a human-readable message when the format isn't
    recognised or no valid transactions are found.
    """
    text = _decode(data)
    reader = csv.reader(io.StringIO(text))
    rows = [r for r in reader if any((c or "").strip() for c in r)]
    if not rows:
        raise ValueError("The file is empty.")

    header = [(c or "").strip().lower() for c in rows[0]]
    idx = {name: i for i, name in enumerate(header) if name}
    header_set = set(header)

    if all(c in header_set for c in _BANK_REQUIRED):
        return _parse_bank_rows(rows[1:], idx)
    if all(c in header_set for c in _CC_REQUIRED):
        return _parse_cc_rows(rows[1:], idx)

    # Neither format matched — give a helpful error.
    if "transaction date" in header_set or "debit amount" in header_set:
        missing = [c for c in _BANK_REQUIRED if c not in header_set]
        raise ValueError(
            "Looks like a bank account CSV but missing column(s): "
            + ", ".join(missing) + "."
        )
    raise ValueError(
        "Unrecognised CSV format. Expected either the Lloyds bank account "
        "export (columns: Transaction Date, Debit Amount, Credit Amount) or "
        "the Lloyds credit card statement export (columns: Date, Description, "
        "Amount, Card Ending)."
    )


# ── storage ─────────────────────────────────────────────────────────────────
def _cutoff() -> str:
    return (
        dt.datetime.now(dt.timezone.utc).date()
        - dt.timedelta(days=RETENTION_DAYS)
    ).isoformat()


def _load(db_path: str) -> dict[str, dict[str, Any]]:
    raw = get_json_setting(db_path, _TX_KEY, {}) or {}
    return raw if isinstance(raw, dict) else {}


def ingest_csv(db_path: str, data: bytes | str) -> dict[str, Any]:
    """Parse + merge an uploaded CSV into the persistent store.

    Returns a summary dict for display.  Rows older than the retention window
    are ignored; rows already on file (same deterministic id) are skipped.
    """
    parsed = parse_csv(data)
    cutoff = _cutoff()
    store = _load(db_path)
    # Prune anything that has aged out before merging.
    store = {k: v for k, v in store.items() if (v.get("date") or "") >= cutoff}

    added = 0
    skipped_dup = 0
    too_old = 0
    for tx in parsed:
        if tx["date"] < cutoff:
            too_old += 1
            continue
        if tx["transaction_id"] in store:
            skipped_dup += 1
            continue
        store[tx["transaction_id"]] = tx
        added += 1

    set_json_setting(db_path, _TX_KEY, store)
    dates = sorted(v.get("date") or "" for v in store.values())
    meta = {
        "last_upload_at": dt.datetime.now(dt.timezone.utc)
        .strftime("%Y-%m-%d %H:%M UTC"),
        "last_upload_added": added,
    }
    set_json_setting(db_path, _META_KEY, meta)
    return {
        "parsed": len(parsed),
        "added": added,
        "skipped_duplicates": skipped_dup,
        "too_old": too_old,
        "total": len(store),
        "date_from": dates[0] if dates else "",
        "date_to": dates[-1] if dates else "",
    }


def get_transactions(db_path: str) -> list[dict[str, Any]]:
    """All stored CSV transactions inside the retention window, newest first."""
    cutoff = _cutoff()
    txs = [v for v in _load(db_path).values() if (v.get("date") or "") >= cutoff]
    txs.sort(key=lambda t: (t.get("date") or "", t.get("transaction_id") or ""),
             reverse=True)
    return txs


def has_data(db_path: str) -> bool:
    try:
        return bool(get_transactions(db_path))
    except Exception:
        return False


def clear(db_path: str) -> None:
    set_json_setting(db_path, _TX_KEY, {})
    set_json_setting(db_path, _META_KEY, {})
    set_json_setting(db_path, _LABELS_KEY, {})


# ── account labels (Xero bank account name per CSV account_id) ───────────────
def get_account_labels(db_path: str) -> dict[str, dict[str, str]]:
    """Return mapping: {csv_account_id: {xero_account_id, xero_account_name}}."""
    raw = get_json_setting(db_path, _LABELS_KEY, {}) or {}
    return raw if isinstance(raw, dict) else {}


def set_account_label(db_path: str, account_id: str, *,
                      xero_account_id: str, xero_account_name: str) -> None:
    """Associate a CSV account_id with a named Xero bank account."""
    labels = get_account_labels(db_path)
    labels[account_id] = {
        "xero_account_id": xero_account_id,
        "xero_account_name": xero_account_name,
    }
    set_json_setting(db_path, _LABELS_KEY, labels)


def status(db_path: str) -> dict[str, Any]:
    """Safe summary of the CSV store for display + account linking.

    ``accounts`` contains one entry per distinct account_id with its own
    transaction count and date range, so the UI can show a per-card breakdown.
    """
    txs = get_transactions(db_path)
    meta = get_json_setting(db_path, _META_KEY, {}) or {}

    # Build per-account stats in a single pass.
    acct_counts: dict[str, int] = {}
    acct_dates: dict[str, list[str]] = {}
    acct_months: dict[str, dict[str, int]] = {}  # account_id → {YYYY-MM: tx_count}
    for t in txs:
        acc = t.get("account_id") or ""
        if not acc:
            continue
        acct_counts[acc] = acct_counts.get(acc, 0) + 1
        d = t.get("date") or ""
        acct_dates.setdefault(acc, []).append(d)
        ym = d[:7]  # YYYY-MM
        if ym:
            acct_months.setdefault(acc, {})
            acct_months[acc][ym] = acct_months[acc].get(ym, 0) + 1

    accounts = []
    for acc, count in sorted(acct_counts.items()):
        dates = sorted(d for d in acct_dates.get(acc, []) if d)
        accounts.append({
            "account_id": acc,
            "name": "Bank account (CSV upload)",
            "mask": acc[-4:],
            "type": "csv",
            "subtype": "csv upload",
            "transaction_count": count,
            "date_from": dates[0] if dates else "",
            "date_to": dates[-1] if dates else "",
            "months": acct_months.get(acc, {}),
        })

    all_dates = [t.get("date") or "" for t in txs if t.get("date")]
    return {
        "has_data": bool(txs),
        "transaction_count": len(txs),
        "date_from": min(all_dates) if all_dates else "",
        "date_to": max(all_dates) if all_dates else "",
        "last_upload_at": meta.get("last_upload_at") or "",
        "accounts": accounts,
        "retention_days": RETENTION_DAYS,
    }
