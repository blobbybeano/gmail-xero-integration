from __future__ import annotations

import datetime as dt
import hashlib
import itertools
import json
import os
import re
import uuid
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Callable

import requests

from .admin_store import get_cashflows_settings
from .config import AppConfig


MONEY = Decimal("0.01")
DEFAULT_LOOKBACK_DAYS = 14
DEFAULT_MATCH_WINDOW_DAYS = 5
MAX_COMBINATION_SIZE = 5


def _money(value: Any) -> Decimal:
    if value is None or value == "":
        return Decimal("0.00")
    if isinstance(value, Decimal):
        return value.quantize(MONEY, rounding=ROUND_HALF_UP)
    text = str(value).strip()
    text = text.replace(",", "").replace("£", "").replace("GBP", "").strip()
    text = re.sub(r"[^\d.\-]", "", text)
    if not text or text in {"-", ".", "-."}:
        return Decimal("0.00")
    try:
        return Decimal(text).quantize(MONEY, rounding=ROUND_HALF_UP)
    except InvalidOperation:
        return Decimal("0.00")


def _money_float(value: Decimal | Any) -> float:
    return float(_money(value))


def _date(value: Any) -> dt.date | None:
    if not value:
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    text = str(value).strip()
    if not text:
        return None
    xero_match = re.search(r"/Date\((\d+)", text)
    if xero_match:
        try:
            return dt.datetime.fromtimestamp(
                int(xero_match.group(1)) / 1000, tz=dt.timezone.utc
            ).date()
        except Exception:
            return None
    text = text.replace("Z", "+00:00")
    for candidate in (text, text[:10]):
        try:
            return dt.datetime.fromisoformat(candidate).date()
        except Exception:
            try:
                return dt.date.fromisoformat(candidate)
            except Exception:
                pass
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%d %b %Y", "%d %B %Y"):
        try:
            return dt.datetime.strptime(text[:20], fmt).date()
        except Exception:
            continue
    return None


def _date_text(value: dt.date | None) -> str:
    return value.isoformat() if value else ""


def _days_between(a: dt.date | None, b: dt.date | None) -> int | None:
    if not a or not b:
        return None
    return abs((a - b).days)


def _first(raw: dict[str, Any], *names: str) -> Any:
    lower_map = {str(k).lower(): v for k, v in raw.items()}
    for name in names:
        if name in raw:
            return raw.get(name)
        low = name.lower()
        if low in lower_map:
            return lower_map[low]
    return None


@dataclass
class XeroBankLine:
    id: str
    date: dt.date | None
    description: str
    amount: Decimal
    currency: str = "GBP"
    raw: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.date = _date(self.date)
        self.amount = _money(self.amount)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "date": _date_text(self.date),
            "description": self.description,
            "amount": _money_float(self.amount),
            "currency": self.currency,
        }


@dataclass
class CashflowsSettlement:
    id: str
    settlement_date: dt.date | None
    gross_amount: Decimal
    net_amount: Decimal
    fees: Decimal
    currency: str = "GBP"
    raw: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.settlement_date = _date(self.settlement_date)
        self.gross_amount = _money(self.gross_amount)
        self.net_amount = _money(self.net_amount)
        self.fees = _money(self.fees)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "settlement_date": _date_text(self.settlement_date),
            "gross_amount": _money_float(self.gross_amount),
            "net_amount": _money_float(self.net_amount),
            "fees": _money_float(self.fees),
            "currency": self.currency,
        }


@dataclass
class XeroInvoiceCandidate:
    id: str
    number: str
    contact_name: str
    contact_id: str
    date: dt.date | None
    due_date: dt.date | None
    amount_due: Decimal
    total: Decimal
    status: str = ""
    branding_theme_id: str = ""
    reference: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.date = _date(self.date)
        self.due_date = _date(self.due_date)
        self.amount_due = _money(self.amount_due)
        self.total = _money(self.total)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "number": self.number,
            "contact_name": self.contact_name,
            "contact_id": self.contact_id,
            "date": _date_text(self.date),
            "due_date": _date_text(self.due_date),
            "amount_due": _money_float(self.amount_due),
            "total": _money_float(self.total),
            "status": self.status,
            "branding_theme_id": self.branding_theme_id,
            "reference": self.reference,
        }


