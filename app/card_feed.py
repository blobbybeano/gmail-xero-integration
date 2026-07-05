"""Unified card-feed facade: Enable Banking (automatic) + CSV upload (fallback).

The rest of the app (matcher, dump reconciliation, engineer feed,
subcontractor settlement) talks to THIS module through the same small surface
the old ``plaid_client`` / ``enable_banking_client`` modules exposed:

    is_connected(db)            -> bool  (any source has data)
    connection_status(db)       -> merged, safe dict for display / card linking
    get_cached_transactions(db) -> merged, normalised transactions

Transactions from both sources share one normalised shape
(``{transaction_id, account_id, date, amount, name, pending}``) with money OUT
positive, money IN negative, so nothing downstream cares where a line came
from.

Enable Banking pieces are re-exported for the /cardfeed routes; the CSV pieces
come from ``csv_card_feed``.
"""
from __future__ import annotations

from collections import Counter
from typing import Any

from . import csv_card_feed
from .enable_banking_client import (  # noqa: F401  (re-exported for routes)
    EnableBankingClient,
    connection_status as eb_connection_status,
    disconnect,
    get_cached_transactions as eb_get_cached_transactions,
    is_connected as eb_is_connected,
    store_session,
    sync_transactions,
)

# CSV feed surface (re-exported for the /cardfeed CSV routes)
ingest_csv = csv_card_feed.ingest_csv
csv_status = csv_card_feed.status
csv_clear = csv_card_feed.clear
get_account_labels = csv_card_feed.get_account_labels
set_account_label = csv_card_feed.set_account_label


def is_connected(db_path: str) -> bool:
    """True when ANY card-feed source has data to match against."""
    try:
        if eb_is_connected(db_path):
            return True
    except Exception:
        pass
    return csv_card_feed.has_data(db_path)


def connection_status(db_path: str) -> dict[str, Any]:
    """Merged, secret-free status: bank connection + CSV store.

    ``accounts`` contains every linkable card account across both sources so
    the expenses page can link an engineer to a CSV-fed account exactly like a
    bank-connected one.
    """
    try:
        eb = eb_connection_status(db_path)
    except Exception:
        eb = {"connected": False}
    try:
        cs = csv_card_feed.status(db_path)
    except Exception:
        cs = {"has_data": False, "accounts": [], "transaction_count": 0}

    if not (eb.get("connected") or cs.get("has_data")):
        return {"connected": False, "csv": cs}

    if eb.get("connected"):
        out = dict(eb)
    else:
        out = {
            "connected": True,
            "institution_name": "Bank CSV upload",
            "connected_at": "",
            "last_sync_at": cs.get("last_upload_at") or "",
            "valid_until": "",
            "transaction_count": 0,
        }
    accounts = list(out.get("accounts") or [])
    seen = {a.get("account_id") for a in accounts}
    for a in cs.get("accounts") or []:
        if a.get("account_id") not in seen:
            accounts.append(a)
    out["accounts"] = accounts
    out["transaction_count"] = (
        int(out.get("transaction_count") or 0)
        + int(cs.get("transaction_count") or 0)
    )
    out["csv"] = cs
    return out


def get_cached_transactions(db_path: str) -> list[dict[str, Any]]:
    """Every known card transaction across sources, normalised.

    If BOTH sources have data, the same real payment can appear twice under
    different ids (bank feed + statement upload). The CSV statement is
    authoritative, so bank-feed lines that match a CSV line on (date, amount)
    are dropped — count-aware, so genuinely repeated identical payments
    survive and bank-only lines (e.g. pending) are kept.
    """
    eb_txs: list[dict[str, Any]] = []
    try:
        if eb_is_connected(db_path):
            eb_txs = list(eb_get_cached_transactions(db_path) or [])
    except Exception:
        eb_txs = []
    try:
        csv_txs = csv_card_feed.get_transactions(db_path)
    except Exception:
        csv_txs = []

    if eb_txs and csv_txs:
        remaining = Counter(
            ((t.get("date") or ""), round(float(t.get("amount") or 0), 2))
            for t in csv_txs
        )
        kept = []
        for t in eb_txs:
            key = ((t.get("date") or ""),
                   round(float(t.get("amount") or 0), 2))
            if remaining.get(key, 0) > 0:
                remaining[key] -= 1
                continue
            kept.append(t)
        eb_txs = kept
    return eb_txs + csv_txs
