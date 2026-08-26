from __future__ import annotations

import datetime as dt
import base64
import json
import os
import threading
import time
from urllib.parse import urlparse
from typing import Dict

import requests

from .config import AppConfig
from .admin_store import get_json_setting, get_xero_tenants

TOKEN_URL = "https://identity.xero.com/connect/token"
DEFAULT_SALES_ACCOUNT_CODE = "200"
DEFAULT_PAYMENT_ACCOUNT_CODE = "090"
_TOKEN_REFRESH_LOCK = threading.Lock()
_XERO_RATE_LIMIT_LOCK = threading.Lock()
_XERO_REQUEST_THROTTLE_LOCK = threading.Lock()
_XERO_RATE_LIMIT_UNTIL_TS = 0.0
_XERO_LAST_REQUEST_AT_TS = 0.0


def _log_xero_request(method: str, url: str, status: int | str, *, retry_after: str = "") -> None:
    """Emit a compact audit line for Xero traffic without logging customer payloads."""
    try:
        parsed = urlparse(url)
        path = parsed.path or url
        if parsed.query:
            path = f"{path}?{parsed.query[:160]}"
        suffix = f" retry_after={retry_after}" if retry_after else ""
        print(f"[xero-request] {str(method).upper()} {path} -> {status}{suffix}", flush=True)
    except Exception:
        pass


def _throttle_xero_request() -> None:
    """Optional process-wide pacing for Xero API calls.

    Xero counts all endpoints together for the same connection. Event-level
    guards are not enough because a single calendar event can do several
    requests: contact lookup, invoice mutation, online invoice URL, payment
    check. This throttle spaces those requests out before Xero has to reject us.
    """
    global _XERO_LAST_REQUEST_AT_TS
    raw_interval = os.getenv("XERO_MIN_REQUEST_INTERVAL_SECONDS", "").strip()
    try:
        min_interval = max(0.0, float(raw_interval or "0"))
    except Exception:
        min_interval = 0.0
    if min_interval <= 0:
        return
    with _XERO_REQUEST_THROTTLE_LOCK:
        now_ts = time.time()
        wait_for = (_XERO_LAST_REQUEST_AT_TS + min_interval) - now_ts
        if wait_for > 0:
            time.sleep(wait_for)
            now_ts = time.time()
        _XERO_LAST_REQUEST_AT_TS = now_ts


class XeroDisabledError(RuntimeError):
    """Raised when a Xero request is attempted while Xero is paused."""


def xero_is_disabled() -> bool:
    """Global kill-switch. When the XERO_DISABLED env var is truthy, the app
    makes NO outbound requests to Xero (API calls and token refreshes alike).
    Used to fully pause Xero traffic, e.g. while rate-limited / locked out."""
    return os.getenv("XERO_DISABLED", "").strip().lower() in ("1", "true", "yes", "on")


def guard_xero(action: str = "Xero request") -> None:
    if xero_is_disabled():
        raise XeroDisabledError(
            f"{action} blocked: Xero is paused (XERO_DISABLED is set). "
            "No data is being sent to Xero."
        )


def persisted_xero_lockout_until(config: AppConfig) -> float:
    """Return the persisted Xero lockout timestamp without making network calls."""
    try:
        with open(config.state_file, "r", encoding="utf-8") as f:
            state = json.load(f)
        return float(state.get("xero_lockout_until_ts") or 0.0)
    except Exception:
        return 0.0


def xero_lockout_is_active(config: AppConfig) -> bool:
    return persisted_xero_lockout_until(config) > time.time()


def get_xero_rate_limit_until_ts() -> float:
    with _XERO_RATE_LIMIT_LOCK:
        return float(_XERO_RATE_LIMIT_UNTIL_TS or 0.0)


def record_xero_rate_limit_from_response(response, *, default_seconds: int = 300) -> int:
    """Record a process-wide Xero cooldown from a 429 response.

    Returns the cooldown seconds applied, or 0 when the response was not a 429.
    """
    global _XERO_RATE_LIMIT_UNTIL_TS
    if getattr(response, "status_code", None) != 429:
        return 0
    retry_after_seconds = int(default_seconds or 300)
    raw_retry = str(getattr(response, "headers", {}).get("Retry-After") or "").strip()
    if raw_retry.isdigit():
        try:
            retry_after_seconds = max(60, int(raw_retry))
        except Exception:
            retry_after_seconds = int(default_seconds or 300)
    with _XERO_RATE_LIMIT_LOCK:
        _XERO_RATE_LIMIT_UNTIL_TS = max(
            _XERO_RATE_LIMIT_UNTIL_TS,
            time.time() + retry_after_seconds,
        )
    return retry_after_seconds


def _short_reference(event: Dict) -> str:
    event_id = (event.get("id") or "").strip()
    date_raw = (event.get("start") or "").split("T", 1)[0].replace("-", "")
    suffix = event_id[-4:] if event_id else "0000"
    if date_raw:
        return f"GC-{date_raw}-{suffix}"
    return f"GC-{suffix}"


