from __future__ import annotations

import datetime as dt
import base64
import json
from typing import Dict

import requests

from .config import AppConfig
from .admin_store import get_json_setting

TOKEN_URL = "https://identity.xero.com/connect/token"
DEFAULT_SALES_ACCOUNT_CODE = "200"
DEFAULT_PAYMENT_ACCOUNT_CODE = "090"


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
    ):
        self.access_token = access_token
        self.tenant_id = tenant_id
        self.dry_run = dry_run
        self.base_url = "https://api.xero.com/api.xro/2.0"
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self.token_file = token_file

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Xero-tenant-id": self.tenant_id,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _refresh_access_token(self) -> bool:
        if not self.refresh_token:
            return False
        refreshed = refresh_xero_token(
            client_id=self.client_id,
            client_secret=self.client_secret,
            refresh_token=self.refresh_token,
        )
        refreshed["tenant_id"] = self.tenant_id
        save_xero_token(self.token_file, refreshed)
        self.access_token = refreshed.get("access_token", self.access_token)
        self.refresh_token = refreshed.get("refresh_token", self.refresh_token)
        return True

    def _request(self, method: str, url: str, **kwargs):
        response = requests.request(method, url, headers=self._headers(), timeout=30, **kwargs)
        if response.status_code == 401 and self._refresh_access_token():
            response = requests.request(
                method, url, headers=self._headers(), timeout=30, **kwargs
            )
        return response

    def get_organisation(self) -> Dict:
        url = f"{self.base_url}/Organisation"
        response = self._request("GET", url)
        response.raise_for_status()
        return response.json()

    def create_invoice_from_event(
        self, event: Dict, contact: Dict | None = None, line_items: list | None = None
    ) -> Dict:
        """
        Minimal example: create a draft ACCREC invoice referencing the event.
        Customize this to match your accounting workflow.
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
                    "AccountCode": DEFAULT_SALES_ACCOUNT_CODE,
                }
            ],
            "Reference": _short_reference(event),
            "Status": "DRAFT",
        }

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
        payload = {
            "Invoices": [
                {
                    "InvoiceID": invoice_id,
                    "Contact": contact if contact else None,
                    "LineItems": prepared_line_items if prepared_line_items else [],
                }
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

    def _prepare_line_items(self, line_items: list | None) -> list:
        if not line_items:
            return []
        prepared = []
        for item in line_items:
            line = dict(item)
            if not line.get("AccountCode") and not line.get("AccountID"):
                line["AccountCode"] = DEFAULT_SALES_ACCOUNT_CODE
            prepared.append(line)
        return prepared

    def get_online_invoice_url(self, invoice_id: str) -> str | None:
        """
        Return the public Online Invoice URL if available.
        """
        url = f"{self.base_url}/Invoices/{invoice_id}/OnlineInvoice"
        response = self._request("GET", url)
        if not response.ok:
            return None
        data = response.json()
        online = data.get("OnlineInvoices", [{}])[0]
        return online.get("OnlineInvoiceUrl") or online.get("Url")

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

    def authorize_invoice(self, invoice_id: str) -> Dict:
        payload = {"Invoices": [{"InvoiceID": invoice_id, "Status": "AUTHORISED"}]}
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
        account_code: str = DEFAULT_PAYMENT_ACCOUNT_CODE,
        when: str | None = None,
    ) -> Dict:
        if amount <= 0:
            return {"skipped": True, "reason": "No amount due"}
        payment_date = when or dt.date.today().isoformat()
        payload = {
            "Payments": [
                {
                    "Invoice": {"InvoiceID": invoice_id},
                    "Account": {"Code": account_code},
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

    def find_contacts_by_name(self, name: str) -> Dict:
        url = f"{self.base_url}/Contacts"
        params = {"where": f'Name=="{name}"'}
        response = self._request("GET", url, params=params)
        response.raise_for_status()
        return response.json()

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

    def ensure_contact(self, name: str, email: str, phone: str, address: Dict | None) -> Dict:
        """
        Find contact by name. If a matching email exists, reuse it.
        If name matches but email differs, create a new contact with
        'Name (email)' format.
        """
        contacts_resp = self.find_contacts_by_name(name)
        contacts = contacts_resp.get("Contacts", [])

        if contacts and email:
            for contact in contacts:
                if contact.get("EmailAddress", "").lower() == email.lower():
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
    Build a Xero client from either env vars or xero_token.json (preferred).
    If a refresh token exists, automatically refresh access token and persist.
    Credentials are read from env vars first, then from the admin JSON store.
    """
    token = load_xero_token(config.xero_token_file)

    # Read credentials from env vars first, fall back to admin JSON store
    client_id = config.xero_client_id or str(
        get_json_setting(config.admin_db_file, "xero_client_id", "")
    ).strip()
    client_secret = config.xero_client_secret or str(
        get_json_setting(config.admin_db_file, "xero_client_secret", "")
    ).strip()

    access_token = config.xero_access_token or token.get("access_token", "")
    tenant_id = config.xero_tenant_id or token.get("tenant_id", "")

    refresh_token = token.get("refresh_token")
    if refresh_token and (not access_token or token_is_expired(token)):
        refreshed = refresh_xero_token(
            client_id=client_id,
            client_secret=client_secret,
            refresh_token=refresh_token,
        )
        token = {**token, **refreshed}
        # Refresh response doesn't include tenant_id, preserve it.
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
    response.raise_for_status()
    payload = response.json()
    payload["issued_at"] = int(dt.datetime.now(dt.timezone.utc).timestamp())
    return payload
