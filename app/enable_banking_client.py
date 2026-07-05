"""Enable Banking open-banking client for pulling the company card's real transactions.

Why this exists
---------------
Xero never exposes *unreconciled* bank-feed statement lines over its public API
(a contractual/regulatory block, not a scope gap).  To get the company card's
payments automatically we connect to the card's bank directly via open banking
and read its transactions.

This module replaces the previous Plaid integration.  Enable Banking's
"Restricted Production" tier is free for connecting your OWN whitelisted accounts
(a business linking its own 1-2 company cards), which is exactly this use case.

The public surface mirrors the old ``plaid_client`` module so the rest of the
app (matcher, dump reconciliation, engineer feed, subcontractor settlement) is
unchanged:

    is_connected(db)            -> bool
    connection_status(db)       -> safe dict for display
    get_cached_transactions(db) -> [{transaction_id, account_id, date, amount, name, pending}]
    sync_transactions(db, c)    -> {added, modified, removed, total}
    disconnect(db, c)           -> None

The normalised account id we store in ``expense_engineers.plaid_account_id`` is
now the Enable Banking *account uid* (the column name is kept to avoid a schema
migration; it is just an opaque "linked card account id").

Security
--------
- The application id (``ENABLE_BANKING_APP_ID``) and RSA private key
  (``ENABLE_BANKING_PRIVATE_KEY``) come from the environment ONLY.  The private
  key is never written to the database or to code and is never logged.
- API auth uses short-lived (1 hour) RS256 JSON Web Tokens signed with that
  private key, per Enable Banking's spec.
- The bank ``session_id`` is encrypted at rest with Fernet before storage, using
  a key derived from WEB_SECRET_KEY (or ENABLE_BANKING_ENC_KEY).
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import os
from typing import Any

import jwt as pyjwt
import requests
from cryptography.fernet import Fernet, InvalidToken

from .admin_store import get_json_setting, set_json_setting

# admin-DB setting keys
_CONN_KEY = "enable_banking_connection"
_TX_KEY = "enable_banking_transactions"

_API_BASE = "https://api.enablebanking.com"


# ── private-key / token encryption ──────────────────────────────────────────
def _private_key() -> str:
    """Return the application's RSA private key (PEM), env-only.

    Accepts a real multi-line PEM or one with escaped ``\\n`` sequences (as can
    happen when pasting into a single-line secret field).
    """
    raw = os.getenv("ENABLE_BANKING_PRIVATE_KEY") or ""
    raw = raw.strip().strip('"').strip("'")
    if "\\n" in raw and "\n" not in raw:
        raw = raw.replace("\\n", "\n")
    return raw


def _enc_key() -> bytes:
    """Derive a stable Fernet key from the app secret."""
    secret = (
        os.getenv("ENABLE_BANKING_ENC_KEY")
        or os.getenv("WEB_SECRET_KEY")
        or ""
    ).strip()
    if not secret:
        raise RuntimeError(
            "Cannot secure the bank session: set WEB_SECRET_KEY (or "
            "ENABLE_BANKING_ENC_KEY) so the session id can be encrypted at rest."
        )
    return base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest())


def _encrypt(plaintext: str) -> str:
    return Fernet(_enc_key()).encrypt(plaintext.encode("utf-8")).decode("ascii")


def _decrypt(token_enc: str) -> str:
    try:
        return Fernet(_enc_key()).decrypt(token_enc.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError) as exc:
        raise RuntimeError(
            "Stored bank session could not be decrypted (the app secret may have "
            "changed). Reconnect the bank to fix this."
        ) from exc


# ── connection persistence ──────────────────────────────────────────────────
def get_connection(db_path: str) -> dict[str, Any]:
    conn = get_json_setting(db_path, _CONN_KEY, {})
    return conn if isinstance(conn, dict) else {}


def save_connection(db_path: str, conn: dict[str, Any]) -> None:
    set_json_setting(db_path, _CONN_KEY, conn)


def clear_connection(db_path: str) -> None:
    set_json_setting(db_path, _CONN_KEY, {})
    set_json_setting(db_path, _TX_KEY, [])


def is_connected(db_path: str) -> bool:
    conn = get_connection(db_path)
    return bool(conn.get("session_id_enc") and conn.get("accounts"))


def connection_status(db_path: str) -> dict[str, Any]:
    """A safe (secret-free) view of the connection for display."""
    conn = get_connection(db_path)
    if not (conn.get("session_id_enc") and conn.get("accounts")):
        return {"connected": False}
    return {
        "connected": True,
        "institution_name": conn.get("institution_name") or "Connected bank",
        "accounts": conn.get("accounts") or [],
        "connected_at": conn.get("connected_at") or "",
        "last_sync_at": conn.get("last_sync_at") or "",
        "valid_until": conn.get("valid_until") or "",
        "transaction_count": len(get_json_setting(db_path, _TX_KEY, []) or []),
    }


# ── client ──────────────────────────────────────────────────────────────────
class EnableBankingClient:
    def __init__(self) -> None:
        self.app_id = (os.getenv("ENABLE_BANKING_APP_ID") or "").strip()
        self.private_key = _private_key()
        self.country = (os.getenv("ENABLE_BANKING_COUNTRY") or "GB").strip().upper()
        self.psu_type = (os.getenv("ENABLE_BANKING_PSU_TYPE") or "business").strip().lower()
        self.timeout_seconds = int(os.getenv("ENABLE_BANKING_TIMEOUT", "30") or 30)
        # How long the access consent should last, and how far back to fetch.
        self.valid_days = int(os.getenv("ENABLE_BANKING_VALID_DAYS", "90") or 90)
        self.fetch_days = int(os.getenv("ENABLE_BANKING_FETCH_DAYS", "90") or 90)

    @property
    def configured(self) -> bool:
        return bool(self.app_id and self.private_key)

    # auth -------------------------------------------------------------------
    def _jwt(self) -> str:
        iat = int(dt.datetime.now(dt.timezone.utc).timestamp())
        body = {
            "iss": "enablebanking.com",
            "aud": "api.enablebanking.com",
            "iat": iat,
            "exp": iat + 3600,
        }
        token = pyjwt.encode(
            body, self.private_key, algorithm="RS256",
            headers={"kid": self.app_id},
        )
        # PyJWT >=2 returns str already; be defensive for older builds.
        return token.decode("ascii") if isinstance(token, bytes) else token

    def _headers(self) -> dict[str, str]:
        if not self.configured:
            raise RuntimeError(
                "Enable Banking is not configured (set ENABLE_BANKING_APP_ID / "
                "ENABLE_BANKING_PRIVATE_KEY)."
            )
        return {"Authorization": f"Bearer {self._jwt()}",
                "Content-Type": "application/json"}

    def _request(self, method: str, path: str, *, params: dict | None = None,
                 json: dict | None = None) -> dict[str, Any]:
        resp = requests.request(
            method, f"{_API_BASE}{path}",
            headers=self._headers(), params=params, json=json,
            timeout=self.timeout_seconds,
        )
        try:
            data = resp.json() or {}
        except ValueError:
            data = {}
        if resp.status_code >= 300:
            msg = (
                data.get("message")
                or data.get("error")
                or data.get("detail")
                or resp.text
                or ""
            )
            raise RuntimeError(
                f"Enable Banking HTTP {resp.status_code}: {str(msg)[:300]}"
            )
        return data

    # endpoints --------------------------------------------------------------
    def list_aspsps(self, country: str = "") -> list[dict[str, Any]]:
        data = self._request(
            "GET", "/aspsps", params={"country": (country or self.country)}
        )
        out = []
        for a in data.get("aspsps") or []:
            out.append({
                "name": a.get("name") or "",
                "country": a.get("country") or (country or self.country),
                "logo": a.get("logo") or "",
            })
        return out

    def start_auth(self, aspsp_name: str, redirect_url: str, state: str,
                   country: str = "") -> dict[str, Any]:
        valid_until = (
            dt.datetime.now(dt.timezone.utc)
            + dt.timedelta(days=self.valid_days)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        body = {
            "access": {"valid_until": valid_until},
            "aspsp": {"name": aspsp_name, "country": (country or self.country)},
            "state": state,
            "redirect_url": redirect_url,
            "psu_type": self.psu_type,
        }
        data = self._request("POST", "/auth", json=body)
        return {
            "url": data.get("url") or "",
            "authorization_id": data.get("authorization_id") or "",
            "valid_until": valid_until,
        }

    def create_session(self, code: str) -> dict[str, Any]:
        return self._request("POST", "/sessions", json={"code": code})

    def get_account_transactions(self, account_uid: str, date_from: str = "",
                                 continuation_key: str = "") -> dict[str, Any]:
        params: dict[str, Any] = {}
        if date_from:
            params["date_from"] = date_from
        if continuation_key:
            params["continuation_key"] = continuation_key
        return self._request(
            "GET", f"/accounts/{account_uid}/transactions", params=params
        )

    def delete_session(self, session_id: str) -> None:
        try:
            self._request("DELETE", f"/sessions/{session_id}")
        except RuntimeError:
            # Best-effort: even if the API rejects it, we still drop our copy.
            pass


# ── helpers ─────────────────────────────────────────────────────────────────
def _account_mask(acct: dict[str, Any]) -> str:
    aid = acct.get("account_id") or {}
    iban = str(aid.get("iban") or "")
    if iban:
        return iban[-4:]
    other = str(aid.get("other", {}).get("identification") or "") if isinstance(
        aid.get("other"), dict) else ""
    return other[-4:] if other else ""


def _safe_accounts(raw_accounts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for a in raw_accounts or []:
        uid = a.get("uid") or ""
        if not uid:
            continue
        out.append({
            # account_id is the value stored in expense_engineers.plaid_account_id
            "account_id": uid,
            "name": a.get("name") or a.get("product") or "Account",
            "mask": _account_mask(a),
            "type": a.get("cash_account_type") or "",
            "subtype": a.get("usage") or a.get("cash_account_type") or "",
        })
    return out


def store_session(db_path: str, client: EnableBankingClient,
                  session: dict[str, Any], aspsp_name: str,
                  valid_until: str = "") -> dict[str, Any]:
    """Encrypt + persist a freshly authorised bank session, returning safe status."""
    accounts = _safe_accounts(session.get("accounts") or [])
    if not accounts:
        raise RuntimeError(
            "The bank returned no accessible accounts for this connection."
        )
    conn = {
        "session_id_enc": _encrypt(str(session.get("session_id") or "")),
        "institution_name": aspsp_name or "Connected bank",
        "accounts": accounts,
        "connected_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "last_sync_at": "",
        "valid_until": valid_until
        or (session.get("access") or {}).get("valid_until") or "",
    }
    save_connection(db_path, conn)
    set_json_setting(db_path, _TX_KEY, [])
    return connection_status(db_path)


def _join_name(raw: dict[str, Any]) -> str:
    """Build a searchable name: counterparty + remittance information.

    The subcontractor settlement scans this for the app's payment reference
    (PWSUB<id>), while dump matching uses the merchant (counterparty) name; both
    are included.
    """
    indicator = (raw.get("credit_debit_indicator") or "").upper()
    # For money OUT (DBIT) the counterparty is the creditor (the merchant we
    # paid); for money IN (CRDT) it is the debtor.
    party = raw.get("creditor") if indicator == "DBIT" else raw.get("debtor")
    name = ""
    if isinstance(party, dict):
        name = str(party.get("name") or "")
    rem = raw.get("remittance_information") or []
    if isinstance(rem, str):
        rem = [rem]
    parts = [p for p in ([name] + [str(x) for x in rem]) if p]
    return " ".join(parts).strip()


def _normalise_tx(raw: dict[str, Any], account_uid: str) -> dict[str, Any]:
    """Reduce an Enable Banking transaction to the fields the matcher needs.

    Amount is signed so that money OUT (a card spend / DBIT) is positive and
    money IN (CRDT) is negative — receipts are positive, so credits naturally
    fail the matcher and are skipped by the engineer feed.
    """
    amt_obj = raw.get("transaction_amount") or {}
    try:
        magnitude = abs(float(amt_obj.get("amount") or 0.0))
    except (TypeError, ValueError):
        magnitude = 0.0
    indicator = (raw.get("credit_debit_indicator") or "").upper()
    amount = magnitude if indicator != "CRDT" else -magnitude

    date = (
        raw.get("booking_date")
        or raw.get("value_date")
        or raw.get("transaction_date")
        or ""
    )
    name = _join_name(raw)

    tx_id = str(raw.get("transaction_id") or raw.get("entry_reference") or "").strip()
    if not tx_id:
        seed = f"{account_uid}|{date}|{magnitude}|{name}"
        tx_id = "eb_" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]

    status = (raw.get("status") or "").upper()
    return {
        "transaction_id": tx_id,
        "account_id": account_uid,
        "date": str(date)[:10],
        "amount": round(amount, 2),
        "name": name,
        "pending": status not in ("BOOK", "BOOKED", ""),
    }


def sync_transactions(db_path: str, client: EnableBankingClient) -> dict[str, Any]:
    """Re-fetch each linked account's recent transactions into the local cache."""
    conn = get_connection(db_path)
    if not (conn.get("session_id_enc") and conn.get("accounts")):
        raise RuntimeError("No bank is connected.")

    old_total = len(get_json_setting(db_path, _TX_KEY, []) or [])
    date_from = (
        dt.datetime.now(dt.timezone.utc).date()
        - dt.timedelta(days=client.fetch_days)
    ).isoformat()

    cache: dict[str, dict[str, Any]] = {}
    for acct in conn.get("accounts") or []:
        uid = acct.get("account_id") or ""
        if not uid:
            continue
        continuation = ""
        for _ in range(50):  # guard against a pagination loop
            page = client.get_account_transactions(uid, date_from, continuation)
            for raw in page.get("transactions") or []:
                tx = _normalise_tx(raw, uid)
                if tx["transaction_id"]:
                    cache[tx["transaction_id"]] = tx
            continuation = page.get("continuation_key") or ""
            if not continuation:
                break

    conn["last_sync_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    save_connection(db_path, conn)
    txs = list(cache.values())
    set_json_setting(db_path, _TX_KEY, txs)
    total = len(txs)
    return {
        "added": max(0, total - old_total),
        "modified": 0,
        "removed": max(0, old_total - total),
        "total": total,
    }


def disconnect(db_path: str, client: EnableBankingClient) -> None:
    conn = get_connection(db_path)
    enc = conn.get("session_id_enc")
    if enc and client.configured:
        try:
            client.delete_session(_decrypt(enc))
        except RuntimeError:
            pass
    clear_connection(db_path)


def get_cached_transactions(db_path: str) -> list[dict[str, Any]]:
    return list(get_json_setting(db_path, _TX_KEY, []) or [])