class XeroClient:
    def __init__(
        self,
        access_token: str,
        tenant_id: str,
        dry_run: bool = True,
        *,
        client_id: str = "",
        client_secret: str = "",
        refresh_token: str = "",
        token_file: str = "",
        sales_account_code: str = "",
        payment_account_code: str | None = None,
        branding_theme_id: str = "",
        premium_theme_id: str = "",
        premium_threshold: float | None = None,
    ):
        self.access_token = access_token
        self.tenant_id = tenant_id
        self.dry_run = dry_run
        self.base_url = "https://api.xero.com/api.xro/2.0"
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self.token_file = token_file
        self.sales_account_code = sales_account_code or DEFAULT_SALES_ACCOUNT_CODE
        self.payment_account_code = (
            DEFAULT_PAYMENT_ACCOUNT_CODE
            if payment_account_code is None
            else (payment_account_code or "")
        )
        self.branding_theme_id = branding_theme_id or ""
        self.premium_theme_id = premium_theme_id or ""
        self.premium_threshold = premium_threshold

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Xero-tenant-id": self.tenant_id,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _refresh_access_token(self) -> bool:
        with _TOKEN_REFRESH_LOCK:
            latest = load_xero_token(self.token_file) if self.token_file else {}
            latest_access = latest.get("access_token", "")
            latest_refresh = latest.get("refresh_token", "")
            if latest_access and latest_refresh:
                # Another thread/worker may have already refreshed and rotated tokens.
                self.access_token = latest_access
                self.refresh_token = latest_refresh
                if not token_is_expired(latest):
                    return True

            refresh_token_value = self.refresh_token or latest_refresh
            if not refresh_token_value:
                return False

            try:
                refreshed = refresh_xero_token(
                    client_id=self.client_id,
                    client_secret=self.client_secret,
                    refresh_token=refresh_token_value,
                )
            except Exception as exc:
                # Xero refresh tokens are one-time use; if another thread rotated it,
                # recover by adopting the newest saved token from disk.
                newest = load_xero_token(self.token_file) if self.token_file else {}
                newest_access = newest.get("access_token", "")
                newest_refresh = newest.get("refresh_token", "")
                if newest_access and newest_refresh and (
                    newest_access != self.access_token or newest_refresh != self.refresh_token
                ):
                    self.access_token = newest_access
                    self.refresh_token = newest_refresh
                    if not token_is_expired(newest):
                        return True
                print(f"[xero] token refresh failed: {exc}", flush=True)
                return False

            refreshed["tenant_id"] = self.tenant_id
            save_xero_token(self.token_file, refreshed)
            self.access_token = refreshed.get("access_token", self.access_token)
            self.refresh_token = refreshed.get("refresh_token", self.refresh_token)
            return True

    def _request(self, method: str, url: str, **kwargs):
        global _XERO_RATE_LIMIT_UNTIL_TS
        now_ts = time.time()
        if now_ts < _XERO_RATE_LIMIT_UNTIL_TS:
            remaining = int(max(1, _XERO_RATE_LIMIT_UNTIL_TS - now_ts))
            _log_xero_request(method, url, "blocked-local-cooldown", retry_after=str(remaining))
            raise RuntimeError(
                f"Xero rate-limited: 429 cooldown active (Retry-After={remaining}s)"
            )

        extra_headers = kwargs.pop("headers", None) or {}
        headers = self._headers()
        headers.update(extra_headers)

        _throttle_xero_request()
        response = requests.request(method, url, headers=headers, timeout=30, **kwargs)
        _log_xero_request(
            method,
            response.url or url,
            response.status_code,
            retry_after=str(response.headers.get("Retry-After") or "").strip(),
        )
        if response.status_code == 401 and self._refresh_access_token():
            _throttle_xero_request()
            headers = self._headers()
            headers.update(extra_headers)
            response = requests.request(
                method, url, headers=headers, timeout=30, **kwargs
            )
            _log_xero_request(
                method,
                response.url or url,
                response.status_code,
                retry_after=str(response.headers.get("Retry-After") or "").strip(),
            )
        record_xero_rate_limit_from_response(response)
        return response

    def get_organisation(self) -> Dict:
        url = f"{self.base_url}/Organisation"
        response = self._request("GET", url)
        response.raise_for_status()
        return response.json()

    def _pick_branding_theme(self, line_items: list | None) -> str:
        """Return the branding theme ID to use based on line-item pre-VAT subtotal."""
        if self.premium_theme_id and self.premium_threshold is not None:
            subtotal = sum(
                float(li.get("UnitAmount", 0)) * float(li.get("Quantity", 1))
                for li in (line_items or [])
            )
            if subtotal >= self.premium_threshold:
                return self.premium_theme_id
        return self.branding_theme_id

    def create_invoice_from_event(
        self, event: Dict, contact: Dict | None = None, line_items: list | None = None
    ) -> Dict:
        """
        Create a draft ACCREC invoice referencing the event.
        Branding theme is chosen automatically based on pre-VAT subtotal vs premium threshold.
        """
        today = dt.date.today().isoformat()
        contact_payload = (
            {"ContactID": contact.get("ContactID")}
            if contact and contact.get("ContactID")
            else {"Name": event.get("summary") or "Calendar Event"}
        )
        prepared_line_items = self._prepare_line_items(line_items)
        payload = {
            "Type": "ACCREC",
            "Contact": contact_payload,
            "Date": today,
            "DueDate": (dt.date.today() + dt.timedelta(days=7)).isoformat(),
            "LineItems": prepared_line_items
            if prepared_line_items
            else [
                {
                    "Description": event.get("description") or "Calendar event",
                    "Quantity": 1,
                    "UnitAmount": 0,
                    "AccountCode": self.sales_account_code,
                }
            ],
            "Reference": _short_reference(event),
            "Status": "DRAFT",
        }
        theme_id = self._pick_branding_theme(prepared_line_items or line_items)
        if theme_id:
            payload["BrandingThemeID"] = theme_id

        if self.dry_run:
            return {"dry_run": True, "payload": payload, "Contacts": []}

        url = f"{self.base_url}/Invoices"
        response = self._request("POST", url, json=payload)
        if not response.ok:
            raise RuntimeError(
                f"Xero invoice create failed: {response.status_code} {response.text}"
            )
        return response.json()

    def update_invoice(
        self, invoice_id: str, contact: Dict | None = None, line_items: list | None = None
    ) -> Dict:
        prepared_line_items = self._prepare_line_items(line_items)
        theme_id = self._pick_branding_theme(prepared_line_items or line_items)
        invoice_payload: Dict[str, object] = {
            "InvoiceID": invoice_id,
            "Contact": contact if contact else None,
            "LineItems": prepared_line_items if prepared_line_items else [],
        }
        if theme_id:
            invoice_payload["BrandingThemeID"] = theme_id
        payload = {
            "Invoices": [
                invoice_payload
            ]
        }
        if self.dry_run:
            return {"dry_run": True, "payload": payload}
        url = f"{self.base_url}/Invoices"
        response = self._request("POST", url, json=payload)
        if not response.ok:
            raise RuntimeError(
                f"Xero invoice update failed: {response.status_code} {response.text}"
            )
        return response.json()

    def create_simple_invoice(
        self,
        contact_name: str,
        description: str,
        amount: float,
        reference: str = "",
        invoice_date: str | None = None,
        status: str = "AUTHORISED",
    ) -> Dict:
        """
        Create a small standalone ACCREC invoice whose TOTAL equals `amount`
        (tax inclusive), raised to a standard catch-all contact. Used by the
        Cashflows reconciliation "quick invoice" action to give a stray card
        payment (e.g. parking) something to reconcile against in Xero.
        """
        today = invoice_date or dt.date.today().isoformat()
        payload = {
            "Type": "ACCREC",
            "Contact": {"Name": (contact_name or "Sundry")},
            "Date": today,
            "DueDate": today,
            "LineAmountTypes": "Inclusive",
            "LineItems": [
                {
                    "Description": description or "Card payment",
                    "Quantity": 1,
                    "UnitAmount": round(float(amount), 2),
                    "AccountCode": self.sales_account_code,
                }
            ],
            "Reference": reference or "",
            "Status": status,
        }
        if self.dry_run:
            return {"dry_run": True, "payload": payload}
        url = f"{self.base_url}/Invoices"
        response = self._request("POST", url, json=payload)
        if not response.ok:
            raise RuntimeError(
                f"Xero quick invoice create failed: {response.status_code} {response.text}"
            )
        return response.json()

    def create_bill(
        self,
        *,
        contact: Dict,
        line_items: list,
        reference: str = "",
        bill_date: str | None = None,
        due_date: str | None = None,
        status: str = "AUTHORISED",
        line_amount_types: str = "Inclusive",
    ) -> Dict:
        """Create an ACCPAY purchase bill (money owed to a supplier, e.g. a
        subcontractor). ``contact`` is a Xero contact ref ({"ContactID": ...} or
        {"Name": ...}); ``line_items`` are pre-built Xero line dicts (Description,
        Quantity, UnitAmount, AccountCode, optional TaxType). Returns the created
        invoice dict (with InvoiceID) or, in dry-run, the payload.
        """
        today = bill_date or dt.date.today().isoformat()
        payload = {
            "Type": "ACCPAY",
            "Contact": contact,
            "Date": today,
            "DueDate": due_date or today,
            "LineAmountTypes": line_amount_types,
            "LineItems": line_items,
            "Reference": reference or "",
            "Status": status,
        }
        if self.dry_run:
            return {"dry_run": True, "payload": payload}
        url = f"{self.base_url}/Invoices"
        response = self._request("POST", url, json=payload)
        if not response.ok:
            raise RuntimeError(
                f"Xero bill create failed: {response.status_code} {response.text}"
            )
        data = response.json()
        invoices = data.get("Invoices") or []
        if not invoices:
            raise RuntimeError("Xero bill create returned empty invoice list")
        return invoices[0]

    def _prepare_line_items(self, line_items: list | None) -> list:
        if not line_items:
            return []
        prepared = []
        for item in line_items:
            line = dict(item)
            if not line.get("AccountCode") and not line.get("AccountID"):
                line["AccountCode"] = self.sales_account_code
            prepared.append(line)
        return prepared

    def _line_update_signature(self, line_item: Dict) -> tuple[str, float, float, str, str, str]:
        desc = " ".join(str((line_item or {}).get("Description") or "").split()).lower()
        try:
            qty = round(float((line_item or {}).get("Quantity") or 1.0), 4)
        except Exception:
            qty = 1.0
        try:
            unit = round(float((line_item or {}).get("UnitAmount") or 0.0), 4)
        except Exception:
            unit = 0.0
        tax_type = str((line_item or {}).get("TaxType") or "").strip().upper()
        account_code = str((line_item or {}).get("AccountCode") or "").strip()
        account_id = str((line_item or {}).get("AccountID") or "").strip()
        return desc, qty, unit, tax_type, account_code, account_id

    def _attach_existing_line_item_ids(
        self, invoice_id: str, prepared_line_items: list[Dict]
    ) -> list[Dict]:
        try:
            current = self.get_invoice(invoice_id)
        except Exception:
            return prepared_line_items

        existing_by_signature: dict[tuple[str, float, float, str, str, str], list[str]] = {}
        for line in current.get("LineItems") or []:
            line_id = str((line or {}).get("LineItemID") or "").strip()
            if not line_id:
                continue
            sig = self._line_update_signature(line)
            existing_by_signature.setdefault(sig, []).append(line_id)

        out: list[Dict] = []
        for line in prepared_line_items:
            next_line = dict(line)
            if not next_line.get("LineItemID"):
                ids = existing_by_signature.get(self._line_update_signature(next_line)) or []
                if ids:
                    next_line["LineItemID"] = ids.pop(0)
            out.append(next_line)
        return out

    def get_online_invoice_url(self, invoice_id: str) -> str | None:
        """
        Return the public Online Invoice URL if available.
        Do not fall back to internal portal URLs that may require Xero login.
        """
        url = f"{self.base_url}/Invoices/{invoice_id}/OnlineInvoice"
        response = self._request("GET", url)
        if not response.ok:
            return None
        data = response.json()
        online = data.get("OnlineInvoices", [{}])[0]
        public_url = str(online.get("OnlineInvoiceUrl") or "").strip()
        if not public_url:
            return None
        if public_url.startswith("http://") or public_url.startswith("https://"):
            return public_url
        return None

    def get_invoice(self, invoice_id: str) -> Dict:
        url = f"{self.base_url}/Invoices/{invoice_id}"
        response = self._request("GET", url)
        if not response.ok:
            raise RuntimeError(
                f"Xero invoice fetch failed: {response.status_code} {response.text}"
            )
        data = response.json()
        invoices = data.get("Invoices") or []
        if not invoices:
            raise RuntimeError("Xero invoice fetch returned empty invoice list")
        return invoices[0]

    def get_payments_to_account(
        self,
        account_code: str,
        *,
        end_date: "dt.date | None" = None,
    ) -> Dict:
        """Fetch payments coded against a given account, up to ``end_date``.

        Used by the Receipt Dump subcontractor-balancing feature to total what
        has actually been paid to a recurring account. Returns
        ``{"Payments": [...], "total": <float>}``. Subject to the global Xero
        kill-switch via ``_request``.
        """
        code = str(account_code or "").strip()
        if not code:
            return {"Payments": [], "total": 0.0}
        clauses = [f'Account.Code=="{code}"', 'Status=="AUTHORISED"']
        if end_date is not None:
            clauses.append(
                f"Date<=DateTime({end_date.year},{end_date.month},{end_date.day})"
            )
        where = "&&".join(clauses)
        all_items: list[Dict] = []
        page = 1
        while True:
            response = self._request(
                "GET",
                f"{self.base_url}/Payments",
                params={"where": where, "page": page},
            )
            if not response.ok:
                raise RuntimeError(
                    f"Xero payments fetch failed: {response.status_code} {response.text}"
                )
            items = (response.json() or {}).get("Payments") or []
            all_items.extend(items)
            if len(items) < 100:
                break
            page += 1
        total = 0.0
        for p in all_items:
            try:
                total += float(p.get("Amount") or 0)
            except (TypeError, ValueError):
                continue
        return {"Payments": all_items, "total": round(total, 2)}

    def get_payments(
        self,
        *,
        start_date: "dt.date | None" = None,
        end_date: "dt.date | None" = None,
    ) -> Dict:
        """Fetch authorised payments in a bounded date range."""
        clauses = ['Status=="AUTHORISED"']
        if start_date is not None:
            clauses.append(
                f"Date>=DateTime({start_date.year},{start_date.month},{start_date.day})"
            )
        if end_date is not None:
            clauses.append(
                f"Date<=DateTime({end_date.year},{end_date.month},{end_date.day})"
            )
        where = "&&".join(clauses)
        all_items: list[Dict] = []
        page = 1
        while True:
            response = self._request(
                "GET",
                f"{self.base_url}/Payments",
                params={"where": where, "page": page},
            )
            if not response.ok:
                raise RuntimeError(
                    f"Xero payments fetch failed: {response.status_code} {response.text}"
                )
            items = (response.json() or {}).get("Payments") or []
            all_items.extend(items)
            if len(items) < 100:
                break
            page += 1
        return {"Payments": all_items}

    def get_purchase_bills(
        self,
        *,
        start_date: "dt.date | None" = None,
        end_date: "dt.date | None" = None,
    ) -> Dict:
        """Fetch supplier bills (ACCPAY) in a bounded date range."""
        clauses = ['Type=="ACCPAY"']
        if start_date is not None:
            clauses.append(
                f"Date>=DateTime({start_date.year},{start_date.month},{start_date.day})"
            )
        if end_date is not None:
            clauses.append(
                f"Date<=DateTime({end_date.year},{end_date.month},{end_date.day})"
            )
        where = "&&".join(clauses)
        all_items: list[Dict] = []
        page = 1
        while True:
            response = self._request(
                "GET",
                f"{self.base_url}/Invoices",
                params={"where": where, "page": page},
            )
            if not response.ok:
                raise RuntimeError(
                    f"Xero purchase bills fetch failed: {response.status_code} {response.text}"
                )
            items = (response.json() or {}).get("Invoices") or []
            all_items.extend(items)
            if len(items) < 100:
                break
            page += 1
        return {"Invoices": all_items}

    def get_attachments(self, endpoint: str, guid: str) -> list:
        """List attachments on a Xero object (e.g. endpoint='BankTransactions'
        or 'Invoices'). Returns the Attachments list (possibly empty)."""
        if not (endpoint and guid):
            return []
        response = self._request(
            "GET", f"{self.base_url}/{endpoint}/{guid}/Attachments"
        )
        if not response.ok:
            raise RuntimeError(
                f"Xero attachments list failed: {response.status_code} {response.text}"
            )
        return (response.json() or {}).get("Attachments") or []

    def get_attachment_content(
        self, endpoint: str, guid: str, filename: str
    ) -> "tuple[bytes, str]":
        """Download a single attachment's bytes from a Xero object.

        Returns (content_bytes, mime_type). Used to retrieve a previously
        submitted receipt image from Xero for cross-person duplicate checks.
        """
        response = self._request(
            "GET",
            f"{self.base_url}/{endpoint}/{guid}/Attachments/{filename}",
            headers={**self._headers(), "Accept": "*/*"},
        )
        if not response.ok:
            raise RuntimeError(
                f"Xero attachment fetch failed: {response.status_code} {response.text}"
            )
        return response.content, response.headers.get("Content-Type", "")

    def attach_file_to_object(
        self, endpoint: str, guid: str, filename: str, content_type: str, data: bytes
    ) -> dict:
        """Attach a file to a Xero object, e.g. Invoices or BankTransactions."""
        import re as _re

        endpoint = (endpoint or "").strip().strip("/")
        if endpoint not in {"Invoices", "BankTransactions"}:
            raise ValueError(f"Unsupported Xero attachment endpoint: {endpoint}")
        safe_name = (_re.sub(r"[^\w.\-]", "_", filename or "receipt.jpg")[:100] or "receipt.jpg")
        if self.dry_run:
            return {
                "dry_run": True,
                "endpoint": endpoint,
                "guid": guid,
                "filename": safe_name,
                "content_type": content_type,
                "size_bytes": len(data),
            }
        guard_xero("Xero attachment upload")
        url = f"{self.base_url}/{endpoint}/{guid}/Attachments/{safe_name}"
        hdrs = {k: v for k, v in self._headers().items() if k != "Content-Type"}
        hdrs["Content-Type"] = content_type
        _throttle_xero_request()
        resp = requests.request("PUT", url, headers=hdrs, data=data, timeout=60)
        _log_xero_request(
            "PUT",
            resp.url or url,
            resp.status_code,
            retry_after=str(resp.headers.get("Retry-After") or "").strip(),
        )
        if resp.status_code == 401 and self._refresh_access_token():
            hdrs["Authorization"] = f"Bearer {self.access_token}"
            _throttle_xero_request()
            resp = requests.request("PUT", url, headers=hdrs, data=data, timeout=60)
            _log_xero_request(
                "PUT",
                resp.url or url,
                resp.status_code,
                retry_after=str(resp.headers.get("Retry-After") or "").strip(),
            )
        record_xero_rate_limit_from_response(resp)
        if not resp.ok:
            raise RuntimeError(
                f"Xero attachment failed ({resp.status_code}): {resp.text[:300]}"
            )
        return resp.json()

    def attach_file_to_invoice(
        self, invoice_id: str, filename: str, content_type: str, data: bytes
    ) -> dict:
        """Attach a file to a Xero invoice via PUT /Invoices/{id}/Attachments/{name}.

        Dry-run returns a preview dict without writing to Xero.
        """
        return self.attach_file_to_object(
            "Invoices", invoice_id, filename, content_type, data
        )

    def attach_file_to_bank_transaction(
        self, bank_transaction_id: str, filename: str, content_type: str, data: bytes
    ) -> dict:
        """Attach a file to a Xero spend/bank transaction."""
        return self.attach_file_to_object(
            "BankTransactions", bank_transaction_id, filename, content_type, data
        )

    def delete_draft_invoice(self, invoice_id: str) -> Dict:
        """
        Remove an invoice from active receivables.

        Behavior:
        - DRAFT invoices are set to DELETED
        - non-DRAFT invoices are set to VOIDED

        Idempotent:
        - already DELETED/VOIDED is treated as success
        """
        target_status = "DELETED"
        try:
            current = self.get_invoice(invoice_id)
            current_status = str(current.get("Status") or "").upper()
            if current_status in {"DELETED", "VOIDED"}:
                return {"Invoices": [current]}
            if current_status and current_status != "DRAFT":
                target_status = "VOIDED"
        except Exception:
            # If lookup fails, fall back to DELETED attempt first.
            current_status = ""

        payload = {"Invoices": [{"InvoiceID": invoice_id, "Status": target_status}]}
        if self.dry_run:
            return {"dry_run": True, "payload": payload}
        url = f"{self.base_url}/Invoices"
        response = self._request("POST", url, json=payload)
        if response.ok:
            body = response.json()
            try:
                refreshed = self.get_invoice(invoice_id)
                refreshed_status = str(refreshed.get("Status") or "").upper()
                if refreshed_status in {"DELETED", "VOIDED"}:
                    return {"Invoices": [refreshed]}
            except Exception:
                # Some tenants stop returning DELETED invoices by id; treat successful POST as success.
                return body
            # API accepted but invoice still active: return body for higher-level handling/logging.
            return body
        else:
            try:
                body = response.json()
                elements = body.get("Elements") or body.get("Invoices") or []
                if elements:
                    status = str((elements[0] or {}).get("Status") or "").upper()
                    if status in {"DELETED", "VOIDED"}:
                        return body
                # Fallback idempotency check from latest invoice state.
                refreshed = self.get_invoice(invoice_id)
                refreshed_status = str(refreshed.get("Status") or "").upper()
                if refreshed_status in {"DELETED", "VOIDED"}:
                    return body
            except Exception:
                pass
            raise RuntimeError(
                f"Xero invoice void failed: {response.status_code} {response.text}"
            )

    def authorize_invoice(self, invoice_id: str, *, issue_date: str | None = None) -> Dict:
        invoice_payload: Dict[str, str] = {
            "InvoiceID": invoice_id,
            "Status": "AUTHORISED",
        }
        if issue_date:
            # Keep invoice issue date aligned to the day the invoice is actually sent.
            invoice_payload["Date"] = issue_date
        payload = {"Invoices": [invoice_payload]}
        if self.dry_run:
            return {"dry_run": True, "payload": payload}
        url = f"{self.base_url}/Invoices"
        response = self._request("POST", url, json=payload)
        if not response.ok:
            raise RuntimeError(
                f"Xero invoice authorise failed: {response.status_code} {response.text}"
            )
        return response.json()

    def email_invoice(self, invoice_id: str) -> bool:
        """
        Ask Xero to email invoice using organisation defaults.
        Returns True when API accepts request.
        """
        if self.dry_run:
            return True
        url = f"{self.base_url}/Invoices/{invoice_id}/Email"
        response = self._request("POST", url, json={})
        return bool(response.ok)

    def record_invoice_payment(
        self,
        invoice_id: str,
        amount: float,
        *,
        account_code: str = "",
        when: str | None = None,
    ) -> Dict:
        account_code = account_code or self.payment_account_code
        if amount <= 0:
            return {"skipped": True, "reason": "No amount due"}
        if not account_code:
            raise RuntimeError(
                "Xero payment account is not configured for the active organisation. "
                "Set 'Payment bank account' in Xero Organisations settings."
            )
        account_ref: Dict[str, str]
        account_value = str(account_code or "").strip()
        if account_value.lower().startswith("id:"):
            account_ref = {"AccountID": account_value[3:].strip()}
        elif (
            len(account_value) == 36
            and account_value.count("-") == 4
        ):
            # Backward-compatible support if an AccountID was saved directly.
            account_ref = {"AccountID": account_value}
        else:
            account_ref = {"Code": account_value}
        payment_date = when or dt.date.today().isoformat()
        payload = {
            "Payments": [
                {
                    "Invoice": {"InvoiceID": invoice_id},
                    "Account": account_ref,
                    "Date": payment_date,
                    "Amount": float(amount),
                }
            ]
        }
        if self.dry_run:
            return {"dry_run": True, "payload": payload}
        url = f"{self.base_url}/Payments"
        response = self._request("POST", url, json=payload)
        if not response.ok:
            raise RuntimeError(
                f"Xero payment create failed: {response.status_code} {response.text}"
            )
        return response.json()

    def get_bank_transactions(
        self,
        *,
        start_date: dt.date | None,
        end_date: dt.date | None,
    ) -> Dict:
        """
        Fetch Xero bank transactions in a bounded date range.
        The Cashflows reconciliation layer filters these to `CFE SETT`.
        Returns an empty result dict if either date is None.
        """
        if start_date is None or end_date is None:
            return {}
        all_items: list[Dict] = []
        where = (
            f'Date>=DateTime({start_date.year},{start_date.month},{start_date.day})'
            f'&&Date<=DateTime({end_date.year},{end_date.month},{end_date.day})'
        )
        page = 1
        while True:
            url = f"{self.base_url}/BankTransactions"
            response = self._request(
                "GET",
                url,
                params={"where": where, "page": page},
            )
            if not response.ok:
                raise RuntimeError(
                    f"Xero bank transactions fetch failed: {response.status_code} {response.text}"
                )
            data = response.json() or {}
            items = data.get("BankTransactions") or []
            all_items.extend(items)
            if len(items) < 100:
                break
            page += 1
        return {"BankTransactions": all_items}

    def get_bank_accounts(self) -> list[Dict]:
        """List connected bank / card accounts (Account Type == BANK).

        Used to let the user say which card a batch of receipts belongs to.
        Returns a list of {"name", "id", "code"} dicts; empty on any error so
        the upload form can fall back to a free-text field.
        """
        url = f"{self.base_url}/Accounts"
        try:
            response = self._request("GET", url, params={"where": 'Type=="BANK"'})
            if not response.ok:
                return []
            data = response.json() or {}
        except Exception:
            return []
        out: list[Dict] = []
        for a in data.get("Accounts") or []:
            out.append({
                "name": str(a.get("Name") or "").strip() or "Bank account",
                "id": str(a.get("AccountID") or ""),
                "code": str(a.get("Code") or ""),
            })
        return out

    def get_open_invoices(self) -> Dict:
        """Fetch open receivable invoices that may be closed by a settlement."""
        all_items: list[Dict] = []
        where = 'Type=="ACCREC"&&Status=="AUTHORISED"&&AmountDue>0'
        page = 1
        while True:
            url = f"{self.base_url}/Invoices"
            response = self._request(
                "GET",
                url,
                params={"where": where, "page": page},
            )
            if not response.ok:
                raise RuntimeError(
                    f"Xero open invoices fetch failed: {response.status_code} {response.text}"
                )
            data = response.json() or {}
            items = data.get("Invoices") or []
            all_items.extend(items)
            if len(items) < 100:
                break
            page += 1
        return {"Invoices": all_items}

    def get_paid_invoices(self, start_date: dt.date, end_date: dt.date) -> Dict:
        """Fetch PAID receivable invoices within a date window.

        Card payments via Cashflows are marked PAID in Xero at the time of the
        transaction — they will not appear in get_open_invoices(). Fetching
        PAID invoices for the CSV date range is required to match them.
        """
        all_items: list[Dict] = []
        where = (
            'Type=="ACCREC"&&Status=="PAID"'
            f'&&Date>=DateTime({start_date.year},{start_date.month},{start_date.day})'
            f'&&Date<=DateTime({end_date.year},{end_date.month},{end_date.day})'
        )
        page = 1
        while True:
            url = f"{self.base_url}/Invoices"
            response = self._request(
                "GET",
                url,
                params={"where": where, "page": page},
            )
            if not response.ok:
                raise RuntimeError(
                    f"Xero paid invoices fetch failed: {response.status_code} {response.text}"
                )
            data = response.json() or {}
            items = data.get("Invoices") or []
            all_items.extend(items)
            if len(items) < 100:
                break
            page += 1
        return {"Invoices": all_items}

    def create_invoice_payload(self, payload: Dict) -> Dict:
        if self.dry_run:
            return {"dry_run": True, "payload": payload}
        url = f"{self.base_url}/Invoices"
        response = self._request("POST", url, json=payload)
        if not response.ok:
            raise RuntimeError(
                f"Xero invoice payload post failed: {response.status_code} {response.text}"
            )
        return response.json()

    def create_batch_payment_payload(self, payload: Dict) -> Dict:
        if self.dry_run:
            return {"dry_run": True, "payload": payload}
        url = f"{self.base_url}/BatchPayments"
        response = self._request("POST", url, json=payload)
        if not response.ok:
            raise RuntimeError(
                f"Xero batch payment post failed: {response.status_code} {response.text}"
            )
        return response.json()

    def create_bank_transaction_payload(self, payload: Dict) -> Dict:
        if self.dry_run:
            return {"dry_run": True, "payload": payload}
        url = f"{self.base_url}/BankTransactions"
        response = self._request("POST", url, json=payload)
        if not response.ok:
            raise RuntimeError(
                f"Xero bank transaction post failed: {response.status_code} {response.text}"
            )
        return response.json()

    def update_bank_transaction_payload(self, bank_transaction_id: str, payload: Dict) -> Dict:
        if self.dry_run:
            return {"dry_run": True, "payload": payload}
        url = f"{self.base_url}/BankTransactions/{bank_transaction_id}"
        response = self._request("POST", url, json=payload)
        if not response.ok:
            raise RuntimeError(
                f"Xero bank transaction update failed: {response.status_code} {response.text}"
            )
        return response.json()

    def update_invoice_payload(self, invoice_id: str, payload: Dict) -> Dict:
        if self.dry_run:
            return {"dry_run": True, "payload": payload}
        url = f"{self.base_url}/Invoices/{invoice_id}"
        response = self._request("POST", url, json=payload)
        if not response.ok:
            raise RuntimeError(
                f"Xero invoice update failed: {response.status_code} {response.text}"
            )
        return response.json()

    def create_credit_note_payload(self, payload: Dict) -> Dict:
        if self.dry_run:
            return {"dry_run": True, "payload": payload}
        url = f"{self.base_url}/CreditNotes"
        response = self._request("POST", url, json=payload)
        if not response.ok:
            raise RuntimeError(
                f"Xero credit note post failed: {response.status_code} {response.text}"
            )
        return response.json()

    def allocate_credit_note_payload(self, credit_note_id: str, payload: Dict) -> Dict:
        if self.dry_run:
            return {"dry_run": True, "credit_note_id": credit_note_id, "payload": payload}
        credit_note_id = str(credit_note_id or "").strip()
        if not credit_note_id:
            raise RuntimeError("Missing Xero CreditNoteID for allocation.")
        url = f"{self.base_url}/CreditNotes/{credit_note_id}/Allocations"
        response = self._request("PUT", url, json=payload)
        if not response.ok:
            raise RuntimeError(
                f"Xero credit note allocation failed: {response.status_code} {response.text}"
            )
        return response.json()

    def find_contacts_by_name(self, name: str) -> Dict:
        url = f"{self.base_url}/Contacts"
        params = {"where": f'Name=="{name}"'}
        response = self._request("GET", url, params=params)
        response.raise_for_status()
        return response.json()

    def find_contact_by_name(self, name: str) -> Dict | None:
        """
        Return a single contact for a business/profile name lookup.
        Prefers exact case-insensitive name match; falls back to first result.
        """
        if not (name or "").strip():
            return None
        contacts = (self.find_contacts_by_name(name.strip()).get("Contacts") or [])
        if not contacts:
            return None
        wanted = name.strip().lower()
        for contact in contacts:
            if str(contact.get("Name") or "").strip().lower() == wanted:
                return contact
        return contacts[0]

    def create_contact(self, name: str, email: str, phone: str, address: Dict | None) -> Dict:
        payload = {
            "Contacts": [
                {
                    "Name": name,
                    "EmailAddress": email or None,
                    "IsCustomer": True,
                    "Phones": [
                        {
                            "PhoneType": "MOBILE",
                            "PhoneNumber": phone or "",
                        }
                    ]
                    if phone
                    else [],
                    "Addresses": [address] if address else [],
                }
            ]
        }
        if self.dry_run:
            return {"dry_run": True, "payload": payload}
        url = f"{self.base_url}/Contacts"
        response = self._request("POST", url, json=payload)
        if not response.ok:
            raise RuntimeError(
                f"Xero contact create failed: {response.status_code} {response.text}"
            )
        return response.json()

    def update_contact(self, contact_id: str, email: str, phone: str, address: Dict | None) -> Dict:
        payload = {
            "Contacts": [
                {
                    "ContactID": contact_id,
                    "EmailAddress": email or None,
                    "Phones": [
                        {
                            "PhoneType": "MOBILE",
                            "PhoneNumber": phone or "",
                        }
                    ]
                    if phone
                    else [],
                    # Only send billing address (POBOX) to avoid duplicates.
                    # Also include a blank STREET to clear any prior delivery address.
                    "Addresses": (
                        [address, {"AddressType": "STREET", "AddressLine1": ""}]
                        if address
                        else [{"AddressType": "STREET", "AddressLine1": ""}]
                    ),
                    "IsCustomer": True,
                }
            ]
        }
        if self.dry_run:
            return {"dry_run": True, "payload": payload, "Contacts": []}
        url = f"{self.base_url}/Contacts"
        response = self._request("POST", url, json=payload)
        if not response.ok:
            raise RuntimeError(
                f"Xero contact update failed: {response.status_code} {response.text}"
            )
        return response.json()

    def rename_contact(self, contact_id: str, new_name: str) -> Dict:
        """Rename an existing Xero contact."""
        payload = {"Contacts": [{"ContactID": contact_id, "Name": new_name}]}
        if self.dry_run:
            return {"dry_run": True, "payload": payload}
        url = f"{self.base_url}/Contacts"
        response = self._request("POST", url, json=payload)
        if not response.ok:
            raise RuntimeError(
                f"Xero contact rename failed: {response.status_code} {response.text}"
            )
        return response.json()

    @staticmethod
    def _contact_addr_line1(contact: Dict) -> str:
        """Return AddressLine1 from a contact's POBOX address, lower-stripped."""
        for addr in contact.get("Addresses", []):
            if addr.get("AddressType") == "POBOX" and addr.get("AddressLine1", "").strip():
                return addr["AddressLine1"].strip()
        return ""

    def ensure_contact(self, name: str, email: str, phone: str, address: Dict | None) -> Dict:
        """
        Find contact by name.
        - Same name + same email + SAME address → reuse existing.
        - Same name + same email + DIFFERENT address → rename original to
          'Name (original address line 1)', create new 'Name (new address line 1)'.
        - Same name + different email → create new as 'Name (email)'.
        - No match → create new with given name.
        """
        contacts_resp = self.find_contacts_by_name(name)
        contacts = contacts_resp.get("Contacts", [])

        if contacts and email:
            for contact in contacts:
                if contact.get("EmailAddress", "").lower() == email.lower():
                    new_line1 = (address or {}).get("AddressLine1", "").strip()
                    orig_line1 = self._contact_addr_line1(contact)
                    if new_line1 and orig_line1 and new_line1.lower() != orig_line1.lower():
                        # Different address — archive the original, create a fresh contact
                        renamed_name = f"{name} ({orig_line1})"
                        new_contact_name = f"{name} ({new_line1})"
                        try:
                            self.rename_contact(contact["ContactID"], renamed_name)
                        except Exception as exc:
                            print(f"[xero] rename_contact failed: {exc}", flush=True)
                        created = self.create_contact(new_contact_name, email, phone, address)
                        return {
                            "contact": _extract_first_contact(created),
                            "created": True,
                            "address_split": True,
                            "orig_name": renamed_name,
                            "new_name": new_contact_name,
                        }
                    return {"contact": contact, "created": False}

        if contacts and email:
            alt_name = f"{name} ({email})"
            created = self.create_contact(alt_name, email, phone, address)
            return {"contact": _extract_first_contact(created), "created": True}

        if not contacts:
            created = self.create_contact(name, email, phone, address)
            return {"contact": _extract_first_contact(created), "created": True}

        return {"contact": contacts[0], "created": False}