@dataclass
class ReconciliationMatch:
    id: str
    bank_line: XeroBankLine
    settlements: list[CashflowsSettlement]
    invoices: list[XeroInvoiceCandidate]
    method: str
    confidence: int
    merchant_fee: Decimal
    difference: Decimal
    missing_invoice_required: bool
    warning: str
    logic: str
    ai_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "bank_line": self.bank_line.to_dict(),
            "settlements": [s.to_dict() for s in self.settlements],
            "invoices": [i.to_dict() for i in self.invoices],
            "method": self.method,
            "confidence": int(self.confidence),
            "merchant_fee": _money_float(self.merchant_fee),
            "difference": _money_float(self.difference),
            "missing_invoice_required": bool(self.missing_invoice_required),
            "warning": self.warning,
            "logic": self.logic,
            "ai_reason": self.ai_reason,
        }


class CashflowsClient:
    def __init__(self, settings: dict[str, Any]):
        self.base_url = str(settings.get("base_url") or "").strip()
        self.configuration_id = str(settings.get("configuration_id") or "").strip()
        self.api_key = str(settings.get("api_key") or "").strip()
        self.timeout_seconds = int(settings.get("timeout_seconds") or 15)
        self.settlements_action = str(
            settings.get("settlements_action")
            or os.getenv("CASHFLOWS_SETTLEMENTS_ACTION")
            or "GetSettlementPayouts"
        ).strip() or "GetSettlementPayouts"

    @classmethod
    def from_config(cls, config: AppConfig) -> "CashflowsClient":
        return cls(get_cashflows_settings(config.admin_db_file))

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.api_key)

    def _headers(self, payload_text: str) -> dict[str, str]:
        hash_value = hashlib.sha512((payload_text + self.api_key).encode("utf-8")).hexdigest().upper()
        return {
            "Content-Type": "application/json",
            "ConfigurationId": self.configuration_id,
            "Hash": hash_value,
        }

    def _post_raw(self, payload: dict[str, Any]) -> requests.Response:
        if not self.configured:
            raise RuntimeError("Cashflows settings are incomplete.")
        payload_text = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        return requests.post(
            self.base_url,
            data=payload_text,
            headers=self._headers(payload_text),
            timeout=self.timeout_seconds,
        )

    def post(self, payload: dict[str, Any]) -> dict[str, Any]:
        resp = self._post_raw(payload)
        if resp.status_code >= 300:
            sample = (resp.text or "").strip().replace("\n", " ")[:300]
            raise RuntimeError(f"Cashflows HTTP {resp.status_code}: {sample}")
        try:
            return resp.json() or {}
        except Exception:
            return {"raw": resp.text or ""}

    def settlement_payload(self, start_date: dt.date, end_date: dt.date) -> dict[str, Any]:
        return {
            "Action": self.settlements_action,
            "DateFrom": start_date.isoformat(),
            "DateTo": end_date.isoformat(),
        }

    def diagnose_settlements(self, start_date: dt.date, end_date: dt.date) -> dict[str, Any]:
        payload = self.settlement_payload(start_date, end_date)
        safe_payload = dict(payload)
        try:
            resp = self._post_raw(payload)
        except Exception as exc:
            return {
                "ok": False,
                "configured": self.configured,
                "action": self.settlements_action,
                "status_code": None,
                "parsed_settlement_count": 0,
                "message": str(exc).splitlines()[0][:300],
                "request_payload": safe_payload,
                "sample": "",
            }
        sample = (resp.text or "").strip().replace("\n", " ").replace("\r", " ")[:300]
        parsed_count = 0
        parse_error = ""
        if resp.status_code < 300:
            try:
                parsed_count = len(parse_cashflows_settlements(resp.json() or {}))
            except Exception as exc:
                parse_error = str(exc).splitlines()[0][:180]
        ok = bool(resp.status_code < 300 and not parse_error)
        message = (
            f"Settlement read OK ({parsed_count} parsed settlement rows)."
            if ok
            else (
                f"Settlement read returned HTTP {resp.status_code}. "
                "Check Cashflows base URL/action/payload for settlement reports."
                if resp.status_code >= 300
                else f"Settlement response could not be parsed: {parse_error}"
            )
        )
        return {
            "ok": ok,
            "configured": self.configured,
            "action": self.settlements_action,
            "status_code": resp.status_code,
            "parsed_settlement_count": parsed_count,
            "message": message,
            "request_payload": safe_payload,
            "sample": sample,
        }

    def fetch_settlements(self, start_date: dt.date, end_date: dt.date) -> list[CashflowsSettlement]:
        payload = self.settlement_payload(start_date, end_date)
        data = self.post(payload)
        return parse_cashflows_settlements(data)


