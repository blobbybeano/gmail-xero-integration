"""
Cross-reference Cashflows card payments with Google Calendar events.

For each unmatched sale in the CSV preview, looks up calendar events in a
window around the payment date, extracts customer name and amount (using
structured parsing for well-formatted recent events, OpenAI for older /
loosely-formatted ones), then returns ranked suggestions so the user can
identify who the payment was from.

Phase 1: suggestions are display-only.  Nothing is written to Xero.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
from decimal import Decimal, InvalidOperation
from typing import Any

import requests

from .google_calendar import build_calendar_service
from .event_processor import extract_invoice_lines, parse_customer_fields, payment_choice

log = logging.getLogger(__name__)

# Search window either side of sale date (days).
_WINDOW_DAYS = 1
# Maximum loosely-formatted events sent to the AI parser (cost/perf guard).
_MAX_AI_EVENTS = 60
# Amount difference still counted as "near-match" for scoring.
_AMOUNT_NEAR = Decimal("5.00")
# Minimum score for a suggestion to be included.
_MIN_SCORE = 0.1
# Calendars to ignore when falling back to "all accessible" (no customer jobs).
_NOISE_HINTS = ("holiday", "weather", "birthday", "uk holidays")


# ---------------------------------------------------------------------------
# Amount extraction helpers
# ---------------------------------------------------------------------------

def _parse_amount_from_text(text: str) -> Decimal | None:
    """Extract the first plausible £ amount from arbitrary text."""
    text = text or ""
    # Patterns searched in priority order:
    #   £135, £135.00, £135 + VAT, 135+VAT, 135.00 + VAT
    for pat in (
        r"£\s*([\d,]+(?:\.\d{1,2})?)\s*\+?\s*vat",
        r"([\d,]+(?:\.\d{1,2})?)\s*\+\s*vat",
        r"£\s*([\d,]+(?:\.\d{1,2})?)",
    ):
        m = re.search(pat, text, re.I)
        if m:
            try:
                val = Decimal(m.group(1).replace(",", ""))
                if val > 0:
                    return val
            except InvalidOperation:
                pass
    return None


def _gross_from_invoice_lines(lines: list[dict]) -> Decimal | None:
    """Sum line items; apply 20 % VAT where TaxType == OUTPUT2."""
    if not lines:
        return None
    total = Decimal("0")
    for line in lines:
        amt = Decimal(str(line.get("UnitAmount") or 0))
        qty = Decimal(str(line.get("Quantity") or 1))
        sub = (amt * qty).quantize(Decimal("0.01"))
        if (line.get("TaxType") or "") == "OUTPUT2":
            sub = (sub * Decimal("1.20")).quantize(Decimal("0.01"))
        total += sub
    return total if total > 0 else None


# ---------------------------------------------------------------------------
# Event datetime helpers
# ---------------------------------------------------------------------------

def _event_end_dt(event: dict) -> dt.datetime | None:
    raw = (event.get("end") or {}).get("dateTime") or (event.get("end") or {}).get("date")
    if not raw:
        return None
    try:
        return dt.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except Exception:
        return None


def _event_start_dt(event: dict) -> dt.datetime | None:
    raw = (event.get("start") or {}).get("dateTime") or (event.get("start") or {}).get("date")
    if not raw:
        return None
    try:
        return dt.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except Exception:
        return None


def _mins_apart(a: dt.datetime, b: dt.datetime) -> int:
    return abs(int((a.replace(tzinfo=None) - b.replace(tzinfo=None)).total_seconds() / 60))


# ---------------------------------------------------------------------------
# Structured parser (no AI required)
# ---------------------------------------------------------------------------

def _parse_event_structured(event: dict) -> dict:
    """
    Extract customer name and amount from a well-formatted event.
    Returns {"customer": str, "event_gross": Decimal|None}.
    """
    summary = event.get("summary") or ""
    description = event.get("description") or ""

    customer = parse_customer_fields(description).get("name") or ""

    # Amount: prefer the [invoice] block (most reliable)
    lines = extract_invoice_lines(description)
    event_gross = _gross_from_invoice_lines(lines)

    # Fallback: scan title then description for £ amounts
    if event_gross is None:
        event_gross = (
            _parse_amount_from_text(summary)
            or _parse_amount_from_text(description)
        )

    return {"customer": customer, "event_gross": event_gross}


def _is_explicit_card_event(event: dict) -> bool:
    """
    Cashflows CSV sales are card-terminal payments. Calendar suggestions should
    therefore only use diary entries explicitly marked as CARD; INVOICE/CASH or
    unmarked entries are not evidence for a Cashflows card settlement.
    """
    return payment_choice(event.get("description") or "") == "card"


# ---------------------------------------------------------------------------
# AI batch parser (OpenAI)
# ---------------------------------------------------------------------------

def _ai_parse_events_batch(events: list[dict], api_key: str | None = None) -> list[dict]:
    """
    Send up to len(events) calendar events in ONE OpenAI call and get back a
    list of {"customer": str, "amount": float|null} objects (same order).
    Sale-agnostic: just extracts who the job was for and how much it cost.
    Returns [] on failure / API not configured.
    """
    if not api_key:
        api_key = (os.getenv("OPENAI_API_KEY") or "").strip()
    if not api_key or not events:
        return []

    model = (os.getenv("OPENAI_MODEL") or "gpt-4o-mini").strip()

    events_text = ""
    for i, ev in enumerate(events):
        title = (ev.get("summary") or "")[:200]
        notes = re.sub(r"<[^>]+>", " ", ev.get("description") or "")[:400]
        end = _event_end_dt(ev)
        end_str = end.strftime("%H:%M") if end else "?"
        events_text += f"\n[{i}] End: {end_str}  Title: {title}\nNotes: {notes}\n"

    prompt = (
        f"Below are {len(events)} Google Calendar events for a cleaning business.\n"
        "For each event (index 0 to N-1), extract the customer's full name and the total amount "
        "charged in £ (include VAT if mentioned as +VAT or +20%). "
        'Return a JSON array of objects in the same order: '
        '[{"customer": "...", "amount": 123.45}, ...]. '
        "Use null for any field you cannot determine. Return JSON only.\n"
        + events_text
    )

    try:
        resp = requests.post(
            "https://api.openai.com/v1/responses",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "input": [
                    {
                        "role": "system",
                        "content": (
                            "You extract structured data from calendar event notes. "
                            "Reply with a JSON array only — no markdown, no prose."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
            },
            timeout=30,
        )
        if resp.status_code >= 300:
            log.debug("AI batch parse HTTP %s", resp.status_code)
            return []

        data = resp.json() or {}
        text = data.get("output_text") or ""
        if not text:
            for item in data.get("output") or []:
                for part in item.get("content") or []:
                    if part.get("type") == "output_text":
                        text = part.get("text") or ""
                        break
                if text:
                    break

        text = text.strip()
        m = re.search(r"\[.*\]", text, re.S)
        if not m:
            return []
        parsed = json.loads(m.group())
        if not isinstance(parsed, list):
            return []
        return parsed
    except Exception as exc:
        log.debug("AI batch calendar parse failed: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def _score_parsed(
    parsed: dict,
    sale_gross: Decimal,
    sale_dt: dt.datetime | None,
) -> float:
    """
    Score 0.0–1.0 for a pre-parsed event against one sale.

    Amount match dominates (up to 0.6) because the card-settlement TIME in the
    Cashflows CSV often lags the actual job by hours, so time is only a weak
    secondary signal (up to 0.3).  A present customer name adds a small bump.
    """
    score = 0.0

    event_gross = parsed.get("event_gross")
    if event_gross is not None:
        diff = abs(event_gross - sale_gross)
        if diff <= Decimal("0.01"):
            score += 0.6
        elif diff <= Decimal("1.00"):
            score += 0.45
        elif diff <= _AMOUNT_NEAR:
            score += 0.2

    end = parsed.get("end_dt")
    if sale_dt and end:
        mins = _mins_apart(sale_dt, end)
        if mins <= 30:
            score += 0.3
        elif mins <= 90:
            score += 0.22
        elif mins <= 180:
            score += 0.12
        elif mins <= 360:
            score += 0.06

    if parsed.get("customer"):
        score += 0.05

    return round(min(score, 1.0), 3)


# ---------------------------------------------------------------------------
# Calendar resolution
# ---------------------------------------------------------------------------

def _resolve_calendars(service: Any, preferred_ids: list[str] | None) -> list[str]:
    """
    Decide which calendars to read.

    The stored "active" selection can become stale when the Google account is
    reconnected (the old IDs then 404).  So: use the preferred IDs only if they
    are actually accessible by the current token; otherwise fall back to ALL
    accessible calendars (minus holiday/weather/birthday noise).
    """
    try:
        cl = service.calendarList().list().execute()
    except Exception as exc:
        log.warning("calendarList lookup failed: %s", exc)
        return list(preferred_ids or [])

    accessible: dict[str, str] = {}
    for c in cl.get("items", []):
        cid = c.get("id")
        if cid:
            accessible[cid] = (c.get("summary") or "")

    if preferred_ids:
        ok = [c for c in preferred_ids if c in accessible]
        if ok:
            return ok
        log.info(
            "Stored calendar selection not accessible by current account; "
            "falling back to all accessible calendars"
        )

    usable: list[str] = []
    for cid, name in accessible.items():
        low = name.lower()
        if any(h in low for h in _NOISE_HINTS):
            continue
        if cid.endswith("#holiday@group.v.calendar.google.com"):
            continue
        if "#contacts@" in cid:
            continue
        usable.append(cid)
    return usable


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class CalendarPool:
    """A pre-fetched, pre-parsed set of calendar events for a date range.

    Built once per CSV preview, then queried per sale via ``suggest_for_sale``
    so we make exactly one calendar fetch and (at most) one AI batch call for
    the whole reconciliation, instead of one per unmatched sale.
    """

    def __init__(self, parsed: list[dict]):
        self._parsed = parsed

    def __len__(self) -> int:
        return len(self._parsed)

    def suggest_for_sale(
        self,
        sale_date: dt.date | None,
        sale_time_str: str | None,
        sale_gross: Decimal,
        *,
        window_days: int = _WINDOW_DAYS,
        limit: int = 5,
    ) -> list[dict]:
        """Return ranked calendar suggestions for one sale, best-first.

        Each suggestion dict has keys: customer, event_gross, event_summary,
        event_start, event_end, event_date, score, source.
        """
        if not sale_date or not self._parsed:
            return []

        sale_dt: dt.datetime | None = None
        if sale_time_str:
            try:
                t = dt.datetime.strptime(sale_time_str.strip()[:8], "%H:%M:%S")
                sale_dt = dt.datetime.combine(sale_date, t.time())
            except Exception:
                pass

        out: list[dict] = []
        for p in self._parsed:
            ev_date = p.get("date")
            if ev_date is None or abs((ev_date - sale_date).days) > window_days:
                continue
            score = _score_parsed(p, sale_gross, sale_dt)
            if score < _MIN_SCORE:
                continue
            start_dt = p.get("start_dt")
            end_dt = p.get("end_dt")
            eg = p.get("event_gross")
            out.append({
                "customer": p.get("customer") or "",
                "event_gross": float(eg) if eg is not None else None,
                "event_summary": (p["event"].get("summary") or "")[:120],
                "event_start": start_dt.strftime("%H:%M") if start_dt else "",
                "event_end": end_dt.strftime("%H:%M") if end_dt else "",
                "event_date": ev_date.isoformat(),
                "score": score,
                "source": p.get("source") or "structured",
            })

        out.sort(key=lambda s: s["score"], reverse=True)
        return out[:limit]


def _pack_event(ev: dict, customer: str, event_gross: Decimal | None, source: str) -> dict:
    start_dt = _event_start_dt(ev)
    end_dt = _event_end_dt(ev)
    return {
        "event": ev,
        "customer": customer,
        "event_gross": event_gross,
        "source": source,
        "start_dt": start_dt,
        "end_dt": end_dt,
        "date": start_dt.date() if start_dt else None,
    }


def build_calendar_pool(
    config: Any,
    date_min: dt.date,
    date_max: dt.date,
    preferred_ids: list[str] | None = None,
) -> CalendarPool:
    """Fetch + parse all timed events across accessible calendars in a range.

    Returns an empty pool (never raises) if Google Calendar is unavailable.
    """
    try:
        service = build_calendar_service(config)
    except Exception as exc:
        log.warning("Calendar unavailable for suggestion lookup: %s", exc)
        return CalendarPool([])

    cal_ids = _resolve_calendars(service, preferred_ids)
    if not cal_ids:
        return CalendarPool([])

    time_min = dt.datetime.combine(date_min, dt.time.min).isoformat() + "Z"
    time_max = dt.datetime.combine(date_max, dt.time.max).isoformat() + "Z"

    raw_events: list[dict] = []
    for cal_id in cal_ids:
        try:
            page: dict = (
                service.events()
                .list(
                    calendarId=cal_id,
                    singleEvents=True,
                    orderBy="startTime",
                    timeMin=time_min,
                    timeMax=time_max,
                    maxResults=2500,
                )
                .execute()
            )
            raw_events.extend(page.get("items", []))
        except Exception as exc:
            log.debug("Calendar %s fetch failed: %s", cal_id, exc)

    # Only timed, explicitly-CARD events can be scored on proximity / dated.
    events = [
        e
        for e in raw_events
        if "dateTime" in (e.get("start") or {}) and _is_explicit_card_event(e)
    ]
    if not events:
        return CalendarPool([])

    parsed: list[dict] = []
    needs_ai: list[dict] = []
    for ev in events:
        p = _parse_event_structured(ev)
        if p["customer"] or p["event_gross"] is not None:
            parsed.append(_pack_event(ev, p["customer"], p["event_gross"], "structured"))
        else:
            needs_ai.append(ev)

    try:
        from .admin_store import get_openai_settings as _get_oa
        _oa_key = _get_oa(config.admin_db_file).get("api_key", "")
    except Exception:
        _oa_key = ""
    _effective_api_key = _oa_key or (os.getenv("OPENAI_API_KEY") or "").strip()

    if needs_ai:
        batch = _ai_parse_events_batch(needs_ai[:_MAX_AI_EVENTS], api_key=_effective_api_key)
        for i, ev in enumerate(needs_ai):
            if i < _MAX_AI_EVENTS:
                item = batch[i] if i < len(batch) else {}
                customer = str(item.get("customer") or "")
                raw_amt = item.get("amount")
                try:
                    eg = Decimal(str(raw_amt)) if raw_amt is not None else None
                except InvalidOperation:
                    eg = None
            else:
                customer, eg = "", None
            # Fall back to the event title when neither structured parsing nor
            # AI yielded a customer name.  This keeps the event in the pool so
            # time-proximity scoring still works for events whose description is
            # empty or whose name is only in the title (e.g. "-SM4 W.C Tony Byrne").
            if not customer:
                customer = (ev.get("summary") or "")[:80]
            parsed.append(_pack_event(ev, customer, eg, "ai"))

    return CalendarPool(parsed)


def get_calendar_suggestions(
    config: Any,
    sale_date: dt.date | None,
    sale_time_str: str | None,
    sale_gross: Decimal,
    calendar_ids: list[str] | None,
    *,
    window_days: int = _WINDOW_DAYS,
) -> list[dict]:
    """Convenience wrapper: build a one-sale pool and return its suggestions.

    Prefer ``build_calendar_pool`` + ``suggest_for_sale`` when handling many
    sales, to avoid refetching the calendar for each one.
    """
    if not sale_date:
        return []
    pool = build_calendar_pool(
        config,
        sale_date - dt.timedelta(days=window_days),
        sale_date + dt.timedelta(days=window_days),
        calendar_ids,
    )
    return pool.suggest_for_sale(
        sale_date, sale_time_str, sale_gross, window_days=window_days
    )