def _extract_first_contact(response: Dict) -> Dict:
    if response.get("Contacts"):
        return response["Contacts"][0]
    return response


def build_xero_client(config: AppConfig) -> XeroClient | None:
    """
    Build a Xero client using the first enabled tenant from per-tenant config,
    falling back to the token's stored tenant_id if no per-tenant config exists.
    """
    # This function is used by the poller, Xero webhooks, dashboard health
    # checks, and receipt/cashflows tools. It must not refresh tokens or touch
    # Xero while the global kill-switch or persisted 429 lockout is active.
    if xero_is_disabled() or xero_lockout_is_active(config):
        return None

    token = load_xero_token(config.xero_token_file)

    client_id = config.xero_client_id or str(
        get_json_setting(config.admin_db_file, "xero_client_id", "")
    ).strip()
    client_secret = config.xero_client_secret or str(
        get_json_setting(config.admin_db_file, "xero_client_secret", "")
    ).strip()

    access_token = config.xero_access_token or token.get("access_token", "")

    # Determine tenant: prefer first enabled tenant from per-tenant config.
    # If tenants are configured but all disabled, still use the first configured
    # tenant rather than falling back to the token's stored tenant_id (which may
    # point to a demo/wrong org from the initial OAuth flow).
    tenants = get_xero_tenants(config.admin_db_file)
    enabled_tenants = [t for t in tenants if t.get("enabled", True)]
    chosen = enabled_tenants[0] if enabled_tenants else (tenants[0] if tenants else None)
    if chosen:
        tenant_id = chosen["tenantId"]
        sales_account_code = chosen.get("invoiceAccount", "") or DEFAULT_SALES_ACCOUNT_CODE
        # When a tenant is explicitly selected, require an explicit payment account
        # so card payments cannot silently post to an unintended default account.
        payment_account_code = chosen.get("paymentAccount", "")
        branding_theme_id = chosen.get("brandingThemeId", "") or ""
        premium_theme_id = chosen.get("premiumThemeId", "") or ""
        premium_threshold = chosen.get("premiumThreshold")
    else:
        tenant_id = config.xero_tenant_id or token.get("tenant_id", "")
        sales_account_code = DEFAULT_SALES_ACCOUNT_CODE
        payment_account_code = DEFAULT_PAYMENT_ACCOUNT_CODE
        branding_theme_id = ""
        premium_theme_id = ""
        premium_threshold = None

    refresh_token = token.get("refresh_token")
    if refresh_token and (not access_token or token_is_expired(token)):
        refreshed = refresh_xero_token(
            client_id=client_id,
            client_secret=client_secret,
            refresh_token=refresh_token,
        )
        token = {**token, **refreshed}
        token["tenant_id"] = tenant_id
        save_xero_token(config.xero_token_file, token)
        access_token = token.get("access_token", "")
        print("Refreshed Xero token")

    if not access_token or not tenant_id:
        return None

    return XeroClient(
        access_token=access_token,
        tenant_id=tenant_id,
        dry_run=config.dry_run,
        client_id=client_id,
        client_secret=client_secret,
        refresh_token=refresh_token or "",
        token_file=config.xero_token_file,
        sales_account_code=sales_account_code,
        payment_account_code=payment_account_code,
        branding_theme_id=branding_theme_id,
        premium_theme_id=premium_theme_id,
        premium_threshold=premium_threshold,
    )