def _cashflows_records(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if not isinstance(data, dict):
        return []
    for key in (
        "Settlements",
        "settlements",
        "SettlementPayouts",
        "settlementPayouts",
        "Payouts",
        "payouts",
        "Batches",
        "batches",
        "Transactions",
        "transactions",
        "Items",
        "items",
        "Data",
        "data",
    ):
        value = data.get(key)
        if isinstance(value, list):
            return [x for x in value if isinstance(x, dict)]
        if isinstance(value, dict):
            nested = _cashflows_records(value)
            if nested:
                return nested
    return []


def parse_cashflows_settlements(data: Any) -> list[CashflowsSettlement]:
    settlements: list[CashflowsSettlement] = []
    for idx, raw in enumerate(_cashflows_records(data), start=1):
        settlement_date = _date(
            _first(raw, "SettlementDate", "settlementDate", "PayoutDate", "PaymentDate", "Date", "date")
        )
        net = _money(_first(raw, "NetAmount", "netAmount", "Net", "Amount", "SettlementAmount"))
        gross = _money(_first(raw, "GrossAmount", "grossAmount", "Gross", "TotalSales", "BatchGross"))
        fees = _money(_first(raw, "Fees", "FeeAmount", "MerchantFees", "Charges", "ProcessingFee"))
        if gross == Decimal("0.00") and net and fees:
            gross = (net + fees).quantize(MONEY)
        if fees == Decimal("0.00") and gross and net:
            fees = (gross - net).quantize(MONEY)
        settlement_id = str(
            _first(raw, "SettlementID", "settlementId", "PayoutID", "BatchID", "Id", "id", "Reference")
            or f"settlement-{idx}"
        ).strip()
        currency = str(_first(raw, "Currency", "currency") or "GBP").strip() or "GBP"
        if net == Decimal("0.00") and gross == Decimal("0.00"):
            continue
        settlements.append(
            CashflowsSettlement(
                id=settlement_id,
                settlement_date=settlement_date,
                gross_amount=gross,
                net_amount=net,
                fees=fees,
                currency=currency,
                raw=raw,
            )
        )
    return settlements


def parse_xero_bank_lines(data: Any) -> list[XeroBankLine]:
    if isinstance(data, dict):
        items = data.get("BankTransactions") or data.get("Statements") or data.get("BankStatementLines") or []
    else:
        items = data or []
    lines: list[XeroBankLine] = []
    for idx, raw in enumerate(items, start=1):
        if not isinstance(raw, dict):
            continue
        if raw.get("IsReconciled") is True:
            continue
        if str(raw.get("Status") or "").strip().upper() == "RECONCILED":
            continue
        line_items = raw.get("LineItems") or []
        first_line = line_items[0] if line_items and isinstance(line_items[0], dict) else {}
        description = str(
            raw.get("Reference")
            or raw.get("Description")
            or first_line.get("Description")
            or raw.get("Particulars")
            or ""
        ).strip()
        if not description.upper().startswith("CFE SETT"):
            continue
        amount = _money(
            raw.get("Total")
            or raw.get("Amount")
            or raw.get("LineAmount")
            or first_line.get("LineAmount")
            or first_line.get("UnitAmount")
        )
        line_id = str(
            raw.get("BankTransactionID")
            or raw.get("StatementLineID")
            or raw.get("ID")
            or raw.get("Id")
            or f"bank-line-{idx}"
        ).strip()
        lines.append(
            XeroBankLine(
                id=line_id,
                date=_date(raw.get("Date") or raw.get("date")),
                description=description,
                amount=amount,
                currency=str(raw.get("CurrencyCode") or raw.get("Currency") or "GBP"),
                raw=raw,
            )
        )
    return lines


def parse_xero_invoices(data: Any) -> list[XeroInvoiceCandidate]:
    if isinstance(data, dict):
        items = data.get("Invoices") or []
    else:
        items = data or []
    invoices: list[XeroInvoiceCandidate] = []
    for idx, raw in enumerate(items, start=1):
        if not isinstance(raw, dict):
            continue
        amount_due = _money(raw.get("AmountDue"))
        total = _money(raw.get("Total"))
        if amount_due <= Decimal("0.00") and total <= Decimal("0.00"):
            continue
        contact = raw.get("Contact") or {}
        invoices.append(
            XeroInvoiceCandidate(
                id=str(raw.get("InvoiceID") or raw.get("ID") or f"invoice-{idx}"),
                number=str(raw.get("InvoiceNumber") or raw.get("Reference") or f"Invoice {idx}"),
                contact_name=str(contact.get("Name") or raw.get("ContactName") or ""),
                contact_id=str(contact.get("ContactID") or raw.get("ContactID") or ""),
                date=_date(raw.get("Date")),
                due_date=_date(raw.get("DueDate")),
                amount_due=amount_due,
                total=total,
                status=str(raw.get("Status") or ""),
                branding_theme_id=str(raw.get("BrandingThemeID") or ""),
                reference=str(raw.get("Reference") or ""),
                raw=raw,
            )
        )
    return invoices


def _sum_money(values: list[Decimal]) -> Decimal:
    return sum((_money(v) for v in values), Decimal("0.00")).quantize(MONEY, rounding=ROUND_HALF_UP)


def _same_money(a: Decimal, b: Decimal) -> bool:
    return abs(_money(a) - _money(b)) <= MONEY / 2


def _near_date(a: dt.date | None, b: dt.date | None, *, days: int = DEFAULT_MATCH_WINDOW_DAYS) -> bool:
    diff = _days_between(a, b)
    return diff is not None and diff <= days


def _match_invoices(
    target_gross: Decimal,
    bank_date: dt.date | None,
    invoices: list[XeroInvoiceCandidate],
) -> list[XeroInvoiceCandidate]:
    nearby = [
        inv
        for inv in invoices
        if not inv.date or not bank_date or _near_date(inv.date, bank_date, days=14)
    ]
    for inv in nearby:
        if _same_money(inv.amount_due or inv.total, target_gross) or _same_money(inv.total, target_gross):
            return [inv]
    max_size = min(6, len(nearby))
    for size in range(2, max_size + 1):
        for combo in itertools.combinations(nearby, size):
            total = _sum_money([inv.amount_due or inv.total for inv in combo])
            if _same_money(total, target_gross):
                return list(combo)
    return []


def _logic_text(
    bank_line: XeroBankLine,
    settlements: list[CashflowsSettlement],
    invoices: list[XeroInvoiceCandidate],
    merchant_fee: Decimal,
) -> str:
    sett_ids = ", ".join(s.id for s in settlements) or "unmatched batch"
    invoice_numbers = ", ".join(i.number for i in invoices) or "placeholder invoice"
    return (
        f"Match Bank {bank_line.description} ({_date_text(bank_line.date)}, £{bank_line.amount}) "
        f"to Cashflows Batch {sett_ids}, allocate Fee £{merchant_fee} to Merchant Fees Account, "
        f"and close Invoice {invoice_numbers}."
    )


def _build_match(
    *,
    bank_line: XeroBankLine,
    settlements: list[CashflowsSettlement],
    invoices: list[XeroInvoiceCandidate],
    method: str,
    confidence: int,
    ai_reason: str = "",
) -> ReconciliationMatch:
    gross = _sum_money([s.gross_amount for s in settlements])
    net = _sum_money([s.net_amount for s in settlements])
    fees = _sum_money([s.fees for s in settlements])
    if fees == Decimal("0.00") and gross:
        fees = (gross - abs(bank_line.amount)).quantize(MONEY)
    difference = (abs(_money(bank_line.amount)) - net).quantize(MONEY)
    invoice_matches = invoices
    missing = not bool(invoice_matches)
    warning = "No Matching Invoice Found in Xero - Auto-Creation Required" if missing else ""
    return ReconciliationMatch(
        id=uuid.uuid4().hex[:12],
        bank_line=bank_line,
        settlements=settlements,
        invoices=invoice_matches,
        method=method,
        confidence=confidence,
        merchant_fee=fees,
        difference=difference,
        missing_invoice_required=missing,
        warning=warning,
        logic=_logic_text(bank_line, settlements, invoice_matches, fees),
        ai_reason=ai_reason,
    )


AiMatcher = Callable[
    [XeroBankLine, list[CashflowsSettlement], list[XeroInvoiceCandidate]],
    dict[str, Any] | None,
]


def match_reconciliation(
    bank_lines: list[XeroBankLine],
    settlements: list[CashflowsSettlement],
    invoices: list[XeroInvoiceCandidate],
    *,
    ai_matcher: AiMatcher | None = None,
    window_days: int = DEFAULT_MATCH_WINDOW_DAYS,
) -> list[ReconciliationMatch]:
    matches: list[ReconciliationMatch] = []
    used_settlements: set[str] = set()

    for bank in bank_lines:
        bank_amount = abs(_money(bank.amount))
        candidates = [
            s
            for s in settlements
            if s.id not in used_settlements and _near_date(bank.date, s.settlement_date, days=window_days)
        ]

        exact = next((s for s in candidates if _same_money(abs(s.net_amount), bank_amount)), None)
        if exact:
            matched_invoices = _match_invoices(exact.gross_amount, bank.date, invoices)
            used_settlements.add(exact.id)
            matches.append(
                _build_match(
                    bank_line=bank,
                    settlements=[exact],
                    invoices=matched_invoices,
                    method="strict_exact_net",
                    confidence=100,
                )
            )
            continue

        combo_match: list[CashflowsSettlement] = []
        max_size = min(MAX_COMBINATION_SIZE, len(candidates))
        for size in range(2, max_size + 1):
            for combo in itertools.combinations(candidates, size):
                net_total = _sum_money([abs(s.net_amount) for s in combo])
                if _same_money(net_total, bank_amount):
                    combo_match = list(combo)
                    break
            if combo_match:
                break
        if combo_match:
            matched_invoices = _match_invoices(
                _sum_money([s.gross_amount for s in combo_match]), bank.date, invoices
            )
            used_settlements.update(s.id for s in combo_match)
            matches.append(
                _build_match(
                    bank_line=bank,
                    settlements=combo_match,
                    invoices=matched_invoices,
                    method="combination_net_sum",
                    confidence=95,
                )
            )
            continue

        if ai_matcher:
            ai_payload = ai_matcher(bank, candidates, invoices)
            if ai_payload:
                wanted_settlements = {
                    str(x)
                    for x in (
                        ai_payload.get("settlement_ids")
                        or ai_payload.get("settlements")
                        or []
                    )
                }
                wanted_invoices = {
                    str(x)
                    for x in (
                        ai_payload.get("invoice_ids")
                        or ai_payload.get("invoices")
                        or []
                    )
                }
                ai_settlements = [s for s in candidates if s.id in wanted_settlements]
                ai_invoices = [i for i in invoices if i.id in wanted_invoices or i.number in wanted_invoices]
                if ai_settlements:
                    used_settlements.update(s.id for s in ai_settlements)
                    matches.append(
                        _build_match(
                            bank_line=bank,
                            settlements=ai_settlements,
                            invoices=ai_invoices,
                            method="ai_fuzzy_fallback",
                            confidence=max(0, min(100, int(ai_payload.get("confidence") or 0))),
                            ai_reason=str(ai_payload.get("reason") or ai_payload.get("explanation") or ""),
                        )
                    )
                    continue

        matches.append(
            ReconciliationMatch(
                id=uuid.uuid4().hex[:12],
                bank_line=bank,
                settlements=[],
                invoices=[],
                method="unmatched",
                confidence=0,
                merchant_fee=Decimal("0.00"),
                difference=bank_amount,
                missing_invoice_required=True,
                warning="No Cashflows settlement or Xero invoice match found",
                logic=(
                    f"No safe match found for Bank {bank.description} "
                    f"({_date_text(bank.date)}, £{bank.amount})."
                ),
            )
        )
    return matches


class OpenAIFuzzyMatcher:
    def __init__(self):
        self.enabled = os.getenv("CASHFLOWS_RECONCILE_AI_ENABLED", "false").lower() == "true"
        self.api_key = (os.getenv("OPENAI_API_KEY") or "").strip()
        self.model = (os.getenv("OPENAI_MODEL") or "gpt-5-mini").strip()

    @property
    def configured(self) -> bool:
        return bool(self.enabled and self.api_key)

    def __call__(
        self,
        bank_line: XeroBankLine,
        settlements: list[CashflowsSettlement],
        invoices: list[XeroInvoiceCandidate],
    ) -> dict[str, Any] | None:
        if not self.configured or not settlements:
            return None
        payload = {
            "bank_line": bank_line.to_dict(),
            "nearby_cashflows_settlements": [s.to_dict() for s in settlements[:12]],
            "open_xero_invoices": [i.to_dict() for i in invoices[:30]],
        }
        prompt = (
            "Return JSON only. Evaluate whether the CFE SETT bank line matches one or more "
            "Cashflows settlement batches and Xero invoices. Consider date shifts from weekends/"
            "bank holidays, fee variance where net bank deposit plus processing fee equals gross "
            "batch, and missing invoices. Schema: {\"settlement_ids\":[],\"invoice_ids\":[],"
            "\"confidence\":0,\"reason\":\"\"}.\n\n"
            + json.dumps(payload, ensure_ascii=False)
        )
        try:
            resp = requests.post(
                "https://api.openai.com/v1/responses",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.model,
                    "input": [
                        {
                            "role": "system",
                            "content": "You are a cautious accounting reconciliation assistant.",
                        },
                        {"role": "user", "content": prompt},
                    ],
                },
                timeout=45,
            )
            if resp.status_code >= 300:
                return None
            data = resp.json() or {}
            text = data.get("output_text") or ""
            if not text:
                for item in data.get("output") or []:
                    for part in item.get("content") or []:
                        if part.get("type") in {"output_text", "text"}:
                            text += part.get("text") or ""
            json_match = re.search(r"\{.*\}", text, flags=re.S)
            if not json_match:
                return None
            parsed = json.loads(json_match.group(0))
            return parsed if isinstance(parsed, dict) else None
        except Exception:
            return None


