"""Date-tolerant matching of dump receipts to real card transactions.

Receipt OCR dates can't be trusted, so the algorithm the user specified is:

    1. price match   - exact to the penny (a tiny rounding tolerance only)
    2. ±1 month      - keep only candidates within ~31 days of the receipt date
    3. name compare  - if more than one survives, rank by merchant-name overlap;
                       an optional AI tie-break decides genuinely close calls.

The matcher is pure and side-effect free so it can be unit-reasoned about and
reused by both the per-receipt card-feed check and the batch reconciliation view.
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Any, Callable, Optional

# Words that carry no merchant identity (so "Esso Service Station" still matches
# a bank line that just says "ESSO").  Mirrors the dump's own merchant stop-list.
_STOP = {
    "service", "services", "station", "stations", "limited", "ltd",
    "commercial", "repairs", "repair", "garage", "petrol", "fuel",
    "road", "street", "the", "and", "plc", "uk", "gb", "store",
    "stores", "supermarket", "shop", "filling", "motors", "ltd.",
    "payment", "card", "purchase", "pos", "contactless",
}

PRICE_TOLERANCE = 0.01     # pennies of rounding slack only
WINDOW_DAYS = 31           # the user's "±1 month"


def _norm(s: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).strip()


def _tokens(s: Any) -> set[str]:
    return {t for t in _norm(s).split() if t and t not in _STOP and len(t) > 1}


def name_similarity(a: Any, b: Any) -> float:
    """0..1 overlap of meaningful merchant tokens (Jaccard, with a substring boost)."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    inter = ta & tb
    if inter:
        return len(inter) / len(ta | tb)
    # No shared tokens: allow a substring hit (e.g. "shellsalford" vs "shell").
    na, nb = _norm(a).replace(" ", ""), _norm(b).replace(" ", "")
    if na and nb and (na in nb or nb in na):
        return 0.34
    return 0.0


def _parse_date(value: Any) -> Optional[dt.date]:
    try:
        return dt.date.fromisoformat(str(value or "")[:10])
    except (ValueError, TypeError):
        return None


def match_receipt(
    amount: Optional[float],
    purchased_on: Any,
    merchant: Any,
    transactions: list[dict[str, Any]],
    account_ids: Optional[set[str]] = None,
    ai_tiebreak: Optional[Callable[[dict, list[dict]], Optional[str]]] = None,
) -> dict[str, Any]:
    """Return the best card transaction for one receipt.

    Result: {"status", "transaction", "candidates", "confidence", "reason"}
    where status is one of: "matched", "review", "no_match", "no_data".
    """
    if not transactions:
        return {"status": "no_data", "transaction": None, "candidates": [],
                "confidence": 0, "reason": "No card transactions available."}
    try:
        amt = round(float(amount), 2)
    except (TypeError, ValueError):
        return {"status": "no_match", "transaction": None, "candidates": [],
                "confidence": 0, "reason": "Receipt has no amount."}

    pool = transactions
    if account_ids:
        scoped = [t for t in pool if t.get("account_id") in account_ids]
        # Fall back to the full pool only if the scoping removed everything,
        # so a mis-tagged account can't silently hide every match.
        pool = scoped or pool

    # 1) price match (exact to the penny).
    priced = [t for t in pool if abs(float(t.get("amount") or 0.0) - amt) <= PRICE_TOLERANCE]
    if not priced:
        return {"status": "no_match", "transaction": None, "candidates": [],
                "confidence": 0, "reason": f"No card payment of £{amt:.2f}."}

    # 2) ±1 month window (only when we have a usable receipt date).
    rdate = _parse_date(purchased_on)
    windowed = priced
    if rdate is not None:
        windowed = [
            t for t in priced
            if (d := _parse_date(t.get("date"))) is not None
            and abs((d - rdate).days) <= WINDOW_DAYS
        ]
        if not windowed:
            # Price hits exist but all outside ±1 month — surface for a human,
            # since the receipt date may simply be wrong.
            ranked = sorted(
                priced, key=lambda t: name_similarity(merchant, t.get("name")), reverse=True
            )
            return {"status": "review", "transaction": ranked[0],
                    "candidates": ranked[:5], "confidence": 40,
                    "reason": f"£{amt:.2f} found but outside ±1 month of the receipt date."}

    # Single survivor -> confident match.
    if len(windowed) == 1:
        return {"status": "matched", "transaction": windowed[0],
                "candidates": windowed, "confidence": 95,
                "reason": f"Unique £{amt:.2f} card payment within ±1 month."}

    # 3) name comparison to break the tie.
    ranked = sorted(
        windowed, key=lambda t: name_similarity(merchant, t.get("name")), reverse=True
    )
    top = ranked[0]
    top_score = name_similarity(merchant, top.get("name"))
    second_score = name_similarity(merchant, ranked[1].get("name")) if len(ranked) > 1 else 0.0

    if top_score >= 0.5 and top_score - second_score >= 0.2:
        return {"status": "matched", "transaction": top, "candidates": ranked[:5],
                "confidence": 85,
                "reason": f"Best name match among {len(windowed)} same-priced payments."}

    # Genuinely ambiguous: let the optional AI tie-break decide.
    if ai_tiebreak is not None:
        try:
            chosen_id = ai_tiebreak(
                {"amount": amt, "date": str(purchased_on or ""), "merchant": str(merchant or "")},
                ranked[:5],
            )
        except Exception:
            chosen_id = None
        if chosen_id:
            chosen = next((t for t in ranked if t.get("transaction_id") == chosen_id), None)
            if chosen:
                return {"status": "matched", "transaction": chosen,
                        "candidates": ranked[:5], "confidence": 80,
                        "reason": "AI tie-break among same-priced card payments."}

    return {"status": "review", "transaction": top, "candidates": ranked[:5],
            "confidence": 50,
            "reason": f"{len(windowed)} card payments of £{amt:.2f} within ±1 month — needs a human."}


def match_summary(amount: Optional[float], purchased_on: Any, merchant: Any,
                  transactions: list[dict[str, Any]],
                  account_ids: Optional[set[str]] = None) -> str:
    """Compact status ('matched' / 'review' / 'missing' / '') for the card-feed column."""
    res = match_receipt(amount, purchased_on, merchant, transactions, account_ids)
    status = res["status"]
    if status == "matched":
        return "matched"
    if status == "review":
        return "review"
    if status == "no_match":
        return "missing"
    return ""  # no_data
