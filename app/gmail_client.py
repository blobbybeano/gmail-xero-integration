"""Read-only Gmail API client for the Email Invoice Importer.

This module NEVER modifies messages — it only reads them.  It requests
``gmail.readonly`` scope, which cannot alter read/unread status.

Typical use:
    creds = load_admin_credentials(config)
    client = GmailClient(creds)
    msgs = client.search_messages("2024-01-01", "2024-03-31")
    for m in msgs:
        meta = client.get_message_metadata(m["id"])
        atts = client.get_attachments(m["id"])
"""

from __future__ import annotations

import base64
import email.utils as _eu
import re
from datetime import date, timedelta
from typing import Any

from googleapiclient.discovery import build

GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"

# ── Invoice-attachment heuristics ─────────────────────────────────────────────

_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

_INVOICE_EXTS  = {".pdf", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".tiff", ".docx"}
_INVOICE_MIMES = {
    "application/pdf",
    "image/jpeg", "image/jpg", "image/png",
    "image/gif",  "image/webp", "image/tiff",
    _DOCX_MIME,
}

# Filenames that almost certainly aren't invoices (camera roll shots, etc.)
_PHOTO_RE = re.compile(
    r"^(img_|dsc_|dsc\d|dcim|pano_|vid_|mov_|photo|screenshot|screen_shot)",
    re.IGNORECASE,
)

# Image filenames that are almost always email-signature logos / social icons /
# embedded decoration rather than a real invoice image.  Deliberately does NOT
# include generic names like image001 / img1234 — scanners and MFP devices emit
# real invoices under those names, so they are governed by the inline + size
# tests below (an inline image001.png signature is rejected as inline; a genuine
# scanned image001.jpg attachment of reasonable size is kept).
_LOGO_RE = re.compile(
    r"^(logo|signature|sig[_-]|icon|banner|header|footer|divider|"
    r"spacer|pixel|avatar|emoji|facebook|twitter|linkedin|instagram|youtube|"
    r"social|badge|seal|outlook-)",
    re.IGNORECASE,
)

# Real photographed / scanned invoice images are rarely this small; signature
# logos and embedded icons almost always are. PDFs are exempt from this check.
_MIN_IMAGE_BYTES = 25_000


def is_invoice_attachment(
    filename: str,
    mime_type: str,
    *,
    inline: bool = False,
    size: int | None = None,
) -> bool:
    """Heuristic: is this MIME part likely a real invoice?

    PDFs are accepted (suppliers almost always send PDF invoices). Image parts
    are only accepted when they look like a genuine attachment rather than an
    email-signature logo or embedded decoration: not inline, not a known
    logo/icon filename, and not tiny.
    """
    fn  = (filename or "").strip()
    mt  = (mime_type or "").lower().split(";")[0].strip()
    ext = ("." + fn.rsplit(".", 1)[-1]).lower() if "." in fn else ""
    if ext not in _INVOICE_EXTS and mt not in _INVOICE_MIMES:
        return False
    if _PHOTO_RE.match(fn):
        return False

    # PDFs and Word docs are accepted outright — suppliers send invoices as PDF
    # and (e.g. insurance brokers) as Word .docx. Neither is a signature logo.
    is_doc = (mt == "application/pdf") or ext == ".pdf" or ext == ".docx" or mt == _DOCX_MIME
    if is_doc:
        return True

    # From here on it's an image — apply the stricter "real attachment" tests so
    # we don't OCR every signature logo in the mailbox.
    if inline:
        return False
    if _LOGO_RE.match(fn):
        return False
    if size is not None and size < _MIN_IMAGE_BYTES:
        return False
    return True


# ── Low-level helpers ─────────────────────────────────────────────────────────

def _next_day_str(date_iso: str) -> str:
    """YYYY-MM-DD → next calendar day as YYYY/MM/DD for Gmail before: query."""
    d = date.fromisoformat(date_iso)
    return (d + timedelta(days=1)).strftime("%Y/%m/%d")


def _parse_from(raw: str) -> tuple[str, str]:
    """'Display Name <addr@example.com>' → (addr, display_name)."""
    raw = (raw or "").strip()
    if "<" in raw and ">" in raw:
        name = raw[: raw.index("<")].strip().strip('"').strip("'")
        addr = raw[raw.index("<") + 1 : raw.index(">")].strip()
        return addr.lower(), name
    return raw.lower(), ""


def _parse_email_date(raw: str) -> str:
    """RFC-2822 Date header → YYYY-MM-DD (falls back to today)."""
    try:
        ts = _eu.parsedate_to_datetime(raw)
        return ts.date().isoformat()
    except Exception:
        return date.today().isoformat()


# ── GmailClient ───────────────────────────────────────────────────────────────