def load_xero_token(path: str) -> Dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_xero_token(path: str, token: Dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(token, f, indent=2)


def token_is_expired(token: Dict) -> bool:
    # If no expires_in tracked, assume still valid.
    issued_at = token.get("issued_at")
    expires_in = token.get("expires_in")
    if not issued_at or not expires_in:
        return False
    now = int(dt.datetime.now(dt.timezone.utc).timestamp())
    return now >= (int(issued_at) + int(expires_in) - 60)


def refresh_xero_token(client_id: str, client_secret: str, refresh_token: str) -> Dict:
    if not client_id or not client_secret:
        raise RuntimeError("Missing XERO_CLIENT_ID or XERO_CLIENT_SECRET for refresh.")

    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    headers = {"Authorization": f"Basic {basic}"}
    data = {"grant_type": "refresh_token", "refresh_token": refresh_token}
    response = requests.post(TOKEN_URL, headers=headers, data=data, timeout=30)
    if response.status_code >= 400:
        detail = ""
        try:
            payload = response.json()
            detail = payload.get("error_description") or payload.get("error") or str(payload)
        except Exception:
            detail = response.text[:400]
        raise RuntimeError(
            f"Xero token refresh failed ({response.status_code}): {detail}"
        )
    payload = response.json()
    payload["issued_at"] = int(dt.datetime.now(dt.timezone.utc).timestamp())
    return payload
