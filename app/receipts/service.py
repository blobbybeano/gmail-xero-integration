from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
import re
import time
import uuid
from pathlib import Path
from typing import Any

import requests
from google.auth.transport.requests import Request
from google.oauth2 import service_account

from ..admin_store import get_receipts_settings
from ..config import AppConfig
from .models import ReceiptRecord
from .store import append_receipt, list_receipts, update_receipt


def _parse_uk_date(text: str) -> str:
    """Find a numeric date in OCR text, reading ambiguous DD/MM as UK day-first.

    Returns an ISO ``YYYY-MM-DD`` string, or "" if nothing parseable is found.
    UK receipts write the day first (11/05/26 = 11 May 2026); US-trained parsers
    routinely flip this to month-first, so we deliberately assume day-first and
    only swap to month-first when the second number can't be a month (> 12).
    """
    if not text:
        return ""
    today = dt.date.today()
    for m in re.finditer(r"\b(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{2,4})\b", text):
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if y < 100:
            y += 2000
        # Day-first by default (UK).  Only swap when the second number can't be
        # a month (> 12) but the first one can — that's an unmistakable US
        # month-first date like 05/13 = 13 May.
        if mo > 12 and d <= 12:
            d, mo = mo, d
        if not (1 <= mo <= 12 and 1 <= d <= 31):
            continue
        try:
            cand = dt.date(y, mo, d)
        except ValueError:
            continue
        # Receipts are never dated in the future; reject parses that land ahead
        # of today (usually a mis-swapped day/month).
        if cand > today:
            continue
        return cand.isoformat()
    return ""