class GmailClient:
    """Thin, read-only wrapper around the Gmail API v1.

    Using ``gmail.readonly`` scope guarantees we can never mark messages as
    read/unread, move them, or modify labels.
    """

    def __init__(self, creds) -> None:
        self._svc = build("gmail", "v1", credentials=creds)

    def profile(self) -> dict[str, Any]:
        """Return Gmail profile metadata for the connected account."""
        return self._svc.users().getProfile(userId="me").execute()

    # ------------------------------------------------------------------
    def search_messages(
        self,
        date_from: str,
        date_to: str,
        *,
        extra_query: str = "",
        max_results: int = 2000,
    ) -> list[dict[str, str]]:
        """Return [{id, threadId}] for emails with attachments in the range.

        ``date_from`` / ``date_to`` are YYYY-MM-DD strings (both inclusive).
        Gmail's ``after:`` / ``before:`` are day-granular; we add one day to
        ``before:`` so the end date itself is included.

        This call is *read-only* — Gmail's list endpoint never modifies state.
        """
        d_from = date_from.replace("-", "/")
        d_to   = _next_day_str(date_to)

        parts = [f"has:attachment", f"after:{d_from}", f"before:{d_to}"]
        if extra_query:
            parts.append(extra_query)
        query = " ".join(parts)

        messages: list[dict] = []
        page_token: str | None = None

        while True:
            kw: dict[str, Any] = {
                "userId": "me",
                "q": query,
                "maxResults": min(500, max_results - len(messages)),
            }
            if page_token:
                kw["pageToken"] = page_token
            resp       = self._svc.users().messages().list(**kw).execute()
            batch      = resp.get("messages") or []
            messages.extend(batch)
            page_token = resp.get("nextPageToken")
            if not page_token or len(messages) >= max_results:
                break

        return messages

    # ------------------------------------------------------------------
    def get_message_metadata(self, message_id: str) -> dict[str, Any]:
        """Return header-level info without fetching the body.

        Fields: message_id, thread_id, from (addr), from_name,
                subject, date_iso, label_ids.
        """
        msg = (
            self._svc.users()
            .messages()
            .get(
                userId="me",
                id=message_id,
                format="metadata",
                metadataHeaders=["From", "Subject", "Date"],
            )
            .execute()
        )
        hdrs = {
            h["name"].lower(): h["value"]
            for h in (msg.get("payload") or {}).get("headers", [])
        }
        addr, name = _parse_from(hdrs.get("from", ""))
        return {
            "message_id": message_id,
            "thread_id":  msg.get("threadId", ""),
            "from":       addr,
            "from_name":  name,
            "subject":    hdrs.get("subject", ""),
            "snippet":    msg.get("snippet", ""),
            "date_iso":   _parse_email_date(hdrs.get("date", "")),
            "label_ids":  msg.get("labelIds") or [],
        }

    # ------------------------------------------------------------------
    def get_attachments(self, message_id: str) -> list[dict[str, Any]]:
        """Return all attachment parts as {filename, mime_type, data_bytes}.

        Fetches full message payload (read-only) then walks MIME parts
        recursively, downloading each attachment body.
        """
        msg = (
            self._svc.users()
            .messages()
            .get(userId="me", id=message_id, format="full")
            .execute()
        )
        results: list[dict] = []
        self._walk_parts(message_id, msg.get("payload") or {}, results)
        return results

    # ------------------------------------------------------------------
    def _walk_parts(
        self,
        message_id: str,
        payload: dict,
        out: list,
    ) -> None:
        """Recursively walk MIME parts to collect attachment data."""
        body     = payload.get("body") or {}
        att_id   = body.get("attachmentId")
        filename = (payload.get("filename") or "").strip()
        mime     = (payload.get("mimeType") or "").strip()

        headers  = {
            (h.get("name") or "").lower(): (h.get("value") or "")
            for h in (payload.get("headers") or [])
        }
        disp     = headers.get("content-disposition", "").lower()
        cid      = headers.get("content-id", "") or headers.get("x-attachment-id", "")
        # "inline" disposition, or a Content-ID with no explicit attachment
        # disposition, means the part is embedded in the email body (signature
        # logo, tracking pixel, social icon) rather than a real attachment.
        inline   = ("inline" in disp) or (bool(cid) and "attachment" not in disp)
        size_hint = body.get("size") or 0

        if att_id and filename:
            try:
                data_resp = (
                    self._svc.users()
                    .messages()
                    .attachments()
                    .get(userId="me", messageId=message_id, id=att_id)
                    .execute()
                )
                raw     = data_resp.get("data") or ""
                decoded = base64.urlsafe_b64decode(raw + "==")
            except Exception:
                decoded = b""
            out.append({
                "filename":   filename,
                "mime_type":  mime,
                "data_bytes": decoded,
                "inline":     inline,
                "size":       len(decoded) or size_hint,
            })

        for part in payload.get("parts") or []:
            self._walk_parts(message_id, part, out)
