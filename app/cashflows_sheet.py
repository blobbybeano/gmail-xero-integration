"""
Fetch the public Cashflows–Xero correlation Google Sheet.

The sheet records every calendar booking with Payment Method (CARD / INVOICE)
and the corresponding Xero Invoice Number and GC Event ID.  Because the sheet
is publicly readable no OAuth credentials are required — a plain CSV export
URL suffices.
"""
from __future__ import annotations

import csv
import io
import urllib.request
from typing import NamedTuple


_CSV_EXPORT = "https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv"


class CardLookup(NamedTuple):
    gc_refs: frozenset[str]     # GC-YYYYMMDD-xxxx Event IDs for CARD rows
    inv_numbers: frozenset[str] # INV-XXXX numbers for CARD rows
    total_card: int             # total CARD rows found (for UI status)
    total_rows: int             # total data rows found


def fetch_card_lookup(sheet_id: str, timeout: int = 15) -> CardLookup:
    """
    Download the correlation sheet and return the sets of GC Event IDs and
    Invoice Numbers that correspond to CARD payments.

    Each CARD row in the sheet represents a job paid by card terminal
    (Cashflows).  The Event ID matches the Xero invoice Reference field
    (GC-YYYYMMDD-xxxx); the Invoice Number matches the Xero InvoiceNumber.

    Raises on network or parse failure — callers should catch and degrade
    gracefully (fall back to the GC- prefix heuristic).
    """
    url = _CSV_EXPORT.format(sheet_id=sheet_id)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")

    reader = csv.reader(io.StringIO(raw))
    rows = list(reader)
    if not rows:
        return CardLookup(frozenset(), frozenset(), 0, 0)

    header = rows[0]
    data_rows = rows[1:]

    def _col(row: list[str], name: str) -> str:
        try:
            return row[header.index(name)].strip()
        except (ValueError, IndexError):
            return ""

    gc_refs: set[str] = set()
    inv_numbers: set[str] = set()
    card_count = 0

    for row in data_rows:
        method = _col(row, "Payment Method").upper().strip()
        if method != "CARD":
            continue
        card_count += 1
        gc = _col(row, "Event ID")
        if gc.startswith("GC-"):
            gc_refs.add(gc)
        inv = _col(row, "Invoice Number")
        if inv:
            inv_numbers.add(inv)

    return CardLookup(
        gc_refs=frozenset(gc_refs),
        inv_numbers=frozenset(inv_numbers),
        total_card=card_count,
        total_rows=len(data_rows),
    )