class ReceiptService:
    """
    Receipt processing scaffold service.
    Feature-flagged and isolated from live invoice/calendar processing.
    """

    def __init__(self, config: AppConfig):
        self._config = config
        self._store_file = config.receipts_store_file
        self._upload_dir = Path(config.receipts_upload_dir)

    @property
    def enabled(self) -> bool:
        settings = get_receipts_settings(self._config.admin_db_file)
        return bool(self._config.receipts_enabled and settings.get("enabled"))

    @property
    def is_enabled(self) -> bool:
        return self.enabled

    @property
    def write_confirmation_required(self) -> bool:
        return bool(self._config.receipts_require_write_confirmation)

    def settings(self) -> dict[str, Any]:
        return get_receipts_settings(self._config.admin_db_file)

    def sign_upload_token(self, event_key: str, *, ttl_seconds: int | None = None) -> str:
        ttl = int(ttl_seconds or self._config.receipts_link_ttl_seconds)
        payload = {
            "event_key": (event_key or "").strip(),
            "exp": int(time.time()) + max(ttl, 300),
            "nonce": uuid.uuid4().hex[:12],
        }
        raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        body = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
        sig = hmac.new(
            self._config.web_secret_key.encode("utf-8"),
            body.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        sig_txt = base64.urlsafe_b64encode(sig).decode("ascii").rstrip("=")
        return f"{body}.{sig_txt}"

    def verify_upload_token(self, token: str) -> str:
        token = (token or "").strip()
        if "." not in token:
            raise ValueError("invalid token")
        body, sig_txt = token.split(".", 1)
        expected = hmac.new(
            self._config.web_secret_key.encode("utf-8"),
            body.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        got = base64.urlsafe_b64decode(sig_txt + "=" * (-len(sig_txt) % 4))
        if not hmac.compare_digest(expected, got):
            raise ValueError("invalid signature")
        payload_raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
        payload = json.loads(payload_raw.decode("utf-8"))
        exp = int(payload.get("exp") or 0)
        if exp < int(time.time()):
            raise ValueError("token expired")
        event_key = str(payload.get("event_key") or "").strip()
        if not event_key:
            raise ValueError("missing event key")
        return event_key

    def create_upload_url(self, event_key: str, *, base_url: str) -> str:
        """Generate a short upload URL for the calendar description.

        Registers a short 8-char code in the admin DB so the URL placed in
        the Google Calendar event reads as e.g. https://…/r/Ab3xYz9Q
        instead of a long token string.  Falls back to the full token URL if
        the DB write fails for any reason.
        """
        import secrets as _secrets
        token = self.sign_upload_token(event_key)
        base = base_url.rstrip("/")
        try:
            from ..admin_store import get_json_setting, set_json_setting
            code = _secrets.token_urlsafe(6)          # 8 URL-safe chars
            db = self._config.admin_db_file
            links = get_json_setting(db, "receipt_short_links", {})
            now = int(time.time())
            # Prune already-expired entries to keep the store tidy
            links = {k: v for k, v in links.items()
                     if isinstance(v, dict) and v.get("exp", 0) > now}
            exp = now + max(int(self._config.receipts_link_ttl_seconds), 300)
            links[code] = {"token": token, "exp": exp}
            set_json_setting(db, "receipt_short_links", links)
            return f"{base}/r/{code}"
        except Exception:
            # Fallback: return the long URL so the feature still works
            return f"{base}/receipts/submit?token={token}"

    def process_uploaded_receipt(
        self,
        *,
        file_bytes: bytes,
        filename: str,
        mime_type: str,
        event_key: str,
        source: str = "field_upload",
    ) -> ReceiptRecord:
        if not file_bytes:
            raise ValueError("empty file")
        if len(file_bytes) > 12 * 1024 * 1024:
            raise ValueError("file too large")

        saved = self._save_file(file_bytes=file_bytes, filename=filename)
        # OCR is a bonus, not a requirement.  If Document AI isn't configured
        # (or errors out) we still keep the photo so it can be attached to Xero.
        ocr_error = ""
        try:
            ocr = self._run_document_ai(file_bytes=file_bytes, mime_type=mime_type)
        except Exception as exc:
            ocr = {"text": "", "entities": []}
            ocr_error = str(exc).splitlines()[0][:200]
        raw_text = ocr.get("text") or ""
        parsed = self._parse_best_effort(raw_text)
        record = ReceiptRecord(
            source=source,
            raw_text=raw_text,
            merchant=parsed.get("merchant", ""),
            amount=parsed.get("amount"),
            currency=parsed.get("currency", "GBP"),
            transaction_ref=parsed.get("transaction_ref", ""),
            event_key=event_key,
            status="captured",
            metadata={
                "parser": "document-ai-v1" if not ocr_error else "none",
                "filename": filename,
                "mime_type": mime_type,
                "stored_file": str(saved),
                "ocr_error": ocr_error,
                "document_ai": {
                    "entities": ocr.get("entities", []),
                },
            },
        )
        return append_receipt(self._store_file, record)

    def store_file(self, *, file_bytes: bytes, filename: str) -> str:
        """Persist a file (e.g. a Word-doc invoice we OCR separately) and return
        its saved path. Used when the caller extracts text itself rather than
        going through Document AI."""
        if not file_bytes:
            raise ValueError("empty file")
        if len(file_bytes) > 12 * 1024 * 1024:
            raise ValueError("file too large")
        return str(self._save_file(file_bytes=file_bytes, filename=filename))

    def analyze_upload(
        self, *, file_bytes: bytes, filename: str, mime_type: str
    ) -> dict[str, Any]:
        """Save a receipt photo and read it with Document AI.

        Used by the Field Expenses flow.  Unlike ``process_uploaded_receipt``
        this does NOT persist to the JSON receipt store — the caller owns
        persistence (SQLite).  Returns the saved file path plus best-effort
        extracted fields (merchant / total / net / tax / date / currency).
        OCR failure is non-fatal: the photo is still saved.
        """
        if not file_bytes:
            raise ValueError("empty file")
        if len(file_bytes) > 12 * 1024 * 1024:
            raise ValueError("file too large")
        saved = self._save_file(file_bytes=file_bytes, filename=filename)
        ocr_error = ""
        try:
            ocr = self._run_document_ai(file_bytes=file_bytes, mime_type=mime_type)
        except Exception as exc:
            ocr = {"text": "", "entities": []}
            ocr_error = str(exc).splitlines()[0][:200]
        fields = self.extract_receipt_fields(
            ocr.get("entities", []), ocr.get("text", "")
        )
        return {
            "stored_file": str(saved),
            "raw_text": ocr.get("text", ""),
            "ocr_error": ocr_error,
            **fields,
        }

    def extract_receipt_fields(
        self, entities: list[dict[str, Any]], raw_text: str
    ) -> dict[str, Any]:
        """Pull merchant / amounts / date / currency out of Document AI entities.

        Falls back to the conservative regex parser when the receipt processor
        is not configured or returns nothing useful.
        """

        def _money(ent: dict[str, Any]) -> float | None:
            nv = ent.get("normalizedValue") or {}
            mv = nv.get("moneyValue")
            if isinstance(mv, dict):
                try:
                    units = float(mv.get("units") or 0)
                    nanos = float(mv.get("nanos") or 0) / 1e9
                    val = round(units + nanos, 2)
                    if val:
                        return val
                except Exception:
                    pass
            txt = (ent.get("mentionText") or "").replace(",", "")
            m = re.search(r"(\d+(?:\.\d{1,2})?)", txt)
            if m:
                try:
                    return float(m.group(1))
                except Exception:
                    return None
            return None

        merchant = ""
        total = net = tax = None
        date_iso = ""
        date_norm = ""   # Document AI's own normalised date (may be US-flipped)
        date_text = ""   # the raw date string as printed on the receipt
        currency = ""
        for ent in entities or []:
            etype = (ent.get("type") or "").lower()
            if etype in ("supplier_name", "merchant_name") and not merchant:
                merchant = (ent.get("mentionText") or "").strip()[:120]
            elif etype in ("total_amount", "grand_total", "amount_total"):
                total = _money(ent) or total
            elif etype in ("net_amount", "subtotal_amount", "subtotal"):
                net = _money(ent) or net
            elif etype in ("total_tax_amount", "vat_amount", "tax_amount"):
                tax = _money(ent) or tax
            elif etype in ("receipt_date", "purchase_date", "invoice_date", "date"):
                if not date_text:
                    date_text = (ent.get("mentionText") or "").strip()
                nv = ent.get("normalizedValue") or {}
                dv = nv.get("dateValue")
                if isinstance(dv, dict) and dv.get("year") and not date_norm:
                    try:
                        date_norm = (
                            f"{int(dv['year']):04d}-"
                            f"{int(dv.get('month', 1)):02d}-"
                            f"{int(dv.get('day', 1)):02d}"
                        )
                    except Exception:
                        date_norm = ""
            elif etype == "currency" and ent.get("mentionText") and not currency:
                currency = (ent.get("mentionText") or "").strip().upper()[:3]

        # Resolve the date with a UK day-first bias.  Document AI runs in a US
        # region and routinely flips DD/MM receipts to month-first, so when the
        # printed date is a plain numeric string we re-read it ourselves as
        # day-first.  Order of preference:
        #   1. UK re-parse of the date entity's printed text (most reliable)
        #   2. Document AI's normalised date (for long-form dates like "11 May")
        #   3. UK re-parse of the whole receipt text (catches dates Document AI
        #      missed entirely, e.g. small print at the foot of the receipt)
        date_iso = _parse_uk_date(date_text) or date_norm or _parse_uk_date(raw_text)

        if not merchant or total is None:
            be = self._parse_best_effort(raw_text)
            merchant = merchant or be.get("merchant", "")
            if total is None:
                total = be.get("amount")

        return {
            "merchant": merchant,
            "total": total,
            "net": net,
            "tax": tax,
            "date": date_iso,
            "currency": currency or "GBP",
        }

    def create_draft(self, raw_text: str, *, source: str = "manual") -> ReceiptRecord:
        parsed = self._parse_best_effort(raw_text or "")
        record = ReceiptRecord(
            source=source,
            raw_text=raw_text or "",
            merchant=parsed.get("merchant", ""),
            amount=parsed.get("amount"),
            currency=parsed.get("currency", "GBP"),
            transaction_ref=parsed.get("transaction_ref", ""),
            status="new",
            metadata={"parser": "scaffold-v1"},
        )
        return append_receipt(self._store_file, record)

    def list_recent(self, limit: int = 100) -> list[ReceiptRecord]:
        return list_receipts(self._store_file, limit=limit)

    def link_to_event(self, receipt_id: str, event_key: str) -> ReceiptRecord | None:
        # Link only. No live calendar/Xero writes in scaffold mode.
        return update_receipt(
            self._store_file,
            receipt_id,
            event_key=(event_key or "").strip(),
            status="linked_pending",
        )

    def mark_reconciled(self, receipt_id: str, *, notes: str = "") -> ReceiptRecord | None:
        return update_receipt(
            self._store_file,
            receipt_id,
            status="reconciled",
            notes=notes or "",
        )

    def _save_file(self, *, file_bytes: bytes, filename: str) -> Path:
        self._upload_dir.mkdir(parents=True, exist_ok=True)
        safe_name = Path(filename or "receipt.jpg").name
        suffix = Path(safe_name).suffix.lower()
        if suffix not in {".jpg", ".jpeg", ".png", ".pdf", ".heic", ".webp"}:
            suffix = ".jpg"
        digest = hashlib.sha256(file_bytes).hexdigest()[:16]
        final_name = f"{int(time.time())}_{digest}{suffix}"
        out_path = self._upload_dir / final_name
        out_path.write_bytes(file_bytes)
        return out_path

    def _run_document_ai(self, *, file_bytes: bytes, mime_type: str) -> dict[str, Any]:
        settings = self.settings()
        project_id = str(settings.get("document_ai_project_id") or "").strip()
        location = str(settings.get("document_ai_location") or "us").strip() or "us"
        processor_id = str(settings.get("document_ai_processor_id") or "").strip()
        if not (project_id and processor_id):
            raise ValueError("Document AI is not configured in Receipt settings")

        service_account_file = str(
            settings.get("google_service_account_file")
            or self._config.google_credentials_file
        ).strip() or self._config.google_credentials_file
        creds = service_account.Credentials.from_service_account_file(
            service_account_file,
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )
        creds.refresh(Request())
        token = creds.token
        if not token:
            raise ValueError("Could not authorize Document AI request")

        endpoint = (
            f"https://{location}-documentai.googleapis.com/v1/projects/"
            f"{project_id}/locations/{location}/processors/{processor_id}:process"
        )
        _SUPPORTED = {
            "application/pdf", "image/jpeg", "image/png",
            "image/gif", "image/bmp", "image/tiff", "image/webp",
        }
        mt = (mime_type or "image/jpeg").lower().strip()
        if mt == "image/jpg":
            mt = "image/jpeg"
        if mt not in _SUPPORTED:
            raise ValueError(
                f"File type \u2018{mt}\u2019 is not supported by Document AI "
                f"(supported: JPEG, PNG, PDF, WebP, TIFF, GIF, BMP) \u2014 OCR skipped"
            )
        payload = {
            "rawDocument": {
                "content": base64.b64encode(file_bytes).decode("ascii"),
                "mimeType": mt,
            }
        }
        resp = requests.post(
            endpoint,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=45,
        )
        if resp.status_code >= 300:
            msg = (resp.text or "").strip().replace("\n", " ")
            raise ValueError(f"Document AI error {resp.status_code}: {msg[:220]}")
        data = resp.json() or {}
        document = data.get("document") or {}
        entities = []
        for ent in document.get("entities") or []:
            entities.append(
                {
                    "type": ent.get("type"),
                    "mentionText": ent.get("mentionText"),
                    "normalizedValue": ent.get("normalizedValue"),
                    "confidence": ent.get("confidence"),
                }
            )
        return {
            "text": str(document.get("text") or ""),
            "entities": entities,
        }

    def test_document_ai_connection(self) -> tuple[bool, str]:
        settings = self.settings()
        project_id = str(settings.get("document_ai_project_id") or "").strip()
        location = str(settings.get("document_ai_location") or "us").strip() or "us"
        processor_id = str(settings.get("document_ai_processor_id") or "").strip()
        if not (project_id and processor_id):
            return False, "Document AI project ID and processor ID are required."
        service_account_file = str(
            settings.get("google_service_account_file")
            or self._config.google_credentials_file
        ).strip() or self._config.google_credentials_file
        if not Path(service_account_file).exists():
            return False, f"Google service account file not found: {service_account_file}"
        try:
            creds = service_account.Credentials.from_service_account_file(
                service_account_file,
                scopes=["https://www.googleapis.com/auth/cloud-platform"],
            )
            creds.refresh(Request())
            token = creds.token
            if not token:
                return False, "Failed to obtain Google access token."
            endpoint = (
                f"https://{location}-documentai.googleapis.com/v1/projects/"
                f"{project_id}/locations/{location}/processors/{processor_id}"
            )
            resp = requests.get(
                endpoint,
                headers={"Authorization": f"Bearer {token}"},
                timeout=20,
            )
            if resp.status_code >= 300:
                msg = (resp.text or "").replace("\n", " ").strip()
                return False, f"Document AI HTTP {resp.status_code}: {msg[:220]}"
            return True, "Document AI connection OK."
        except Exception as exc:
            return False, str(exc).splitlines()[0][:220]

    def _parse_best_effort(self, raw_text: str) -> dict:
        text = (raw_text or "").strip()
        if not text:
            return {"merchant": "", "amount": None, "currency": "GBP", "transaction_ref": ""}

        # Keep parser deliberately conservative at scaffold stage.
        amount = None
        m_amt = re.search(r"£\s*(\d+(?:\.\d{1,2})?)", text, flags=re.I)
        if m_amt:
            try:
                amount = float(m_amt.group(1))
            except Exception:
                amount = None

        transaction_ref = ""
        m_ref = re.search(r"\b(?:ref|auth|txn|transaction)\s*[:#-]?\s*([A-Z0-9-]{5,})\b", text, flags=re.I)
        if m_ref:
            transaction_ref = m_ref.group(1).strip()
        else:
            m_ref2 = re.search(r"\b([A-Z0-9]{6,12})\b", text, flags=re.I)
            if m_ref2:
                transaction_ref = m_ref2.group(1).strip()

        merchant = text.splitlines()[0].strip()[:120]
        return {
            "merchant": merchant,
            "amount": amount,
            "currency": "GBP",
            "transaction_ref": transaction_ref,
        }