class CashflowsReconciliationService:
    def __init__(
        self,
        config: AppConfig,
        *,
        xero_client: Any | None = None,
        cashflows_client: CashflowsClient | None = None,
        ai_matcher: AiMatcher | None = None,
    ):
        self.config = config
        self.xero_client = xero_client
        self.cashflows_client = cashflows_client or CashflowsClient.from_config(config)
        self.ai_matcher = ai_matcher if ai_matcher is not None else OpenAIFuzzyMatcher()

    @property
    def production_enabled(self) -> bool:
        return (
            os.getenv("CASHFLOWS_RECONCILE_PRODUCTION", "false").lower() == "true"
            and not bool(self.config.dry_run)
        )

    def default_date_range(self) -> tuple[dt.date, dt.date]:
        days = max(int(os.getenv("CASHFLOWS_RECONCILE_DAYS", str(DEFAULT_LOOKBACK_DAYS)) or DEFAULT_LOOKBACK_DAYS), 1)
        end = dt.date.today()
        return end - dt.timedelta(days=days), end

    def scan(self, *, start_date: dt.date | None = None, end_date: dt.date | None = None) -> dict[str, Any]:
        start, end = self.default_date_range()
        start_date = start_date or start
        end_date = end_date or end
        if not self.xero_client:
            raise RuntimeError("Xero is not connected.")
        bank_lines = parse_xero_bank_lines(
            self.xero_client.get_bank_transactions(start_date=start_date, end_date=end_date)
        )
        settlements = self.cashflows_client.fetch_settlements(start_date, end_date)
        invoices = parse_xero_invoices(self.xero_client.get_open_invoices())
        matches = match_reconciliation(
            bank_lines,
            settlements,
            invoices,
            ai_matcher=self.ai_matcher,
        )
        preview_id = uuid.uuid4().hex
        match_payloads = [m.to_dict() for m in matches]
        for match_payload in match_payloads:
            match_payload["submission_preview"] = self.build_confirm_payloads(match_payload)
        return {
            "preview_id": preview_id,
            "testing_mode": not self.production_enabled,
            "date_from": start_date.isoformat(),
            "date_to": end_date.isoformat(),
            "counts": {
                "xero_cfe_bank_lines": len(bank_lines),
                "cashflows_settlements": len(settlements),
                "open_xero_invoices": len(invoices),
                "matches": len(matches),
            },
            "matches": match_payloads,
        }

    def build_confirm_payloads(self, match: dict[str, Any]) -> dict[str, Any]:
        bank = match.get("bank_line") or {}
        settlements = match.get("settlements") or []
        invoices = match.get("invoices") or []
        gross = _sum_money([_money(s.get("gross_amount")) for s in settlements])
        net = _money(bank.get("amount"))
        fee = _money(match.get("merchant_fee"))
        if fee == Decimal("0.00") and gross:
            fee = (gross - abs(net)).quantize(MONEY)
        bank_fees_account = (os.getenv("CASHFLOWS_BANK_FEES_ACCOUNT_CODE") or "404").strip()
        reference = str(bank.get("description") or "CFE SETT").strip()[:255]
        payment_account = str(getattr(self.xero_client, "payment_account_code", "") or "").strip()
        placeholder_invoice_payload = None
        if bool(match.get("missing_invoice_required")):
            placeholder_invoice_payload = {
                "Invoices": [
                    {
                        "Type": "ACCREC",
                        "Contact": {"Name": "Cashflows Settlement"},
                        "Date": bank.get("date") or dt.date.today().isoformat(),
                        "DueDate": bank.get("date") or dt.date.today().isoformat(),
                        "Reference": reference,
                        "Status": "AUTHORISED",
                        "LineItems": [
                            {
                                "Description": f"Cashflows settlement revenue {reference}",
                                "Quantity": 1,
                                "UnitAmount": _money_float(gross or abs(net)),
                                "AccountCode": getattr(self.xero_client, "sales_account_code", "200"),
                            }
                        ],
                    }
                ]
            }
        batch_payment_payload = {
            "BatchPayments": [
                {
                    "Account": {"Code": payment_account},
                    "Date": bank.get("date") or dt.date.today().isoformat(),
                    "Reference": reference,
                    "Payments": [
                        {
                            "Invoice": {"InvoiceID": inv.get("id")},
                            "Amount": _money_float(_money(inv.get("amount_due") or inv.get("total"))),
                        }
                        for inv in invoices
                        if inv.get("id")
                    ],
                    "Details": f"Cashflows reconciliation. Merchant fee £{fee}.",
                }
            ]
        }
        bank_fee_payload = None
        if fee > Decimal("0.00"):
            bank_fee_payload = {
                "BankTransactions": [
                    {
                        "Type": "SPEND",
                        "Contact": {"Name": "Cashflows"},
                        "Date": bank.get("date") or dt.date.today().isoformat(),
                        "Reference": f"{reference} merchant fee",
                        "BankAccount": {"Code": payment_account},
                        "LineItems": [
                            {
                                "Description": f"Merchant fees for {reference}",
                                "Quantity": 1,
                                "UnitAmount": _money_float(fee),
                                "AccountCode": bank_fees_account,
                            }
                        ],
                    }
                ]
            }
        return {
            "placeholder_invoice": placeholder_invoice_payload,
            "batch_payment": batch_payment_payload,
            "bank_fee": bank_fee_payload,
            "production_enabled": self.production_enabled,
        }

    def confirm(self, match: dict[str, Any]) -> dict[str, Any]:
        payloads = self.build_confirm_payloads(match)
        if not self.production_enabled:
            print(
                "[cashflows-sync] TEST MODE confirm payloads:\n"
                + json.dumps(payloads, indent=2, sort_keys=True),
                flush=True,
            )
            return {
                "mode": "testing",
                "message": "Test mode: no Xero writes were sent. Payloads printed to server console.",
                "payloads": payloads,
            }
        if not self.xero_client:
            raise RuntimeError("Xero is not connected.")
        results: dict[str, Any] = {"mode": "production", "payloads": payloads, "responses": {}}
        if payloads.get("placeholder_invoice"):
            placeholder_response = self.xero_client.create_invoice_payload(
                payloads["placeholder_invoice"]
            )
            results["responses"]["placeholder_invoice"] = placeholder_response
            created_invoice = ((placeholder_response.get("Invoices") or [{}])[0]) or {}
            created_invoice_id = str(created_invoice.get("InvoiceID") or "").strip()
            if created_invoice_id:
                batch = ((payloads.get("batch_payment") or {}).get("BatchPayments") or [{}])[0]
                if not batch.get("Payments"):
                    invoice_total = _money(
                        created_invoice.get("AmountDue")
                        or created_invoice.get("Total")
                        or (((payloads["placeholder_invoice"].get("Invoices") or [{}])[0]).get("LineItems") or [{}])[0].get("UnitAmount")
                    )
                    batch["Payments"] = [
                        {
                            "Invoice": {"InvoiceID": created_invoice_id},
                            "Amount": _money_float(invoice_total),
                        }
                    ]
        results["responses"]["batch_payment"] = self.xero_client.create_batch_payment_payload(
            payloads["batch_payment"]
        )
        if payloads.get("bank_fee"):
            results["responses"]["bank_fee"] = self.xero_client.create_bank_transaction_payload(
                payloads["bank_fee"]
            )
        return results
