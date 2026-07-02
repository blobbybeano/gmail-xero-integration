"""Cashflows merchant-account CSV reconciliation (upload-based).

Phase 1 is preview/test only: this module READS Xero (open invoices and
unreconciled CFE SETT bank lines) and parses a manually downloaded Cashflows
merchant-account statement CSV, then produces a neat batch-by-batch preview of
what would be reconciled. It performs NO writes to Xero.

The "update basis" is enforced primarily by Xero's own state: only
unreconciled bank lines and open (unpaid) invoices are considered, so anything
already reconciled in Xero is naturally excluded. A local store of
already-reconciled payout references provides a secondary guard for batches
this app has previously processed.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import os
import re
import uuid
from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from .admin_store import get_cashflows_reconciled_refs, get_openai_settings
from .cashflows_calendar import build_calendar_pool
from .cashflows_sheet import CardLookup, fetch_card_lookup
from .cashflows_reconciliation import (
    MONEY,
    XeroBankLine,
    XeroInvoiceCandidate,
    _date,
    _date_text,
    _money,
    _money_float,
    parse_xero_bank_lines,
    parse_xero_invoices,
)

SALE_TYPE = "Sale Settlement"
FEE_TYPE = "Merchant Service Charge"
DECLINE_TYPE = "Decline Fee"
REMIT_TYPE = "Transfer for Remittance"

EXPECTED_COLUMNS = (
    "Ref",
    "Date",
    "Time",
    "Description",
    "Type",
    "Debit",
    "Credit",
    "Balance",
)

DEFAULT_INVOICE_MATCH_DAYS = 7
DEFAULT_BANK_MATCH_DAYS = 5
GROUP_TOLERANCE = Decimal("0.06")  # absorbs decline fees / sub-penny rounding
RECOMMENDED_OVERLAP_DAYS = 3
RECOMMENDED_DEFAULT_DAYS = 30

# Xero branding theme IDs — used to distinguish payment types.
# "Bank Account" themed invoices are bank-transfer payments; they are never
# settled via Cashflows card and must be excluded from card matching.
BANK_TRANSFER_THEME_ID = "a1ed21dc-a147-4917-91cf-51555edab4cf"

# Invoices created by this app from Google Calendar bookings carry a
# "GC-YYYYMMDD-xxxx" reference prefix.  Only PAID invoices with this prefix
# are eligible for Cashflows card matching — Stripe invoices have no GC prefix
# and must never be mistaken for card payments.
CALENDAR_REF_PREFIX = "GC-"


def _clean_cell(value: Any) -> str:
    text = str(value or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        text = text[1:-1].strip()
    return text


def _sale_ref_from_sale(description: str) -> str:
    text = re.sub(r"(?i)^sale\b", "", description or "").strip()
    return text.strip()


def _sale_ref_from_fee(description: str) -> str:
    if "Sale Ref:" in (description or ""):
        return description.split("Sale Ref:")[-1].strip()
    return ""


@dataclass
class CsvSale:
    csv_ref: str
    sale_ref: str
    date: dt.date | None
    matured: dt.date | None
    gross: Decimal
    fee: Decimal
    time: str = ""

    @property
    def net(self) -> Decimal:
        return (self.gross - self.fee).quantize(MONEY)

    def to_dict(self) -> dict[str, Any]:
        return {
            "csv_ref": self.csv_ref,
            "sale_ref": self.sale_ref,
            "date": _date_text(self.date),
            "time": self.time,
            "matured": _date_text(self.matured),
            "gross": _money_float(self.gross),
            "fee": _money_float(self.fee),
            "net": _money_float(self.net),
        }


@dataclass
class CsvPayout:
    csv_ref: str
    date: dt.date | None
    amount: Decimal
    balance_after: Decimal

    def to_dict(self) -> dict[str, Any]:
        return {
            "csv_ref": self.csv_ref,
            "date": _date_text(self.date),
            "amount": _money_float(self.amount),
            "balance_after": _money_float(self.balance_after),
        }


@dataclass
class ParsedStatement:
    sales: list[CsvSale]
    payouts: list[CsvPayout]
    decline_total: Decimal
    events: list[tuple[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def gross_total(self) -> Decimal:
        return sum((s.gross for s in self.sales), Decimal("0.00")).quantize(MONEY)

    @property
    def fee_total(self) -> Decimal:
        return sum((s.fee for s in self.sales), Decimal("0.00")).quantize(MONEY)

    @property
    def payout_total(self) -> Decimal:
        return sum((p.amount for p in self.payouts), Decimal("0.00")).quantize(MONEY)


class CsvParseError(ValueError):
    """Raised when the uploaded file is not a recognisable Cashflows statement."""


def parse_merchant_csv(text: str) -> ParsedStatement:
    if not text or not text.strip():
        raise CsvParseError("The uploaded file is empty.")

    rows = [r for r in csv.reader(io.StringIO(text)) if r and any(c.strip() for c in r)]
    if not rows:
        raise CsvParseError("No rows found in the uploaded file.")

    header = [_clean_cell(c) for c in rows[0]]
    header_lower = [h.lower() for h in header]
    index: dict[str, int] = {}
    for name in (
        "Ref",
        "Date",
        "Time",
        "Description",
        "Type",
        "Debit",
        "Credit",
        "Balance",
    ):
        if name.lower() in header_lower:
            index[name] = header_lower.index(name.lower())

    missing = [c for c in EXPECTED_COLUMNS if c not in index]
    if missing:
        raise CsvParseError(
            "This does not look like a Cashflows merchant-account statement. "
            f"Missing column(s): {', '.join(missing)}."
        )

    def cell(row: list[str], name: str) -> str:
        pos = index.get(name)
        if pos is None or pos >= len(row):
            return ""
        return _clean_cell(row[pos])

    sales: list[CsvSale] = []
    payouts: list[CsvPayout] = []
    events: list[tuple[str, Any]] = []
    warnings: list[str] = []
    decline_total = Decimal("0.00")
    pending_sales: dict[str, CsvSale] = {}

    # Cashflows writes one "Transfer for Remittance" row per matured sale, each
    # a small amount. These accumulate until the merchant-account Balance returns
    # to 0.00 — that zero-balance boundary marks one complete settlement run.
    # Cashflows then pays the run's total to the bank as a SINGLE "CFE SETT"
    # deposit, which is the one line Xero shows for reconciliation. So we must
    # aggregate each run into one payout (not one payout per remittance row).
    remit_run: list[tuple[str, Any, Decimal, Decimal]] = []

    def _flush_remit_run() -> None:
        if not remit_run:
            return
        total = sum((d for _, _, d, _ in remit_run), Decimal("0.00")).quantize(MONEY)
        last_ref, last_date, _, last_bal = remit_run[-1]
        payout = CsvPayout(
            csv_ref=last_ref,
            date=last_date,
            amount=total,
            balance_after=last_bal,
        )
        payouts.append(payout)
        events.append(("payout", payout))
        remit_run.clear()

    for raw in rows[1:]:
        row_type = cell(raw, "Type")
        csv_ref = cell(raw, "Ref")
        date = _date(cell(raw, "Date"))
        description = cell(raw, "Description")
        debit = _money(cell(raw, "Debit"))
        credit = _money(cell(raw, "Credit"))
        balance = _money(cell(raw, "Balance"))

        if row_type == SALE_TYPE:
            sale_ref = _sale_ref_from_sale(description)
            sale = CsvSale(
                csv_ref=csv_ref,
                sale_ref=sale_ref,
                date=date,
                matured=date,
                gross=credit,
                fee=Decimal("0.00"),
                time=cell(raw, "Time"),
            )
            sales.append(sale)
            if sale_ref:
                pending_sales[sale_ref] = sale
            events.append(("sale", sale))
        elif row_type == FEE_TYPE:
            sale_ref = _sale_ref_from_fee(description)
            target = pending_sales.get(sale_ref)
            if target is not None:
                target.fee = debit
            else:
                warnings.append(
                    f"Merchant service charge for sale {sale_ref or '?'} had no matching sale row."
                )
            events.append(("fee", debit))
        elif row_type == DECLINE_TYPE:
            decline_total = (decline_total + debit).quantize(MONEY)
            events.append(("decline", debit))
        elif row_type == REMIT_TYPE:
            remit_run.append((csv_ref, date, debit, balance))
            # Balance back to (approx) zero → this settlement run is complete.
            if abs(balance) < (MONEY / 2):
                _flush_remit_run()
        # Other / unknown row types are ignored (kept out of the maths).

    # Any trailing remittances that never closed to zero (statement cut mid-run)
    # still form a final batch so nothing is dropped.
    _flush_remit_run()

    if not sales and not payouts:
        raise CsvParseError(
            "No Cashflows sales or remittances were found in this file. "
            "Make sure you exported the merchant-account statement."
        )

    return ParsedStatement(
        sales=sales,
        payouts=payouts,
        decline_total=decline_total,
        events=events,
        warnings=warnings,
    )


def allocate_sales_to_payouts(
    statement: ParsedStatement,
) -> tuple[dict[str, dict[str, Any]], list[CsvSale]]:
    """Walk the statement in chronological order and assign matured sales to the
    remittance batches that paid them out, FIFO. Decline fees reduce the running
    balance and are absorbed into the batch they fall within.

    Returns a mapping of payout csv_ref -> {"sales": [CsvSale], "decline": Decimal,
    "variance": Decimal} plus the list of sales that have not been paid out yet.
    """
    queue: deque[tuple[str, Any, Decimal]] = deque()
    allocation: dict[str, dict[str, Any]] = {}

    for kind, obj in statement.events:
        if kind == "sale":
            queue.append(("sale", obj, obj.net))
        elif kind == "decline":
            queue.append(("decline", None, -Decimal(obj)))
        elif kind == "payout":
            payout: CsvPayout = obj
            target = payout.amount
            acc = Decimal("0.00")
            taken_sales: list[CsvSale] = []
            decline = Decimal("0.00")
            while queue:
                _kind, _obj, value = queue[0]
                # Stop if we've already reached the target within tolerance and the
                # next item would push us further away.
                if acc >= target - GROUP_TOLERANCE:
                    if abs((acc + value) - target) > abs(acc - target):
                        break
                acc = (acc + value).quantize(MONEY)
                queue.popleft()
                if _kind == "sale":
                    taken_sales.append(_obj)
                else:
                    decline = (decline + (-value)).quantize(MONEY)
                if abs(acc - target) <= MONEY / 2:
                    break
            allocation[payout.csv_ref] = {
                "sales": taken_sales,
                "decline": decline,
                "variance": (acc - target).quantize(MONEY),
            }

    leftover_sales = [obj for kind, obj, _v in queue if kind == "sale"]
    return allocation, leftover_sales


def _match_bank_line(
    payout: CsvPayout,
    bank_lines: list[XeroBankLine],
    used_ids: set[str],
) -> XeroBankLine | None:
    candidates = [
        b
        for b in bank_lines
        if b.id not in used_ids and abs(abs(b.amount) - payout.amount) <= MONEY / 2
    ]
    if not candidates:
        return None
    if payout.date:
        candidates.sort(
            key=lambda b: abs((b.date - payout.date).days) if b.date else 999
        )
    return candidates[0]


def _match_invoice_for_sale(
    sale: CsvSale,
    invoices: list[XeroInvoiceCandidate],
    used_ids: set[str],
    *,
    days: int = DEFAULT_INVOICE_MATCH_DAYS,
) -> tuple[XeroInvoiceCandidate | None, bool]:
    """Return (invoice, ambiguous). Matches on gross amount + date proximity."""
    candidates = [
        inv
        for inv in invoices
        if inv.id not in used_ids
        and (
            abs((inv.total or inv.amount_due) - sale.gross) <= MONEY / 2
            or abs(inv.amount_due - sale.gross) <= MONEY / 2
        )
    ]
    if not candidates:
        return None, False
    if sale.date:
        dated = [
            inv
            for inv in candidates
            if inv.date and abs((inv.date - sale.date).days) <= days
        ]
        pool = dated or candidates

        def _dist(inv: XeroInvoiceCandidate) -> int:
            return abs((inv.date - sale.date).days) if inv.date else 999

        pool.sort(key=_dist)
        best = pool[0]
        best_dist = _dist(best)
        # Genuine ambiguity = more than one candidate equally close by date.
        tied = [inv for inv in pool if _dist(inv) == best_dist]
        return best, len(tied) > 1
    # No sale date to disambiguate on: ambiguous when >1 amount candidate.
    return candidates[0], len(candidates) > 1


def _candidate_dict(inv: XeroInvoiceCandidate, sale: CsvSale) -> dict[str, Any]:
    inv_total = inv.total or inv.amount_due
    days = (
        abs((inv.date - sale.date).days)
        if (inv.date and sale.date)
        else None
    )
    return {
        "id": inv.id,
        "number": inv.number,
        "contact_name": inv.contact_name,
        "reference": inv.reference,
        "date": _date_text(inv.date),
        "total": _money_float(inv_total),
        "amount_due": _money_float(inv.amount_due),
        # An "open" (unpaid) invoice still has money due. Matching one of these
        # is what would flip it to PAID once a real (non-test) run submits.
        "is_open": inv.amount_due > Decimal("0.00"),
        "days_apart": days,
        "amount_match": abs(inv_total - sale.gross) <= (MONEY / 2),
    }


def _rank_candidate_invoices(
    sale: CsvSale,
    pool: list[XeroInvoiceCandidate],
    *,
    limit: int = 8,
) -> list[dict[str, Any]]:
    """Rank unaccounted invoices as manual-match suggestions for a missing sale.

    All unaccounted invoices are considered — the `amount_match` flag in each
    candidate dict already tells the user whether the amount is exact, and the
    sort order (exact amount first, then closest date) surfaces the most-likely
    matches at the top. A hard `limit` keeps the list compact.
    """
    def _key(inv: XeroInvoiceCandidate) -> tuple[int, int]:
        inv_total = inv.total or inv.amount_due
        amount_rank = 0 if abs(inv_total - sale.gross) <= (MONEY / 2) else 1
        days = (
            abs((inv.date - sale.date).days)
            if (inv.date and sale.date)
            else 9999
        )
        return (amount_rank, days)

    ranked = sorted(pool, key=_key)
    return [_candidate_dict(inv, sale) for inv in ranked[:limit]]


def build_csv_reconciliation_preview(
    config: Any,
    csv_text: str,
    *,
    xero_client: Any | None = None,
    correlation_sheet_id: str = "",
    calendar_ids: list[str] | None = None,
) -> dict[str, Any]:
    statement = parse_merchant_csv(csv_text)
    allocation, leftover_sales = allocate_sales_to_payouts(statement)

    reconciled_refs = get_cashflows_reconciled_refs(config.admin_db_file)

    # ---- Try to load the correlation sheet for reliable CARD detection ----
    card_lookup: CardLookup | None = None
    sheet_status = ""
    if correlation_sheet_id:
        try:
            card_lookup = fetch_card_lookup(correlation_sheet_id)
            sheet_status = (
                f"Loaded {card_lookup.total_card} CARD / "
                f"{card_lookup.total_rows} total rows from correlation sheet"
            )
        except Exception as sheet_exc:
            sheet_status = f"Sheet fetch failed: {str(sheet_exc)[:120]}"

    bank_lines: list[XeroBankLine] = []
    invoices: list[XeroInvoiceCandidate] = []
    xero_connected = bool(xero_client)
    xero_error = ""
    bank_scope_missing = False
    if xero_client:
        dates = [p.date for p in statement.payouts if p.date]
        dates += [s.date for s in statement.sales if s.date]
        start = min(dates) - dt.timedelta(days=DEFAULT_BANK_MATCH_DAYS) if dates else None
        end = max(dates) + dt.timedelta(days=DEFAULT_BANK_MATCH_DAYS) if dates else None

        # --- Bank transactions (requires accounting.banktransactions scope) ---
        # Fetch separately so a missing scope here doesn't block invoice matching.
        if start and end:
            try:
                bank_payload = xero_client.get_bank_transactions(start_date=start, end_date=end)
                bank_lines = parse_xero_bank_lines(bank_payload)
            except Exception as exc:  # pragma: no cover
                msg = str(exc)
                if "401" in msg or "AuthorizationUnsuccessful" in msg or "Unauthorized" in msg:
                    bank_scope_missing = True
                    xero_error = (
                        "Bank transaction scope missing (accounting.banktransactions). "
                        "In your Xero developer app, tick accounting.banktransactions, "
                        "save, then reconnect Xero in Settings."
                    )
                else:
                    xero_connected = False
                    xero_error = msg.splitlines()[0][:200]

        # --- Invoices (requires accounting.invoices scope — already granted) ---
        if xero_connected:
            try:
                # Card payments via Cashflows are marked PAID in Xero at transaction
                # time — they do NOT appear in get_open_invoices(). Fetch both:
                #   • PAID invoices for the CSV date range  (card payments already done)
                #   • OPEN invoices (AmountDue>0)           (not yet marked paid in Xero)
                # Then exclude "Bank Account" branding-themed invoices — those are
                # settled by bank transfer, not Cashflows card.
                open_invoices = parse_xero_invoices(xero_client.get_open_invoices())
                paid_invoices = (
                    parse_xero_invoices(
                        xero_client.get_paid_invoices(start_date=start, end_date=end)
                    )
                    if start and end and hasattr(xero_client, "get_paid_invoices")
                    else []
                )
                # Card-eligibility for the invoice pool.
                #
                # Priority 1 — correlation sheet is configured and loaded:
                #   the sheet records Payment Method at the time of booking, so a
                #   GC Event ID or Invoice Number appearing in its CARD rows is
                #   ground truth that the invoice was paid by card.
                #
                # Priority 2 — fallback heuristic / sheet-miss safety net:
                #   a GC- reference marks a calendar booking taken by card.
                #
                # The GC- reference (and correlation-sheet CARD rows) are the
                # authoritative signal that an invoice is a calendar-booked job
                # paid by card via Cashflows. This signal OVERRIDES the branding
                # theme: in some Xero orgs the "Bank Account" branding theme is
                # applied to genuine card invoices too, so the theme alone is NOT
                # a reliable bank-transfer indicator and must never drop an
                # invoice that carries a positive card signal.
                def _has_card_signal(inv: XeroInvoiceCandidate) -> bool:
                    if card_lookup is not None:
                        if inv.reference in card_lookup.gc_refs:
                            return True
                        if inv.number in card_lookup.inv_numbers:
                            return True
                    # Fallback heuristic (and sheet-miss safety net): a GC-
                    # reference marks a calendar booking taken by card.
                    return inv.reference.startswith(CALENDAR_REF_PREFIX)

                # Open invoices: keep everything except invoices that are BOTH
                # bank-transfer-themed AND lack any card signal (those are the
                # only ones we can be confident are settled by bank transfer).
                # Paid invoices: only those with a positive card signal, since a
                # paid invoice is a Cashflows card payment only if it was booked
                # via the calendar / logged in the correlation sheet.
                invoices = [
                    inv
                    for inv in open_invoices
                    if inv.branding_theme_id != BANK_TRANSFER_THEME_ID
                    or _has_card_signal(inv)
                ] + [
                    inv
                    for inv in paid_invoices
                    if _has_card_signal(inv)
                ]
            except Exception as exc:  # pragma: no cover - network/runtime guard
                xero_connected = False
                xero_error = str(exc).splitlines()[0][:200]

    used_bank_ids: set[str] = set()
    used_invoice_ids: set[str] = set()
    batches: list[dict[str, Any]] = []
    status_counts = {
        "ready": 0,
        "needs_review": 0,
        "waiting_invoices": 0,
        "no_bank_line": 0,
        "already_reconciled": 0,
    }

    for payout in statement.payouts:
        alloc = allocation.get(
            payout.csv_ref,
            {"sales": [], "decline": Decimal("0.00"), "variance": Decimal("0.00")},
        )
        batch_sales: list[CsvSale] = alloc["sales"]
        decline = alloc["decline"]
        variance = alloc["variance"]

        gross = sum((s.gross for s in batch_sales), Decimal("0.00")).quantize(MONEY)
        fees = sum((s.fee for s in batch_sales), Decimal("0.00")).quantize(MONEY)

        already_reconciled = str(payout.csv_ref) in reconciled_refs

        bank_line = None
        if xero_connected and not already_reconciled:
            bank_line = _match_bank_line(payout, bank_lines, used_bank_ids)
            if bank_line:
                used_bank_ids.add(bank_line.id)

        sale_rows: list[dict[str, Any]] = []
        matched_count = 0
        ambiguous_count = 0
        for sale in batch_sales:
            invoice = None
            ambiguous = False
            if xero_connected and not already_reconciled:
                invoice, ambiguous = _match_invoice_for_sale(
                    sale, invoices, used_invoice_ids
                )
                if invoice:
                    used_invoice_ids.add(invoice.id)
                    matched_count += 1
                if ambiguous:
                    ambiguous_count += 1
            sale_rows.append(
                {
                    **sale.to_dict(),
                    "invoice": invoice.to_dict() if invoice else None,
                    "ambiguous": bool(ambiguous),
                    # Shown in UI for unmatched sales so the user knows
                    # exactly what invoice to create/mark paid in Xero.
                    "needed_amount": _money_float(sale.gross) if not invoice else None,
                    "needed_date": _date_text(sale.date) if not invoice else None,
                    # Temp handle for the post-pass (stripped before JSON return).
                    "_sale": sale,
                    "candidates": [],
                    "tied_candidates": [],
                }
            )

        if already_reconciled:
            status = "already_reconciled"
        elif not bank_line:
            status = "no_bank_line"
        elif not batch_sales or matched_count < len(batch_sales):
            status = "waiting_invoices"
        elif ambiguous_count > 0:
            status = "needs_review"
        else:
            status = "ready"
        status_counts[status] += 1

        # Collect detail on unmatched sales so the UI can show exactly
        # what invoice needs to be created / marked paid in Xero.
        missing_invoices = [
            {
                "amount": r["needed_amount"],
                "date": r["needed_date"],
                "sale_ref": r.get("sale_ref", ""),
            }
            for r in sale_rows
            if r.get("needed_amount") is not None
        ]

        batches.append(
            {
                "id": uuid.uuid4().hex[:12],
                "payout": payout.to_dict(),
                "status": status,
                "gross": _money_float(gross),
                "fees": _money_float(fees),
                "decline_absorbed": _money_float(decline),
                "net": _money_float(payout.amount),
                "group_variance": _money_float(variance),
                "bank_line": bank_line.to_dict() if bank_line else None,
                "sales": sale_rows,
                "sale_count": len(batch_sales),
                "matched_invoice_count": matched_count,
                "missing_invoice_count": len(batch_sales) - matched_count,
                "ambiguous_count": ambiguous_count,
                "already_reconciled": already_reconciled,
                "missing_invoices": missing_invoices,
            }
        )

    # ---- Candidate and tied-alternative suggestions ----------------------
    # Now that the auto-match pass is complete, anything left in the invoice
    # pool is "unaccounted" and can be offered as a manual-match suggestion.
    unaccounted = [inv for inv in invoices if inv.id not in used_invoice_ids]

    # Build a single calendar event pool covering every sale date: one calendar
    # fetch + one AI batch for the whole preview, instead of one lookup per sale.
    # Returns an empty pool if Calendar is unavailable.
    cal_pool = None
    cal_sale_dates = [
        row["_sale"].date
        for batch in batches
        for row in batch["sales"]
        if row.get("_sale") is not None
        and getattr(row["_sale"], "date", None)
    ]
    if cal_sale_dates:
        try:
            cal_pool = build_calendar_pool(
                config,
                min(cal_sale_dates) - dt.timedelta(days=1),
                max(cal_sale_dates) + dt.timedelta(days=1),
                calendar_ids,
            )
        except Exception:
            cal_pool = None

    def _calendar_suggestions(sale_obj: Any) -> list[dict]:
        if cal_pool is None:
            return []
        try:
            return cal_pool.suggest_for_sale(
                sale_obj.date, sale_obj.time or None, sale_obj.gross
            )
        except Exception:
            return []

    for batch in batches:
        missing_candidate_count = 0
        for row in batch["sales"]:
            sale_obj = row.pop("_sale", None)
            if sale_obj is None:
                continue
            # Calendar cross-reference for EVERY row: a subtle confidence signal
            # on the favoured candidate (closest appointment + amount).
            row["calendar_suggestions"] = _calendar_suggestions(sale_obj)
            if row.get("invoice") is None:
                # Missing sale: offer ranked nearby invoices as manual picks.
                cands = _rank_candidate_invoices(sale_obj, unaccounted)
                row["candidates"] = cands
                missing_candidate_count += len(cands)
            elif row.get("ambiguous"):
                # Tied/ambiguous auto-match: the real "rivals" are the OTHER
                # same-amount invoices the app put on sibling sales in this very
                # batch, plus any still-unaccounted same-amount invoices. Surface
                # both so the user can confirm which customer this payment is for.
                target = sale_obj.gross
                current_id = (row.get("invoice") or {}).get("id")
                seen_ids: set[str] = {current_id} if current_id else set()
                alts: list[dict[str, Any]] = []
                # (a) same-amount invoices assigned to other sales in this batch
                for sib in batch["sales"]:
                    sib_inv = sib.get("invoice")
                    if not sib_inv or sib_inv.get("id") in seen_ids:
                        continue
                    if abs(Decimal(str(sib_inv.get("total") or 0)) - target) <= MONEY / 2:
                        alts.append({**sib_inv, "assigned_to": sib_inv.get("contact_name")})
                        seen_ids.add(sib_inv.get("id"))
                # (b) same-amount invoices not matched anywhere
                for inv in unaccounted:
                    if inv.id in seen_ids:
                        continue
                    if abs((inv.total or inv.amount_due) - target) <= MONEY / 2:
                        alts.append(_candidate_dict(inv, sale_obj))
                        seen_ids.add(inv.id)
                row["tied_candidates"] = alts[:5]
            else:
                # Cleanly matched: still surface any other exact-amount invoices so
                # the user can override the auto-pick from the dropdown if needed.
                target = sale_obj.gross
                current_id = (row.get("invoice") or {}).get("id")
                row["tied_candidates"] = [
                    _candidate_dict(inv, sale_obj)
                    for inv in unaccounted
                    if inv.id != current_id
                    and abs((inv.total or inv.amount_due) - target) <= MONEY / 2
                ][:5]
        batch["missing_candidate_count"] = missing_candidate_count

    waiting_sales = [
        {
            **s.to_dict(),
            "reason": "Sale has matured but no remittance/payout appears in this file yet.",
        }
        for s in leftover_sales
    ]

    bank_matched_count = (
        status_counts["ready"]
        + status_counts["needs_review"]
        + status_counts["waiting_invoices"]
    )
    active_batch_count = sum(
        v for k, v in status_counts.items() if k != "already_reconciled"
    )

    preview_id = uuid.uuid4().hex
    return {
        "preview_id": preview_id,
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "testing_mode": True,
        "xero_connected": xero_connected,
        "bank_scope_missing": bank_scope_missing,
        "xero_error": xero_error,
        "xero_invoice_count": len(invoices) if xero_connected else None,
        "xero_bank_tx_count": len(bank_lines) if (xero_connected and not bank_scope_missing) else None,
        "bank_matched_count": bank_matched_count,
        "active_batch_count": active_batch_count,
        "sheet_status": sheet_status,
        "sheet_card_count": card_lookup.total_card if card_lookup else None,
        "totals": {
            "gross_sales": _money_float(statement.gross_total),
            "merchant_fees": _money_float(statement.fee_total),
            "decline_fees": _money_float(statement.decline_total),
            "remitted": _money_float(statement.payout_total),
            "expected_remit": _money_float(
                (statement.gross_total - statement.fee_total - statement.decline_total)
            ),
            "sale_count": len(statement.sales),
            "payout_count": len(statement.payouts),
        },
        "status_counts": status_counts,
        "batches": batches,
        "unpaid_sales": waiting_sales,
        "warnings": statement.warnings,
        "ai_calendar_configured": bool(
            get_openai_settings(config.admin_db_file).get("api_key")
            or (os.getenv("OPENAI_API_KEY") or "").strip()
        ),
    }


def recommend_export_range(config: Any) -> dict[str, str]:
    """Suggest a date range to export from the Cashflows AMS portal next time.

    Starts a few days before the most recent reconciled payout (overlap is safe
    because of dedup) through today; falls back to the last 30 days if nothing
    has been reconciled yet.
    """
    reconciled = get_cashflows_reconciled_refs(config.admin_db_file)
    today = dt.date.today()
    last_date: dt.date | None = None
    for info in reconciled.values():
        d = _date(info.get("date")) if isinstance(info, dict) else None
        if d and (last_date is None or d > last_date):
            last_date = d
    if last_date:
        start = last_date - dt.timedelta(days=RECOMMENDED_OVERLAP_DAYS)
        reason = (
            f"Overlaps {RECOMMENDED_OVERLAP_DAYS} days before your last reconciled "
            "payout so nothing is missed. Already-reconciled rows are skipped automatically."
        )
    else:
        start = today - dt.timedelta(days=RECOMMENDED_DEFAULT_DAYS)
        reason = "No payouts reconciled yet — a 30-day window is a safe first import."
    return {
        "date_from": start.isoformat(),
        "date_to": today.isoformat(),
        "reason": reason,
    }
