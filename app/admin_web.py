from __future__ import annotations

import datetime as dt
import base64
import hashlib
import json
import os
import re
import secrets
import threading
import time
import uuid
import urllib.parse
from pathlib import Path
from functools import wraps
from html import escape

import requests
from flask import Flask, Response, jsonify, redirect, request, send_file, session, url_for
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.security import check_password_hash, generate_password_hash
from googleapiclient.errors import HttpError

from .admin_store import (
    DEFAULT_SALES_STATS_FIELDS,
    DEFAULT_STATS_FIELDS,
    get_active_calendars,
    get_calendar_cash_sheets,
    get_cash_sheet_target,
    get_calendar_sales_sheets,
    get_cash_backlog,
    get_cash_submitter_sheets,
    get_json_setting,
    get_sales_backlog,
    get_sales_sheet_target,
    get_sales_stats_fields,
    get_sales_submitter_sheets,
    get_sheet_target,
    get_seen_submitters,
    get_stats_fields,
    get_submitter_aliases,
    init_admin_store,
    set_calendar_cash_sheets,
    set_cash_sheet_target,
    set_calendar_sales_sheets,
    set_submitter_aliases,
    set_active_calendars,
    set_cash_submitter_sheets,
    set_json_setting,
    set_sales_sheet_target,
    set_sales_stats_fields,
    set_sales_submitter_sheets,
    set_sheet_target,
    set_stats_fields,
    get_receipts_settings,
    set_receipts_settings,
    get_expense_settings,
    set_expense_settings,
    get_cashflows_settings,
    set_cashflows_settings,
    add_cashflows_reconciled_refs,
    create_expense_test_session,
    get_expense_test_session,
    set_expense_test_result,
)
from .config import load_config
from .event_processor import (
    set_title_status_emoji,
    set_title_mail_emoji,
    sync_invoice_block_from_xero,
    upsert_receipt_submit_link,
)
from .google_admin import (
    build_calendar_service_from_creds,
    build_sheets_service_from_creds,
    list_calendars,
    list_spreadsheets,
    load_admin_credentials,
    oauth_authorization_url,
    oauth_exchange_code,
    save_admin_credentials,
)
from .google_calendar import update_event_description, build_calendar_service
from .config import AppConfig
from .xero_client import (
    load_xero_token,
    save_xero_token,
    token_is_expired,
    refresh_xero_token,
    build_xero_client,
    xero_is_disabled,
    xero_lockout_is_active,
    get_xero_rate_limit_until_ts,
)
from .google_sheets import backfill_submitter_in_sheet, update_invoice_paid_in_sheet
from .google_sheets import ensure_header, append_stats_row
from .event_processor import extract_sales_lines, parse_customer_fields, payment_choice
from .admin_store import (
    get_enabled,
    set_enabled,
    get_google_watches,
    get_xero_webhook_key,
    set_xero_webhook_key,
    get_xero_webhook_verified,
    set_xero_webhook_verified,
    get_xero_tenants,
    set_xero_tenants,
    upsert_xero_tenant,
    get_cashflows_correlation_sheet_id,
    set_cashflows_correlation_sheet_id,
    get_openai_settings,
    set_openai_settings,
)
from .cashflows_sheet import fetch_card_lookup
from .trigger import (
    trigger_poll,
    trigger_watch_check,
    queue_calendar_target,
    queue_event_target,
)
from .state import (
    load_state,
    save_state_merged,
    get_last_sync,
    get_sales_log_marker,
    set_sales_log_marker,
    mark_invoice_sent,
    mark_invoice_paid,
    mark_recent_xero_webhook,
    get_invoice_for_event,
)
from .log_feed import feed as _feed
from .receipts import ReceiptService
from .receipts import expense_store as exp_store
from .receipts import dump_store
from .receipts import email_store as em_store
from .receipts import email_pipeline as em_pipe
from . import card_feed as cardfeed
from . import plaid_match
from . import gmail_client as _gmail_mod
from .cashflows_reconciliation import (
    CashflowsClient,
    CashflowsReconciliationService,
    parse_cashflows_settlements,
    _date,
)
from .cashflows_csv import (
    CsvParseError,
    build_csv_reconciliation_preview,
    recommend_export_range,
)
from .xero_busy import clear_xero_busy, mark_xero_busy, xero_busy_status

# Engineering note:
# Webhook/admin flows here are coupled to poller state semantics.
# If invoice/calendar sync behavior changes, update
# docs/ENGINEERING_LOGIC_GUARDRAILS.md.


XERO_AUTH_URL = "https://login.xero.com/identity/connect/authorize"
XERO_TOKEN_URL = "https://identity.xero.com/connect/token"
XERO_CONNECTIONS_URL = "https://api.xero.com/connections"
REQUIRED_XERO_SCOPES = (
    "offline_access",
    "accounting.banktransactions",
    "accounting.invoices",
    "accounting.contacts",
    "accounting.settings",
    "accounting.payments",
    "accounting.attachments",
)


STAT_OPTIONS = [
    ("diary_entry_name", "Diary entry name"),
    ("submitter", "Person who submitted invoice"),
    ("customer", "Customer name"),
    ("invoice_number", "Invoice number"),
    ("receipt_details", "Receipt details (when implemented)"),
    ("slot_datetime", "Diary slot date/time"),
    ("payment_datetime", "Payment date/time"),
    ("payment_method", "Payment method"),
    ("paid_status", "Payment status (Paid/Pending)"),
    ("job_cost_ex_vat", "Job cost (ex VAT)"),
    ("job_cost_inc_vat", "Job cost (inc VAT)"),
]

SALES_STAT_OPTIONS = [
    ("submitter", "Person who submitted"),
    ("customer", "Customer name"),
    ("slot_datetime", "Diary slot date/time"),
    ("payment_method", "Payment method"),
    ("invoice_number", "Invoice number"),
    ("sales_item_desc", "Sales item + value"),
    ("sales_total_ex_vat", "Sales total"),
]

_BASE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Powwash Admin</title>
<script src="https://cdn.tailwindcss.com"></script>
<style>
  body {{ font-family: 'Inter', system-ui, sans-serif; }}
  .status-dot-green {{ display:inline-block;width:10px;height:10px;border-radius:50%;background:#22c55e;margin-right:6px; }}
  .status-dot-red {{ display:inline-block;width:10px;height:10px;border-radius:50%;background:#ef4444;margin-right:6px; }}
  .status-dot-yellow {{ display:inline-block;width:10px;height:10px;border-radius:50%;background:#f59e0b;margin-right:6px; }}
</style>
<script>
// Suppress JSON-parse SyntaxErrors that arise when a polling fetch follows a
// session-expired redirect and gets HTML instead of JSON.  These are harmless
// (the poller just retries) but without this handler some browsers surface them
// as unhandled errors and trigger false "app crashed" alerts in the preview pane.
window.addEventListener('unhandledrejection', function(e) {{
  var msg = (e.reason && e.reason.message) || '';
  if (msg.indexOf('JSON') !== -1 || msg.indexOf('end of input') !== -1 ||
      msg.indexOf('Unexpected token') !== -1) {{
    e.preventDefault();
  }}
}});
window.addEventListener('error', function(e) {{
  var msg = (e.message || '');
  if (msg.indexOf('end of input') !== -1 || msg.indexOf('Unexpected token') !== -1) {{
    e.preventDefault();
  }}
}});
</script>
</head>
<body class="bg-gray-50 min-h-screen">
{body}
</body>
</html>"""


# ── Receipt background-job store ──────────────────────────────────────────────
# Keyed by job_id (uuid hex).  Values: {status, message}.
# In-memory only — jobs are short-lived (seconds), so no DB needed.
_receipt_jobs: dict[str, dict] = {}
_receipt_jobs_lock = threading.Lock()

# ── Cashflows CSV bulk-submit background job ──────────────────────────────────
# A large CSV can involve dozens of Xero writes.  Doing them inside the HTTP
# request risks (a) the gunicorn worker timeout, (b) Xero's ~60 calls/min rate
# limit, and (c) the browser closing mid-run leaving half-written batches.
# Instead we run the whole submission in ONE daemon thread, paced under the
# rate limit, persisting progress after every batch so the Cashflows page can
# reattach a live progress screen after a reload and so a resume never
# re-writes a batch that already succeeded.
_cf_submit_lock = threading.Lock()          # guards start/registry access
_cf_submit_job: dict[str, dict] = {}        # single live job, keyed by job_id
# DB key (admin settings) mirroring the live job for durability across restarts.
_CF_SUBMIT_JOB_KEY = "cashflows_csv_submit_job"
# Minimum spacing between Xero write calls (seconds).  ~1.1s => <55 calls/min,
# comfortably under Xero's 60/min ceiling so we essentially never trip a 429.
_CF_SUBMIT_PACE_SECONDS = 1.1


def _clear_finished_cashflows_submit_job(admin_db_file: str) -> None:
    """Clear stale Cashflows submit progress after a fresh preview is loaded."""
    with _cf_submit_lock:
        if any(
            str(job.get("status") or "") in {"running", "paused"}
            for job in _cf_submit_job.values()
        ):
            return
        _cf_submit_job.clear()
    try:
        set_json_setting(admin_db_file, _CF_SUBMIT_JOB_KEY, {})
    except Exception:
        pass


# ── Field Expenses helpers ────────────────────────────────────────────────────

_EXPENSE_STATUS_BADGES = {
    "pending_review": ("Needs review", "bg-amber-100 text-amber-800"),
    "approved": ("Approved", "bg-emerald-100 text-emerald-800"),
    "submitted": ("Sent to Xero", "bg-blue-100 text-blue-800"),
    "failed": ("Failed", "bg-red-100 text-red-800"),
    "settled": ("Paid", "bg-gray-200 text-gray-700"),
}


def _exp_money(value, currency: str = "GBP") -> str:
    try:
        amount = float(value or 0)
    except (TypeError, ValueError):
        amount = 0.0
    symbol = "£" if (currency or "GBP").upper() == "GBP" else ""
    prefix = "" if symbol else (currency or "").upper() + " "
    return f"{prefix}{symbol}{amount:,.2f}"


def _exp_status_badge(status: str) -> str:
    label, classes = _EXPENSE_STATUS_BADGES.get(
        status, (status or "—", "bg-gray-100 text-gray-700")
    )
    return (
        f'<span class="inline-block px-2 py-0.5 rounded-full text-xs '
        f'font-medium {classes}">{escape(label)}</span>'
    )


def _exp_day_label(iso_date: str) -> str:
    """Turn an ISO date string into 'Today' / 'Yesterday' / 'Sat 21 Jun'."""
    try:
        d = dt.datetime.fromisoformat(iso_date).date()
    except (TypeError, ValueError):
        return iso_date or "—"
    today = dt.datetime.now(dt.timezone.utc).date()
    delta = (today - d).days
    if delta == 0:
        return "Today"
    if delta == 1:
        return "Yesterday"
    return d.strftime("%a %-d %b")


def _exp_sniff_mime(head: bytes) -> str | None:
    """Return a trusted MIME from magic bytes, or None if not an image/PDF.

    Never trust the client-supplied Content-Type for stored receipts — a token
    holder could otherwise upload active HTML/JS and have it served from our
    own origin.  We only accept the image/PDF types a receipt can actually be.
    """
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"GIF87a") or head.startswith(b"GIF89a"):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head.startswith(b"%PDF"):
        return "application/pdf"
    return None


def _exp_heic_to_jpeg(data: bytes, filename: str, mime: str):
    """If the upload is HEIC/HEIF (iPhone's default photo format), decode and
    re-encode it as a high-quality JPEG.

    Document AI can't OCR HEIC, and most browsers can't display it either, so we
    convert once on upload. Returns (data, filename, mime) unchanged for any
    other format. Quality is kept high (95) so faded / creased receipts stay
    legible for OCR. If the decoder isn't available or the file isn't really
    HEIC, the original bytes are returned and OCR will skip gracefully.
    """
    head = data[:12]
    is_heic = (
        len(head) >= 12 and head[4:8] == b"ftyp"
        and head[8:12] in (
            b"heic", b"heix", b"heim", b"heis",
            b"hevc", b"hevx", b"mif1", b"msf1",
        )
    ) or (mime or "").lower() in ("image/heic", "image/heif")
    if not is_heic:
        return data, filename, mime
    try:
        import io
        import pillow_heif  # type: ignore
        from PIL import Image  # type: ignore

        pillow_heif.register_heif_opener()
        img = Image.open(io.BytesIO(data))
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=95)
        stem = Path(filename or "receipt").stem or "receipt"
        return buf.getvalue(), stem + ".jpg", "image/jpeg"
    except Exception:
        return data, filename, mime


def _exp_resize_for_ocr(data: bytes, filename: str, mime: str):
    """Shrink large receipt photos to max 1200 px on the long edge and
    re-encode as JPEG at quality 88.

    Modern phone cameras produce 10–20 MB images that are far larger than
    Document AI needs. Resizing here cuts upload and OCR time significantly
    while preserving enough detail for accurate reading of even creased or
    faded receipts. PDFs pass through unchanged. Images already within the
    size limit are returned as-is (no re-encoding loss).
    """
    if (mime or "").lower() == "application/pdf":
        return data, filename, mime
    try:
        import io
        from PIL import Image
        img = Image.open(io.BytesIO(data))
        max_dim = 1200
        w, h = img.size
        if max(w, h) <= max_dim:
            return data, filename, mime  # already small enough — skip re-encode
        scale = max_dim / max(w, h)
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=88)
        stem = Path(filename or "receipt").stem or "receipt"
        return buf.getvalue(), stem + ".jpg", "image/jpeg"
    except Exception:
        return data, filename, mime


def _exp_compute_vat(amount_inc, vat_rate: float):
    """Given an inc-VAT total and a rate (%), return (ex, vat) rounded to 2dp."""
    try:
        inc = float(amount_inc or 0)
    except (TypeError, ValueError):
        return (None, None)
    if inc <= 0:
        return (0.0, 0.0)
    rate = (vat_rate or 0) / 100.0
    ex = round(inc / (1 + rate), 2) if rate else round(inc, 2)
    vat = round(inc - ex, 2)
    return (ex, vat)


def _exp_reconcile_amounts(total, net, tax, vat_rate):
    """Reconcile gross / net / VAT, trusting real OCR figures over rate maths.

    Document AI commonly returns the gross total and the *net* (taxable base)
    but no explicit VAT line.  The old logic then assumed the WHOLE total was
    standard-rated (``total - total / 1.2``), which over-states VAT whenever a
    receipt mixes standard- and zero-rated items (e.g. fuel + food).  We instead
    derive VAT from whichever figures we actually have and treat any leftover as
    a zero-rated portion.

    Returns ``(total, net, tax, zero_rated)`` rounded to 2dp (each may be None).
    """
    rate = (vat_rate or 0) / 100.0

    def _f(v):
        try:
            return None if v is None else round(float(v), 2)
        except (TypeError, ValueError):
            return None

    total, net, tax = _f(total), _f(net), _f(tax)
    if total is not None:
        if net is not None and tax is None:
            # Net is the standard-rated taxable base; VAT is rate x net.
            tax = round(net * rate, 2)
        elif tax is not None and net is None:
            net = round(total - tax, 2)
        elif net is None and tax is None:
            net = round(total / (1 + rate), 2) if rate else total
            tax = round(total - net, 2)
    elif net is not None and tax is not None:
        total = round(net + tax, 2)
    elif net is not None:
        tax = round(net * rate, 2)
        total = round(net + tax, 2)

    zero_rated = None
    if total is not None and net is not None and tax is not None:
        zr = round(total - net - tax, 2)
        zero_rated = zr if zr > 0.005 else 0.0
    return total, net, tax, zero_rated


def _exp_uk_date(value: str) -> str:
    """Render an ISO (yyyy-mm-dd) date as UK ``dd/mm/yyyy``.

    Anything that is not a clean ISO date is returned unchanged so we never hide
    whatever the parser actually found.
    """
    s = (value or "").strip()
    if not s:
        return ""
    try:
        return dt.datetime.strptime(s[:10], "%Y-%m-%d").strftime("%d/%m/%Y")
    except (ValueError, TypeError):
        return s


def _receipt_bg_worker(
    job_id: str,
    config,
    push_fn,          # _feed.push callable for activity log
    file_bytes: bytes,
    filename: str,
    content_type: str,
    event_key: str,
) -> None:
    """OCR → Xero attach → calendar update, run in a daemon thread."""

    def _set(status: str, message: str) -> None:
        with _receipt_jobs_lock:
            existing = _receipt_jobs.get(job_id, {})
            created = existing.get("created_at", time.time())
            _receipt_jobs[job_id] = {
                "status": status,
                "message": message,
                "created_at": created,
            }

    try:
        svc = ReceiptService(config)

        # 1 — Save file locally + run OCR
        _set("processing", "Analysing receipt…")
        rec = svc.process_uploaded_receipt(
            file_bytes=file_bytes,
            filename=filename,
            mime_type=content_type,
            event_key=event_key,
            source="signed_upload",
        )

        # 2 — Attach to Xero invoice
        # The invoice map is keyed by the FULL "calendar_id:event_id" composite
        # (see set_invoice_for_event in main.py).  Test links use a "_test:"
        # prefix which we strip to reach the real composite key.
        xero_note = ""
        lookup_key = event_key
        if lookup_key.startswith("_test:"):
            lookup_key = lookup_key[len("_test:"):]
        event_id = lookup_key
        is_test = event_key.startswith("_test:")
        state = load_state(config.state_file)
        invoice_id = get_invoice_for_event(state, lookup_key)

        # The whole point of the upload is to land the photo on the Xero
        # invoice, so a missing invoice mapping or unavailable Xero connection
        # is a real failure the engineer must see — never report success.
        if not invoice_id and not is_test:
            _set(
                "failed",
                "Receipt saved, but no Xero invoice is linked to this job yet. "
                "It will need to be attached manually.",
            )
            return
        if invoice_id:
            _set("processing", "Attaching to Xero invoice…")
            xero_client = build_xero_client(config)
            if not xero_client:
                _set(
                    "failed",
                    "Receipt saved, but the Xero connection is not available. "
                    "Reconnect Xero and try again.",
                )
                return
            result = xero_client.attach_file_to_invoice(
                invoice_id, filename, content_type, file_bytes
            )
            if result.get("dry_run"):
                xero_note = "dry-run: would attach to Xero invoice"
            else:
                xero_note = "attached to Xero invoice"
            push_fn(
                f"Receipt photo attached to Xero invoice for event {event_id}: {filename}",
                "success" if not result.get("dry_run") else "system",
            )

        # 3 — Update Google Calendar description (best-effort)
        parts = event_key.split(":", 1)
        if len(parts) == 2 and not parts[0].startswith("_"):
            cal_id, cal_event_id = parts[0], parts[1]
            try:
                _set("processing", "Updating calendar…")
                cal_svc = build_calendar_service(config)
                ev = cal_svc.events().get(calendarId=cal_id, eventId=cal_event_id).execute()
                old_desc = ev.get("description") or ""
                stamp = dt.datetime.now().strftime("%-d %b %Y %H:%M")
                new_desc = upsert_receipt_submit_link(old_desc, f"Receipt submitted \u2713 {stamp}")
                if new_desc != old_desc:
                    update_event_description(config, cal_event_id, new_desc, calendar_id=cal_id)
            except Exception:
                pass  # Calendar update is best-effort; don't fail the whole job

        msg = "Receipt saved"
        if xero_note:
            msg += f" — {xero_note}"
        _set("success", msg)

    except Exception as exc:
        _set("failed", str(exc).splitlines()[0][:300])


_NAV_LINKS = [
    ("/", "Live Feed"),
    ("/receipts/expenses", "Field Expenses"),
    ("/cardfeed", "Bank Statement"),
    ("/receipts/emails", "Email Invoices"),
    ("/cashflows-sync", "Cashflows Sync"),
    ("/assistant", "Assistant"),
    ("/settings", "Settings"),
]


def _nav_is_active(path: str, href: str) -> bool:
    if href == "/":
        return path == "/"
    return path == href or path.startswith(href + "/")


def _nav() -> str:
    """Shared, responsive top navigation used on every admin page.

    Gated on the admin session (`logged_in`) so it never renders on the public
    login page or the engineer/subcontractor portal (which also use `_page`).
    Collapses to a hamburger menu on small screens and includes a universal
    Back button so the menu is identical and complete on every page.
    """
    try:
        if not session.get("logged_in"):
            return ""
        path = request.path
    except Exception:
        return ""

    desktop, mobile = [], []
    for href, label in _NAV_LINKS:
        active = _nav_is_active(path, href)
        d_cls = ("bg-indigo-600 text-white" if active
                 else "text-neutral-300 hover:text-white hover:bg-neutral-800")
        desktop.append(
            f'<a href="{href}" class="px-3 py-1.5 text-xs font-medium rounded-lg '
            f'transition-colors {d_cls}">{label}</a>'
        )
        m_cls = ("bg-indigo-600 text-white" if active
                 else "text-neutral-200 hover:text-white hover:bg-neutral-800")
        mobile.append(
            f'<a href="{href}" class="block px-3 py-2 text-sm font-medium '
            f'rounded-lg {m_cls}">{label}</a>'
        )
    desktop_html = "".join(desktop)
    mobile_html = "".join(mobile)

    return f"""
<header class="bg-neutral-900 border-b border-neutral-800 sticky top-0 z-40">
  <div class="max-w-7xl mx-auto px-4 sm:px-6">
    <div class="flex items-center justify-between h-14 gap-2">
      <div class="flex items-center gap-1 min-w-0">
        <a href="/" class="flex items-center gap-2 shrink-0 mr-1">
          <span class="w-8 h-8 bg-indigo-600 rounded-lg flex items-center justify-center">
            <svg class="w-4 h-4 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 10V3L4 14h7v7l9-11h-7z"/></svg>
          </span>
          <span class="text-sm font-semibold text-white tracking-tight hidden sm:block">Powwash Bridge</span>
        </a>
        <button type="button" onclick="history.back()" class="inline-flex items-center gap-1 text-xs font-medium text-neutral-400 hover:text-white px-2 py-1 rounded-lg hover:bg-neutral-800 transition-colors">
          <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 19l-7-7 7-7"/></svg>
          Back
        </button>
      </div>
      <nav class="hidden md:flex items-center gap-1">
        {desktop_html}
        <a href="/logout" class="ml-1 px-3 py-1.5 text-xs font-medium text-neutral-400 hover:text-white rounded-lg transition-colors">Sign out</a>
      </nav>
      <button type="button" aria-label="Menu" onclick="var m=document.getElementById('pw-mobile-nav');if(m)m.classList.toggle('hidden');" class="md:hidden inline-flex items-center justify-center w-9 h-9 rounded-lg text-neutral-300 hover:text-white hover:bg-neutral-800 transition-colors">
        <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 6h16M4 12h16M4 18h16"/></svg>
      </button>
    </div>
    <nav id="pw-mobile-nav" class="hidden md:hidden pb-3 space-y-1">
      {mobile_html}
      <a href="/logout" class="block px-3 py-2 text-sm font-medium text-neutral-400 hover:text-white rounded-lg">Sign out</a>
    </nav>
  </div>
</header>"""


def _page(body: str) -> str:
    return _BASE_HTML.format(body=_nav() + body)


def _current_base_url() -> str:
    """Return the public HTTPS base URL from the current request — works on any domain."""
    base = request.host_url.rstrip("/")
    return base.replace("http://", "https://", 1)


def _url_row(label: str, url: str, hint: str) -> str:
    """Render a copy-able URL row for the Deployment URLs panel."""
    url_esc = escape(url)
    url_js = url.replace("'", "\\'")
    return (
        f'<div class="flex items-center gap-3 px-5 py-3">'
        f'<div class="min-w-0 flex-1">'
        f'<p class="text-xs font-medium text-gray-700">{label}</p>'
        f'<p class="text-xs text-gray-400 mt-0.5">{escape(hint)}</p>'
        f'</div>'
        f'<div class="flex items-center gap-2 shrink-0">'
        f'<code class="text-xs font-mono text-indigo-700 bg-indigo-50 px-2 py-1 rounded truncate max-w-xs hidden sm:block">{url_esc}</code>'
        f'<button type="button" '
        f'onclick="navigator.clipboard.writeText(\'{url_js}\');this.textContent=\'Copied!\';setTimeout(()=>this.textContent=\'Copy\',1500)" '
        f'class="shrink-0 px-2.5 py-1 text-xs font-medium bg-gray-100 hover:bg-gray-200 text-gray-700 rounded-lg transition-colors">'
        f'Copy'
        f'</button>'
        f'</div>'
        f'</div>'
    )


def _extract_spreadsheet_id(value: str) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", text)
    if m:
        return m.group(1)
    return text


def _first_url(text: str) -> str:
    m = re.search(r"https://[^\s\"'<>]+", text or "")
    return m.group(0) if m else ""


def _validate_google_credentials_json(raw: bytes) -> tuple[bool, str]:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception:
        return False, "File is not valid UTF-8 JSON."

    section = None
    if isinstance(payload, dict):
        if isinstance(payload.get("installed"), dict):
            section = payload["installed"]
        elif isinstance(payload.get("web"), dict):
            section = payload["web"]
    if not section:
        return False, "JSON must contain 'installed' or 'web' OAuth credentials."

    client_id = str(section.get("client_id") or "").strip()
    client_secret = str(section.get("client_secret") or "").strip()
    if not client_id or not client_secret:
        return False, "Credentials JSON is missing client_id/client_secret."
    return True, ""


def _validate_google_service_account_json(raw: bytes) -> tuple[bool, str]:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception:
        return False, "File is not valid UTF-8 JSON."
    if not isinstance(payload, dict):
        return False, "JSON must be an object."
    if str(payload.get("type") or "").strip() != "service_account":
        return False, "JSON must be a Google service account key (type=service_account)."
    required = ["client_email", "private_key", "token_uri"]
    missing = [k for k in required if not str(payload.get(k) or "").strip()]
    if missing:
        return False, f"Missing fields: {', '.join(missing)}"
    return True, ""


def _default_sa_path(admin_db_file: str) -> str:
    """Default Google service-account file location.

    Production mounts a writable persistent volume at /data, but the dev/preview
    environment does not (it is a read-only filesystem). Fall back to a path next
    to the admin DB, which is writable in both environments.
    """
    try:
        data_dir = Path("/data")
        if data_dir.is_dir() and os.access(str(data_dir), os.W_OK):
            return "/data/google_service_account.json"
    except Exception:
        pass
    return str(Path(admin_db_file).resolve().with_name("google_service_account.json"))


def _resolve_writable_sa_path(configured: str, admin_db_file: str) -> Path:
    """Pick a writable location to store the uploaded service-account file.

    Tries the configured path first, then the environment default, then a path
    next to the admin DB. Skips any candidate whose directory cannot be written.
    """
    candidates: list[Path] = []
    cfg = (configured or "").strip()
    if cfg:
        candidates.append(Path(cfg))
    candidates.append(Path(_default_sa_path(admin_db_file)))
    fallback = Path(admin_db_file).resolve().with_name("google_service_account.json")
    candidates.append(fallback)
    for cand in candidates:
        try:
            cand.parent.mkdir(parents=True, exist_ok=True)
            probe = cand.parent / ".sa_write_test"
            probe.write_bytes(b"")
            probe.unlink()
            return cand
        except Exception:
            continue
    return fallback


def _assistant_config(db_path: str) -> dict:
    raw = get_json_setting(
        db_path,
        "assistant_config",
        {
            "write_enabled": False,
            "always_confirm": True,
        },
    )
    if not isinstance(raw, dict):
        raw = {}
    return {
        "write_enabled": bool(raw.get("write_enabled", False)),
        "always_confirm": True,  # enforced
    }


def _set_assistant_config(db_path: str, *, write_enabled: bool) -> None:
    set_json_setting(
        db_path,
        "assistant_config",
        {"write_enabled": bool(write_enabled), "always_confirm": True},
    )


def _append_assistant_chat(role: str, text: str) -> None:
    history = session.get("assistant_chat_history") or []
    if not isinstance(history, list):
        history = []
    history.append(
        {
            "role": role,
            "text": text,
            "ts": dt.datetime.now(dt.timezone.utc).astimezone().strftime("%H:%M"),
        }
    )
    session["assistant_chat_history"] = history[-30:]


def _active_calendar_ids(config: AppConfig) -> list[str]:
    return get_active_calendars(config.admin_db_file, config.google_calendar_id)


def _find_events_by_title(config: AppConfig, phrase: str, *, days_back: int = 180, days_forward: int = 60) -> list[dict]:
    creds = load_admin_credentials(config)
    if not creds:
        return []
    svc = build_calendar_service(config)
    now = dt.datetime.now(dt.timezone.utc)
    tmin = (now - dt.timedelta(days=days_back)).isoformat().replace("+00:00", "Z")
    tmax = (now + dt.timedelta(days=days_forward)).isoformat().replace("+00:00", "Z")
    target = (phrase or "").strip().lower()
    if not target:
        return []
    hits: list[dict] = []
    for cal_id in _active_calendar_ids(config):
        page_token = None
        while True:
            resp = (
                svc.events()
                .list(
                    calendarId=cal_id,
                    timeMin=tmin,
                    timeMax=tmax,
                    singleEvents=True,
                    orderBy="startTime",
                    maxResults=250,
                    pageToken=page_token,
                )
                .execute()
            )
            for ev in (resp.get("items") or []):
                summary = str(ev.get("summary") or "")
                if target in summary.lower():
                    hits.append({"calendar_id": cal_id, "event": ev})
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
    return hits


def _list_today_created_event_titles(config: AppConfig) -> list[str]:
    creds = load_admin_credentials(config)
    if not creds:
        return []
    svc = build_calendar_service(config)
    tz = dt.datetime.now().astimezone().tzinfo or dt.timezone.utc
    today = dt.datetime.now(tz).date()
    out: list[tuple[dt.datetime, str]] = []
    for cal_id in _active_calendar_ids(config):
        page_token = None
        while True:
            resp = (
                svc.events()
                .list(
                    calendarId=cal_id,
                    singleEvents=True,
                    orderBy="updated",
                    maxResults=250,
                    pageToken=page_token,
                )
                .execute()
            )
            for ev in (resp.get("items") or []):
                created = str(ev.get("created") or "").strip()
                if not created:
                    continue
                try:
                    created_dt = dt.datetime.fromisoformat(created.replace("Z", "+00:00")).astimezone(tz)
                except Exception:
                    continue
                if created_dt.date() == today:
                    out.append((created_dt, str(ev.get("summary") or "(no title)")))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
    out.sort(key=lambda x: x[0])
    return [title for _, title in out]


def _xero_fetch_draft_invoices(config: AppConfig) -> list[dict]:
    if xero_is_disabled():
        return []
    tok = load_xero_token(config.xero_token_file)
    access_token = str(tok.get("access_token") or "").strip()
    tenant_id = str(tok.get("tenant_id") or "").strip()
    if not (access_token and tenant_id):
        return []
    url = "https://api.xero.com/api.xro/2.0/Invoices"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Xero-tenant-id": tenant_id,
        "Accept": "application/json",
    }
    results: list[dict] = []
    for page in range(1, 8):
        resp = requests.get(
            url,
            headers=headers,
            params={"where": 'Status=="DRAFT"', "page": page},
            timeout=20,
        )
        if not resp.ok:
            break
        rows = (resp.json() or {}).get("Invoices") or []
        if not rows:
            break
        results.extend(rows)
        if len(rows) < 100:
            break
    return results


def _openai_assistant_reply(prompt: str) -> tuple[str | None, str | None]:
    """
    Return (reply, error_code). error_code is one of:
    - missing_key
    - api_error
    """
    _db_key = get_openai_settings(config.admin_db_file).get("api_key", "")
    api_key = _db_key or (os.getenv("OPENAI_API_KEY") or "").strip()
    if not api_key:
        return None, "missing_key"

    _db_model = get_openai_settings(config.admin_db_file).get("model", "")
    model = _db_model or (os.getenv("OPENAI_MODEL") or "gpt-4o-mini").strip()
    system_prompt = (
        "You are an assistant for a field-service calendar and invoicing app. "
        "Be concise and practical. If a user asks for risky write actions, tell them "
        "the app requires explicit approval before making changes."
    )
    payload = {
        "model": model,
        "input": [
            {
                "role": "system",
                "content": [{"type": "input_text", "text": system_prompt}],
            },
            {
                "role": "user",
                "content": [{"type": "input_text", "text": prompt}],
            },
        ],
        "max_output_tokens": 500,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    try:
        resp = requests.post(
            "https://api.openai.com/v1/responses",
            headers=headers,
            json=payload,
            timeout=45,
        )
    except Exception:
        return None, "api_error"

    if not resp.ok:
        return None, "api_error"

    try:
        data = resp.json()
    except Exception:
        return None, "api_error"

    out = str(data.get("output_text") or "").strip()
    if out:
        return out, None

    # Fallback parse for response content structure.
    parts: list[str] = []
    for item in (data.get("output") or []):
        if str(item.get("type") or "") != "message":
            continue
        for c in (item.get("content") or []):
            ctype = str(c.get("type") or "").lower()
            if ctype in {"output_text", "text", "message_text"}:
                txt = str(c.get("text") or "").strip()
                if txt:
                    parts.append(txt)
    merged = "\n".join(parts).strip()
    if merged:
        return merged, None
    return None, "api_error"


_xero_acct_cache: "dict[str, tuple[float, list, list, list, str]]" = {}
_xero_all_acct_cache: "dict[str, tuple[float, list, str]]" = {}
_xero_conn_cache: "dict[str, tuple[float, list[dict]]]" = {}
_XERO_CACHE_TTL = 300  # seconds (5 min)
_XERO_CONN_CACHE_TTL = 300  # seconds


def _get_tenant_acct_themes(at: str, tid: str) -> "tuple[list, list, list, str]":
    """Return (revenue_accounts, bank_accounts, branding_themes) for a Xero tenant.
    Results are cached for _XERO_CACHE_TTL seconds so the settings page doesn't
    make live API calls on every load."""
    if xero_is_disabled():
        cached = _xero_acct_cache.get(tid)
        if cached:
            _ts, _rev, _bank, _themes, _w = cached
            return _rev, _bank, _themes, "Xero is paused — showing cached account data."
        return [], [], [], "Xero is paused — account data unavailable until Xero is re-enabled."
    # Cache by tenant only (not access token) so the settings page stays fast
    # across normal token refreshes.
    key = tid
    cached = _xero_acct_cache.get(key)
    if cached:
        ts, rev, bank, themes, warning = cached
        if time.time() - ts < _XERO_CACHE_TTL:
            return rev, bank, themes, warning
    hdrs = {
        "Authorization": f"Bearer {at}",
        "Xero-tenant-id": tid,
        "Accept": "application/json",
    }
    rev: list = []
    bank: list = []
    all_accounts: list = []
    warnings: list[str] = []
    try:
        _ar = requests.get(
            "https://api.xero.com/api.xro/2.0/Accounts",
            headers=hdrs,
            timeout=3,
        )
        if _ar.ok:
            for _a in _ar.json().get("Accounts", []):
                if _a.get("Status") != "ACTIVE":
                    continue
                all_accounts.append(_a)
                _typ = _a.get("Type", "")
                if _typ in ("REVENUE", "SALES", "OTHERINCOME"):
                    rev.append(_a)
                elif _typ == "BANK":
                    bank.append(_a)
        else:
            if _ar.status_code == 403:
                warnings.append("Cannot load account list (missing accounting.settings scope).")
            elif _ar.status_code == 401:
                warnings.append("Cannot load account list (token unauthorised: reconnect Xero with accounting.settings scope).")
            else:
                warnings.append(f"Cannot load account list (HTTP {_ar.status_code}).")
    except Exception:
        warnings.append("Cannot load account list (request failed).")
    themes: list = []
    try:
        _tr = requests.get(
            "https://api.xero.com/api.xro/2.0/BrandingThemes",
            headers=hdrs,
            timeout=3,
        )
        if _tr.ok:
            themes = sorted(
                _tr.json().get("BrandingThemes", []),
                key=lambda x: (x.get("SortOrder", 999), x.get("Name", "")),
            )
        else:
            if _tr.status_code == 403:
                warnings.append("Cannot load branding themes (missing accounting.settings scope).")
            elif _tr.status_code == 401:
                warnings.append("Cannot load branding themes (token unauthorised: reconnect Xero with accounting.settings scope).")
            else:
                warnings.append(f"Cannot load branding themes (HTTP {_tr.status_code}).")
    except Exception:
        warnings.append("Cannot load branding themes (request failed).")
    warning = " ".join(dict.fromkeys([w.strip() for w in warnings if w.strip()]))
    # If live fetch fails but we have stale cache, use stale data immediately so
    # Settings remains responsive instead of blocking on repeated API failures.
    if warning and cached:
        _, c_rev, c_bank, c_themes, c_warning = cached
        if c_rev or c_bank or c_themes:
            _warn = warning
            if c_warning and c_warning not in _warn:
                _warn = f"{_warn} Using cached options."
            return c_rev, c_bank, c_themes, _warn

    # Avoid caching empty+failed responses for long periods; this keeps recovery fast
    # after a reconnect/refresh.
    if warning and not (rev or bank or themes):
        # short-lived cache for failed empty responses, so UI recovers quickly
        _xero_acct_cache[key] = (time.time() - (_XERO_CACHE_TTL - 20), rev, bank, themes, warning)
    else:
        _xero_acct_cache[key] = (time.time(), rev, bank, themes, warning)
    if all_accounts or warning:
        _xero_all_acct_cache[key] = (time.time(), all_accounts, warning)
    return rev, bank, themes, warning


def _get_tenant_cached_only(tid: str) -> "tuple[list, list, list, str]":
    """Return cached tenant options only (never perform network I/O)."""
    cached = _xero_acct_cache.get(tid)
    if not cached:
        return [], [], [], ""
    _, rev, bank, themes, warning = cached
    return rev, bank, themes, warning


_XERO_ALL_ACCT_SNAPSHOT_KEY = "xero_active_accounts_snapshot"


def _get_xero_active_accounts(
    at: str, tid: str, db_path: str | None = None
) -> "tuple[list, str]":
    """Return active Xero accounts for owner-paid receipt account selection."""
    if xero_is_disabled():
        snap = []
        if db_path:
            snap = get_json_setting(db_path, _XERO_ALL_ACCT_SNAPSHOT_KEY, []) or []
            if not snap:
                snap = get_json_setting(db_path, _XERO_EXP_ACCT_SNAPSHOT_KEY, []) or []
        if snap:
            return snap, "Xero is paused — showing the last saved account list."
        cached = _xero_all_acct_cache.get(tid)
        if cached:
            _ts, accounts, _warning = cached
            return accounts, "Xero is paused — showing cached account data."
        return [], "Xero is paused — account data unavailable until Xero is re-enabled."
    cached = _xero_all_acct_cache.get(tid)
    if cached:
        ts, accounts, warning = cached
        if time.time() - ts < _XERO_CACHE_TTL:
            return accounts, warning
    if not at or not tid:
        return [], "Xero account list unavailable until Xero is connected."
    hdrs = {
        "Authorization": f"Bearer {at}",
        "Xero-tenant-id": tid,
        "Accept": "application/json",
    }
    try:
        res = requests.get(
            "https://api.xero.com/api.xro/2.0/Accounts",
            headers=hdrs,
            timeout=3,
        )
        if res.ok:
            accounts = [
                a for a in res.json().get("Accounts", [])
                if a.get("Status") == "ACTIVE" and str(a.get("Code") or "").strip()
            ]
            accounts.sort(key=lambda a: (str(a.get("Type") or ""), str(a.get("Name") or "")))
            _xero_all_acct_cache[tid] = (time.time(), accounts, "")
            if db_path and accounts:
                try:
                    set_json_setting(db_path, _XERO_ALL_ACCT_SNAPSHOT_KEY, accounts)
                except Exception:
                    pass
            return accounts, ""
        warning = f"Cannot load full Xero account list (HTTP {res.status_code})."
    except Exception:
        warning = "Cannot load full Xero account list (request failed)."
    if cached:
        _ts, accounts, _old_warning = cached
        return accounts, warning + " Using cached options."
    if db_path:
        snap = get_json_setting(db_path, _XERO_ALL_ACCT_SNAPSHOT_KEY, []) or []
        if not snap:
            snap = get_json_setting(db_path, _XERO_EXP_ACCT_SNAPSHOT_KEY, []) or []
        if snap:
            return snap, warning + " Using last saved account list."
    _xero_all_acct_cache[tid] = (
        time.time() - (_XERO_CACHE_TTL - 20),
        [],
        warning,
    )
    return [], warning


# Expense-style Xero accounts (the categories engineers code receipts against).
_xero_exp_acct_cache: "dict[str, tuple[float, list, str]]" = {}


def _load_xero_at_tid(config) -> "tuple[str, str, str]":
    """Return (access_token, tenant_id, warning), refreshing the token if expired."""
    try:
        tok = load_xero_token(config.xero_token_file)
    except Exception:
        return "", "", "Xero is not connected."
    if not tok:
        return "", "", "Xero is not connected."
    warn = ""
    try:
        if token_is_expired(tok) and tok.get("refresh_token"):
            cid, csec = _get_xero_creds(config)
            if cid and csec:
                refreshed = refresh_xero_token(cid, csec, tok["refresh_token"])
                tok = {**tok, **refreshed}
                save_xero_token(config.xero_token_file, tok)
    except Exception:
        warn = "Could not refresh the Xero connection."
    at = str(tok.get("access_token") or "").strip()
    tid = str(tok.get("tenant_id") or "").strip()
    if not (at and tid):
        return "", "", (warn or "Xero is not connected.")
    return at, tid, warn


_XERO_EXP_ACCT_SNAPSHOT_KEY = "xero_expense_accounts_snapshot"


def _get_xero_expense_accounts(
    at: str, tid: str, db_path: str | None = None
) -> "tuple[list, str]":
    """Return (expense_accounts, warning).

    Expense accounts are the Xero accounts engineers code receipts against
    (fuel, advertising, plant & hire, etc.). Filters to expense-style account
    types and caches the result for _XERO_CACHE_TTL seconds.

    When Xero is paused (XERO_DISABLED), no live request is made; the last
    saved snapshot from the DB is returned so receipt category prediction can
    still be tested offline."""
    if xero_is_disabled():
        snap = []
        if db_path:
            snap = get_json_setting(db_path, _XERO_EXP_ACCT_SNAPSHOT_KEY, []) or []
        if snap:
            return snap, "Xero is paused — showing the last saved account list."
        return (
            [],
            "Xero is paused — no saved account list yet "
            "(re-enable Xero once to load and save the options).",
        )
    if not (at and tid):
        return [], "Xero is not connected."
    key = tid
    cached = _xero_exp_acct_cache.get(key)
    if cached:
        ts, accts, warning = cached
        if time.time() - ts < _XERO_CACHE_TTL:
            return accts, warning
    hdrs = {
        "Authorization": f"Bearer {at}",
        "Xero-tenant-id": tid,
        "Accept": "application/json",
    }
    accts: list = []
    warning = ""
    try:
        r = requests.get(
            "https://api.xero.com/api.xro/2.0/Accounts",
            headers=hdrs,
            timeout=3,
        )
        if r.ok:
            for a in r.json().get("Accounts", []):
                if a.get("Status") != "ACTIVE":
                    continue
                if a.get("Type", "") in ("EXPENSE", "OVERHEADS", "DIRECTCOSTS"):
                    accts.append(a)
            accts.sort(key=lambda x: (x.get("Name") or "").lower())
            if db_path and accts:
                try:
                    set_json_setting(db_path, _XERO_EXP_ACCT_SNAPSHOT_KEY, accts)
                except Exception:
                    pass
        elif r.status_code == 403:
            warning = "Cannot load Xero expense accounts (missing accounting.settings scope)."
        elif r.status_code == 401:
            warning = "Cannot load Xero expense accounts (reconnect Xero)."
        else:
            warning = f"Cannot load Xero expense accounts (HTTP {r.status_code})."
    except Exception:
        warning = "Cannot load Xero expense accounts (request failed)."
    # Prefer stale cache over showing nothing when a live fetch fails.
    if warning and cached:
        _, c_accts, _c_warn = cached
        if c_accts:
            return c_accts, f"{warning} Using cached options."
    if warning and not accts:
        _xero_exp_acct_cache[key] = (time.time() - (_XERO_CACHE_TTL - 20), accts, warning)
    else:
        _xero_exp_acct_cache[key] = (time.time(), accts, warning)
    return accts, warning


def _exp_acct_label(account: dict) -> str:
    """Human label for a Xero account dropdown option: 'Name (Code)'."""
    code = str(account.get("Code") or "").strip()
    name = str(account.get("Name") or "").strip()
    if name and code:
        return f"{name} ({code})"
    return name or code


def _exp_acct_options(accounts: list, selected: str, *, default_label: str) -> str:
    """Build <option>s for an expense/bank account <select>.

    Value is the Xero account Code. ``selected`` is the currently saved code;
    if it isn't in the list it's preserved as an extra option so saved values
    are never silently dropped."""
    sel = str(selected or "").strip()
    out = f"<option value=''>{escape(default_label)}</option>"
    found = False
    for a in accounts:
        code = str(a.get("Code") or "").strip()
        if not code:
            continue
        is_sel = " selected" if code == sel else ""
        if is_sel:
            found = True
        out += f"<option value='{escape(code)}'{is_sel}>{escape(_exp_acct_label(a))}</option>"
    if sel and not found:
        out += f"<option value='{escape(sel)}' selected>Saved code ({escape(sel)})</option>"
    return out


def _expense_owner_paid_enabled(eng: dict | None) -> bool:
    if not eng or eng.get("kind") != "company_card":
        return False
    return bool(eng.get("allow_owner_paid")) and bool(
        str(eng.get("owner_paid_account_code") or "").strip()
    )


def _expense_normalise_payment_source(
    eng: dict | None,
    requested: str | None,
) -> "tuple[str, str]":
    """Return (payment_source, owner_paid_account_code) for a receipt.

    Subcontractor receipts are always owner-paid. Company-card engineers only
    get owner-paid if the office explicitly enables it and assigns a Xero
    account in Field Expenses.
    """
    source = (requested or "company_card").strip()
    if source not in {"company_card", "owner_paid"}:
        source = "company_card"
    owner_code = str((eng or {}).get("owner_paid_account_code") or "").strip()
    if eng and eng.get("kind") != "company_card":
        return "owner_paid", owner_code
    if source == "owner_paid" and _expense_owner_paid_enabled(eng):
        return "owner_paid", owner_code
    return "company_card", ""


_AI_HINTS_KEY = "ai_receipt_hints"


def get_ai_receipt_hints(db_path: str) -> str:
    """Free-text admin hints that steer how receipts are coded to accounts
    (e.g. 'petrol receipts below £30 can be machinery fuel'). Stored in the
    admin DB; empty string when unset."""
    raw = get_json_setting(db_path, _AI_HINTS_KEY, "")
    return raw.strip() if isinstance(raw, str) else ""


def set_ai_receipt_hints(db_path: str, hints: str) -> None:
    set_json_setting(db_path, _AI_HINTS_KEY, (hints or "").strip())


def _ai_receipt_hints_block(db_path: str) -> str:
    """Prompt fragment injecting the admin's coding hints, or '' when none."""
    hints = get_ai_receipt_hints(db_path)
    if not hints:
        return ""
    return (
        "The business owner has provided the following bookkeeping hints. "
        "Apply each hint ONLY when its stated conditions are clearly and "
        "specifically met by THIS receipt — do NOT generalise a conditional "
        "rule to receipts that do not satisfy every condition it states:\n"
        + hints[:2000] + "\n\n"
    )


def _ai_categorize_receipt(
    db_path: str,
    merchant: str,
    raw_text: str,
    amount,
    accounts: list,
) -> "tuple[str, str]":
    """Use the LLM to pick the best-matching Xero expense account for a receipt.

    Uses the app's OWN OpenAI credentials — the API key the admin saves in
    Settings (stored in the admin DB), falling back to an ``OPENAI_API_KEY``
    environment variable. This intentionally does NOT use Replit AI
    Integrations. Returns (code, name); empty strings when it can't confidently
    decide or no key/model/accounts are available.
    """
    if not accounts:
        return "", ""
    _oa = get_openai_settings(db_path)
    api_key = (_oa.get("api_key") or "").strip() or (os.getenv("OPENAI_API_KEY") or "").strip()
    model = (_oa.get("model") or "").strip() or (os.getenv("OPENAI_MODEL") or "gpt-4o-mini").strip()
    if not api_key:
        return "", ""

    # Build the menu of valid choices the model must pick from.
    valid: "dict[str, str]" = {}
    lines = []
    for a in accounts:
        code = str(a.get("Code") or "").strip()
        name = str(a.get("Name") or "").strip()
        if not code:
            continue
        valid[code] = name
        desc = str(a.get("Description") or "").strip()
        lines.append(f"- {code}: {name}" + (f" — {desc}" if desc else ""))
    if not valid:
        return "", ""

    menu = "\n".join(lines)
    snippet = (raw_text or "")[:1500]
    amt = "" if amount is None else f"\nTotal amount: {amount}"
    hints = _ai_receipt_hints_block(db_path)
    prompt = (
        "You are a UK bookkeeping assistant. Pick the single best expense "
        "account for this receipt from the list below. Only choose a code that "
        "appears in the list. If nothing fits well, return an empty code.\n\n"
        + hints
        + f"Receipt merchant: {merchant or 'unknown'}{amt}\n"
        f"Receipt text:\n{snippet}\n\n"
        f"Available accounts (code: name):\n{menu}\n\n"
        'Respond ONLY as JSON: {"code": "<account code or empty>"}'
    )

    try:
        resp = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "response_format": {"type": "json_object"},
            },
            timeout=45,
        )
        if not resp.ok:
            return "", ""
        data = resp.json()
        content = (
            (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
        ).strip()
        parsed = json.loads(content) if content else {}
        code = str(parsed.get("code") or "").strip()
        if code and code in valid:
            return code, valid[code]
    except Exception:
        return "", ""
    return "", ""


def _ai_analyze_receipt(db_path, merchant, raw_text, total, accounts, vat_rate):
    """Break a receipt into one or more spend segments, each coded to a real
    Xero expense account, so split receipts (e.g. fuel + food) are accounted for
    separately.

    Uses the app's OWN OpenAI key (admin DB, falling back to ``OPENAI_API_KEY``).
    Returns a list of segments::

        {"label", "account_code", "account_name", "gross", "net", "vat",
         "vat_rate"}

    Empty list when it can't decide / no key / no accounts, or when the AI's
    split fails to reconcile against the receipt total. The caller is then
    responsible for falling back to single-account categorisation.
    """
    if not accounts:
        return []
    _oa = get_openai_settings(db_path)
    api_key = (_oa.get("api_key") or "").strip() or (os.getenv("OPENAI_API_KEY") or "").strip()
    model = (_oa.get("model") or "").strip() or (os.getenv("OPENAI_MODEL") or "gpt-4o-mini").strip()
    if not api_key:
        return []

    valid: "dict[str, str]" = {}
    lines = []
    for a in accounts:
        code = str(a.get("Code") or "").strip()
        name = str(a.get("Name") or "").strip()
        if not code:
            continue
        valid[code] = name
        desc = str(a.get("Description") or "").strip()
        lines.append(f"- {code}: {name}" + (f" — {desc}" if desc else ""))
    if not valid:
        return []

    menu = "\n".join(lines)
    snippet = (raw_text or "")[:2000]
    tot = "" if total is None else f"\nReceipt total (inc VAT): {total}"
    hints = _ai_receipt_hints_block(db_path)
    prompt = (
        "You are a UK bookkeeping assistant. Break this receipt into spend "
        "segments and code each to ONE expense account from the list below.\n"
        "- Most receipts are a SINGLE segment.\n"
        "- If a receipt clearly mixes different kinds of spend (for example "
        "road fuel AND food/drink), return a SEPARATE segment for each part.\n"
        "- 'gross' is the amount paid for that segment INCLUDING VAT, in the "
        "receipt currency.\n"
        "- 'vat_rate' is the UK VAT percentage for that segment: 20 for fuel and "
        "most standard goods, 0 for zero-rated items such as most cold takeaway "
        "food and basic groceries. Use your best judgement.\n"
        "- The gross amounts MUST add up to the receipt total.\n"
        "- Only use account codes that appear in the list.\n\n"
        + hints
        + f"Receipt merchant: {merchant or 'unknown'}{tot}\n"
        f"Receipt text:\n{snippet}\n\n"
        f"Available accounts (code: name):\n{menu}\n\n"
        'Respond ONLY as JSON: {"segments": [{"label": "<short label>", '
        '"account_code": "<code>", "gross": <number>, "vat_rate": <number>}]}'
    )

    try:
        resp = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "response_format": {"type": "json_object"},
            },
            timeout=60,
        )
        if not resp.ok:
            return []
        data = resp.json()
        content = (
            (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
        ).strip()
        parsed = json.loads(content) if content else {}
    except Exception:
        return []

    raw_segs = parsed.get("segments") if isinstance(parsed, dict) else None
    if not isinstance(raw_segs, list) or not raw_segs:
        return []

    segments = []
    for seg in raw_segs:
        if not isinstance(seg, dict):
            continue
        code = str(seg.get("account_code") or "").strip()
        if code not in valid:
            continue
        try:
            gross = round(float(seg.get("gross")), 2)
        except (TypeError, ValueError):
            continue
        if gross <= 0:
            continue
        try:
            seg_rate = float(seg.get("vat_rate"))
        except (TypeError, ValueError):
            seg_rate = vat_rate or 0
        if seg_rate < 0 or seg_rate > 100:
            seg_rate = vat_rate or 0
        r = seg_rate / 100.0
        seg_net = round(gross / (1 + r), 2) if r else gross
        seg_vat = round(gross - seg_net, 2)
        segments.append({
            "label": (str(seg.get("label") or valid[code]).strip() or valid[code])[:60],
            "account_code": code,
            "account_name": valid[code],
            "gross": gross,
            "net": seg_net,
            "vat": seg_vat,
            "vat_rate": round(seg_rate, 2),
        })

    if not segments:
        return []

    # Sanity-check the segment grosses against the receipt total. If the AI's
    # split doesn't reconcile, don't trust it. Allow a little rounding slack
    # (2%) but cap it at £1 so high-value receipts can't drift materially.
    if total is not None and total > 0:
        seg_sum = round(sum(s["gross"] for s in segments), 2)
        tolerance = min(max(0.10, total * 0.02), 1.00)
        if abs(seg_sum - total) > tolerance:
            return []

    return segments


def _ai_split_multi_receipts(db_path, raw_text):
    """Detect when ONE photo contains SEVERAL physical receipts (e.g. two pump
    receipts photographed side by side) and split the OCR text into one record
    per receipt.

    Uses the app's OWN OpenAI key. Returns a list of dicts::

        {"merchant", "date", "total", "net", "tax", "text"}

    — but ONLY when the AI confidently finds two or more distinct receipts,
    each with its own payment total. Returns [] in every other case (single
    receipt, no key, uncertain, or implausible output), so callers can treat
    [] as "process normally as one receipt".
    """
    text = (raw_text or "").strip()
    if len(text) < 80:
        return []
    _oa = get_openai_settings(db_path)
    api_key = (_oa.get("api_key") or "").strip() or (os.getenv("OPENAI_API_KEY") or "").strip()
    model = (_oa.get("model") or "").strip() or (os.getenv("OPENAI_MODEL") or "gpt-4o-mini").strip()
    if not api_key:
        return []

    prompt = (
        "You are reading the OCR text of ONE photograph. Sometimes a photo "
        "contains TWO OR MORE separate physical receipts placed side by side, "
        "each with its own merchant header, its own total and its own payment.\n"
        "Decide whether this text contains more than one separate receipt.\n"
        "- Be conservative: if unsure, or if it is one receipt (even a long "
        "one with sub-totals), answer with a single receipt.\n"
        "- Separate receipts each have their OWN final amount paid (e.g. two "
        "'BALANCE DUE' / card payment blocks).\n"
        "- 'total' is the amount paid for THAT receipt including VAT.\n"
        "- 'date' is that receipt's purchase date as YYYY-MM-DD (empty if "
        "unreadable).\n"
        "- 'text' is the portion of the OCR text belonging to that receipt.\n\n"
        "OCR text:\n" + text[:6000] + "\n\n"
        'Respond ONLY as JSON: {"receipts": [{"merchant": "<name>", '
        '"date": "YYYY-MM-DD", "total": <number>, "vat": <number or null>, '
        '"text": "<that receipt\'s text>"}]}'
    )
    try:
        resp = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "response_format": {"type": "json_object"},
            },
            timeout=60,
        )
        if not resp.ok:
            return []
        data = resp.json()
        content = (
            (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
        ).strip()
        parsed = json.loads(content) if content else {}
    except Exception:
        return []

    raw = parsed.get("receipts") if isinstance(parsed, dict) else None
    if not isinstance(raw, list) or len(raw) < 2 or len(raw) > 6:
        return []
    # Anti-hallucination guard: every sub-receipt's total must literally
    # appear as an amount in the OCR text. (The photo's own OCR "total" can
    # legitimately be just ONE of the receipts, so summing against it would
    # wrongly reject valid splits — instead we demand textual evidence for
    # each amount the AI claims.)
    _digits = text.replace(",", "")
    out = []
    for r in raw:
        if not isinstance(r, dict):
            return []
        try:
            tot = round(float(r.get("total")), 2)
        except (TypeError, ValueError):
            return []
        if tot <= 0:
            return []
        if f"{tot:.2f}" not in _digits:
            return []
        try:
            vat = round(float(r.get("vat")), 2) if r.get("vat") is not None else None
        except (TypeError, ValueError):
            vat = None
        net = round(tot - vat, 2) if vat is not None else None
        date = str(r.get("date") or "").strip()[:10]
        try:
            if date:
                dt.date.fromisoformat(date)
        except ValueError:
            date = ""
        out.append({
            "merchant": str(r.get("merchant") or "").strip()[:120],
            "date": date,
            "total": tot,
            "net": net,
            "tax": vat,
            "text": str(r.get("text") or "").strip()[:5000],
        })
    return out


_FUEL_THRESHOLD_GBP = 40.0


def _find_fuel_accounts(accounts: list) -> "tuple[tuple[str, str], tuple[str, str]]":
    """Locate the (machinery, van) fuel accounts by name in the Xero expense
    account list. Returns ((code, name), (code, name)); either pair is
    ("", "") when no such account exists."""
    machinery = van = ("", "")
    for a in accounts or []:
        code = str(a.get("Code") or "").strip()
        name = str(a.get("Name") or "").strip()
        low = name.lower()
        if not code:
            continue
        is_mach = "fuel" in low and ("machin" in low or "plant" in low)
        is_van = "fuel" in low and "van" in low
        if is_mach and is_van:
            # Ambiguous name (mentions both) — don't guess either way.
            continue
        if is_mach:
            machinery = (code, name)
        elif is_van:
            van = (code, name)
    return machinery, van


def _apply_fuel_threshold(segments, cat_code, cat_name, total, accounts,
                          raw_text=""):
    """Deterministically enforce the business rule 'fuel under £40 is machinery
    fuel; fuel £40 or over is van fuel' — with one override: machinery fuel
    MUST be unleaded petrol, so any receipt whose text shows DIESEL always goes
    to van fuel regardless of amount (machines run on petrol, vans on diesel).

    The AI decides WHETHER a spend is fuel (by coding it to either fuel
    account); this function then makes the amount comparison in code, because
    LLMs are unreliable at numeric thresholds. Applies per-segment on split
    receipts and to the headline category otherwise. No-op unless BOTH fuel
    accounts exist in Xero. Returns (segments, cat_code, cat_name).
    """
    (m_code, m_name), (v_code, v_name) = _find_fuel_accounts(accounts)
    if not m_code or not v_code:
        return segments, cat_code, cat_name
    fuel_codes = {m_code, v_code}
    is_diesel = "diesel" in (raw_text or "").lower()

    def _pick(amount):
        if is_diesel:
            # Diesel is never machinery fuel — machines take unleaded.
            return v_code, v_name
        try:
            amt = float(amount)
        except (TypeError, ValueError):
            return None
        if amt <= 0:
            return None
        if amt < _FUEL_THRESHOLD_GBP:
            return m_code, m_name
        return v_code, v_name

    if segments:
        for seg in segments:
            if str(seg.get("account_code") or "") in fuel_codes:
                picked = _pick(seg.get("gross"))
                if picked:
                    seg["account_code"], seg["account_name"] = picked
        # Keep the headline category in step with the (possibly re-coded)
        # largest segment when it is fuel.
        if str(cat_code or "") in fuel_codes:
            primary = max(segments, key=lambda s: s.get("gross") or 0)
            if str(primary.get("account_code") or "") in fuel_codes:
                cat_code = primary["account_code"]
                cat_name = primary["account_name"]
    elif str(cat_code or "") in fuel_codes:
        picked = _pick(total)
        if picked:
            cat_code, cat_name = picked
    return segments, cat_code, cat_name


# ── Receipt Dump helpers (bulk past-receipt upload & reconciliation) ─────────

def _dump_digests(file_bytes: bytes) -> "tuple[str, str]":
    """Return (full_sha256, digest16) for a file. digest16 matches the digest
    baked into saved receipt filenames by ReceiptService._save_file."""
    import hashlib
    full = hashlib.sha256(file_bytes or b"").hexdigest()
    return full, full[:16]


def _dump_stored_digest(stored_file: str) -> str:
    """Pull the sha256[:16] digest out of a saved receipt filename
    ('{epoch}_{digest16}{suffix}'). Returns '' when not parseable."""
    import os as _os
    import re as _re
    name = _os.path.basename(str(stored_file or ""))
    m = _re.match(r"^\d+_([0-9a-fA-F]{6,64})\.", name)
    return m.group(1).lower() if m else ""


def _dump_norm_merchant(value: str) -> str:
    """Loose merchant key for logical duplicate detection."""
    return "".join(ch for ch in (value or "").lower() if ch.isalnum())


def _dump_mime_for(path: str) -> str:
    ext = (path.rsplit(".", 1)[-1].lower() if "." in path else "")
    return {
        "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
        "webp": "image/webp", "gif": "image/gif",
    }.get(ext, "")


def _dump_compare_receipt_images(db_path: str, path_a: str, path_b: str) -> dict:
    """The "Google receipt checker": ask the app's OpenAI vision model whether
    two receipt photos are the SAME physical receipt.

    Uses the app's OWN OpenAI key (admin DB, falling back to OPENAI_API_KEY).
    Degrades gracefully — returns {"available": False, ...} with a reason when
    it cannot run (no key, missing/PDF images, API error) so the caller can fall
    back to a manual-review flag instead of guessing.
    """
    import base64 as _b64
    import os as _os
    out = {"available": False, "same": None, "confidence": 0.0, "reason": ""}
    for p in (path_a, path_b):
        if not (p and _os.path.exists(p)):
            out["reason"] = "One of the receipt images is not available locally."
            return out
        if not _dump_mime_for(p):
            out["reason"] = "Image comparison only supports photo formats (not PDF)."
            return out
    _oa = get_openai_settings(db_path)
    api_key = (_oa.get("api_key") or "").strip() or (os.getenv("OPENAI_API_KEY") or "").strip()
    model = (_oa.get("model") or "").strip() or (os.getenv("OPENAI_MODEL") or "gpt-4o-mini").strip()
    if not api_key:
        out["reason"] = "No OpenAI key configured for image comparison."
        return out
    try:
        imgs = []
        for p in (path_a, path_b):
            with open(p, "rb") as fh:
                b64 = _b64.b64encode(fh.read()).decode("ascii")
            imgs.append({
                "type": "image_url",
                "image_url": {"url": f"data:{_dump_mime_for(p)};base64,{b64}"},
            })
        prompt = (
            "You are checking for duplicate expense claims. Two receipt photos "
            "are shown. Decide whether they are the SAME physical receipt (same "
            "transaction), even if photographed differently. Compare merchant, "
            "date, time, total and line items. Respond ONLY as JSON: "
            '{"same": true|false, "confidence": 0-1, "reason": "<short>"}.'
        )
        content = [{"type": "text", "text": prompt}] + imgs
        resp = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": content}],
                "response_format": {"type": "json_object"},
            },
            timeout=60,
        )
        if not resp.ok:
            out["reason"] = f"Image comparison API error ({resp.status_code})."
            return out
        data = resp.json()
        raw = ((data.get("choices") or [{}])[0].get("message", {}).get("content") or "").strip()
        parsed = json.loads(raw) if raw else {}
        out["available"] = True
        out["same"] = bool(parsed.get("same"))
        try:
            out["confidence"] = round(float(parsed.get("confidence") or 0.0), 2)
        except (TypeError, ValueError):
            out["confidence"] = 0.0
        out["reason"] = str(parsed.get("reason") or "").strip()[:300]
    except Exception as exc:
        out["reason"] = f"Image comparison failed: {str(exc).splitlines()[0][:160]}"
    return out


def _sheets_status_data(
    config: AppConfig, creds, target: dict[str, str]
) -> tuple[bool, str]:
    if not creds:
        return False, "Not connected to Google yet."

    spreadsheet_id = (target.get("spreadsheet_id") or "").strip()
    sheet_name = (target.get("sheet_name") or "Sheet1").strip() or "Sheet1"
    if not spreadsheet_id:
        return False, "No spreadsheet URL or ID saved yet."

    scopes = set(getattr(creds, "scopes", []) or [])
    if "https://www.googleapis.com/auth/spreadsheets" not in scopes:
        return False, "Token is missing spreadsheets scope. Reconnect Google."

    try:
        service = build_sheets_service_from_creds(creds)
        meta = (
            service.spreadsheets()
            .get(
                spreadsheetId=spreadsheet_id,
                fields="properties.title,sheets.properties.title",
            )
            .execute()
        )
    except HttpError as exc:
        text = str(exc)
        reason = ""
        message = text
        try:
            payload = json.loads((exc.content or b"{}").decode("utf-8"))
            err = payload.get("error", {}) if isinstance(payload, dict) else {}
            message = err.get("message") or message
            for d in err.get("details", []) or []:
                if isinstance(d, dict) and d.get("reason"):
                    reason = d["reason"]
                    break
            if not reason:
                first_err = (err.get("errors") or [{}])[0]
                if isinstance(first_err, dict):
                    reason = first_err.get("reason", "")
        except Exception:
            pass

        if reason == "SERVICE_DISABLED":
            url = _first_url(text) or "https://console.cloud.google.com/apis/library/sheets.googleapis.com"
            return False, f"Google Sheets API is disabled. Enable it at: {url}"
        if exc.resp and exc.resp.status == 404:
            return False, "Spreadsheet not found. Check the URL/ID."
        if exc.resp and exc.resp.status == 403:
            return False, f"No access to this spreadsheet: {message}"
        return False, f"Failed to access spreadsheet: {message}"

    workbook = (meta.get("properties", {}) or {}).get("title", "")
    tabs = {
        ((s.get("properties", {}) or {}).get("title") or "").strip()
        for s in (meta.get("sheets") or [])
    }
    if sheet_name not in tabs:
        return False, f"Spreadsheet found but tab '{sheet_name}' does not exist in '{workbook or spreadsheet_id}'."

    return True, f"Connected — {workbook or spreadsheet_id} / {sheet_name}"


def _oauth_client_id(config: AppConfig) -> str:
    path = Path(config.google_credentials_file)
    if path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            section = payload.get("web") or payload.get("installed") or {}
            return str(section.get("client_id") or "")
        except Exception:
            return ""
    return ""


def _xero_scope_string(scopes: list[str]) -> str:
    seen: set[str] = set()
    ordered: list[str] = []
    for scope in [*REQUIRED_XERO_SCOPES, *(scopes or [])]:
        s = str(scope).strip()
        if not s or s in seen:
            continue
        seen.add(s)
        ordered.append(s)
    return " ".join(ordered)


def _parse_scope_value(value) -> set[str]:
    if not value:
        return set()
    if isinstance(value, list):
        return {str(v).strip() for v in value if str(v).strip()}
    if isinstance(value, str):
        return {v.strip() for v in value.replace(",", " ").split() if v.strip()}
    return set()


def _get_xero_creds(config: AppConfig) -> tuple[str, str]:
    """Return (client_id, client_secret) from env vars or JSON settings store."""
    client_id = config.xero_client_id or str(
        get_json_setting(config.admin_db_file, "xero_client_id", "")
    ).strip()
    client_secret = config.xero_client_secret or str(
        get_json_setting(config.admin_db_file, "xero_client_secret", "")
    ).strip()
    return client_id, client_secret


def _xero_authorization_url(
    config: AppConfig,
    state: str,
    redirect_uri: str | None = None,
    *,
    force_consent: bool = False,
) -> str:
    client_id, _ = _get_xero_creds(config)
    uri = redirect_uri or config.xero_redirect_uri
    # Build manually so spaces in scope are encoded as %20 (not +) — Xero requires %20
    scope_str = _xero_scope_string(config.xero_scopes)
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": uri,
        "state": state,
    }
    if force_consent:
        # Ensures Xero shows the consent/org-selection screen again so users
        # can add missing organisations to the app connection.
        params["prompt"] = "consent"
    base_params = urllib.parse.urlencode(params)
    scope_param = "scope=" + urllib.parse.quote(scope_str, safe="")
    return f"{XERO_AUTH_URL}?{base_params}&{scope_param}"


def _exchange_xero_code(
    config: AppConfig, code: str, redirect_uri: str | None = None
) -> dict:
    client_id, client_secret = _get_xero_creds(config)
    uri = redirect_uri or config.xero_redirect_uri
    basic = base64.b64encode(
        f"{client_id}:{client_secret}".encode()
    ).decode()
    headers = {"Authorization": f"Basic {basic}"}
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": uri,
    }
    response = requests.post(XERO_TOKEN_URL, headers=headers, data=data, timeout=30)
    response.raise_for_status()
    token = response.json()
    token["issued_at"] = int(time.time())
    return token


def _get_xero_tenant_id(access_token: str) -> str:
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
    response = requests.get(XERO_CONNECTIONS_URL, headers=headers, timeout=30)
    response.raise_for_status()
    connections = response.json()
    if not connections:
        raise RuntimeError("No Xero organisation connections returned.")
    return connections[0].get("tenantId", "")


def _get_all_xero_connections(access_token: str) -> list[dict]:
    """Return all authorised Xero connections as [{tenantId, tenantName}]."""
    if xero_is_disabled():
        return []
    if not access_token:
        return []
    cache_key = access_token[-24:]
    cached = _xero_conn_cache.get(cache_key)
    if cached:
        ts, rows = cached
        if time.time() - ts < _XERO_CONN_CACHE_TTL:
            return rows
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
    response = requests.get(XERO_CONNECTIONS_URL, headers=headers, timeout=4)
    response.raise_for_status()
    rows = [
        {"tenantId": c.get("tenantId", ""), "tenantName": c.get("tenantName", c.get("tenantId", ""))}
        for c in response.json()
        if c.get("tenantId")
    ]
    _xero_conn_cache[cache_key] = (time.time(), rows)
    return rows


def _xero_status_data(config: AppConfig) -> tuple[bool, str, str]:
    """Returns (connected, status_text, tenant_id)"""
    token = load_xero_token(config.xero_token_file)
    client_id, client_secret = _get_xero_creds(config)
    has_credentials = bool(client_id and client_secret)
    if not has_credentials:
        return False, "Enter your Xero Client ID and Secret below, then click Save.", ""
    access = token.get("access_token", "")
    tenant = token.get("tenant_id", "")
    refresh_token = token.get("refresh_token", "")

    if not access:
        return False, "Not connected — click Connect Xero below.", ""

    if token_is_expired(token):
        if not (client_id and client_secret and refresh_token):
            return False, "Xero token expired — reconnect Xero.", ""
        try:
            refreshed = refresh_xero_token(client_id, client_secret, refresh_token)
            token = {**token, **refreshed}
            save_xero_token(config.xero_token_file, token)
            access = token.get("access_token", "")
            tenant = token.get("tenant_id", tenant)
        except Exception as exc:
            short = str(exc).splitlines()[0][:180]
            return False, f"Xero token refresh failed: {short}", ""

    try:
        connections = _get_all_xero_connections(access)
    except Exception as exc:
        short = str(exc).splitlines()[0][:180]
        return False, f"Connected token, but organisation lookup failed: {short}", tenant

    if not connections:
        return False, "Connected token, but no organisations are authorised. Reconnect Xero and choose an organisation.", ""

    valid_tenants = {c.get("tenantId", "") for c in connections}
    if not tenant or tenant not in valid_tenants:
        tenant = next(iter(valid_tenants))
        token["tenant_id"] = tenant
        save_xero_token(config.xero_token_file, token)

    count = len(connections)
    return True, f"Connected ({count} organisation{'s' if count != 1 else ''})", tenant


def _save_submitter_aliases_from_form(config: AppConfig, form) -> dict[str, str]:
    current = get_submitter_aliases(config.admin_db_file)
    next_aliases = dict(current)
    prefix = "submitter_alias__"
    for k, v in form.items():
        if not k.startswith(prefix):
            continue
        email = k[len(prefix):].strip().lower()
        name = (v or "").strip()
        if not email:
            continue
        if name:
            next_aliases[email] = name
        else:
            next_aliases.pop(email, None)
    set_submitter_aliases(config.admin_db_file, next_aliases)
    return next_aliases


def _save_cash_submitter_sheets_from_form(config: AppConfig, form) -> dict[str, dict[str, str]]:
    current = get_cash_submitter_sheets(config.admin_db_file)
    next_map = dict(current)
    prefix_id = "cash_sheet_id__"
    prefix_name = "cash_sheet_name__"

    candidate_emails: set[str] = set()
    for k in form.keys():
        if k.startswith(prefix_id):
            candidate_emails.add(k[len(prefix_id):].strip().lower())
        elif k.startswith(prefix_name):
            candidate_emails.add(k[len(prefix_name):].strip().lower())

    for email in candidate_emails:
        raw_id = (form.get(f"{prefix_id}{email}") or "").strip()
        sid = _extract_spreadsheet_id(raw_id)
        sname = (form.get(f"{prefix_name}{email}") or "Sheet1").strip() or "Sheet1"
        if sid:
            next_map[email] = {"spreadsheet_id": sid, "sheet_name": sname}
        else:
            next_map.pop(email, None)

    set_cash_submitter_sheets(config.admin_db_file, next_map)
    return next_map


def _save_sales_submitter_sheets_from_form(config: AppConfig, form) -> dict[str, dict[str, str]]:
    current = get_sales_submitter_sheets(config.admin_db_file)
    next_map = dict(current)
    prefix_id = "sales_sheet_id__"
    prefix_name = "sales_sheet_name__"

    candidate_emails: set[str] = set()
    for k in form.keys():
        if k.startswith(prefix_id):
            candidate_emails.add(k[len(prefix_id):].strip().lower())
        elif k.startswith(prefix_name):
            candidate_emails.add(k[len(prefix_name):].strip().lower())

    for email in candidate_emails:
        raw_id = (form.get(f"{prefix_id}{email}") or "").strip()
        sid = _extract_spreadsheet_id(raw_id)
        sname = (form.get(f"{prefix_name}{email}") or "Sales").strip() or "Sales"
        if sid:
            next_map[email] = {"spreadsheet_id": sid, "sheet_name": sname}
        else:
            next_map.pop(email, None)

    set_sales_submitter_sheets(config.admin_db_file, next_map)
    return next_map


def _save_calendar_sales_sheets_from_form(config: AppConfig, form) -> dict[str, dict[str, str]]:
    current = get_calendar_sales_sheets(config.admin_db_file)
    next_map = dict(current)
    prefix_id = "cal_sales_sheet_id__"
    prefix_name = "cal_sales_sheet_name__"

    candidate_cals: set[str] = set()
    for k in form.keys():
        if k.startswith(prefix_id):
            candidate_cals.add(k[len(prefix_id):].strip())
        elif k.startswith(prefix_name):
            candidate_cals.add(k[len(prefix_name):].strip())

    for cal_id in candidate_cals:
        raw_id = (form.get(f"{prefix_id}{cal_id}") or "").strip()
        sid = _extract_spreadsheet_id(raw_id)
        sname = (form.get(f"{prefix_name}{cal_id}") or "Sales").strip() or "Sales"
        if sid:
            next_map[cal_id] = {"spreadsheet_id": sid, "sheet_name": sname}
        else:
            next_map.pop(cal_id, None)

    set_calendar_sales_sheets(config.admin_db_file, next_map)
    return next_map


def _save_calendar_cash_sheets_from_form(config: AppConfig, form) -> dict[str, dict[str, str]]:
    current = get_calendar_cash_sheets(config.admin_db_file)
    next_map = dict(current)
    prefix_id = "cal_cash_sheet_id__"
    prefix_name = "cal_cash_sheet_name__"

    candidate_cals: set[str] = set()
    for k in form.keys():
        if k.startswith(prefix_id):
            candidate_cals.add(k[len(prefix_id):].strip())
        elif k.startswith(prefix_name):
            candidate_cals.add(k[len(prefix_name):].strip())

    for cal_id in candidate_cals:
        raw_id = (form.get(f"{prefix_id}{cal_id}") or "").strip()
        sid = _extract_spreadsheet_id(raw_id)
        sname = (form.get(f"{prefix_name}{cal_id}") or "Sheet1").strip() or "Sheet1"
        if sid:
            next_map[cal_id] = {"spreadsheet_id": sid, "sheet_name": sname}
        else:
            next_map.pop(cal_id, None)

    set_calendar_cash_sheets(config.admin_db_file, next_map)
    return next_map


def _apply_alias_to_description(
    description: str,
    aliases: dict[str, str],
    fallback_email: str = "",
) -> tuple[str, bool]:
    if not description:
        return description, False

    m = re.search(
        r"(\[app-status\])(.*?)(\[/app-status\])",
        description,
        flags=re.I | re.S,
    )
    if not m:
        return description, False

    start, block, end = m.group(1), m.group(2), m.group(3)
    lines = block.splitlines()
    idx = None
    for i, line in enumerate(lines):
        plain = re.sub(r"<[^>]+>", "", line).strip().lower()
        if plain.startswith("submitted by:"):
            idx = i
            break

    mapped_name = ""
    if idx is not None:
        plain_line = re.sub(r"<[^>]+>", "", lines[idx]).strip()
        found = re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", plain_line)
        email = found.group(0).lower() if found else ""
        mapped_name = aliases.get(email, "")
        if not mapped_name:
            return description, False
        new_line = f"Submitted by: {mapped_name}"
        if lines[idx].strip() == new_line:
            return description, False
        lines[idx] = new_line
    else:
        mapped_name = aliases.get((fallback_email or "").strip().lower(), "")
        if not mapped_name:
            return description, False
        insert_at = len(lines)
        for i, line in enumerate(lines):
            plain = re.sub(r"<[^>]+>", "", line).strip().lower()
            if plain.startswith("payment type"):
                insert_at = i
                break
        lines.insert(insert_at, f"Submitted by: {mapped_name}")

    new_block = "\n".join(lines)
    new_description = description[: m.start()] + start + new_block + end + description[m.end():]
    return new_description, new_description != description


def _backfill_submitter_aliases(config: AppConfig, aliases: dict[str, str]) -> tuple[int, int]:
    creds = load_admin_credentials(config)
    if not creds or not aliases:
        return 0, 0

    service = build_calendar_service_from_creds(creds)
    now = dt.datetime.now(dt.timezone.utc)
    time_min = (now - dt.timedelta(days=365)).isoformat()
    time_max = (now + dt.timedelta(days=365)).isoformat()
    active_calendars = get_active_calendars(config.admin_db_file, config.google_calendar_id)

    updated_count = 0
    error_count = 0
    for calendar_id in active_calendars:
        page_token = None
        while True:
            resp = (
                service.events()
                .list(
                    calendarId=calendar_id,
                    timeMin=time_min,
                    timeMax=time_max,
                    singleEvents=True,
                    maxResults=250,
                    pageToken=page_token,
                    fields="items(id,description,creator/email,organizer/email),nextPageToken",
                )
                .execute()
            )
            for event in resp.get("items", []):
                description = event.get("description") or ""
                fallback_email = (
                    (event.get("creator", {}) or {}).get("email")
                    or (event.get("organizer", {}) or {}).get("email")
                    or ""
                )
                new_description, changed = _apply_alias_to_description(
                    description, aliases, fallback_email=fallback_email
                )
                if not changed:
                    continue
                try:
                    (
                        service.events()
                        .patch(
                            calendarId=calendar_id,
                            eventId=event["id"],
                            body={"description": new_description},
                        )
                        .execute()
                    )
                    updated_count += 1
                except Exception:
                    error_count += 1
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
    return updated_count, error_count


def _status_badge(ok: bool, text: str) -> str:
    if ok:
        return (
            f'<span class="inline-flex items-center gap-1.5 px-3 py-1 rounded-full text-sm font-medium bg-green-100 text-green-800">'
            f'<svg class="w-3.5 h-3.5" fill="currentColor" viewBox="0 0 20 20"><path fill-rule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zm3.707-9.293a1 1 0 00-1.414-1.414L9 10.586 7.707 9.293a1 1 0 00-1.414 1.414l2 2a1 1 0 001.414 0l4-4z" clip-rule="evenodd"/></svg>'
            f'{escape(text)}</span>'
        )
    return (
        f'<span class="inline-flex items-center gap-1.5 px-3 py-1 rounded-full text-sm font-medium bg-red-100 text-red-800">'
        f'<svg class="w-3.5 h-3.5" fill="currentColor" viewBox="0 0 20 20"><path fill-rule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zm-1-5a1 1 0 012 0v1a1 1 0 01-2 0v-1zm0-8a1 1 0 012 0v4a1 1 0 01-2 0V5z" clip-rule="evenodd"/></svg>'
        f'{escape(text)}</span>'
    )


def _xero_tenant_cards(
    xero_ok: bool,
    tenant_accounts: list[dict],
    warning_message: str = "",
    global_sync_on: bool = True,
) -> str:
    """Render per-tenant cards with enable toggle and separate account mapping.

    tenant_accounts: list of {tenantId, tenantName, enabled, invoiceAccount,
                               paymentAccount, revenueAccounts, bankAccounts}
    global_sync_on: the master kill-switch from the Live Feed toggle. When False,
                    no invoices are created regardless of per-tenant settings.
    """
    if not xero_ok and not tenant_accounts:
        return (
            '<div class="bg-white rounded-2xl shadow-sm border border-gray-200 p-6 opacity-50 mt-4">'
            '<h2 class="font-semibold text-gray-900 mb-1">Xero Organisations</h2>'
            '<p class="text-sm text-gray-400">Connect Xero first to configure organisations and account mapping.</p>'
            '</div>'
        )

    if not tenant_accounts:
        warning_html = (
            f'<p class="text-xs text-amber-700 bg-amber-50 border border-amber-200 rounded-lg px-3 py-2 mb-3">{escape(warning_message)}</p>'
            if warning_message
            else ""
        )
        return (
            '<div class="bg-white rounded-2xl shadow-sm border border-gray-200 p-6 mt-4">'
            '<h2 class="font-semibold text-gray-900 mb-1">Xero Organisations</h2>'
            f'{warning_html}'
            '<p class="text-sm text-gray-400">No organisations found — reconnect Xero to discover them.</p>'
            '</div>'
        )

    def _account_value(account: dict, *, allow_id_fallback: bool = False) -> str:
        code = str(account.get("Code") or "").strip()
        if code:
            return code
        if allow_id_fallback:
            aid = str(account.get("AccountID") or "").strip()
            if aid:
                return f"id:{aid}"
        return ""

    def _find_name(accounts, saved_value, *, allow_id_fallback: bool = False):
        for a in accounts:
            if _account_value(a, allow_id_fallback=allow_id_fallback) == str(saved_value or "").strip():
                return a.get("Name", saved_value)
        return None

    def _opts(accounts, saved, *, allow_id_fallback: bool = False):
        out = '<option value="">— select account —</option>'
        found_saved = False
        for a in sorted(accounts, key=lambda x: x.get("Name", "")):
            raw_value = _account_value(a, allow_id_fallback=allow_id_fallback)
            if not raw_value:
                continue
            code = escape(raw_value)
            name = escape(a.get("Name", ""))
            code_display = escape(str(a.get("Code") or "").strip() or "no-code")
            sel = ' selected' if raw_value == saved else ""
            if sel:
                found_saved = True
            out += f'<option value="{code}"{sel}>{name} ({code_display})</option>'
        if saved and not found_saved:
            out += f'<option value="{escape(saved)}" selected>Saved account ({escape(saved)})</option>'
        return out

    def _theme_opts(themes, saved, blank_label="— use Xero default —"):
        out = f'<option value="">{blank_label}</option>'
        for th in themes:
            tid_val = escape(th.get("BrandingThemeID", ""))
            tname_val = escape(th.get("Name", tid_val))
            sel = ' selected' if th.get("BrandingThemeID", "") == saved else ""
            out += f'<option value="{tid_val}"{sel}>{tname_val}</option>'
        return out

    def _saved_badge(accounts, saved_value, *, allow_id_fallback: bool = False):
        if not saved_value:
            return '<p class="text-xs text-gray-400 mt-1">No account saved yet.</p>'
        name = _find_name(accounts, saved_value, allow_id_fallback=allow_id_fallback)
        label = escape(f"{name} ({saved_value})") if name else escape(saved_value)
        return f'<p class="text-xs text-emerald-600 mt-1">&#10003; Saved: <span class="font-medium">{label}</span></p>'

    def _theme_saved_badge(themes, theme_id):
        if not theme_id:
            return '<p class="text-xs text-gray-400 mt-1">Using Xero default template.</p>'
        name = next((escape(th.get("Name", theme_id)) for th in themes if th.get("BrandingThemeID") == theme_id), escape(theme_id))
        return f'<p class="text-xs text-emerald-600 mt-1">&#10003; Saved: <span class="font-medium">{name}</span></p>'

    cards_html = ""
    for t in tenant_accounts:
        tid = escape(t["tenantId"])
        tname = escape(t.get("tenantName") or t["tenantId"])
        enabled = t.get("enabled", True)
        rev_accounts = t.get("revenueAccounts", [])
        bank_accounts = t.get("bankAccounts", [])
        themes = t.get("brandingThemes", [])
        saved_inv = t.get("invoiceAccount", "")
        saved_pay = t.get("paymentAccount", "")
        saved_theme = t.get("brandingThemeId", "")
        saved_premium_theme = t.get("premiumThemeId", "")
        saved_threshold = t.get("premiumThreshold")
        load_warning = str(t.get("loadWarning", "") or "").strip()
        threshold_val = f'{saved_threshold:g}' if saved_threshold is not None else ""
        missing_bits: list[str] = []
        if enabled and not saved_pay:
            missing_bits.append("Payment bank account")
        if enabled and not saved_theme:
            missing_bits.append("Invoice template")
        setup_notice = (
            '<div class="mb-3 text-xs text-amber-700 bg-amber-50 border border-amber-200 rounded-lg px-3 py-2">'
            f"Action needed: set {escape(', '.join(missing_bits))} before full automation can run."
            "</div>"
            if missing_bits
            else ""
        )
        load_warning_html = (
            '<div class="mb-3 text-xs text-amber-700 bg-amber-50 border border-amber-200 rounded-lg px-3 py-2">'
            f"{escape(load_warning)}</div>"
            if load_warning
            else ""
        )

        toggle_color = "bg-emerald-500" if enabled else "bg-gray-300"
        toggle_label = "Cal→Xero sync: On" if enabled else "Cal→Xero sync: Off"
        border_cls = "border-emerald-200" if enabled else "border-gray-200"

        rev_opts = _opts(rev_accounts, saved_inv)
        bank_opts = _opts(bank_accounts, saved_pay, allow_id_fallback=True)
        rev_badge = _saved_badge(rev_accounts, saved_inv)
        bank_badge = _saved_badge(bank_accounts, saved_pay, allow_id_fallback=True)

        _input_cls = "w-full text-sm border border-gray-200 rounded-lg px-3 py-2 focus:outline-none focus:ring-2 focus:ring-indigo-300"
        _manual_note = '<p class="text-xs text-amber-600 mt-1">Account list unavailable — enter the Xero account code directly (e.g. 200).</p>'
        if rev_accounts:
            rev_field = f'<select name="invoice_account_code" class="{_input_cls}">{rev_opts}</select>'
        else:
            rev_field = (
                f'<input type="text" name="invoice_account_code" value="{escape(saved_inv)}" '
                f'placeholder="Enter account code (e.g. 200)" class="{_input_cls}">'
                f'{_manual_note}'
            )
        if bank_accounts:
            bank_field = f'<select name="payment_account_code" class="{_input_cls}">{bank_opts}</select>'
        else:
            bank_field = (
                f'<input type="text" name="payment_account_code" value="{escape(saved_pay)}" '
                f'placeholder="Enter account code (e.g. 090)" class="{_input_cls}">'
                f'{_manual_note}'
            )
        theme_opts = _theme_opts(themes, saved_theme)
        theme_badge = _theme_saved_badge(themes, saved_theme)

        premium_theme_opts = _theme_opts(themes, saved_premium_theme, blank_label="— same as default —")
        premium_theme_badge = _theme_saved_badge(themes, saved_premium_theme)

        no_themes_msg = (
            '' if themes or load_warning else
            '<p class="text-xs text-amber-700 bg-amber-50 border border-amber-100 rounded-lg px-3 py-2 mt-1">'
            'No branding themes found — create one in Xero first (Settings → Invoice Settings → New Style).'
            '</p>'
        )
        advanced_open = ' open' if (saved_premium_theme or saved_threshold) else ''

        cards_html += f"""
        <details class="bg-white rounded-2xl shadow-sm border {border_cls} overflow-hidden group/org">
          <summary class="flex items-center justify-between px-5 py-4 cursor-pointer list-none select-none hover:bg-gray-50 transition-colors">
            <div class="flex items-center gap-3">
              <div class="w-2.5 h-2.5 rounded-full {"bg-emerald-400" if enabled else "bg-gray-300"} shrink-0 mt-0.5"></div>
              <div>
                <span class="font-semibold text-gray-900 text-sm">{tname}</span>
                <p class="text-xs {"text-emerald-600" if enabled else "text-gray-400"} mt-0.5">{"Active" if enabled else "Paused"}</p>
              </div>
            </div>
            <div class="flex items-center gap-2">
              <form method="post" action="/toggle-xero-tenant/{tid}" onclick="event.stopPropagation()">
                <button type="submit" title="Toggle {tname}"
                  class="flex items-center gap-1.5 px-2.5 py-1 text-xs font-medium rounded-lg border transition-colors
                         {'border-emerald-300 bg-emerald-50 text-emerald-700 hover:bg-emerald-100' if enabled else 'border-gray-300 bg-gray-50 text-gray-600 hover:bg-gray-100'}">
                  <span class="inline-flex w-7 h-4 rounded-full {toggle_color} relative transition-colors">
                    <span class="absolute top-0.5 left-0.5 w-3 h-3 bg-white rounded-full shadow transform transition-transform {"translate-x-3" if enabled else "translate-x-0"}"></span>
                  </span>
                  {toggle_label}
                </button>
              </form>
              <svg class="w-4 h-4 text-gray-400 transition-transform duration-200 group-open/org:rotate-180 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"/>
              </svg>
            </div>
          </summary>
          <div class="px-5 pb-5 pt-4 border-t border-gray-100">
            <p class="text-xs text-gray-400 font-mono mb-4">{tid}</p>
            {setup_notice}
            {load_warning_html}
            <form method="post" action="/save-xero-tenant/{tid}" class="space-y-4">

              <!-- Account mapping -->
              <div>
                <label class="block text-xs font-medium text-gray-700 mb-1">Invoice income account <span class="text-gray-400 font-normal">(revenue / sales)</span></label>
                {rev_field}
                {rev_badge}
              </div>
              <div>
                <label class="block text-xs font-medium text-gray-700 mb-1">Payment bank account <span class="text-red-500">*</span> <span class="text-gray-400 font-normal">(where payments land)</span></label>
                {bank_field}
                {bank_badge}
              </div>

              <!-- Invoice template -->
              <div class="pt-1 border-t border-gray-100">
                <label class="block text-xs font-medium text-gray-700 mb-1">Invoice template <span class="text-red-500">*</span> <span class="text-gray-400 font-normal">(branding theme)</span></label>
                <select name="branding_theme_id"
                  class="w-full text-sm border border-gray-200 rounded-lg px-3 py-2 focus:outline-none focus:ring-2 focus:ring-indigo-300 {"opacity-40" if not themes else ""}">
                  {theme_opts}
                </select>
                {theme_badge}
                {no_themes_msg}
              </div>

              <!-- Advanced -->
              <details class="border border-gray-100 rounded-xl overflow-hidden group/adv"{advanced_open}>
                <summary class="flex items-center justify-between px-4 py-3 bg-gray-50 cursor-pointer list-none select-none hover:bg-gray-100 transition-colors">
                  <div class="flex items-center gap-2">
                    <svg class="w-3.5 h-3.5 text-gray-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 6V4m0 2a2 2 0 100 4m0-4a2 2 0 110 4m-6 8a2 2 0 100-4m0 4a2 2 0 110-4m0 4v2m0-6V4m6 6v10m6-2a2 2 0 100-4m0 4a2 2 0 110-4m0 4v2m0-6V4"/>
                    </svg>
                    <span class="text-xs font-semibold text-gray-700">Advanced — premium template rules</span>
                  </div>
                  <svg class="w-3.5 h-3.5 text-gray-400 transition-transform duration-200 group-open/adv:rotate-180 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"/>
                  </svg>
                </summary>
                <div class="px-4 py-3 space-y-3">
                  <p class="text-xs text-gray-500">Use a different invoice template when the job total is above a set amount — handy for VIP or large-spend clients.</p>
                  <div>
                    <label class="block text-xs font-medium text-gray-700 mb-1">Premium template <span class="text-gray-400 font-normal">(used when spend is above threshold)</span></label>
                    <select name="premium_theme_id"
                      class="w-full text-sm border border-gray-200 rounded-lg px-3 py-2 focus:outline-none focus:ring-2 focus:ring-indigo-300 {"opacity-40" if not themes else ""}">
                      {premium_theme_opts}
                    </select>
                    {premium_theme_badge}
                  </div>
                  <div>
                    <label class="block text-xs font-medium text-gray-700 mb-1">Spend threshold <span class="text-gray-400 font-normal">(£ ex-VAT — premium template used at or above this amount)</span></label>
                    <div class="relative">
                      <span class="absolute left-3 top-1/2 -translate-y-1/2 text-sm text-gray-400 pointer-events-none">£</span>
                      <input type="number" name="premium_threshold" value="{threshold_val}" min="0" step="0.01"
                        placeholder="e.g. 500"
                        class="w-full pl-7 pr-3 py-2 text-sm border border-gray-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-indigo-300">
                    </div>
                    {f'<p class="text-xs text-emerald-600 mt-1">&#10003; Premium template used when job is £{threshold_val}+ ex-VAT</p>' if threshold_val else '<p class="text-xs text-gray-400 mt-1">No threshold set — premium template unused.</p>'}
                  </div>
                </div>
              </details>

              <button type="submit"
                class="px-4 py-2 bg-indigo-600 hover:bg-indigo-700 text-white text-sm font-medium rounded-lg transition-colors">
                Save
              </button>
            </form>
          </div>
        </details>"""

    warning_html = (
        f'<div class="text-xs text-amber-700 bg-amber-50 border border-amber-200 rounded-lg px-3 py-2">{escape(warning_message)}</div>'
        if warning_message
        else ""
    )

    global_off_banner = (
        '<div class="flex items-start gap-2.5 bg-amber-50 border border-amber-200 rounded-xl px-4 py-3 text-sm text-amber-800">'
        '<svg class="w-4 h-4 mt-0.5 shrink-0 text-amber-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">'
        '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z"/>'
        '</svg>'
        '<span><strong>Cal→Xero sync is globally paused.</strong> The master switch on the '
        '<a href="/" class="underline hover:text-amber-900 font-medium">Live Feed</a> page is off — '
        'no invoices will be created until you turn it back on, regardless of the per-organisation settings below.</span>'
        '</div>'
        if not global_sync_on else ""
    )
    return f"""
    <div class="space-y-4 mt-4">
      <div class="flex items-center justify-between">
        <h2 class="font-semibold text-gray-900">Xero Organisations</h2>
        <p class="text-xs text-gray-400">{len(tenant_accounts)} organisation{"s" if len(tenant_accounts) != 1 else ""} connected</p>
      </div>
      {global_off_banner}
      {warning_html}
      {cards_html}
    </div>"""


def _btn_primary(label: str, href: str = "", form_action: str = "", extra_class: str = "") -> str:
    if href:
        return (
            f'<a href="{href}" class="inline-flex items-center gap-2 px-4 py-2 bg-indigo-600 hover:bg-indigo-700 '
            f'text-white text-sm font-medium rounded-lg transition-colors {extra_class}">{label}</a>'
        )
    return (
        f'<button type="submit" formaction="{form_action}" '
        f'class="inline-flex items-center gap-2 px-4 py-2 bg-indigo-600 hover:bg-indigo-700 '
        f'text-white text-sm font-medium rounded-lg transition-colors {extra_class}">{label}</button>'
    )


def _btn_secondary(label: str, href: str = "", form_action: str = "") -> str:
    if href:
        return (
            f'<a href="{href}" class="inline-flex items-center gap-2 px-4 py-2 bg-white hover:bg-gray-50 '
            f'text-gray-700 text-sm font-medium rounded-lg border border-gray-300 transition-colors">{label}</a>'
        )
    return (
        f'<button type="submit" formaction="{form_action}" '
        f'class="inline-flex items-center gap-2 px-4 py-2 bg-white hover:bg-gray-50 '
        f'text-gray-700 text-sm font-medium rounded-lg border border-gray-300 transition-colors">{label}</button>'
    )


def _bootstrap_credentials(config) -> None:
    """Write credentials from env vars to disk on first boot (Fly.io / Docker)."""
    import os
    creds_b64 = os.getenv("GOOGLE_CREDENTIALS_JSON", "")
    if creds_b64:
        creds_path = Path(config.google_credentials_file)
        if not creds_path.exists():
            creds_path.parent.mkdir(parents=True, exist_ok=True)
            creds_path.write_bytes(base64.b64decode(creds_b64))


def _save_admin_auth_file(config: AppConfig, username: str, password: str) -> None:
    path = Path(config.admin_auth_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "username": username.strip(),
        "password": password,
        "updated_at": int(time.time()),
    }
    path.write_text(json.dumps(payload, indent=2))


def _load_admin_auth(config: AppConfig) -> tuple[str, str]:
    """
    Read admin auth from persistent file first, env fallback second.
    """
    path = Path(config.admin_auth_file)
    if path.exists():
        try:
            raw = json.loads(path.read_text())
            username = str(raw.get("username", "")).strip()
            password = str(raw.get("password", ""))
            if username and password:
                return username, password
        except Exception:
            pass
    return config.admin_username, config.admin_password


def _bootstrap_admin_auth(config: AppConfig) -> None:
    """
    Ensure a persistent admin auth file exists.
    """
    username, password = _load_admin_auth(config)
    if not username or not password:
        return
    if not Path(config.admin_auth_file).exists():
        _save_admin_auth_file(config, username, password)


def create_app() -> Flask:
    config = load_config()
    _bootstrap_credentials(config)
    _bootstrap_admin_auth(config)
    init_admin_store(config.admin_db_file)

    app = Flask(__name__)
    app.secret_key = config.web_secret_key
    app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024  # 500 MB — receipt dump bulk uploads

    @app.errorhandler(413)
    def upload_too_large(e):
        body = (
            "<div class='max-w-xl mx-auto px-4 py-16 text-center'>"
            "<div class='text-5xl mb-4'>📦</div>"
            "<h1 class='text-2xl font-bold text-gray-900 mb-2'>Upload too large</h1>"
            "<p class='text-gray-600 mb-6'>The combined size of the files you selected "
            "exceeds the 500 MB limit. Try splitting the upload into two smaller batches.</p>"
            "<a href='/receipts/expenses/dump' class='inline-block px-5 py-2 bg-indigo-600 "
            "text-white rounded-lg text-sm hover:bg-indigo-700'>← Back to Receipt Dump</a>"
            "</div>"
        )
        from flask import Response
        return Response(_page(body), status=413)

    def require_login(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if not session.get("logged_in"):
                return redirect(url_for("login"))
            return fn(*args, **kwargs)
        return wrapper

    @app.get("/privacy")
    def privacy_policy():
        return _page("""
        <div class="min-h-screen bg-gray-50 px-4 py-12">
          <div class="max-w-2xl mx-auto bg-white rounded-2xl shadow p-8">
            <h1 class="text-2xl font-bold text-gray-900 mb-4">Privacy Policy</h1>
            <div class="space-y-4 text-sm text-gray-700 leading-relaxed">
              <p>This application is an internal business administration tool used solely by
                 our own staff to manage our own company records (invoicing, expenses and
                 bank-account reconciliation).</p>
              <p>Bank account data accessed via open banking is used only to reconcile our
                 own company card transactions against submitted expense receipts. It is not
                 shared with, or sold to, any third party.</p>
              <p>Data is stored securely, access is restricted to authorised staff, and bank
                 connection credentials are encrypted at rest. Bank consent can be withdrawn
                 at any time from within the application or via the bank.</p>
              <p>For any privacy questions, please contact the business directly.</p>
            </div>
          </div>
        </div>
        """)

    @app.get("/terms")
    def terms_of_service():
        return _page("""
        <div class="min-h-screen bg-gray-50 px-4 py-12">
          <div class="max-w-2xl mx-auto bg-white rounded-2xl shadow p-8">
            <h1 class="text-2xl font-bold text-gray-900 mb-4">Terms of Use</h1>
            <div class="space-y-4 text-sm text-gray-700 leading-relaxed">
              <p>This application is a private, internal business tool. It is not offered as
                 a service to the public and no third-party accounts are connected through it.</p>
              <p>Use is restricted to authorised staff of the business. Open banking access is
                 used exclusively to view the business's own account transactions for expense
                 reconciliation purposes.</p>
              <p>The application is provided as-is for internal use only.</p>
            </div>
          </div>
        </div>
        """)

    @app.get("/login")
    def login():
        return _page(f"""
        <div class="min-h-screen flex items-center justify-center bg-gradient-to-br from-indigo-50 to-blue-100 px-4">
          <div class="w-full max-w-md">
            <div class="bg-white rounded-2xl shadow-xl p-8">
              <div class="text-center mb-8">
                <div class="inline-flex items-center justify-center w-16 h-16 bg-indigo-100 rounded-2xl mb-4">
                  <svg class="w-8 h-8 text-indigo-600" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2"
                      d="M9 12l2 2 4-4m5.618-4.016A11.955 11.955 0 0112 2.944a11.955 11.955 0 01-8.618 3.04A12.02 12.02 0 003 9c0 5.591 3.824 10.29 9 11.622 5.176-1.332 9-6.03 9-11.622 0-1.042-.133-2.052-.382-3.016z"/>
                  </svg>
                </div>
                <h1 class="text-2xl font-bold text-gray-900">Powwash Admin</h1>
                <p class="text-gray-500 text-sm mt-1">Sign in to manage your integration settings</p>
              </div>
              <form method="post" class="space-y-5">
                <div>
                  <label class="block text-sm font-medium text-gray-700 mb-1.5">Username</label>
                  <input name="username" autocomplete="username"
                    class="w-full px-4 py-2.5 border border-gray-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500 focus:border-transparent"
                    placeholder="Enter your username">
                </div>
                <div>
                  <label class="block text-sm font-medium text-gray-700 mb-1.5">Password</label>
                  <input name="password" type="password" autocomplete="current-password"
                    class="w-full px-4 py-2.5 border border-gray-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500 focus:border-transparent"
                    placeholder="Enter your password">
                </div>
                <button type="submit"
                  class="w-full py-2.5 px-4 bg-indigo-600 hover:bg-indigo-700 text-white font-medium rounded-lg text-sm transition-colors">
                  Sign in
                </button>
              </form>
              <p class="text-center mt-4 text-xs text-gray-500">
                Can't sign in? <a href="/admin/reset-login" class="text-indigo-600 hover:text-indigo-700">Reset admin login</a>
              </p>
            </div>
          </div>
        </div>
        """)

    @app.post("/login")
    def login_post():
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        expected_user, expected_pass = _load_admin_auth(config)
        if username == expected_user and password == expected_pass:
            session["logged_in"] = True
            return redirect(url_for("dashboard"))
        return _page(f"""
        <div class="min-h-screen flex items-center justify-center bg-gradient-to-br from-indigo-50 to-blue-100 px-4">
          <div class="w-full max-w-md">
            <div class="bg-white rounded-2xl shadow-xl p-8">
              <div class="text-center mb-6">
                <div class="inline-flex items-center justify-center w-16 h-16 bg-red-100 rounded-2xl mb-4">
                  <svg class="w-8 h-8 text-red-600" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"/>
                  </svg>
                </div>
                <h1 class="text-2xl font-bold text-gray-900">Invalid credentials</h1>
                <p class="text-gray-500 text-sm mt-1">Please check your username and password.</p>
              </div>
              <a href="/login" class="block w-full text-center py-2.5 px-4 bg-indigo-600 hover:bg-indigo-700 text-white font-medium rounded-lg text-sm transition-colors">
                Try again
              </a>
            </div>
          </div>
        </div>
        """), 401

    @app.get("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.get("/assistant")
    @require_login
    def assistant_page():
        cfg = _assistant_config(config.admin_db_file)
        _oa = get_openai_settings(config.admin_db_file)
        openai_configured = bool(_oa.get("api_key") or (os.getenv("OPENAI_API_KEY") or "").strip())
        openai_model = _oa.get("model") or (os.getenv("OPENAI_MODEL") or "gpt-4o-mini").strip()
        history = session.get("assistant_chat_history") or []
        if not isinstance(history, list):
            history = []
        pending = session.get("assistant_pending_action") or {}
        pending_html = ""
        if isinstance(pending, dict) and pending.get("id") and pending.get("summary"):
            pending_html = f"""
              <div class="rounded-xl border border-amber-300 bg-amber-50 p-4">
                <p class="text-sm font-semibold text-amber-800">Pending write action</p>
                <p class="text-sm text-amber-900 mt-1">{escape(str(pending.get("summary") or ""))}</p>
                <form method="post" action="/assistant/confirm" class="mt-3 flex flex-wrap gap-2">
                  <input type="hidden" name="action_id" value="{escape(str(pending.get("id")))}">
                  <button name="decision" value="approve" class="px-3 py-1.5 text-sm rounded-lg bg-emerald-600 text-white hover:bg-emerald-700">Approve & Run</button>
                  <button name="decision" value="reject" class="px-3 py-1.5 text-sm rounded-lg bg-gray-200 text-gray-800 hover:bg-gray-300">Reject</button>
                </form>
              </div>
            """

        lines: list[str] = []
        for row in history[-20:]:
            role = "You" if row.get("role") == "user" else "Assistant"
            role_cls = "text-indigo-700" if role == "You" else "text-emerald-700"
            lines.append(
                f'<div class="py-2 border-b border-gray-100">'
                f'<p class="text-xs {role_cls} font-semibold">{role} · {escape(str(row.get("ts") or ""))}</p>'
                f'<p class="text-sm text-gray-800 whitespace-pre-wrap">{escape(str(row.get("text") or ""))}</p>'
                f"</div>"
            )
        chat_html = "".join(lines) or '<p class="text-sm text-gray-500">No messages yet.</p>'

        return _page(
            f"""
            <div class="max-w-5xl mx-auto py-6 px-4 sm:px-6">
              <div class="mb-4">
                <h1 class="text-2xl font-bold text-gray-900">Assistant</h1>
              </div>

              <div class="rounded-xl border border-gray-200 bg-white p-4 mb-4">
                <p class="text-sm text-gray-700 mb-3">Write mode controls whether the assistant can execute changes after your approval.</p>
                <form method="post" action="/assistant/config" class="flex flex-wrap items-center gap-4">
                  <label class="inline-flex items-center gap-2 text-sm text-gray-800">
                    <input type="checkbox" name="write_enabled" value="1" {"checked" if cfg.get("write_enabled") else ""}>
                    Enable write actions
                  </label>
                  <span class="text-xs text-gray-500">Always confirm is enforced.</span>
                  <button class="px-3 py-1.5 text-sm rounded-lg bg-indigo-600 text-white hover:bg-indigo-700">Save</button>
                </form>
                <div class="mt-3 text-xs text-gray-600">
                  <p>OpenAI: <span class="font-semibold {"text-emerald-700" if openai_configured else "text-amber-700"}">{"Connected" if openai_configured else "Not configured"}</span> · model <code>{escape(openai_model)}</code></p>
                  <p class="mt-1">API keys: <a class="text-indigo-600 hover:underline" href="https://platform.openai.com/api-keys" target="_blank" rel="noopener">platform.openai.com/api-keys</a></p>
                </div>
              </div>

              {pending_html}

              <div class="grid grid-cols-1 lg:grid-cols-2 gap-4 mt-4">
                <div class="rounded-xl border border-gray-200 bg-white p-4">
                  <h2 class="text-sm font-semibold text-gray-900 mb-2">Ask Assistant</h2>
                  <form method="post" action="/assistant/ask">
                    <textarea name="prompt" rows="7" class="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm" placeholder="Ask a calendar/Xero question..."></textarea>
                    <button class="mt-3 px-3 py-1.5 text-sm rounded-lg bg-gray-900 text-white hover:bg-black">Send</button>
                  </form>
                  <p class="text-xs text-gray-500 mt-3">Examples: "what new entries were created today", "who booked W5 D.S John O'Discoll", "delete orphan drafts".</p>
                </div>
                <div class="rounded-xl border border-gray-200 bg-white p-4 max-h-[480px] overflow-y-auto">
                  <h2 class="text-sm font-semibold text-gray-900 mb-2">Conversation</h2>
                  {chat_html}
                </div>
              </div>
            </div>
            """
        )

    @app.post("/assistant/config")
    @require_login
    def assistant_config_save():
        write_enabled = (request.form.get("write_enabled") or "").strip() in {"1", "true", "on", "yes", "y"}
        _set_assistant_config(config.admin_db_file, write_enabled=write_enabled)
        _append_assistant_chat("assistant", f"Write mode {'enabled' if write_enabled else 'disabled'}.")
        return redirect(url_for("assistant_page"))

    @app.post("/assistant/ask")
    @require_login
    def assistant_ask():
        prompt = (request.form.get("prompt") or "").strip()
        if not prompt:
            return redirect(url_for("assistant_page"))
        _append_assistant_chat("user", prompt)
        cfg = _assistant_config(config.admin_db_file)
        low = prompt.lower()

        # Read-only: list today's new entries.
        if (
            ("new" in low and "entr" in low and "today" in low)
            or ("created today" in low and "calendar" in low)
        ):
            titles = _list_today_created_event_titles(config)
            if not titles:
                _append_assistant_chat("assistant", "No new entries found today.")
            else:
                _append_assistant_chat("assistant", "New entries today:\n- " + "\n- ".join(titles))
            return redirect(url_for("assistant_page"))

        # Read-only: who booked <title>.
        if "who booked" in low:
            phrase = re.sub(r"^.*who booked\s*", "", prompt, flags=re.I).strip(" ?\"'")
            hits = _find_events_by_title(config, phrase)
            if not hits:
                _append_assistant_chat("assistant", f"No matching event found for: {phrase}")
                return redirect(url_for("assistant_page"))
            ev = hits[0]["event"]
            created = ev.get("created") or ""
            creator = ((ev.get("creator") or {}).get("email") or "").strip() or "unknown"
            summary = str(ev.get("summary") or "(no title)")
            _append_assistant_chat(
                "assistant",
                f'Booked by: {creator}\nEvent: {summary}\nCreated: {created}',
            )
            return redirect(url_for("assistant_page"))

        # Write intent: remove orphan drafts.
        if ("delete" in low or "remove" in low) and "draft" in low and "xero" in low:
            if not cfg.get("write_enabled"):
                _append_assistant_chat("assistant", "Write mode is OFF. Enable write actions first, then ask again.")
                return redirect(url_for("assistant_page"))
            state = load_state(config.state_file)
            mapped = {str(v).strip() for v in (state.get("event_invoice_map") or {}).values() if str(v).strip()}
            drafts = _xero_fetch_draft_invoices(config)
            orphan = [d for d in drafts if str(d.get("InvoiceID") or "").strip() and str(d.get("InvoiceID") or "").strip() not in mapped]
            preview = [str(d.get("InvoiceNumber") or d.get("InvoiceID") or "") for d in orphan[:20]]
            action_id = str(uuid.uuid4())
            session["assistant_pending_action"] = {
                "id": action_id,
                "type": "delete_orphan_drafts",
                "invoice_ids": [str(d.get("InvoiceID")) for d in orphan],
                "summary": f"Delete {len(orphan)} orphan draft invoice(s): {', '.join(preview) if preview else 'none'}",
            }
            _append_assistant_chat(
                "assistant",
                f"Ready to run: delete {len(orphan)} orphan draft invoice(s). Please approve below.",
            )
            return redirect(url_for("assistant_page"))

        # Write intent: void app-tracked invoices by contact name fragment.
        if "void" in low and ("invoice" in low or "payment" in low):
            if not cfg.get("write_enabled"):
                _append_assistant_chat("assistant", "Write mode is OFF. Enable write actions first, then ask again.")
                return redirect(url_for("assistant_page"))
            m = re.search(r"under\s+(.+)$", prompt, flags=re.I)
            if not m:
                _append_assistant_chat("assistant", "Please include a name fragment, e.g. 'void invoices under Test Name'.")
                return redirect(url_for("assistant_page"))
            needle = m.group(1).strip(" .\"'")
            xc = build_xero_client(config)
            if not xc:
                _append_assistant_chat("assistant", "Xero client is not connected.")
                return redirect(url_for("assistant_page"))
            state = load_state(config.state_file)
            invoice_ids = sorted({str(v).strip() for v in (state.get("event_invoice_map") or {}).values() if str(v).strip()})
            matches: list[dict] = []
            for inv_id in invoice_ids[:500]:
                try:
                    inv = xc.get_invoice(inv_id)
                except Exception:
                    continue
                cname = str(((inv.get("Contact") or {}).get("Name") or "")).strip()
                if needle.lower() in cname.lower():
                    status = str(inv.get("Status") or "").upper()
                    if status not in {"VOIDED", "DELETED"}:
                        matches.append(inv)
            action_id = str(uuid.uuid4())
            preview = [str(inv.get("InvoiceNumber") or inv.get("InvoiceID") or "") for inv in matches[:20]]
            session["assistant_pending_action"] = {
                "id": action_id,
                "type": "void_invoices_by_name",
                "invoice_ids": [str(inv.get("InvoiceID")) for inv in matches],
                "summary": f'Void {len(matches)} invoice(s) for name containing "{needle}": {", ".join(preview) if preview else "none"}',
            }
            _append_assistant_chat(
                "assistant",
                f'Ready to run: void {len(matches)} invoice(s) for "{needle}". Please approve below.',
            )
            return redirect(url_for("assistant_page"))

        ai_reply, ai_err = _openai_assistant_reply(prompt)
        if ai_reply:
            _append_assistant_chat("assistant", ai_reply)
            return redirect(url_for("assistant_page"))
        if ai_err == "missing_key":
            _append_assistant_chat(
                "assistant",
                "OpenAI API key is not configured. Add `OPENAI_API_KEY` to Fly secrets. "
                "Key page: https://platform.openai.com/api-keys",
            )
            return redirect(url_for("assistant_page"))

        _append_assistant_chat(
            "assistant",
            "I couldn't reach OpenAI right now. Try again shortly.",
        )
        return redirect(url_for("assistant_page"))

    @app.post("/assistant/confirm")
    @require_login
    def assistant_confirm():
        decision = (request.form.get("decision") or "").strip().lower()
        action_id = (request.form.get("action_id") or "").strip()
        pending = session.get("assistant_pending_action") or {}
        if not pending or pending.get("id") != action_id:
            _append_assistant_chat("assistant", "No matching pending action found.")
            return redirect(url_for("assistant_page"))
        if decision != "approve":
            session.pop("assistant_pending_action", None)
            _append_assistant_chat("assistant", "Pending action cancelled.")
            return redirect(url_for("assistant_page"))

        cfg = _assistant_config(config.admin_db_file)
        if not cfg.get("write_enabled"):
            _append_assistant_chat("assistant", "Write mode is OFF; action not executed.")
            session.pop("assistant_pending_action", None)
            return redirect(url_for("assistant_page"))

        xc = build_xero_client(config)
        if not xc:
            _append_assistant_chat("assistant", "Xero not connected; action not executed.")
            session.pop("assistant_pending_action", None)
            return redirect(url_for("assistant_page"))

        invoice_ids = [str(v).strip() for v in (pending.get("invoice_ids") or []) if str(v).strip()]
        max_ops = 50
        invoice_ids = invoice_ids[:max_ops]
        done = 0
        failed = 0
        for inv_id in invoice_ids:
            try:
                xc.delete_draft_invoice(inv_id)
                done += 1
            except Exception:
                failed += 1
        session.pop("assistant_pending_action", None)
        _append_assistant_chat(
            "assistant",
            f"Write action completed. Success: {done}, Failed: {failed}, Attempted: {len(invoice_ids)}.",
        )
        return redirect(url_for("assistant_page"))

    @app.get("/admin/reset-login")
    def reset_login_page():
        token_set = bool((config.admin_reset_token or "").strip())
        hint = (
            ""
            if token_set
            else '<p class="text-sm text-amber-700 bg-amber-50 border border-amber-200 rounded-lg px-3 py-2 mb-4">ADMIN_RESET_TOKEN is not set in environment/secrets yet.</p>'
        )
        return _page(
            f"""
        <div class="min-h-screen flex items-center justify-center bg-gradient-to-br from-indigo-50 to-blue-100 px-4">
          <div class="w-full max-w-md">
            <div class="bg-white rounded-2xl shadow-xl p-8">
              <h1 class="text-2xl font-bold text-gray-900 mb-2">Reset Admin Login</h1>
              <p class="text-gray-500 text-sm mb-6">Use your reset token to replace admin username/password stored on volume.</p>
              {hint}
              <form method="post" class="space-y-4">
                <div>
                  <label class="block text-sm font-medium text-gray-700 mb-1.5">Reset token</label>
                  <input name="reset_token" type="password" class="w-full px-4 py-2.5 border border-gray-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500" placeholder="ADMIN_RESET_TOKEN">
                </div>
                <div>
                  <label class="block text-sm font-medium text-gray-700 mb-1.5">New username</label>
                  <input name="new_username" class="w-full px-4 py-2.5 border border-gray-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500" placeholder="admin@example.com">
                </div>
                <div>
                  <label class="block text-sm font-medium text-gray-700 mb-1.5">New password</label>
                  <input name="new_password" type="password" class="w-full px-4 py-2.5 border border-gray-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500" placeholder="New password">
                </div>
                <div>
                  <label class="block text-sm font-medium text-gray-700 mb-1.5">Confirm password</label>
                  <input name="confirm_password" type="password" class="w-full px-4 py-2.5 border border-gray-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500" placeholder="Repeat password">
                </div>
                <button type="submit" class="w-full py-2.5 px-4 bg-indigo-600 hover:bg-indigo-700 text-white font-medium rounded-lg text-sm transition-colors">Reset Login</button>
              </form>
              <a href="/login" class="block text-center mt-4 text-sm text-gray-500 hover:text-gray-700">Back to login</a>
            </div>
          </div>
        </div>
        """
        )

    @app.post("/admin/reset-login")
    def reset_login_post():
        configured_token = (config.admin_reset_token or "").strip()
        provided_token = (request.form.get("reset_token") or "").strip()
        new_username = (request.form.get("new_username") or "").strip()
        new_password = request.form.get("new_password") or ""
        confirm_password = request.form.get("confirm_password") or ""

        if not configured_token:
            return _page(
                """
            <div class="min-h-screen flex items-center justify-center bg-gradient-to-br from-indigo-50 to-blue-100 px-4">
              <div class="bg-white rounded-2xl shadow-xl p-8 w-full max-w-md">
                <h1 class="text-2xl font-bold text-red-700 mb-2">Reset Unavailable</h1>
                <p class="text-sm text-gray-600">ADMIN_RESET_TOKEN is not configured on this deployment.</p>
                <a href="/login" class="inline-block mt-4 px-4 py-2 bg-indigo-600 hover:bg-indigo-700 text-white rounded-lg text-sm">Back to login</a>
              </div>
            </div>
            """
            ), 400

        if not provided_token or not secrets.compare_digest(provided_token, configured_token):
            return _page(
                """
            <div class="min-h-screen flex items-center justify-center bg-gradient-to-br from-indigo-50 to-blue-100 px-4">
              <div class="bg-white rounded-2xl shadow-xl p-8 w-full max-w-md">
                <h1 class="text-2xl font-bold text-red-700 mb-2">Invalid Reset Token</h1>
                <p class="text-sm text-gray-600">The reset token is invalid.</p>
                <a href="/admin/reset-login" class="inline-block mt-4 px-4 py-2 bg-indigo-600 hover:bg-indigo-700 text-white rounded-lg text-sm">Try again</a>
              </div>
            </div>
            """
            ), 401

        if not new_username or not new_password:
            return _page(
                """
            <div class="min-h-screen flex items-center justify-center bg-gradient-to-br from-indigo-50 to-blue-100 px-4">
              <div class="bg-white rounded-2xl shadow-xl p-8 w-full max-w-md">
                <h1 class="text-2xl font-bold text-red-700 mb-2">Missing Fields</h1>
                <p class="text-sm text-gray-600">Username and password are required.</p>
                <a href="/admin/reset-login" class="inline-block mt-4 px-4 py-2 bg-indigo-600 hover:bg-indigo-700 text-white rounded-lg text-sm">Try again</a>
              </div>
            </div>
            """
            ), 400

        if new_password != confirm_password:
            return _page(
                """
            <div class="min-h-screen flex items-center justify-center bg-gradient-to-br from-indigo-50 to-blue-100 px-4">
              <div class="bg-white rounded-2xl shadow-xl p-8 w-full max-w-md">
                <h1 class="text-2xl font-bold text-red-700 mb-2">Password Mismatch</h1>
                <p class="text-sm text-gray-600">New password and confirmation do not match.</p>
                <a href="/admin/reset-login" class="inline-block mt-4 px-4 py-2 bg-indigo-600 hover:bg-indigo-700 text-white rounded-lg text-sm">Try again</a>
              </div>
            </div>
            """
            ), 400

        _save_admin_auth_file(config, new_username, new_password)
        session.clear()
        return _page(
            """
        <div class="min-h-screen flex items-center justify-center bg-gradient-to-br from-indigo-50 to-blue-100 px-4">
          <div class="bg-white rounded-2xl shadow-xl p-8 w-full max-w-md">
            <h1 class="text-2xl font-bold text-emerald-700 mb-2">Login Reset Complete</h1>
            <p class="text-sm text-gray-600">Admin credentials were saved to persistent storage.</p>
            <a href="/login" class="inline-block mt-4 px-4 py-2 bg-indigo-600 hover:bg-indigo-700 text-white rounded-lg text-sm">Go to login</a>
          </div>
        </div>
        """
        )

    @app.post("/save-xero-creds")
    @require_login
    def save_xero_creds():
        client_id = (request.form.get("xero_client_id") or "").strip()
        client_secret = (request.form.get("xero_client_secret") or "").strip()
        if client_id:
            set_json_setting(config.admin_db_file, "xero_client_id", client_id)
        if client_secret:
            set_json_setting(config.admin_db_file, "xero_client_secret", client_secret)
        if client_id or client_secret:
            session["save_notice"] = "success:Xero credentials saved. Now click Connect Xero."
        else:
            session["save_notice"] = "error:No credentials entered."
        return redirect(url_for("index"))

    @app.post("/save-xero-accounts")
    @require_login
    def save_xero_accounts():
        invoice_code = (request.form.get("invoice_account_code") or "").strip()
        payment_code = (request.form.get("payment_account_code") or "").strip()
        set_json_setting(config.admin_db_file, "xero_invoice_account_code", invoice_code)
        set_json_setting(config.admin_db_file, "xero_payment_account_code", payment_code)
        session["save_notice"] = "success:Xero account mapping saved."
        return redirect(url_for("index"))

    @app.post("/save-xero-tenant/<tenant_id>")
    @require_login
    def save_xero_tenant(tenant_id: str):
        invoice_code = (request.form.get("invoice_account_code") or "").strip()
        payment_code = (request.form.get("payment_account_code") or "").strip()
        branding_theme_id = (request.form.get("branding_theme_id") or "").strip()
        premium_theme_id = (request.form.get("premium_theme_id") or "").strip()
        premium_threshold_raw = (request.form.get("premium_threshold") or "").strip()
        try:
            premium_threshold = float(premium_threshold_raw) if premium_threshold_raw else None
        except ValueError:
            premium_threshold = None
        upsert_xero_tenant(
            config.admin_db_file,
            tenant_id,
            invoice_account=invoice_code,
            payment_account=payment_code,
            branding_theme_id=branding_theme_id,
            premium_theme_id=premium_theme_id if premium_theme_id else None,
            premium_threshold=premium_threshold,
        )
        if not payment_code:
            session["save_notice"] = (
                "error:Saved, but Payment bank account is missing. "
                "Card payments will fail until it is set."
            )
        elif not branding_theme_id:
            session["save_notice"] = (
                "success:Saved. Invoice template not set; Xero default template will be used."
            )
        else:
            session["save_notice"] = "success:Account mapping and invoice template saved."
        return redirect(url_for("index"))

    @app.post("/toggle-xero-tenant/<tenant_id>")
    @require_login
    def toggle_xero_tenant(tenant_id: str):
        tenants = get_xero_tenants(config.admin_db_file)
        current = next((t for t in tenants if t["tenantId"] == tenant_id), {})
        if not current:
            session["save_notice"] = "error:Organisation not found."
            return redirect(url_for("index"))

        # Single-active-tenant model: selecting one tenant enables it and pauses others.
        if current.get("enabled", True):
            session["save_notice"] = "success:Organisation already active."
            return redirect(url_for("index"))

        for t in tenants:
            t["enabled"] = (t.get("tenantId") == tenant_id)
        set_xero_tenants(config.admin_db_file, tenants)

        # Keep token tenant in sync with selected active tenant.
        try:
            tok = load_xero_token(config.xero_token_file)
            if tok:
                tok["tenant_id"] = tenant_id
                save_xero_token(config.xero_token_file, tok)
        except Exception:
            pass

        session["save_notice"] = "success:Active organisation switched."
        return redirect(url_for("index"))

    @app.get("/connect-xero")
    @require_login
    def connect_xero():
        xero_client_id, xero_client_secret = _get_xero_creds(config)
        if not xero_client_id or not xero_client_secret:
            session["save_notice"] = "error:Xero connect failed: enter your Client ID and Secret in the Xero card first."
            return redirect(url_for("index"))
        state = secrets.token_urlsafe(24)
        dynamic_xero_redirect = _current_base_url() + "/xero/callback"
        xero_auth_url = _xero_authorization_url(
            config,
            state,
            redirect_uri=dynamic_xero_redirect,
            force_consent=True,
        )
        session["xero_oauth_state"] = state
        session["xero_auth_url"] = xero_auth_url
        session["xero_redirect_uri"] = dynamic_xero_redirect
        # Persist to DB so the callback works even when opened in a separate browser tab
        set_json_setting(config.admin_db_file, "xero_oauth_pending_state", state)
        set_json_setting(config.admin_db_file, "xero_oauth_pending_redirect_uri", dynamic_xero_redirect)
        wants_json = (
            (request.args.get("mode") or "").strip().lower() == "json"
            or request.headers.get("X-Requested-With", "") == "XMLHttpRequest"
            or "application/json" in (request.headers.get("Accept", "") or "")
        )
        if wants_json:
            import flask as _flask
            return _flask.jsonify({"url": xero_auth_url})
        session["save_notice"] = "success:Xero authorisation link generated below."
        return redirect(url_for("index"))

    @app.get("/xero/callback")
    def xero_callback():
        code = request.args.get("code") or ""
        state = request.args.get("state") or ""
        err = request.args.get("error") or ""
        if err:
            return _page(f"""
            <div class="min-h-screen flex items-center justify-center bg-gray-50">
              <div class="bg-white rounded-2xl shadow p-8 max-w-md w-full text-center">
                <p class="text-red-600 font-medium mb-4">Xero OAuth error: {escape(err)}</p>
                <a href="/" class="text-indigo-600 hover:underline text-sm">Back to dashboard</a>
              </div>
            </div>
            """), 400
        expected_session = session.get("xero_oauth_state") or ""
        expected_store = str(
            get_json_setting(config.admin_db_file, "xero_oauth_pending_state", "")
        ).strip()
        state_ok = bool(state) and (state == expected_session or state == expected_store)
        if not code or not state_ok:
            return _page("""
            <div class="min-h-screen flex items-center justify-center bg-gray-50">
              <div class="bg-white rounded-2xl shadow p-8 max-w-md w-full text-center">
                <p class="text-red-600 font-medium mb-2">Xero callback invalid state/code.</p>
                <p class="text-gray-500 text-sm mb-4">Use one host consistently for login and callback, then retry.</p>
                <a href="/" class="text-indigo-600 hover:underline text-sm">Back to dashboard</a>
              </div>
            </div>
            """), 400
        try:
            dynamic_xero_redirect = (
                session.get("xero_redirect_uri")
                or str(get_json_setting(config.admin_db_file, "xero_oauth_pending_redirect_uri", "")).strip()
                or _current_base_url() + "/xero/callback"
            )
            token = _exchange_xero_code(config, code, redirect_uri=dynamic_xero_redirect)
            all_conns = _get_all_xero_connections(token.get("access_token", ""))
            if not all_conns:
                raise RuntimeError("No Xero organisation connections returned.")
        except Exception as exc:
            return _page(f"""
            <div class="min-h-screen flex items-center justify-center bg-gray-50">
              <div class="bg-white rounded-2xl shadow p-8 max-w-md w-full text-center">
                <p class="text-red-600 font-medium mb-4">Xero connect failed: {escape(str(exc))}</p>
                <a href="/" class="text-indigo-600 hover:underline text-sm">Back to dashboard</a>
              </div>
            </div>
            """), 400

        existing_tenants = get_xero_tenants(config.admin_db_file)
        existing_by_id = {t.get("tenantId"): t for t in existing_tenants if t.get("tenantId")}
        existing_active = next(
            (t.get("tenantId") for t in existing_tenants if t.get("enabled", True) and t.get("tenantId")),
            "",
        )
        previous_token = str(load_xero_token(config.xero_token_file).get("tenant_id", "") or "").strip()
        conn_ids = {c["tenantId"] for c in all_conns}
        tenant_id = (
            existing_active
            if existing_active in conn_ids
            else (previous_token if previous_token in conn_ids else all_conns[0]["tenantId"])
        )

        rebuilt_tenants: list[dict] = []
        for conn in all_conns:
            tid = conn["tenantId"]
            saved = existing_by_id.get(tid, {})
            rebuilt_tenants.append(
                {
                    "tenantId": tid,
                    "tenantName": conn.get("tenantName", tid),
                    "enabled": tid == tenant_id,
                    "invoiceAccount": saved.get("invoiceAccount", ""),
                    "paymentAccount": saved.get("paymentAccount", ""),
                    "brandingThemeId": saved.get("brandingThemeId", ""),
                    "premiumThemeId": saved.get("premiumThemeId", ""),
                    "premiumThreshold": saved.get("premiumThreshold"),
                }
            )
        set_xero_tenants(config.admin_db_file, rebuilt_tenants)

        token["tenant_id"] = tenant_id
        save_xero_token(config.xero_token_file, token)
        session["logged_in"] = True
        session.pop("xero_oauth_state", None)
        session.pop("xero_auth_url", None)
        session.pop("xero_redirect_uri", None)
        set_json_setting(config.admin_db_file, "xero_oauth_pending_state", "")
        set_json_setting(config.admin_db_file, "xero_oauth_pending_redirect_uri", "")
        set_json_setting(config.admin_db_file, "xero_auth_url", "")
        session["save_notice"] = "success:Xero connected successfully."
        return redirect(url_for("index"))

    @app.post("/upload-google-credentials")
    @require_login
    def upload_google_credentials():
        f = request.files.get("google_credentials")
        if not f or not f.filename:
            session["save_notice"] = "error:No credentials file selected."
            return redirect(url_for("index"))
        raw = f.read()
        ok, err = _validate_google_credentials_json(raw)
        if not ok:
            session["save_notice"] = f"error:Credentials upload failed: {err}"
            return redirect(url_for("index"))

        path = Path(config.google_credentials_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        set_json_setting(
            config.admin_db_file,
            "google_credentials_meta",
            {"uploaded_name": f.filename, "stored_path": str(path)},
        )
        session["save_notice"] = (
            f"success:Credentials uploaded ({f.filename}). Now click Connect Google to authorise."
        )
        return redirect(url_for("index"))

    @app.post("/upload-google-service-account")
    @require_login
    def upload_google_service_account():
        f = request.files.get("google_service_account")
        if not f or not f.filename:
            session["save_notice"] = "error:No Google service account file selected."
            return redirect(url_for("index"))
        raw = f.read()
        ok, err = _validate_google_service_account_json(raw)
        if not ok:
            session["save_notice"] = f"error:Service account upload failed: {err}"
            return redirect(url_for("index"))
        rset = get_receipts_settings(config.admin_db_file)
        configured = str(rset.get("google_service_account_file") or "").strip()
        # Production mounts a writable volume at /data; dev does not. Pick a path
        # that is actually writable in the current environment.
        try:
            path = _resolve_writable_sa_path(configured, config.admin_db_file)
            path.write_bytes(raw)
        except Exception as exc:
            session["save_notice"] = (
                f"error:Could not save service account file: {str(exc).splitlines()[0][:160]}"
            )
            return redirect(url_for("index"))
        rset["google_service_account_file"] = str(path)
        set_receipts_settings(config.admin_db_file, rset)
        set_json_setting(
            config.admin_db_file,
            "receipts_parser_test_status",
            {"ok": None, "message": "Not tested yet", "tested_at": ""},
        )
        session["save_notice"] = (
            f"success:Google service account uploaded ({f.filename})."
        )
        return redirect(url_for("index"))

    @app.post("/test-document-ai-connection")
    @require_login
    def test_document_ai_connection():
        # Accept unsaved form values so "Test" works immediately.
        current = get_receipts_settings(config.admin_db_file)
        project_id = (request.form.get("document_ai_project_id") or "").strip()
        location = (request.form.get("document_ai_location") or "").strip()
        processor_id = (request.form.get("document_ai_processor_id") or "").strip()
        if project_id:
            current["document_ai_project_id"] = project_id
        if location:
            current["document_ai_location"] = location
        if processor_id:
            current["document_ai_processor_id"] = processor_id
        set_receipts_settings(config.admin_db_file, current)

        svc = ReceiptService(config)
        ok, msg = svc.test_document_ai_connection()
        set_json_setting(
            config.admin_db_file,
            "receipts_parser_test_status",
            {
                "ok": bool(ok),
                "message": str(msg),
                "tested_at": dt.datetime.now(dt.timezone.utc).astimezone().strftime("%d %b %Y %H:%M"),
            },
        )
        session["save_notice"] = f"{'success' if ok else 'error'}:{msg}"
        _rt = (request.form.get("return_to") or "").strip()
        if _rt.startswith("/") and not _rt.startswith("//"):
            return redirect(_rt)
        return redirect(url_for("index"))

    @app.post("/save-cashflows-settings")
    @require_login
    def save_cashflows_settings():
        environment = (request.form.get("cashflows_environment") or "integration").strip().lower()
        enabled = bool(request.form.get("cashflows_enabled"))
        current_cashflows = get_cashflows_settings(config.admin_db_file)
        base_url = (request.form.get("cashflows_base_url") or "").strip()
        configuration_id = (request.form.get("cashflows_configuration_id") or "").strip()
        api_key = (request.form.get("cashflows_api_key") or "").strip() or str(
            current_cashflows.get("api_key") or ""
        ).strip()
        timeout_raw = (request.form.get("cashflows_timeout_seconds") or "15").strip()
        settlements_action = (
            request.form.get("cashflows_settlements_action")
            or "GetSettlementPayouts"
        ).strip() or "GetSettlementPayouts"
        try:
            timeout_seconds = max(int(timeout_raw or "15"), 5)
        except Exception:
            timeout_seconds = 15
        set_cashflows_settings(
            config.admin_db_file,
            {
                "enabled": enabled,
                "environment": environment,
                "base_url": base_url,
                "configuration_id": configuration_id,
                "api_key": api_key,
                "timeout_seconds": timeout_seconds,
                "settlements_action": settlements_action,
            },
        )
        set_json_setting(
            config.admin_db_file,
            "cashflows_test_status",
            {"ok": None, "message": "Not tested yet", "tested_at": ""},
        )
        session["save_notice"] = "success:Cashflows settings saved."
        return redirect(url_for("index"))

    @app.post("/save-openai-settings")
    @require_login
    def save_openai_settings_route():
        current = get_openai_settings(config.admin_db_file)
        clear_key = request.form.get("clear_key") == "1"
        new_key = (request.form.get("openai_api_key") or "").strip()
        new_model = (request.form.get("openai_model") or "").strip() or "gpt-4o-mini"
        set_openai_settings(
            config.admin_db_file,
            {
                "api_key": "" if clear_key else (new_key or current.get("api_key", "")),
                "model": new_model,
            },
        )
        set_json_setting(
            config.admin_db_file,
            "openai_test_status",
            {"ok": None, "message": "Not tested yet", "tested_at": ""},
        )
        session["save_notice"] = "success:OpenAI settings saved."
        return redirect(url_for("index"))

    @app.post("/test-openai-connection")
    @require_login
    def test_openai_connection():
        incoming_key = (request.form.get("openai_api_key") or "").strip()
        incoming_model = (request.form.get("openai_model") or "").strip() or "gpt-4o-mini"
        current = get_openai_settings(config.admin_db_file)
        if incoming_key:
            current["api_key"] = incoming_key
        if incoming_model:
            current["model"] = incoming_model
        set_openai_settings(config.admin_db_file, current)

        wants_json = (
            request.headers.get("X-Requested-With") == "fetch"
            or "application/json" in (request.headers.get("Accept") or "")
        )

        def _finish(test_ok, test_msg, notice):
            tested_at = dt.datetime.now(dt.timezone.utc).astimezone().strftime("%d %b %Y %H:%M")
            set_json_setting(
                config.admin_db_file,
                "openai_test_status",
                {"ok": test_ok, "message": test_msg, "tested_at": tested_at},
            )
            if wants_json:
                return jsonify({"ok": bool(test_ok), "message": test_msg, "tested_at": tested_at})
            session["save_notice"] = notice
            _rt = (request.form.get("return_to") or "").strip()
            if _rt.startswith("/") and not _rt.startswith("//"):
                return redirect(_rt)
            return redirect(url_for("index"))

        api_key = current.get("api_key") or (os.getenv("OPENAI_API_KEY") or "").strip()
        if not api_key:
            return _finish(False, "No API key configured.", "error:No OpenAI API key configured.")

        try:
            resp = requests.get(
                "https://api.openai.com/v1/models",
                headers={"Authorization": "Bearer " + api_key},
                timeout=10,
            )
            code = resp.status_code
            if code == 200:
                return _finish(True, "Connected successfully.", "success:OpenAI connection verified.")
            elif code in {401, 403}:
                return _finish(False, "API key rejected (invalid or expired).", "error:OpenAI API key is invalid or expired.")
            else:
                sample = (resp.text or "").strip()[:140]
                return _finish(False, f"Unexpected response HTTP {code}. {sample}", f"error:OpenAI test returned HTTP {code}.")
        except Exception as exc:
            return _finish(False, f"Connection error: {str(exc)[:200]}", f"error:OpenAI test failed: {str(exc)[:120]}")

    @app.post("/save-receipts-settings")
    @require_login
    def save_receipts_settings():
        current = get_receipts_settings(config.admin_db_file)
        enabled = bool(request.form.get("receipts_enabled_runtime"))
        project_id = (request.form.get("document_ai_project_id") or "").strip()
        location = (request.form.get("document_ai_location") or "us").strip() or "us"
        processor_id = (request.form.get("document_ai_processor_id") or "").strip()
        retention_raw = (request.form.get("retention_days") or str(current.get("retention_days") or 2)).strip()
        try:
            retention_days = max(int(retention_raw or "2"), 1)
        except Exception:
            retention_days = int(current.get("retention_days") or 2)
        current.update(
            {
                "enabled": enabled,
                "document_ai_project_id": project_id,
                "document_ai_location": location,
                "document_ai_processor_id": processor_id,
                "retention_days": retention_days,
            }
        )
        set_receipts_settings(config.admin_db_file, current)
        set_json_setting(
            config.admin_db_file,
            "receipts_parser_test_status",
            {"ok": None, "message": "Not tested yet", "tested_at": ""},
        )
        session["save_notice"] = "success:Receipt parser settings saved."
        return redirect(url_for("index"))

    @app.post("/test-cashflows-connection")
    @require_login
    def test_cashflows_connection():
        # Accept unsaved form values so "Test" works immediately.
        incoming_env = (request.form.get("cashflows_environment") or "").strip().lower()
        incoming_base = (request.form.get("cashflows_base_url") or "").strip()
        incoming_cfg = (request.form.get("cashflows_configuration_id") or "").strip()
        incoming_key = (request.form.get("cashflows_api_key") or "").strip()
        incoming_timeout = (request.form.get("cashflows_timeout_seconds") or "").strip()
        incoming_action = (request.form.get("cashflows_settlements_action") or "").strip()

        cset = get_cashflows_settings(config.admin_db_file)
        if incoming_env in {"integration", "production"}:
            cset["environment"] = incoming_env
        if incoming_base:
            cset["base_url"] = incoming_base
        if incoming_cfg:
            cset["configuration_id"] = incoming_cfg
        if incoming_key:
            cset["api_key"] = incoming_key
        if incoming_timeout:
            try:
                cset["timeout_seconds"] = max(int(incoming_timeout), 5)
            except Exception:
                pass
        if incoming_action:
            cset["settlements_action"] = incoming_action
        set_cashflows_settings(config.admin_db_file, cset)

        base_url = str(cset.get("base_url") or "").strip()
        configuration_id = str(cset.get("configuration_id") or "").strip()
        api_key = str(cset.get("api_key") or "").strip()
        timeout_seconds = int(cset.get("timeout_seconds") or 15)
        if not base_url or not api_key:
            set_json_setting(
                config.admin_db_file,
                "cashflows_test_status",
                {
                    "ok": False,
                    "message": "Cashflows test needs a base URL and API key.",
                    "tested_at": dt.datetime.now(dt.timezone.utc).astimezone().strftime("%d %b %Y %H:%M"),
                },
            )
            session["save_notice"] = "error:Cashflows test needs a base URL and API key."
            return redirect(url_for("index"))
        payload_text = "{}"
        hash_value = hashlib.sha512((payload_text + api_key).encode("utf-8")).hexdigest().upper()
        headers = {
            "Content-Type": "application/json",
            "ConfigurationId": configuration_id,
            "Hash": hash_value,
        }
        try:
            resp = requests.post(
                base_url,
                data=payload_text,
                headers=headers,
                timeout=timeout_seconds,
            )
            code = resp.status_code
            sample = (resp.text or "").strip().replace("\n", " ")[:180]
            if code in {200, 201, 202, 204}:
                session["save_notice"] = f"success:Cashflows reachable (HTTP {code})."
                test_msg = f"Cashflows reachable (HTTP {code})."
                test_ok = True
            elif code in {400, 404, 405}:
                session["save_notice"] = (
                    f"error:Cashflows endpoint reached but did not accept the probe "
                    f"(HTTP {code}). Check the base URL and settlement action."
                )
                test_msg = (
                    f"Endpoint reached but probe was rejected (HTTP {code}). "
                    "Run Cashflows Sync diagnostics with a real date range."
                )
                test_ok = False
            elif code in {401, 403}:
                session["save_notice"] = (
                    f"error:Cashflows rejected credentials (HTTP {code}). {sample}"
                )
                test_msg = f"Credentials rejected (HTTP {code}). {sample}"
                test_ok = False
            else:
                session["save_notice"] = (
                    f"error:Cashflows test returned HTTP {code}. {sample}"
                )
                test_msg = f"Cashflows returned HTTP {code}. {sample}"
                test_ok = False
            set_json_setting(
                config.admin_db_file,
                "cashflows_test_status",
                {
                    "ok": bool(test_ok),
                    "message": test_msg,
                    "tested_at": dt.datetime.now(dt.timezone.utc).astimezone().strftime("%d %b %Y %H:%M"),
                },
            )
        except Exception as exc:
            session["save_notice"] = f"error:Cashflows test failed: {str(exc)[:220]}"
            set_json_setting(
                config.admin_db_file,
                "cashflows_test_status",
                {
                    "ok": False,
                    "message": f"Cashflows test failed: {str(exc)[:220]}",
                    "tested_at": dt.datetime.now(dt.timezone.utc).astimezone().strftime("%d %b %Y %H:%M"),
                },
            )
        return redirect(url_for("index"))

    @app.get("/connect-google")
    @require_login
    def connect_google():
        if not Path(config.google_credentials_file).exists():
            session["save_notice"] = (
                "error:No credentials file found. Please upload your Google OAuth JSON file first using the Upload JSON button."
            )
            return redirect(url_for("index"))
        try:
            dynamic_google_redirect = _current_base_url() + "/oauth/callback"
            auth_url, state = oauth_authorization_url(config, redirect_uri=dynamic_google_redirect)
        except Exception as exc:
            session["save_notice"] = f"error:Could not start Google OAuth: {exc}"
            return redirect(url_for("index"))
        print(f"[Google OAuth] redirect_uri={dynamic_google_redirect}")
        print(f"[Google OAuth] auth_url={auth_url}")
        session["oauth_state"] = state
        session["oauth_auth_url"] = auth_url
        session["google_redirect_uri"] = dynamic_google_redirect
        set_json_setting(config.admin_db_file, "oauth_pending_state", state)
        set_json_setting(config.admin_db_file, "oauth_auth_url", auth_url)
        return redirect(url_for("index"))

    @app.get("/oauth/callback")
    def oauth_callback():
        print(f"[OAuth Callback] ALL args from Google: {dict(request.args)}")
        code = request.args.get("code") or ""
        state = request.args.get("state") or ""
        google_error = request.args.get("error") or ""
        if google_error:
            print(f"[OAuth Callback] Google returned ERROR: {google_error}")
        expected_session = session.get("oauth_state") or ""
        expected_store = str(
            get_json_setting(config.admin_db_file, "oauth_pending_state", "")
        ).strip()
        state_ok = bool(state) and (state == expected_session or state == expected_store)
        if not code or not state_ok:
            error_detail = google_error or ("missing code" if not code else "state mismatch")
            print(f"[OAuth Callback] Failing: code={'present' if code else 'MISSING'}, state_ok={state_ok}, google_error={google_error!r}")
            return _page(f"""
            <div class="min-h-screen flex items-center justify-center bg-gray-50">
              <div class="bg-white rounded-2xl shadow p-8 max-w-md w-full text-center">
                <p class="text-red-600 font-medium mb-2">OAuth callback failed.</p>
                <p class="text-gray-700 text-sm mb-2">Google said: <strong>{error_detail}</strong></p>
                <p class="text-gray-500 text-sm mb-4">code={'present' if code else 'missing'} &nbsp;|&nbsp; state_ok={state_ok}</p>
                <a href="/" class="text-indigo-600 hover:underline text-sm">Back to dashboard</a>
              </div>
            </div>
            """), 400
        dynamic_google_redirect = (
            session.get("google_redirect_uri")
            or _current_base_url() + "/oauth/callback"
        )
        try:
            creds = oauth_exchange_code(config, state=state, code=code, redirect_uri=dynamic_google_redirect)
        except Exception as exc:
            print(f"[OAuth Callback] token exchange failed: {exc}")
            return _page(f"""
            <div class="min-h-screen flex items-center justify-center bg-gray-50">
              <div class="bg-white rounded-2xl shadow p-8 max-w-md w-full text-center">
                <p class="text-red-600 font-medium mb-2">Google token exchange failed.</p>
                <p class="text-gray-700 text-sm mb-2">{escape(str(exc))}</p>
                <p class="text-gray-500 text-sm mb-4">Make sure <strong>{escape(dynamic_google_redirect)}</strong> is listed
                as an authorised redirect URI in your Google Cloud Console OAuth client.</p>
                <a href="/settings" class="text-indigo-600 hover:underline text-sm">Back to settings</a>
              </div>
            </div>
            """), 400
        save_admin_credentials(config, creds)
        session["logged_in"] = True
        session.pop("oauth_state", None)
        set_json_setting(config.admin_db_file, "oauth_pending_state", "")
        session["save_notice"] = "success:Google connected successfully."
        session.pop("oauth_auth_url", None)
        set_json_setting(config.admin_db_file, "oauth_auth_url", "")
        return redirect(url_for("index"))

    # ── Dashboard (main page) ──────────────────────────────────────────────────

    @app.get("/")
    @require_login
    def dashboard():
        state = load_state(config.state_file)
        inv_map = state.get("event_invoice_map", {})
        total_invoices = len(inv_map)
        watches = get_google_watches(config.admin_db_file)
        xero_tok = load_xero_token(config.xero_token_file)
        try:
            google_ok = bool(load_admin_credentials(config))
        except Exception:
            google_ok = False
        xero_ok = bool(xero_tok and not token_is_expired(xero_tok))
        watch_count = len(watches)
        enabled = get_enabled(config.admin_db_file)
        receipt_settings = get_receipts_settings(config.admin_db_file)
        receipts_runtime_enabled = bool(receipt_settings.get("enabled"))
        receipts_code_enabled = bool(config.receipts_enabled)
        xero_lockout_until_ts = float(state.get("xero_lockout_until_ts") or 0.0)
        xero_lockout_active = xero_lockout_until_ts > time.time()
        xero_lockout_reason = str(state.get("xero_lockout_reason") or "").strip()
        xero_pressure = state.get("xero_pressure", {}) or {}
        xero_pressure_level = str(xero_pressure.get("level") or "unknown").strip().lower()
        try:
            xero_pressure_updated_ts = float(xero_pressure.get("updated_at_ts") or 0.0)
        except Exception:
            xero_pressure_updated_ts = 0.0
        xero_pressure_recent = bool(
            xero_pressure_updated_ts and (time.time() - xero_pressure_updated_ts) <= 900
        )
        xero_events_used = int(xero_pressure.get("events_used") or 0)
        xero_events_per_cycle = int(xero_pressure.get("events_per_cycle") or 0)
        xero_deferred_events = int(xero_pressure.get("deferred_events") or 0)
        xero_active_retry_count = int(xero_pressure.get("active_retry_count") or 0)
        xero_deferred_sample = xero_pressure.get("deferred_sample") or []
        if not isinstance(xero_deferred_sample, list):
            xero_deferred_sample = []
        xero_lockout_banner = ""
        if xero_lockout_active:
            _mins = int(max(1, (xero_lockout_until_ts - time.time()) // 60))
            _until_local = dt.datetime.fromtimestamp(
                xero_lockout_until_ts, tz=dt.timezone.utc
            ).astimezone().strftime("%d %b %Y %H:%M")
            _reason_txt = xero_lockout_reason or "Xero is temporarily rate-limited."
            xero_lockout_banner = (
                '<div class="mx-6 mt-4 flex items-start gap-3 px-4 py-3 rounded-lg border '
                'border-red-700/50 bg-red-950/40 text-red-200 text-sm">'
                '<span class="shrink-0 mt-0.5">🔒</span>'
                f'<span><strong>Xero lockout active.</strong> {_reason_txt} '
                f'No Xero requests will be sent until unlock. '
                f'Estimated unlock: {_until_local} '
                f'(<span id="xero-lockout-countdown" data-until="{int(xero_lockout_until_ts)}">{_mins}m</span>).</span>'
                '</div>'
            )

        # Build home-page warnings
        _dash_warnings: list[str] = []
        if not google_ok:
            _dash_warnings.append(
                "Google not connected \u2014 calendar events cannot be read and webhooks cannot be "
                "registered. Go to Settings to reconnect."
            )
        if not xero_ok:
            _dash_warnings.append(
                "Xero not connected \u2014 invoices cannot be created. Go to Settings to reconnect."
            )
        _now_ms = int(time.time() * 1000)
        _active_cals = get_active_calendars(config.admin_db_file, config.google_calendar_id)
        for _cal_id in _active_cals:
            _winfo = watches.get(_cal_id) or {}
            _exp_ms = int(_winfo.get("expiration_ms") or 0)
            if not _winfo or not _winfo.get("channel_id"):
                _dash_warnings.append(
                    f"No webhook registered for calendar {_cal_id} \u2014 events will only be "
                    "caught by the polling fallback (check Settings \u2192 Calendars)."
                )
            elif _exp_ms and _exp_ms < _now_ms:
                _dash_warnings.append(
                    f"Google webhook expired for {_cal_id} \u2014 automatic renewal will be "
                    "attempted on the next poll cycle."
                )
        if not xero_lockout_active and xero_pressure_recent:
            if xero_pressure_level == "danger":
                _sample_names = [
                    str((row or {}).get("summary") or "").strip()
                    for row in xero_deferred_sample[:3]
                    if isinstance(row, dict) and str((row or {}).get("summary") or "").strip()
                ]
                _sample_txt = f" Sample: {', '.join(_sample_names)}." if _sample_names else ""
                _dash_warnings.append(
                    f"Xero pressure high \u2014 {xero_deferred_events} event(s) were deferred after "
                    f"using {xero_events_used}/{xero_events_per_cycle} Xero slots. "
                    f"The app is throttling itself; pause before bulk edits.{_sample_txt}"
                )
            elif xero_pressure_level == "warn":
                _dash_warnings.append(
                    f"Xero pressure watch \u2014 used {xero_events_used}/{xero_events_per_cycle} "
                    "Xero slots in the last cycle. Avoid bulk edits until this settles."
                )

        def _warning_banner(msg: str) -> str:
            return (
                f'<div class="flex items-start gap-3 px-4 py-3 rounded-lg border '
                f'border-amber-600/40 bg-amber-950/40 text-amber-300 text-sm">'
                f'<span class="shrink-0 text-amber-400 mt-0.5">\u26a0</span>'
                f'<span>{escape(msg)}</span>'
                f'</div>'
            )

        warnings_html = (
            '<div class="mx-6 mt-4 flex flex-col gap-2">'
            + "".join(_warning_banner(w) for w in _dash_warnings)
            + "</div>"
            if _dash_warnings
            else ""
        )

        # Keep dashboard rendering snappy even when the ring buffer is large.
        recent_logs = _feed.recent(30)
        last_seq_val = recent_logs[-1]["seq"] if recent_logs else 0

        # Scan feed for meaningful status signals
        last_event_entry = None
        last_contact_entry = None
        last_invoice_entry = None
        for entry in reversed(recent_logs):
            msg = entry.get("msg", "")
            level = entry.get("level", "")
            if last_event_entry is None and level == "event" and "DONE event detected:" in msg:
                last_event_entry = entry
            if last_contact_entry is None and level == "event" and msg.startswith("New contact:"):
                last_contact_entry = entry
            if last_invoice_entry is None and level == "success" and "Invoice created" in msg:
                last_invoice_entry = entry
            if last_event_entry and last_contact_entry and last_invoice_entry:
                break

        def _ago(ts_f: float) -> str:
            diff = time.time() - ts_f
            if diff < 5:
                return "just now"
            if diff < 60:
                return f"{int(diff)}s ago"
            if diff < 3600:
                return f"{int(diff // 60)}m ago"
            if diff < 86400:
                return f"{int(diff // 3600)}h ago"
            return f"{int(diff // 86400)}d ago"

        def _signal_card(label: str, value: str, sub: str, color: str) -> str:
            return (
                f'<div class="bg-neutral-900 border border-neutral-800 rounded-xl p-4 min-w-0">'
                f'<p class="text-xs text-neutral-500 mb-1.5 uppercase tracking-wider">{label}</p>'
                f'<p class="text-sm font-semibold {color} truncate" title="{escape(value)}">{escape(value)}</p>'
                f'<p class="text-xs text-neutral-600 mt-0.5">{escape(sub)}</p>'
                f'</div>'
            )

        # Webhook signal card
        if watch_count:
            wh_value = f"{watch_count} active webhook{'s' if watch_count != 1 else ''}"
            wh_sub = "Google push notifications on"
            wh_color = "text-indigo-300"
        else:
            wh_value = "Polling only"
            wh_sub = "No webhooks registered"
            wh_color = "text-neutral-400"
        webhook_card = _signal_card("Webhooks", wh_value, wh_sub, wh_color)

        if xero_lockout_active:
            xero_pressure_card = _signal_card("Xero pressure", "Locked", "429 cooldown active", "text-red-300")
        elif not xero_pressure_recent:
            xero_pressure_card = _signal_card("Xero pressure", "No cycle yet", "Waiting for poller", "text-neutral-500")
        elif xero_pressure_level == "danger":
            xero_pressure_card = _signal_card(
                "Xero pressure",
                "High",
                f"{xero_deferred_events} deferred, {xero_events_used}/{xero_events_per_cycle} used",
                "text-red-300",
            )
        elif xero_pressure_level == "warn":
            xero_pressure_card = _signal_card(
                "Xero pressure",
                "Watch",
                f"{xero_events_used}/{xero_events_per_cycle} slots used",
                "text-amber-300",
            )
        elif xero_pressure_level == "watch":
            xero_pressure_card = _signal_card(
                "Xero pressure",
                "Retry queue",
                f"{xero_active_retry_count} event(s) cooling down",
                "text-amber-300",
            )
        else:
            xero_pressure_card = _signal_card(
                "Xero pressure",
                "OK",
                f"{xero_events_used}/{xero_events_per_cycle} slots used",
                "text-emerald-300",
            )

        # Last DONE event card
        if last_event_entry:
            ev_msg = last_event_entry["msg"].replace("DONE event detected: ", "").strip('"')
            ev_sub = _ago(last_event_entry["ts"])
            event_card = _signal_card("Last event formatted", ev_msg, ev_sub, "text-cyan-300")
        else:
            event_card = _signal_card("Last event formatted", "None yet", "Waiting for a DONE event…", "text-neutral-500")

        # Last new contact card
        if last_contact_entry:
            ct_name = last_contact_entry["msg"].replace("New contact: ", "").strip()
            ct_sub = _ago(last_contact_entry["ts"])
            contact_card = _signal_card("Last new contact", ct_name, ct_sub, "text-emerald-300")
        else:
            contact_card = _signal_card("Last new contact", "None yet", "New submitters appear here", "text-neutral-500")

        # Last invoice card
        if last_invoice_entry:
            inv_msg = last_invoice_entry["msg"].replace("Invoice created in Xero — ", "").strip()
            inv_sub = _ago(last_invoice_entry["ts"])
            invoice_card = _signal_card("Last invoice", inv_msg[:40], inv_sub, "text-blue-300")
        else:
            invoice_card = _signal_card("Last invoice", f"{total_invoices} total" if total_invoices else "None yet", "Session history", "text-neutral-400")

        # Connection badges
        google_badge = (
            '<span class="flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium bg-emerald-900/60 text-emerald-300 border border-emerald-700/50">'
            '<span class="w-1.5 h-1.5 rounded-full bg-emerald-400 shrink-0"></span>Google</span>'
            if google_ok else
            '<span class="flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium bg-neutral-800 text-neutral-400 border border-neutral-700">'
            '<span class="w-1.5 h-1.5 rounded-full bg-neutral-500 shrink-0"></span>Google</span>'
        )
        xero_badge = (
            '<span class="flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium bg-blue-900/60 text-blue-300 border border-blue-700/50">'
            '<span class="w-1.5 h-1.5 rounded-full bg-blue-400 shrink-0"></span>Xero</span>'
            if xero_ok else
            '<span class="flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium bg-neutral-800 text-neutral-400 border border-neutral-700">'
            '<span class="w-1.5 h-1.5 rounded-full bg-neutral-500 shrink-0"></span>Xero</span>'
        )

        # On/Off toggle state
        enabled_js = "true" if enabled else "false"
        receipts_enabled_js = "true" if receipts_runtime_enabled else "false"
        receipts_code_enabled_js = "true" if receipts_code_enabled else "false"
        toggle_label = "Cal→Xero: On" if enabled else "Cal→Xero: Off"
        receipts_toggle_label = (
            "Receipts On" if (receipts_code_enabled and receipts_runtime_enabled) else "Receipts Off"
        )

        def _render_line(entry):
            ts = dt.datetime.fromtimestamp(entry["ts"], tz=dt.timezone.utc).strftime("%H:%M:%S")
            level = entry.get("level", "info")
            msg = entry.get("msg", "")
            from html import escape as _esc
            color_map = {
                "event":   "text-cyan-400",
                "success": "text-emerald-400",
                "warn":    "text-amber-400",
                "error":   "text-red-400",
                "paid":    "text-emerald-300",
                "system":  "text-neutral-500",
                "info":    "text-neutral-300",
            }
            prefix_map = {
                "event":   "◆",
                "success": "✓",
                "warn":    "⚠",
                "error":   "✗",
                "paid":    "£",
                "system":  "·",
                "info":    "›",
            }
            col = color_map.get(level, "text-neutral-300")
            pre = prefix_map.get(level, "›")
            return (
                f'<div class="term-line flex gap-3 leading-relaxed">'
                f'<span class="text-neutral-600 shrink-0 select-none">{_esc(ts)}</span>'
                f'<span class="{col} shrink-0 select-none">{pre}</span>'
                f'<span class="{col}">{_esc(msg)}</span>'
                f'</div>'
            )

        history_html = "\n".join(_render_line(e) for e in recent_logs) if recent_logs else (
            '<div class="text-neutral-600 text-sm italic">No activity yet — waiting for events…</div>'
        )

        return (
            f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Powwash Bridge</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <style>
    body {{ background: #0a0a0f; }}
    #terminal {{ scroll-behavior: smooth; }}
    .term-line {{ padding: 1px 0; }}
  </style>
</head>
<body class="min-h-screen text-neutral-100 font-sans">

  {_nav()}

  <!-- Connection status -->
  <div class="flex items-center justify-end gap-2 px-6 pt-3">
    {google_badge}
    {xero_badge}
  </div>

  {warnings_html}
  {xero_lockout_banner}

  <!-- Automation controls -->
  <div class="flex flex-wrap items-center gap-3 px-6 pt-5">
    <span class="text-xs font-semibold text-neutral-500 uppercase tracking-wide mr-1">Automation</span>
    <!-- On/Off Toggle Switch -->
    <label for="toggle-switch" class="flex items-center gap-2 px-2.5 py-1.5 rounded-lg border border-neutral-700 bg-neutral-800/70 cursor-pointer">
      <span id="toggle-label" class="text-xs font-semibold {'text-emerald-300' if enabled else 'text-neutral-400'}">{toggle_label}</span>
      <input id="toggle-switch" type="checkbox" class="sr-only" {'checked' if enabled else ''} onchange="toggleEnabled(this.checked)">
      <span id="toggle-track" class="relative inline-flex h-5 w-9 items-center rounded-full transition-colors {'bg-emerald-500' if enabled else 'bg-neutral-600'}">
        <span id="toggle-knob" class="inline-block h-4 w-4 transform rounded-full bg-white transition-transform {'translate-x-4' if enabled else 'translate-x-1'}"></span>
      </span>
    </label>
    <label for="receipts-toggle-switch" class="flex items-center gap-2 px-2.5 py-1.5 rounded-lg border border-neutral-700 bg-neutral-800/70 cursor-pointer {'opacity-60' if not receipts_code_enabled else ''}">
      <span id="receipts-toggle-label" class="text-xs font-semibold {'text-emerald-300' if (receipts_code_enabled and receipts_runtime_enabled) else 'text-neutral-400'}">{receipts_toggle_label}</span>
      <input id="receipts-toggle-switch" type="checkbox" class="sr-only" {'checked' if receipts_runtime_enabled else ''} {'disabled' if not receipts_code_enabled else ''} onchange="toggleReceiptsEnabled(this.checked)">
      <span id="receipts-toggle-track" class="relative inline-flex h-5 w-9 items-center rounded-full transition-colors {'bg-emerald-500' if (receipts_code_enabled and receipts_runtime_enabled) else 'bg-neutral-600'}">
        <span id="receipts-toggle-knob" class="inline-block h-4 w-4 transform rounded-full bg-white transition-transform {'translate-x-4' if receipts_runtime_enabled else 'translate-x-1'}"></span>
      </span>
    </label>
  </div>

  <!-- Status signals -->
  <div class="grid grid-cols-2 sm:grid-cols-5 gap-3 px-6 pt-4 pb-4">
    {webhook_card}
    {xero_pressure_card}
    {event_card}
    {contact_card}
    {invoice_card}
  </div>

  <!-- Terminal -->
  <div class="mx-6 mb-6 rounded-xl overflow-hidden border border-neutral-800 shadow-2xl">
    <!-- Terminal chrome -->
    <div class="flex items-center justify-between px-4 py-2.5 bg-neutral-900 border-b border-neutral-800">
      <div class="flex items-center gap-1.5">
        <div class="w-3 h-3 rounded-full bg-red-500/80"></div>
        <div class="w-3 h-3 rounded-full bg-amber-400/80"></div>
        <div class="w-3 h-3 rounded-full bg-emerald-500/80"></div>
      </div>
      <span class="text-xs font-mono text-neutral-500">live feed</span>
      <div class="flex items-center gap-2">
        <span id="conn-dot" class="w-2 h-2 rounded-full bg-neutral-600"></span>
        <span id="conn-label" class="text-xs text-neutral-600">connecting…</span>
        <button id="scroll-btn" onclick="toggleScroll()"
          class="text-xs text-neutral-500 hover:text-neutral-300 px-2 py-0.5 rounded border border-neutral-700 transition-colors">
          ↓ Auto-scroll
        </button>
      </div>
    </div>
    <!-- Terminal body -->
    <div id="terminal"
      class="h-[55vh] overflow-y-auto bg-neutral-950 p-4 font-mono text-sm"
      onscroll="onUserScroll()">
      {history_html}
    </div>
  </div>

  <!-- Legend -->
  <div class="flex flex-wrap gap-4 px-6 pb-6 text-xs font-mono">
    <span class="text-cyan-400">◆ event / new contact</span>
    <span class="text-emerald-400">✓ created / logged</span>
    <span class="text-emerald-300">£ payment received</span>
    <span class="text-amber-400">⚠ needs attention</span>
    <span class="text-red-400">✗ error</span>
    <span class="text-neutral-500">· system</span>
  </div>

<script>
const term = document.getElementById('terminal');
const connDot = document.getElementById('conn-dot');
const connLabel = document.getElementById('conn-label');
const scrollBtn = document.getElementById('scroll-btn');
const MAX_TERM_LINES = 30;
let autoScroll = true;

function _fmtRemaining(seconds) {{
  const s = Math.max(0, Math.floor(seconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (h > 0) return `${{h}}h ${{m}}m`;
  return `${{m}}m`;
}}

function updateXeroLockoutCountdowns() {{
  const ids = ['xero-lockout-countdown'];
  let hasActive = false;
  ids.forEach((id) => {{
    const el = document.getElementById(id);
    if (!el) return;
    const until = parseInt(el.dataset.until || '0', 10);
    if (!until) return;
    const now = Math.floor(Date.now() / 1000);
    const remaining = until - now;
    if (remaining > 0) {{
      hasActive = true;
      el.textContent = _fmtRemaining(remaining);
    }} else {{
      el.textContent = '0m';
    }}
  }});
}}
updateXeroLockoutCountdowns();
setInterval(updateXeroLockoutCountdowns, 1000);

function scrollToBottom() {{
  term.scrollTop = term.scrollHeight;
}}
scrollToBottom();

function toggleScroll() {{
  autoScroll = !autoScroll;
  scrollBtn.textContent = autoScroll ? '↓ Auto-scroll' : '⏸ Paused';
  scrollBtn.classList.toggle('text-indigo-400', autoScroll);
  scrollBtn.classList.toggle('text-neutral-500', !autoScroll);
  if (autoScroll) scrollToBottom();
}}

function onUserScroll() {{
  // Keep auto-scroll sticky unless user explicitly toggles pause.
}}

const levelColor = {{
  event: 'text-cyan-400', success: 'text-emerald-400', warn: 'text-amber-400',
  error: 'text-red-400', paid: 'text-emerald-300', system: 'text-neutral-500', info: 'text-neutral-300',
}};
const levelPrefix = {{
  event: '◆', success: '✓', warn: '⚠', error: '✗', paid: '£', system: '·', info: '›',
}};

function escHtml(s) {{
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}}

function appendLine(entry) {{
  const ts = new Date(entry.ts * 1000).toLocaleTimeString('en-GB', {{hour:'2-digit',minute:'2-digit',second:'2-digit'}});
  const lvl = entry.level || 'info';
  const col = levelColor[lvl] || 'text-neutral-300';
  const pre = levelPrefix[lvl] || '›';
  const div = document.createElement('div');
  div.className = 'term-line flex gap-3 leading-relaxed';
  div.innerHTML = `<span class="text-neutral-600 shrink-0 select-none">${{ts}}</span>`
    + `<span class="${{col}} shrink-0 select-none">${{pre}}</span>`
    + `<span class="${{col}}">${{escHtml(entry.msg)}}</span>`;
  term.appendChild(div);
  while (term.children.length > MAX_TERM_LINES) {{
    term.removeChild(term.firstElementChild);
  }}
  if (autoScroll) scrollToBottom();
}}

let lastSeq = {last_seq_val};
const es = new EventSource('/feed?since=' + lastSeq);

es.onopen = () => {{
  connDot.className = 'w-2 h-2 rounded-full bg-emerald-400';
  connLabel.textContent = 'live';
  connLabel.className = 'text-xs text-emerald-400';
}};

es.onmessage = (e) => {{
  try {{
    const data = JSON.parse(e.data);
    if (data.type !== 'log' && !data.seq) return;
    appendLine(data);
    lastSeq = data.seq;
  }} catch (err) {{}}
}};

es.onerror = () => {{
  connDot.className = 'w-2 h-2 rounded-full bg-red-400';
  connLabel.textContent = 'reconnecting…';
  connLabel.className = 'text-xs text-red-400';
}};

// On/Off toggle
let _enabled = {enabled_js};
let _receiptsEnabled = {receipts_enabled_js};
const _receiptsCodeEnabled = {receipts_code_enabled_js};
const toggleSwitch = document.getElementById('toggle-switch');
const toggleTrack = document.getElementById('toggle-track');
const toggleKnob = document.getElementById('toggle-knob');
const toggleLbl = document.getElementById('toggle-label');
const receiptsToggleSwitch = document.getElementById('receipts-toggle-switch');
const receiptsToggleTrack = document.getElementById('receipts-toggle-track');
const receiptsToggleKnob = document.getElementById('receipts-toggle-knob');
const receiptsToggleLbl = document.getElementById('receipts-toggle-label');

function applyToggleState(on) {{
  _enabled = on;
  toggleSwitch.checked = !!on;
  toggleLbl.textContent = on ? 'Running' : 'Paused';
  if (on) {{
    toggleLbl.className = 'text-xs font-semibold text-emerald-300';
    toggleTrack.className = 'relative inline-flex h-5 w-9 items-center rounded-full transition-colors bg-emerald-500';
    toggleKnob.className = 'inline-block h-4 w-4 transform rounded-full bg-white transition-transform translate-x-4';
  }} else {{
    toggleLbl.className = 'text-xs font-semibold text-neutral-400';
    toggleTrack.className = 'relative inline-flex h-5 w-9 items-center rounded-full transition-colors bg-neutral-600';
    toggleKnob.className = 'inline-block h-4 w-4 transform rounded-full bg-white transition-transform translate-x-1';
  }}
}}

function toggleEnabled(requested) {{
  toggleSwitch.disabled = true;
  const previous = _enabled;
  applyToggleState(!!requested);
  fetch('/toggle-enabled', {{method: 'POST'}})
    .then(r => {{
      if (!r.ok) throw new Error('toggle failed');
      return r.json();
    }})
    .then(d => {{ applyToggleState(!!d.enabled); toggleSwitch.disabled = false; }})
    .catch(() => {{
      applyToggleState(previous);
      toggleSwitch.disabled = false;
      alert('Toggle failed. Please refresh and try again.');
    }});
}}

function applyReceiptsToggleState(on) {{
  _receiptsEnabled = !!on;
  receiptsToggleSwitch.checked = !!on;
  receiptsToggleLbl.textContent = on ? 'Receipts On' : 'Receipts Off';
  if (on) {{
    receiptsToggleLbl.className = 'text-xs font-semibold text-emerald-300';
    receiptsToggleTrack.className = 'relative inline-flex h-5 w-9 items-center rounded-full transition-colors bg-emerald-500';
    receiptsToggleKnob.className = 'inline-block h-4 w-4 transform rounded-full bg-white transition-transform translate-x-4';
  }} else {{
    receiptsToggleLbl.className = 'text-xs font-semibold text-neutral-400';
    receiptsToggleTrack.className = 'relative inline-flex h-5 w-9 items-center rounded-full transition-colors bg-neutral-600';
    receiptsToggleKnob.className = 'inline-block h-4 w-4 transform rounded-full bg-white transition-transform translate-x-1';
  }}
}}

function toggleReceiptsEnabled(requested) {{
  if (!_receiptsCodeEnabled) {{
    receiptsToggleSwitch.checked = false;
    alert('Set RECEIPTS_ENABLED=true in environment first.');
    return;
  }}
  receiptsToggleSwitch.disabled = true;
  const previous = _receiptsEnabled;
  applyReceiptsToggleState(!!requested);
  fetch('/toggle-receipts-enabled', {{method: 'POST'}})
    .then(r => {{
      if (!r.ok) throw new Error('toggle failed');
      return r.json();
    }})
    .then(d => {{ applyReceiptsToggleState(!!d.enabled); receiptsToggleSwitch.disabled = false; }})
    .catch(() => {{
      applyReceiptsToggleState(previous);
      receiptsToggleSwitch.disabled = false;
      alert('Receipts toggle failed. Please refresh and try again.');
    }});
}}
</script>

</body>
</html>"""
        )

    @app.get("/feed")
    def feed_stream():
        if not session.get("logged_in"):
            return "", 401
        last_seq = int(request.args.get("since", 0))
        from flask import Response, stream_with_context
        def generate():
            yield from _feed.stream(last_seq)
        return Response(
            stream_with_context(generate()),
            content_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/poll-now")
    @require_login
    def poll_now():
        _feed.push("Manual poll triggered", "system")
        trigger_poll()
        return "", 204

    @app.post("/toggle-enabled")
    @require_login
    def toggle_enabled():
        current = get_enabled(config.admin_db_file)
        new_state = not current
        set_enabled(config.admin_db_file, new_state)
        # Wake worker immediately so pause/resume takes effect now, not after
        # the next poll timeout.
        trigger_poll()
        if new_state:
            _feed.push("System resumed from Live View toggle", "system")
        else:
            _feed.push("System paused from Live View toggle", "system")
        import flask as _flask
        return _flask.jsonify({"enabled": new_state})

    @app.post("/toggle-receipts-enabled")
    @require_login
    def toggle_receipts_enabled():
        if not config.receipts_enabled:
            import flask as _flask
            return _flask.jsonify({"enabled": False, "error": "feature_flag_disabled"}), 400
        current = get_receipts_settings(config.admin_db_file)
        new_state = not bool(current.get("enabled"))
        current["enabled"] = new_state
        set_receipts_settings(config.admin_db_file, current)
        _feed.push(
            "Receipts flow enabled from Live View toggle" if new_state else "Receipts flow paused from Live View toggle",
            "system",
        )
        trigger_poll()
        import flask as _flask
        return _flask.jsonify({"enabled": new_state})

    @app.get("/settings")
    @require_login
    def index():
        # Persist the currently used public base URL so the worker can always
        # register/renew Google watches using the same domain.
        current_base = _current_base_url()
        stored_base = str(get_json_setting(config.admin_db_file, "public_base_url", "") or "").strip()
        if current_base and current_base != stored_base:
            set_json_setting(config.admin_db_file, "public_base_url", current_base)
            trigger_watch_check()
            trigger_poll()

        creds = load_admin_credentials(config)
        active = set(get_active_calendars(config.admin_db_file, config.google_calendar_id))
        stats_selected = set(get_stats_fields(config.admin_db_file))
        target = get_sheet_target(config.admin_db_file)
        sales_target = get_sales_sheet_target(config.admin_db_file)
        sales_stats_selected = set(get_sales_stats_fields(config.admin_db_file))
        sheets_ok, sheets_msg = _sheets_status_data(config, creds, target)
        xero_ok, xero_msg, xero_tenant = _xero_status_data(config)
        google_ok = creds is not None
        google_gmail_ok = bool(
            google_ok
            and _gmail_mod.GMAIL_READONLY_SCOPE in (getattr(creds, "scopes", None) or set())
        )
        _gmail_icon = (
            '<svg class="w-3.5 h-3.5 {c}" fill="none" stroke="currentColor" viewBox="0 0 24 24">'
            '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" '
            'd="M3 8l7.89 5.26a2 2 0 002.22 0L21 8M5 19h14a2 2 0 002-2V7a2 2 0 00-2-2H5a2 2 0 '
            '00-2 2v10a2 2 0 002 2z"/></svg>'
        )
        if not google_ok:
            gmail_scan_block = (
                '<div class="mt-2 p-3 rounded-xl border border-gray-200 bg-gray-50">'
                '<div class="flex items-center gap-2 flex-wrap">'
                + _gmail_icon.format(c="text-gray-400")
                + '<span class="text-xs font-semibold text-gray-700">Gmail &mdash; invoice scanning</span>'
                '<span class="text-xs font-medium text-gray-500 bg-gray-100 px-2 py-0.5 rounded-full">Connect Google first</span>'
                '</div>'
                '<p class="text-xs text-gray-500 mt-1">Reading invoices from email uses <strong>this same Google login</strong> &mdash; there is no separate email account to connect. Connect Google above and approve the Gmail read-only permission.</p>'
                '</div>'
            )
        elif google_gmail_ok:
            gmail_scan_block = (
                '<div class="mt-2 p-3 rounded-xl border border-emerald-200 bg-emerald-50">'
                '<div class="flex items-center gap-2 flex-wrap">'
                + _gmail_icon.format(c="text-emerald-600")
                + '<span class="text-xs font-semibold text-gray-700">Gmail &mdash; invoice scanning</span>'
                '<span class="text-xs font-medium text-emerald-700 bg-emerald-50 border border-emerald-200 px-2 py-0.5 rounded-full">&#10003; Read-only access granted</span>'
                '</div>'
                '<p class="text-xs text-gray-500 mt-1">Same Google login as Calendar &amp; Sheets. The email invoice importer can read your inbox (read-only &mdash; it never marks emails as read or sends anything).</p>'
                '</div>'
            )
        else:
            gmail_scan_block = (
                '<div class="mt-2 p-3 rounded-xl border border-amber-200 bg-amber-50">'
                '<div class="flex items-center gap-2 flex-wrap">'
                + _gmail_icon.format(c="text-amber-600")
                + '<span class="text-xs font-semibold text-gray-700">Gmail &mdash; invoice scanning</span>'
                '<span class="text-xs font-medium text-amber-700 bg-amber-50 border border-amber-200 px-2 py-0.5 rounded-full">Not approved yet</span>'
                '</div>'
                '<p class="text-xs text-amber-700 mt-1">Email invoice scanning needs read-only Gmail access. This is the <strong>same Google login</strong> &mdash; no separate email account. Click <strong>Reconnect</strong> above and approve the Gmail permission.</p>'
                '</div>'
            )
        receipts_settings = get_receipts_settings(config.admin_db_file)
        cashflows_settings = get_cashflows_settings(config.admin_db_file)
        openai_settings = get_openai_settings(config.admin_db_file)
        _oa_db_ok = bool(openai_settings.get("api_key"))
        _oa_env_ok = bool((os.getenv("OPENAI_API_KEY") or "").strip())
        _oa_has_key = _oa_db_ok or _oa_env_ok
        openai_test = get_json_setting(
            config.admin_db_file,
            "openai_test_status",
            {"ok": None, "message": "Not tested yet", "tested_at": ""},
        )
        _oa_test_ok = openai_test.get("ok")
        if _oa_test_ok is True:
            openai_badge_cls = "bg-emerald-100 text-emerald-800 border border-emerald-300 font-semibold"
            openai_badge_text = "✓ Connected"
        elif _oa_test_ok is False:
            openai_badge_cls = "bg-red-50 text-red-700 border border-red-200"
            openai_badge_text = "✗ Test failed"
        elif _oa_has_key:
            openai_badge_cls = "bg-gray-100 text-gray-600 border border-gray-200"
            openai_badge_text = "Not tested"
        else:
            openai_badge_cls = "bg-amber-50 text-amber-700 border border-amber-200"
            openai_badge_text = "Not configured"
        openai_test_status_text = escape(str(openai_test.get("message") or "Not tested yet"))
        openai_tested_at = escape(str(openai_test.get("tested_at") or ""))
        _oa_details_open = _oa_test_ok is not True
        parser_test = get_json_setting(
            config.admin_db_file,
            "receipts_parser_test_status",
            {"ok": None, "message": "Not tested yet", "tested_at": ""},
        )
        cashflows_test = get_json_setting(
            config.admin_db_file,
            "cashflows_test_status",
            {"ok": None, "message": "Not tested yet", "tested_at": ""},
        )
        save_notice = session.pop("save_notice", "")
        creds_meta = get_json_setting(
            config.admin_db_file,
            "google_credentials_meta",
            {"uploaded_name": "", "stored_path": config.google_credentials_file},
        )
        seen_submitters = get_seen_submitters(config.admin_db_file)
        submitter_aliases = get_submitter_aliases(config.admin_db_file)
        cash_submitter_sheets = get_cash_submitter_sheets(config.admin_db_file)
        cash_backlog = get_cash_backlog(config.admin_db_file)
        sales_submitter_sheets = get_sales_submitter_sheets(config.admin_db_file)
        sales_backlog = get_sales_backlog(config.admin_db_file)
        calendar_sales_sheets = get_calendar_sales_sheets(config.admin_db_file)
        calendar_cash_sheets = get_calendar_cash_sheets(config.admin_db_file)
        client_id = _oauth_client_id(config)
        creds_file_exists = Path(config.google_credentials_file).exists()
        stored_xero_id, stored_xero_secret = _get_xero_creds(config)
        xero_has_creds = bool(stored_xero_id and stored_xero_secret)
        # Read once and clear (like save_notice) — shows after Connect click, gone on next refresh
        xero_pending_auth_url = session.pop("xero_auth_url", None) or "" if not xero_ok else ""
        receipt_sa_path = str(
            receipts_settings.get("google_service_account_file")
            or _default_sa_path(config.admin_db_file)
        ).strip() or _default_sa_path(config.admin_db_file)
        receipt_sa_exists = Path(receipt_sa_path).exists()
        parser_ok = parser_test.get("ok")
        parser_status_badge = (
            '<span class="text-xs font-medium text-emerald-700 bg-emerald-50 border border-emerald-200 px-2 py-0.5 rounded-full">Parser test passed</span>'
            if parser_ok is True
            else (
                '<span class="text-xs font-medium text-red-700 bg-red-50 border border-red-200 px-2 py-0.5 rounded-full">Parser test failed</span>'
                if parser_ok is False
                else '<span class="text-xs font-medium text-gray-600 bg-gray-100 border border-gray-200 px-2 py-0.5 rounded-full">Parser not tested</span>'
            )
        )
        parser_status_text = escape(str(parser_test.get("message") or "Not tested yet"))
        parser_tested_at = escape(str(parser_test.get("tested_at") or ""))

        cashflows_ok = cashflows_test.get("ok")
        cashflows_status_badge = (
            '<span class="text-xs font-medium text-emerald-700 bg-emerald-50 border border-emerald-200 px-2 py-0.5 rounded-full">Cashflows test passed</span>'
            if cashflows_ok is True
            else (
                '<span class="text-xs font-medium text-red-700 bg-red-50 border border-red-200 px-2 py-0.5 rounded-full">Cashflows test failed</span>'
                if cashflows_ok is False
                else '<span class="text-xs font-medium text-gray-600 bg-gray-100 border border-gray-200 px-2 py-0.5 rounded-full">Cashflows not tested</span>'
            )
        )
        cashflows_status_text = escape(str(cashflows_test.get("message") or "Not tested yet"))
        cashflows_tested_at = escape(str(cashflows_test.get("tested_at") or ""))

        # Fetch Xero accounts per tenant for account-mapping UI
        xero_tenant_account_data: list[dict] = []
        xero_tenant_warning = ""
        _xero_tok_raw = load_xero_token(config.xero_token_file)
        if xero_ok or _xero_tok_raw:
            try:
                _tok = load_xero_token(config.xero_token_file)
                _granted_scopes = _parse_scope_value(_tok.get("scope"))
                _missing_scopes = [s for s in REQUIRED_XERO_SCOPES if s not in _granted_scopes]
                if _missing_scopes:
                    _miss = " ".join(_missing_scopes)
                    _warn_scopes = (
                        f"Xero token missing scopes ({_miss}). "
                        "Click Reconnect Xero so account/theme lists and card payments can work."
                    )
                    xero_tenant_warning = (
                        f"{xero_tenant_warning} | {_warn_scopes}" if xero_tenant_warning else _warn_scopes
                    )
                if token_is_expired(_tok) and _tok.get("refresh_token"):
                    _cid, _csec = _get_xero_creds(config)
                    if _cid and _csec:
                        _refreshed = refresh_xero_token(_cid, _csec, _tok["refresh_token"])
                        _tok = {**_tok, **_refreshed}
                        save_xero_token(config.xero_token_file, _tok)
                _at = _tok.get("access_token", "")
                _all_conns = _get_all_xero_connections(_at)
                _acct_load_warnings: list[str] = []
                _saved_tenants = {t["tenantId"]: t for t in get_xero_tenants(config.admin_db_file)}
                for _conn in _all_conns:
                    _tid = _conn["tenantId"]
                    _tname = _conn.get("tenantName", _tid)
                    _cfg = _saved_tenants.get(_tid, {})
                    _enabled = _cfg.get("enabled", _tid == xero_tenant)
                    try:
                        # Keep settings snappy: fetch live options for the active tenant,
                        # use cached options only for inactive tenants.
                        if _enabled:
                            _rev, _bank, _themes, _load_warn = _get_tenant_acct_themes(_at, _tid)
                        else:
                            _rev, _bank, _themes, _load_warn = _get_tenant_cached_only(_tid)
                            if not (_rev or _bank or _themes):
                                _load_warn = (
                                    "Options load for the active organisation to keep settings fast. "
                                    "Set this organisation Active to load its account/theme lists."
                                )
                    except Exception:
                        _rev, _bank, _themes, _load_warn = [], [], [], "Cannot load Xero account/theme options."
                    if _load_warn:
                        _acct_load_warnings.append(f"{_tname}: {_load_warn}")
                    xero_tenant_account_data.append({
                        "tenantId": _tid,
                        "tenantName": _tname,
                        "enabled": _enabled,
                        "invoiceAccount": _cfg.get("invoiceAccount", ""),
                        "paymentAccount": _cfg.get("paymentAccount", ""),
                        "revenueAccounts": _rev,
                        "bankAccounts": _bank,
                        "brandingThemes": _themes,
                        "loadWarning": _load_warn,
                        "brandingThemeId": _cfg.get("brandingThemeId", ""),
                        "premiumThemeId": _cfg.get("premiumThemeId", ""),
                        "premiumThreshold": _cfg.get("premiumThreshold"),
                    })
                if _acct_load_warnings:
                    _warn = " | ".join(_acct_load_warnings[:3])
                    if len(_acct_load_warnings) > 3:
                        _warn += f" | +{len(_acct_load_warnings)-3} more"
                    xero_tenant_warning = (
                        f"{xero_tenant_warning} | {_warn}" if xero_tenant_warning else _warn
                    )
            except Exception as exc:
                _saved_only = get_xero_tenants(config.admin_db_file)
                for _cfg in _saved_only:
                    _tid = _cfg.get("tenantId") or ""
                    if not _tid:
                        continue
                    xero_tenant_account_data.append(
                        {
                            "tenantId": _tid,
                            "tenantName": _cfg.get("tenantName") or _tid,
                            "enabled": _cfg.get("enabled", _tid == xero_tenant),
                            "invoiceAccount": _cfg.get("invoiceAccount", ""),
                            "paymentAccount": _cfg.get("paymentAccount", ""),
                            "revenueAccounts": [],
                            "bankAccounts": [],
                            "brandingThemes": [],
                            "loadWarning": "Reconnect Xero to load live account and branding theme options.",
                            "brandingThemeId": _cfg.get("brandingThemeId", ""),
                            "premiumThemeId": _cfg.get("premiumThemeId", ""),
                            "premiumThreshold": _cfg.get("premiumThreshold"),
                        }
                    )
                xero_tenant_warning = (
                    f"Could not refresh organisation list from Xero: {str(exc).splitlines()[0][:240]}"
                )
        setup_issues: list[str] = []
        for _t in xero_tenant_account_data:
            if not _t.get("enabled", True):
                continue
            _missing: list[str] = []
            if not str(_t.get("paymentAccount", "")).strip():
                _missing.append("Payment bank account")
            if not str(_t.get("brandingThemeId", "")).strip():
                _missing.append("Invoice template")
            if _missing:
                _setup_name = str(_t.get("tenantName") or _t.get("tenantId") or "Organisation")
                setup_issues.append(f"{_setup_name}: missing {', '.join(_missing)}")
        if setup_issues:
            setup_msg = "Setup required — " + " | ".join(setup_issues[:4])
            if len(setup_issues) > 4:
                setup_msg += f" | +{len(setup_issues) - 4} more"
            xero_tenant_warning = (
                f"{xero_tenant_warning} | {setup_msg}" if xero_tenant_warning else setup_msg
            )
        pending_auth_url = (
            session.get("oauth_auth_url")
            or str(get_json_setting(config.admin_db_file, "oauth_auth_url", "")).strip()
        ) if not google_ok else ""

        calendars = []
        spreadsheets = []
        calendar_error = ""
        if creds:
            try:
                calendars = list_calendars(creds)
            except Exception as exc:
                calendar_error = f"Failed to load calendars: {exc}"
            try:
                spreadsheets = list_spreadsheets(creds)
            except Exception:
                pass

        # --- Notice banner ---
        notice_html = ""
        if save_notice:
            is_error = save_notice.startswith("error:")
            msg = save_notice[6:] if save_notice.startswith(("error:", "success:")) else save_notice
            if is_error:
                notice_html = f"""
                <div class="mb-6 flex items-start gap-3 p-4 bg-red-50 border border-red-200 rounded-xl text-red-800 text-sm">
                  <svg class="w-5 h-5 mt-0.5 shrink-0" fill="currentColor" viewBox="0 0 20 20">
                    <path fill-rule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zM8.707 7.293a1 1 0 00-1.414 1.414L8.586 10l-1.293 1.293a1 1 0 101.414 1.414L10 11.414l1.293 1.293a1 1 0 001.414-1.414L11.414 10l1.293-1.293a1 1 0 00-1.414-1.414L10 8.586 8.707 7.293z" clip-rule="evenodd"/>
                  </svg>
                  <span>{escape(msg)}</span>
                </div>"""
            else:
                notice_html = f"""
                <div class="mb-6 flex items-start gap-3 p-4 bg-green-50 border border-green-200 rounded-xl text-green-800 text-sm">
                  <svg class="w-5 h-5 mt-0.5 shrink-0" fill="currentColor" viewBox="0 0 20 20">
                    <path fill-rule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zm3.707-9.293a1 1 0 00-1.414-1.414L9 10.586 7.707 9.293a1 1 0 00-1.414 1.414l2 2a1 1 0 001.414 0l4-4z" clip-rule="evenodd"/>
                  </svg>
                  <span>{escape(msg)}</span>
                </div>"""

        # --- Calendars ---
        my_cal_ids: set[str] = set()
        if calendars:
            my_rows = []
            other_rows = []
            for c in calendars:
                cid = c["id"] or ""
                checked = "checked" if (cid in active or (c.get("primary") and "primary" in active)) else ""
                title = escape(c.get("summary_display") or c.get("summary") or cid)
                primary_badge = ' <span class="text-xs bg-indigo-100 text-indigo-700 px-1.5 py-0.5 rounded font-medium">Primary</span>' if c.get("primary") else ""
                hints = []
                if c.get("hidden"):
                    hints.append("hidden")
                if not c.get("selected", True):
                    hints.append("not selected")
                hint = f' <span class="text-gray-400 text-xs">({"; ".join(hints)})</span>' if hints else ""
                row = (
                    f'<label class="flex items-center gap-3 p-3 rounded-lg hover:bg-gray-50 cursor-pointer">'
                    f'<input type="checkbox" name="active_calendars" value="{escape(cid)}" {checked} '
                    f'class="w-4 h-4 text-indigo-600 rounded border-gray-300 focus:ring-indigo-500">'
                    f'<span class="text-sm text-gray-800">{title}{primary_badge}{hint}</span>'
                    f'</label>'
                )
                is_my = (
                    c.get("primary") or c.get("is_birthdays")
                    or (c.get("access_role") in {"owner", "writer"} and not c.get("is_holiday"))
                )
                if is_my:
                    my_rows.append(row)
                    if cid:
                        my_cal_ids.add(cid)
                else:
                    other_rows.append(row)

            cal_html = ""
            if my_rows:
                cal_html += '<p class="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-2">My Calendars</p>'
                cal_html += "".join(my_rows)
            if other_rows:
                if my_rows:
                    cal_html += '<div class="my-3 border-t border-gray-100"></div>'
                cal_html += '<p class="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-2">Other Calendars</p>'
                cal_html += "".join(other_rows)
        elif calendar_error:
            cal_html = f'<p class="text-sm text-red-600">{escape(calendar_error)}</p>'
        else:
            cal_html = '<p class="text-sm text-gray-500">No calendars loaded. Connect Google first.</p>'

        # --- Calendar routing UI ---
        cal_id_to_name: dict[str, str] = {
            c["id"]: (c.get("summary_display") or c.get("summary") or c["id"])
            for c in calendars
            if c.get("id")
        }
        # Also include any active calendar IDs that may not be in the fetched list
        for cid in active:
            if cid not in cal_id_to_name:
                cal_id_to_name[cid] = cid

        sales_backlog_by_cal: dict[str, int] = {}
        for row in sales_backlog:
            cid = str(row.get("calendar_id", "")).strip()
            if cid:
                sales_backlog_by_cal[cid] = sales_backlog_by_cal.get(cid, 0) + 1

        cash_backlog_by_cal: dict[str, int] = {}
        for row in cash_backlog:
            cid = str(row.get("calendar_id", "")).strip()
            if cid:
                cash_backlog_by_cal[cid] = cash_backlog_by_cal.get(cid, 0) + 1

        # Show all "My Calendars" + active + any previously configured — so unticking never loses config
        configured_cal_ids = set(calendar_sales_sheets.keys()) | set(calendar_cash_sheets.keys())
        all_routing_cals = sorted(my_cal_ids | active | configured_cal_ids)

        # Build per-calendar dropdown HTML — one block for sales, one for cash
        def _cal_dropdown_html(prefix_select: str, prefix_panel: str, kind: str) -> str:
            """Return a dropdown + hidden panels for per-calendar sheet routing."""
            no_cal_msg = '<p class="text-sm text-gray-500">No active calendars found. Tick calendars in Active Calendars and save first.</p>'
            cals = [c for c in all_routing_cals if c in active or c in configured_cal_ids]
            if not cals:
                return no_cal_msg
            is_sales = kind == "sales"
            mapping = calendar_sales_sheets if is_sales else calendar_cash_sheets
            backlog_by_cal = sales_backlog_by_cal if is_sales else cash_backlog_by_cal
            default_tab = "Sales" if is_sales else "Cash"
            default_cid = next(
                (c for c in cals if c in mapping),
                next((c for c in cals if c in active), cals[0]),
            )
            # Dropdown
            opts = ""
            for cid in cals:
                label = cal_id_to_name.get(cid, cid)
                status = "● " if cid in active else "○ "
                opts += f'<option value="{escape(cid)}"{" selected" if cid == default_cid else ""}>{escape(status + label)}</option>'
            # Panels
            panels = ""
            for cid in cals:
                mapped = mapping.get(cid, {})
                sid = escape(mapped.get("spreadsheet_id", ""))
                sname = escape(mapped.get("sheet_name", default_tab))
                pending = backlog_by_cal.get(cid, 0)
                pending_badge = (
                    f'<span class="text-xs text-amber-700 bg-amber-50 border border-amber-200 px-2 py-0.5 rounded-full ml-2">{pending} pending</span>'
                    if pending else ""
                )
                display = "block" if cid == default_cid else "none"
                panels += (
                    f'<div id="{prefix_panel}{escape(cid)}" style="display:{display}" class="space-y-3 pt-1">'
                    f'<p class="text-xs text-gray-500">Spreadsheet URL or ID {pending_badge}</p>'
                    f'<input name="cal_{kind}_sheet_id__{escape(cid)}" value="{sid}" '
                    f'placeholder="Paste a Google Sheets URL or spreadsheet ID" '
                    f'class="w-full px-3 py-1.5 border border-gray-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500">'
                    f'<p class="text-xs text-gray-500 mt-2">Tab name</p>'
                    f'<input name="cal_{kind}_sheet_name__{escape(cid)}" value="{sname}" '
                    f'placeholder="{default_tab}" '
                    f'class="w-48 px-3 py-1.5 border border-gray-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500">'
                    f'</div>'
                )
            js = (
                f'<script>'
                f'(function(){{'
                f'function _show_{kind}(cid){{'
                f'document.querySelectorAll("[id^=\'{prefix_panel}\']").forEach(function(el){{el.style.display="none";}});'
                f'var p=document.getElementById("{prefix_panel}"+cid);'
                f'if(p)p.style.display="block";'
                f'}}'
                f'var sel=document.getElementById("{prefix_select}");'
                f'if(sel)sel.addEventListener("change",function(){{_show_{kind}(this.value);}});'
                f'}})();'
                f'</script>'
            )
            return (
                f'<div>'
                f'<label class="block text-sm font-medium text-gray-700 mb-1.5">Calendar / Person</label>'
                f'<select id="{prefix_select}" '
                f'class="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500">'
                f'{opts}'
                f'</select>'
                f'</div>'
                f'{panels}'
                f'{js}'
            )

        cal_sales_routing_html = _cal_dropdown_html("cal_sales_select", "cal_sp__", "sales")
        cal_cash_routing_html = _cal_dropdown_html("cal_cash_select", "cal_cp__", "cash")

        # --- Stats fields ---
        stats_html = ""
        for key, label in STAT_OPTIONS:
            checked = "checked" if key in stats_selected else ""
            stats_html += (
                f'<label class="flex items-center gap-3 p-2.5 rounded-lg hover:bg-gray-50 cursor-pointer">'
                f'<input type="checkbox" name="stats_fields" value="{key}" {checked} '
                f'class="w-4 h-4 text-indigo-600 rounded border-gray-300 focus:ring-indigo-500">'
                f'<span class="text-sm text-gray-800">{escape(label)}</span>'
                f'</label>'
            )
        sales_stats_html = ""
        for key, label in SALES_STAT_OPTIONS:
            checked = "checked" if key in sales_stats_selected else ""
            sales_stats_html += (
                f'<label class="flex items-center gap-3 p-2 rounded-lg hover:bg-gray-50 cursor-pointer">'
                f'<input type="checkbox" name="sales_stats_fields" value="{key}" {checked} '
                f'class="w-4 h-4 text-indigo-600 rounded border-gray-300 focus:ring-indigo-500">'
                f'<span class="text-sm text-gray-800">{escape(label)}</span>'
                f'</label>'
            )

        # --- Submitter aliases ---
        alias_rows_html = ""
        for email in seen_submitters:
            alias = submitter_aliases.get(email, "")
            alias_rows_html += (
                f'<div class="flex items-center gap-3 py-2">'
                f'<span class="text-sm text-gray-600 w-64 truncate" title="{escape(email)}">{escape(email)}</span>'
                f'<input name="submitter_alias__{escape(email)}" value="{escape(alias)}" '
                f'placeholder="Display name" '
                f'class="flex-1 px-3 py-1.5 border border-gray-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500">'
                f'</div>'
            )
        if not alias_rows_html:
            alias_rows_html = '<p class="text-sm text-gray-500">No submitters seen yet — process some events first.</p>'

        sales_backlog_counts: dict[str, int] = {}
        for row in sales_backlog:
            email = str(row.get("submitter_email", "")).strip().lower()
            if not email:
                continue
            sales_backlog_counts[email] = sales_backlog_counts.get(email, 0) + 1

        sales_rows_html = ""
        for email in seen_submitters:
            mapped = sales_submitter_sheets.get(email, {})
            sid = mapped.get("spreadsheet_id", "")
            sname = mapped.get("sheet_name", "Sales")
            pending = sales_backlog_counts.get(email, 0)
            pending_badge = (
                f'<span class="text-xs text-amber-700 bg-amber-50 border border-amber-200 px-2 py-0.5 rounded-full">{pending} pending</span>'
                if pending
                else '<span class="text-xs text-gray-400">No backlog</span>'
            )
            sales_rows_html += (
                f'<div class="py-3 border-b border-gray-100 last:border-b-0">'
                f'<div class="flex items-center justify-between gap-3 mb-2">'
                f'<span class="text-sm text-gray-700 truncate" title="{escape(email)}">{escape(email)}</span>'
                f'{pending_badge}'
                f'</div>'
                f'<div class="grid grid-cols-1 sm:grid-cols-3 gap-2">'
                f'<input name="sales_sheet_id__{escape(email)}" value="{escape(sid)}" '
                f'placeholder="Spreadsheet URL or ID" '
                f'class="sm:col-span-2 px-3 py-1.5 border border-gray-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500">'
                f'<input name="sales_sheet_name__{escape(email)}" value="{escape(sname)}" '
                f'placeholder="Sheet tab (e.g. Sales)" '
                f'class="px-3 py-1.5 border border-gray-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500">'
                f'</div>'
                f'</div>'
            )
        if not sales_rows_html:
            sales_rows_html = '<p class="text-sm text-gray-500">No submitters seen yet — process some events first.</p>'

        cash_backlog_counts: dict[str, int] = {}
        for row in cash_backlog:
            email = str(row.get("submitter_email", "")).strip().lower()
            if not email:
                continue
            cash_backlog_counts[email] = cash_backlog_counts.get(email, 0) + 1

        cash_rows_html = ""
        for email in seen_submitters:
            mapped = cash_submitter_sheets.get(email, {})
            sid = mapped.get("spreadsheet_id", "")
            sname = mapped.get("sheet_name", "Sheet1")
            pending = cash_backlog_counts.get(email, 0)
            pending_badge = (
                f'<span class="text-xs text-amber-700 bg-amber-50 border border-amber-200 px-2 py-0.5 rounded-full">{pending} pending</span>'
                if pending
                else '<span class="text-xs text-gray-400">No backlog</span>'
            )
            cash_rows_html += (
                f'<div class="py-3 border-b border-gray-100 last:border-b-0">'
                f'<div class="flex items-center justify-between gap-3 mb-2">'
                f'<span class="text-sm text-gray-700 truncate" title="{escape(email)}">{escape(email)}</span>'
                f'{pending_badge}'
                f'</div>'
                f'<div class="grid grid-cols-1 sm:grid-cols-3 gap-2">'
                f'<input name="cash_sheet_id__{escape(email)}" value="{escape(sid)}" '
                f'placeholder="Spreadsheet URL or ID" '
                f'class="sm:col-span-2 px-3 py-1.5 border border-gray-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500">'
                f'<input name="cash_sheet_name__{escape(email)}" value="{escape(sname)}" '
                f'placeholder="Sheet tab (e.g. Cash)" '
                f'class="px-3 py-1.5 border border-gray-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500">'
                f'</div>'
                f'</div>'
            )
        if not cash_rows_html:
            cash_rows_html = '<p class="text-sm text-gray-500">No submitters seen yet — process some events first.</p>'

        # --- Spreadsheet options ---
        sheet_options = '<option value="">-- Select a spreadsheet --</option>'
        sheet_current_id = target.get("spreadsheet_id", "")
        sheet_name_val = target.get("sheet_name", "Sheet1")
        sales_sheet_current_id = sales_target.get("spreadsheet_id", "")
        sales_sheet_name_val = sales_target.get("sheet_name", "Sales")
        cash_target = get_cash_sheet_target(config.admin_db_file)
        cash_sheet_current_id = cash_target.get("spreadsheet_id", "")
        cash_sheet_name_val = cash_target.get("sheet_name", "Cash")
        for s in spreadsheets:
            sel = 'selected' if s.get("id") == sheet_current_id else ""
            sheet_options += f'<option value="{escape(s["id"])}" {sel}>{escape(s["name"])}</option>'
        if sheet_current_id and all(s.get("id") != sheet_current_id for s in spreadsheets):
            sheet_options += f'<option value="{escape(sheet_current_id)}" selected>Current saved ({escape(sheet_current_id)})</option>'
        sales_sheet_options = '<option value="">-- Select a spreadsheet --</option>'
        for s in spreadsheets:
            sel = 'selected' if s.get("id") == sales_sheet_current_id else ""
            sales_sheet_options += f'<option value="{escape(s["id"])}" {sel}>{escape(s["name"])}</option>'
        if sales_sheet_current_id and all(s.get("id") != sales_sheet_current_id for s in spreadsheets):
            sales_sheet_options += f'<option value="{escape(sales_sheet_current_id)}" selected>Current saved ({escape(sales_sheet_current_id)})</option>'
        cash_sheet_options = '<option value="">-- Select a spreadsheet --</option>'
        for s in spreadsheets:
            sel = 'selected' if s.get("id") == cash_sheet_current_id else ""
            cash_sheet_options += f'<option value="{escape(s["id"])}" {sel}>{escape(s["name"])}</option>'
        if cash_sheet_current_id and all(s.get("id") != cash_sheet_current_id for s in spreadsheets):
            cash_sheet_options += f'<option value="{escape(cash_sheet_current_id)}" selected>Current saved ({escape(cash_sheet_current_id)})</option>'

        # --- Webhook state ---
        watches = get_google_watches(config.admin_db_file)
        xero_wh_key = get_xero_webhook_key(config.admin_db_file)
        xero_wh_verified = get_xero_webhook_verified(config.admin_db_file)
        base_url = request.host_url.rstrip("/")
        # Replit proxies HTTPS → HTTP internally; always show the public HTTPS URL
        base_url = base_url.replace("http://", "https://", 1)
        gcal_webhook_url = f"{base_url}/webhooks/google-calendar"
        xero_webhook_url = f"{base_url}/webhooks/xero"

        import time as _time
        now_ms = int(_time.time() * 1000)
        active_cals_for_wh = get_active_calendars(config.admin_db_file, config.google_calendar_id)
        watch_rows_html = ""
        for cal_id in active_cals_for_wh:
            winfo = watches.get(cal_id)
            if winfo:
                exp_ms = int(winfo.get("expiration_ms") or 0)
                if exp_ms > now_ms:
                    exp_dt = dt.datetime.fromtimestamp(exp_ms / 1000, tz=dt.timezone.utc)
                    exp_str = exp_dt.strftime("%d/%m/%Y %H:%M UTC")
                    badge = f'<span class="text-xs font-medium text-emerald-700 bg-emerald-50 border border-emerald-200 px-2 py-0.5 rounded-full">Active — expires {escape(exp_str)}</span>'
                else:
                    badge = '<span class="text-xs font-medium text-amber-700 bg-amber-50 border border-amber-200 px-2 py-0.5 rounded-full">Expired — re-register</span>'
            else:
                badge = '<span class="text-xs font-medium text-gray-500 bg-gray-100 px-2 py-0.5 rounded-full">Not registered</span>'
            cal_name = next((c.get("summary_display") or c.get("summary") or cal_id for c in calendars if c.get("id") == cal_id), cal_id)
            watch_rows_html += f'<div class="flex items-center justify-between py-2"><span class="text-sm text-gray-700 truncate">{escape(cal_name)}</span>{badge}</div>'
        if not watch_rows_html:
            watch_rows_html = '<p class="text-sm text-gray-400">No active calendars selected yet.</p>'

        # Google Calendar watch badge
        if watches and active_cals_for_wh and all(
            int((watches.get(c) or {}).get("expiration_ms") or 0) > now_ms
            for c in active_cals_for_wh if watches.get(c)
        ) and all(watches.get(c) for c in active_cals_for_wh):
            gcal_watch_badge = '<span class="text-xs font-medium text-emerald-700 bg-emerald-50 border border-emerald-200 px-2 py-0.5 rounded-full">&#10003; Registered</span>'
        elif watches:
            gcal_watch_badge = '<span class="text-xs font-medium text-amber-700 bg-amber-50 border border-amber-200 px-2 py-0.5 rounded-full">Some watches expired</span>'
        else:
            gcal_watch_badge = '<span class="text-xs font-medium text-gray-500 bg-gray-100 px-2 py-0.5 rounded-full">Polling only</span>'

        # --- Deployment URLs (always computed from live request so they auto-update with domain) ---
        _req_base = _current_base_url()
        google_redirect = escape(_req_base + "/oauth/callback")
        xero_redirect = escape(_req_base + "/xero/callback")

        # --- Pending Google auth URL block ---
        if pending_auth_url:
            _esc_url = escape(pending_auth_url)
            _js_url = pending_auth_url.replace("'", "\\'")
            _preview = escape(pending_auth_url[:72]) + "..."
            pending_auth_url_html = (
                f'<div class="mt-3 p-3 bg-blue-50 border border-blue-200 rounded-xl">'
                f'<p class="text-xs font-semibold text-blue-800 mb-1">&#128279; Open this link in a new tab to authorise Google:</p>'
                f'<a href="{_esc_url}" target="_blank" class="block text-xs text-blue-700 underline break-all hover:text-blue-900 mb-2">{_preview}</a>'
                f'<button type="button" onclick="navigator.clipboard.writeText(\'{_js_url}\');this.textContent=\'Copied!\';setTimeout(()=>this.textContent=\'Copy link\',2000)" '
                f'class="px-2 py-1 text-xs font-medium bg-blue-600 text-white rounded hover:bg-blue-700 transition-colors">Copy link</button>'
                f'<p class="text-xs text-blue-600 mt-1.5">After approving in Google, come back here — the status will update automatically.</p>'
                f'</div>'
            )
        else:
            pending_auth_url_html = ""

        # --- Pending Xero auth URL block ---
        if xero_pending_auth_url:
            _xesc = escape(xero_pending_auth_url)
            _xjs = xero_pending_auth_url.replace("'", "\\'")
            _xprev = escape(xero_pending_auth_url[:72]) + "..."
            xero_pending_auth_url_html = (
                f'<div class="mt-3 p-3 bg-blue-50 border border-blue-200 rounded-xl">'
                f'<p class="text-xs font-semibold text-blue-800 mb-1">&#128279; Open this link in a new tab to authorise Xero:</p>'
                f'<a href="{_xesc}" target="_blank" class="block text-xs text-blue-700 underline break-all hover:text-blue-900 mb-2">{_xprev}</a>'
                f'<button type="button" onclick="navigator.clipboard.writeText(\'{_xjs}\');this.textContent=\'Copied!\';setTimeout(()=>this.textContent=\'Copy link\',2000)" '
                f'class="px-2 py-1 text-xs font-medium bg-blue-600 text-white rounded hover:bg-blue-700 transition-colors">Copy link</button>'
                f'<p class="text-xs text-blue-600 mt-1.5">After approving in Xero, come back here — the status will update automatically.</p>'
                f'</div>'
            )
        else:
            xero_pending_auth_url_html = ""

        return _page(f"""
        <div class="max-w-4xl mx-auto px-4 py-8">

          <!-- Header -->
          <div class="flex items-center justify-between mb-8">
            <div>
              <h1 class="text-2xl font-bold text-gray-900">Powwash Integration</h1>
              <p class="text-gray-500 text-sm mt-0.5">Google Calendar → Xero invoice automation</p>
            </div>
          </div>

          {notice_html}

          <!-- Deployment URLs Panel -->
          <details class="bg-white border border-gray-200 rounded-2xl shadow-sm mb-6 overflow-hidden group">
            <summary class="flex items-center justify-between px-5 py-3 border-b border-gray-100 bg-gray-50 cursor-pointer list-none select-none hover:bg-gray-100 transition-colors">
              <div class="flex items-center gap-2">
                <svg class="w-4 h-4 text-indigo-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13.828 10.172a4 4 0 00-5.656 0l-4 4a4 4 0 105.656 5.656l1.102-1.101m-.758-4.899a4 4 0 005.656 0l4-4a4 4 0 00-5.656-5.656l-1.1 1.1"/>
                </svg>
                <span class="text-sm font-semibold text-gray-800">Deployment URLs</span>
                <span class="text-xs text-gray-400 font-normal ml-1">— tap to expand</span>
              </div>
              <div class="flex items-center gap-2">
                <span class="text-xs font-mono text-indigo-600 truncate max-w-xs hidden sm:block">{escape(_req_base)}</span>
                <svg class="w-4 h-4 text-gray-400 transition-transform duration-200 group-open:rotate-180" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"/>
                </svg>
              </div>
            </summary>
            <div class="divide-y divide-gray-100">
              {_url_row("Google OAuth redirect URI", _req_base + "/oauth/callback", "Register in Google Cloud Console → Credentials → OAuth client → Authorised redirect URIs")}
              {_url_row("Xero OAuth redirect URI", _req_base + "/xero/callback", "Register in Xero Developer Portal → Your App → Redirect URIs")}
              {_url_row("Google Calendar webhook", _req_base + "/webhooks/google-calendar", "Auto-managed for active calendars (created and renewed automatically)")}
              {_url_row("Xero webhook", _req_base + "/webhooks/xero", "Paste into Xero Developer Portal → Your App → Webhooks")}
            </div>
          </details>

          <!-- Setup Steps Overview (collapsible) -->
          <details class="bg-indigo-50 border border-indigo-100 rounded-2xl mb-6 group">
            <summary class="flex items-center justify-between cursor-pointer px-6 py-4 list-none select-none">
              <h2 class="text-sm font-semibold text-indigo-900 flex items-center gap-2">
                <svg class="w-4 h-4" fill="currentColor" viewBox="0 0 20 20">
                  <path fill-rule="evenodd" d="M18 10a8 8 0 11-16 0 8 8 0 0116 0zm-7-4a1 1 0 11-2 0 1 1 0 012 0zM9 9a1 1 0 000 2v3a1 1 0 001 1h1a1 1 0 100-2v-3a1 1 0 00-1-1H9z" clip-rule="evenodd"/>
                </svg>
                Quick Setup Guide
              </h2>
              <svg class="w-4 h-4 text-indigo-400 transition-transform duration-200 group-open:rotate-180" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"/>
              </svg>
            </summary>
            <div class="px-6 pb-5">
              <ol class="space-y-2 text-sm text-indigo-800">
                <li class="flex gap-2"><span class="font-bold shrink-0">1.</span>
                  <span>Create a <a href="https://console.cloud.google.com/apis/credentials" target="_blank" class="underline font-medium hover:text-indigo-600">Google Cloud OAuth 2.0 client</a> (type: <em>Web application</em>), add <code class="bg-indigo-100 px-1 rounded text-xs">{google_redirect}</code> as an authorised redirect URI, then download the JSON and upload it in the Google section below.</span>
                </li>
                <li class="flex gap-2"><span class="font-bold shrink-0">2.</span>
                  <span>Enable the <a href="https://console.cloud.google.com/apis/library/calendar-json.googleapis.com" target="_blank" class="underline font-medium hover:text-indigo-600">Google Calendar API</a> and <a href="https://console.cloud.google.com/apis/library/sheets.googleapis.com" target="_blank" class="underline font-medium hover:text-indigo-600">Google Sheets API</a> in your Google Cloud project.</span>
                </li>
                <li class="flex gap-2"><span class="font-bold shrink-0">3.</span>
                  <span>Create a <a href="https://developer.xero.com/app/manage" target="_blank" class="underline font-medium hover:text-indigo-600">Xero app</a> (type: <em>Web app</em>), set the redirect URI to <code class="bg-indigo-100 px-1 rounded text-xs">{xero_redirect}</code>, and add <code class="bg-indigo-100 px-1 rounded text-xs">XERO_CLIENT_ID</code> / <code class="bg-indigo-100 px-1 rounded text-xs">XERO_CLIENT_SECRET</code> as environment variables.</span>
                </li>
                <li class="flex gap-2"><span class="font-bold shrink-0">4.</span>
                  <span>Click <strong>Connect Google</strong> and <strong>Connect Xero</strong> below, then select your calendars and save.</span>
                </li>
              </ol>
            </div>
          </details>

          <!-- Connection Status Row -->
          <form method="post" action="/save" enctype="multipart/form-data" id="conn-form">
            <div class="grid grid-cols-1 sm:grid-cols-2 gap-4">

              <!-- Google Card -->
              <details id="google" class="bg-white rounded-2xl shadow-sm border border-gray-200 group {'border-green-200' if google_ok else ''}">
                <summary class="flex items-center justify-between p-5 cursor-pointer list-none select-none hover:bg-gray-50 rounded-2xl transition-colors">
                  <div class="flex items-center gap-3">
                    <div class="w-10 h-10 bg-blue-50 rounded-xl flex items-center justify-center shrink-0">
                      <svg class="w-5 h-5 text-blue-600" viewBox="0 0 24 24" fill="currentColor">
                        <path d="M12.545 10.239v3.821h5.445c-.712 2.315-2.647 3.972-5.445 3.972a6.033 6.033 0 110-12.064c1.498 0 2.866.549 3.921 1.453l2.814-2.814A9.969 9.969 0 0012.545 2C7.021 2 2.543 6.477 2.543 12s4.478 10 10.002 10c8.396 0 10.249-7.85 9.426-11.748l-9.426-.013z"/>
                      </svg>
                    </div>
                    <div>
                      <h3 class="font-semibold text-gray-900 text-sm">Google</h3>
                      <p class="text-xs text-gray-500">Calendar · Sheets · Gmail</p>
                    </div>
                  </div>
                  <div class="flex items-center gap-2">
                    {_status_badge(google_ok, "Connected" if google_ok else "Not connected")}
                    <svg class="w-4 h-4 text-gray-400 transition-transform duration-200 group-open:rotate-180 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"/>
                    </svg>
                  </div>
                </summary>

                <div class="px-5 pb-5 border-t border-gray-100 pt-4 space-y-3">
                  {"" if not client_id else f'<p class="text-xs text-gray-400 mb-2 font-mono truncate" title="{escape(client_id)}">Client: {escape(client_id[:40])}{"..." if len(client_id) > 40 else ""}</p>'}
                  <div>
                    <p class="text-xs text-gray-500 mb-1.5 font-medium">Upload OAuth credentials JSON</p>
                    <input type="file" name="google_credentials" accept=".json,application/json"
                      id="gc-creds-input"
                      class="hidden"
                      onchange="if(this.files.length){{document.getElementById('gc-creds-lbl').textContent=this.files[0].name;var f=document.getElementById('conn-form');f.action='/upload-google-credentials';f.submit();}}">
                    {"" if not (creds_meta or {}).get("uploaded_name") else f'<p class="text-xs text-gray-400 mt-1">Last upload: {escape((creds_meta or {{}}).get("uploaded_name", ""))}</p>'}
                  </div>
                  <div>
                    {"" if creds_file_exists else '<p class="text-xs text-amber-700 bg-amber-50 border border-amber-200 rounded-lg px-3 py-2">No credentials file uploaded yet. Select your JSON file and click <strong>Upload JSON</strong> first.</p>'}
                    {"" if not creds_file_exists else '<p class="text-xs text-green-700 bg-green-50 border border-green-200 rounded-lg px-3 py-2">&#10003; Credentials file uploaded. Click <strong>Connect Google</strong> to authorise.</p>'}
                  </div>
                  {pending_auth_url_html}
                  <div class="flex gap-2 flex-wrap items-center pt-1">
                    <button type="button"
                      onclick="document.getElementById('gc-creds-input').click()"
                      class="px-3 py-1.5 text-xs font-medium text-gray-700 bg-gray-100 hover:bg-gray-200 rounded-lg transition-colors">
                      Upload JSON
                    </button>
                    <span id="gc-creds-lbl" class="text-xs text-gray-400 italic truncate max-w-xs"></span>
                    <a href="/connect-google"
                      class="px-3 py-1.5 text-xs font-medium text-white {"bg-blue-600 hover:bg-blue-700" if creds_file_exists else "bg-gray-300 cursor-not-allowed"} rounded-lg transition-colors">
                      {"Reconnect" if google_ok else ("New link" if pending_auth_url else "Connect Google")}
                    </a>
                  </div>

                  {gmail_scan_block}

                  <!-- Calendar Push Notifications (nested) -->
                  <details class="mt-2 border border-gray-100 rounded-xl overflow-hidden group/gcal">
                    <summary class="flex items-center justify-between px-4 py-3 bg-gray-50 cursor-pointer list-none select-none hover:bg-gray-100 transition-colors">
                      <div class="flex items-center gap-2">
                        <svg class="w-3.5 h-3.5 text-blue-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 17h5l-1.405-1.405A2.032 2.032 0 0118 14.158V11a6.002 6.002 0 00-4-5.659V5a2 2 0 10-4 0v.341C7.67 6.165 6 8.388 6 11v3.159c0 .538-.214 1.055-.595 1.436L4 17h5m6 0v1a3 3 0 11-6 0v-1m6 0H9"/>
                        </svg>
                        <span class="text-xs font-semibold text-gray-700">Calendar push notifications</span>
                      </div>
                      <div class="flex items-center gap-2">
                        {gcal_watch_badge}
                        <svg class="w-3.5 h-3.5 text-gray-400 transition-transform duration-200 group-open/gcal:rotate-180 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"/>
                        </svg>
                      </div>
                    </summary>
                    <div class="px-4 py-3 space-y-3">
                      <p class="text-xs text-gray-500">Instant notifications when calendar events change. Watches are auto-created and auto-renewed for active calendars.</p>
                      <div>
                        <label class="block text-xs font-medium text-gray-600 mb-1">Webhook URL <span class="text-gray-400 font-normal">(set automatically)</span></label>
                        <div class="flex gap-2">
                          <input readonly value="{escape(gcal_webhook_url)}"
                            class="flex-1 px-3 py-1.5 border border-gray-200 rounded-lg text-xs font-mono bg-gray-50 text-gray-600">
                          <button type="button"
                            onclick="navigator.clipboard.writeText('{gcal_webhook_url.replace(chr(39), chr(92)+chr(39))}');this.textContent='Copied!';setTimeout(()=>this.textContent='Copy',2000)"
                            class="px-3 py-1.5 text-xs font-medium bg-gray-100 hover:bg-gray-200 text-gray-700 rounded-lg transition-colors">Copy</button>
                        </div>
                      </div>
                      <div class="divide-y divide-gray-50">
                        {watch_rows_html}
                      </div>
                      <p class="text-xs text-gray-500">No manual step needed. Ticking or unticking calendars in <strong>Active Calendars</strong> applies automatically.</p>
                    </div>
                  </details>
                </div>
              </details>

              <!-- Xero Card -->
              <details id="xero" class="bg-white rounded-2xl shadow-sm border border-gray-200 group">
                <summary class="flex items-center justify-between p-5 cursor-pointer list-none select-none hover:bg-gray-50 rounded-2xl transition-colors">
                  <div class="flex items-center gap-3">
                    <div class="w-10 h-10 bg-blue-50 rounded-xl flex items-center justify-center shrink-0">
                      <svg class="w-5 h-5 text-blue-700" viewBox="0 0 24 24" fill="currentColor">
                        <path d="M12 2C6.477 2 2 6.477 2 12s4.477 10 10 10 10-4.477 10-10S17.523 2 12 2zm4.5 13.5h-9v-1.5h9v1.5zm0-3h-9V11h9v1.5zm0-3h-9V8h9v1.5z"/>
                      </svg>
                    </div>
                    <div>
                      <h3 class="font-semibold text-gray-900 text-sm">Xero</h3>
                      <p class="text-xs text-gray-500">Invoice automation</p>
                    </div>
                  </div>
                  <div class="flex items-center gap-2">
                    {_status_badge(xero_ok, "Connected" if xero_ok else "Not connected")}
                    <svg class="w-4 h-4 text-gray-400 transition-transform duration-200 group-open:rotate-180 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"/>
                    </svg>
                  </div>
                </summary>

                <div class="px-5 pb-5 border-t border-gray-100 pt-4 space-y-3">
                  <div class="text-xs text-gray-500 space-y-0.5">
                    <p>{escape(xero_msg)}</p>
                    {"" if not xero_tenant else f'<p class="font-mono text-gray-400 truncate" title="{escape(xero_tenant)}">Tenant: {escape(xero_tenant[:32])}{"..." if len(xero_tenant) > 32 else ""}</p>'}
                    <p>Redirect URI: <code class="bg-gray-100 px-1 py-0.5 rounded text-xs">{xero_redirect}</code></p>
                  </div>
                  <div class="space-y-2">
                    <div>
                      <label class="block text-xs font-medium text-gray-600 mb-1">Client ID</label>
                      <input name="xero_client_id" value="{escape(stored_xero_id)}"
                        placeholder="Paste your Xero Client ID"
                        class="w-full px-3 py-1.5 border border-gray-300 rounded-lg text-xs focus:outline-none focus:ring-2 focus:ring-blue-500 font-mono">
                    </div>
                    <div>
                      <label class="block text-xs font-medium text-gray-600 mb-1">Client Secret</label>
                      <input name="xero_client_secret" type="password"
                        placeholder="{"••••••••  (saved)" if stored_xero_secret else "Paste your Xero Client Secret"}"
                        class="w-full px-3 py-1.5 border border-gray-300 rounded-lg text-xs focus:outline-none focus:ring-2 focus:ring-blue-500 font-mono">
                    </div>
                    <button type="submit" formaction="/save-xero-creds"
                      class="px-3 py-1.5 text-xs font-medium text-gray-700 bg-gray-100 hover:bg-gray-200 rounded-lg transition-colors">
                      Save Credentials
                    </button>
                  </div>
                  {xero_pending_auth_url_html}
                  <div class="flex flex-wrap gap-2">
                    <button type="button" {"disabled" if not xero_has_creds else ""}
                      onclick="fetchXeroUrl('/connect-xero?mode=json')"
                      class="mt-1 px-3 py-1.5 text-xs font-medium text-white bg-blue-700 hover:bg-blue-800 rounded-lg transition-colors disabled:opacity-50 disabled:cursor-not-allowed">
                      {"Reconnect Xero" if xero_ok else "Connect Xero"}
                    </button>
                    <button type="button" {"disabled" if not xero_has_creds else ""}
                      onclick="fetchXeroUrl('/connect-xero?choose_orgs=1&mode=json')"
                      class="mt-1 px-3 py-1.5 text-xs font-medium text-blue-700 bg-blue-50 hover:bg-blue-100 rounded-lg transition-colors disabled:opacity-50 disabled:cursor-not-allowed">
                      Choose organisations
                    </button>
                  </div>
                  <details id="xero-url-box" class="hidden mt-3 border border-blue-200 bg-blue-50 rounded-xl overflow-hidden group/xurl" open>
                    <summary class="flex items-center justify-between px-3 py-2 text-xs font-semibold text-blue-800 cursor-pointer list-none select-none">
                      <span>Authorisation Link (click to expand/collapse)</span>
                      <svg class="w-3.5 h-3.5 text-blue-500 transition-transform duration-200 group-open/xurl:rotate-180 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"/>
                      </svg>
                    </summary>
                    <div class="px-3 pb-3">
                      <p class="text-xs text-blue-700 mb-2">Open this URL in a new tab to continue Xero setup:</p>
                      <div class="flex gap-2 items-center">
                        <input id="xero-url-input" type="text" readonly
                          class="flex-1 text-xs font-mono border border-blue-200 rounded-lg px-3 py-2 bg-white focus:outline-none select-all">
                        <a id="xero-url-open" href="#" target="_blank"
                          class="px-3 py-2 text-xs font-medium text-white bg-blue-600 hover:bg-blue-700 rounded-lg transition-colors whitespace-nowrap">Open</a>
                        <button type="button"
                          onclick="navigator.clipboard.writeText(document.getElementById('xero-url-input').value).then(()=>{{this.textContent='Copied!';setTimeout(()=>this.textContent='Copy',2000)}});"
                          class="px-3 py-2 text-xs font-medium text-gray-700 bg-gray-100 hover:bg-gray-200 rounded-lg transition-colors whitespace-nowrap">
                          Copy
                        </button>
                      </div>
                    </div>
                  </details>
                  <script>
                  function fetchXeroUrl(endpoint) {{
                    fetch(endpoint, {{credentials: 'same-origin'}})
                      .then(r => r.json())
                      .then(data => {{
                        var box = document.getElementById('xero-url-box');
                        var inp = document.getElementById('xero-url-input');
                        var openLink = document.getElementById('xero-url-open');
                        inp.value = data.url;
                        openLink.href = data.url;
                        box.classList.remove('hidden');
                        box.setAttribute('open', 'open');
                        inp.select();
                      }})
                      .catch(e => alert('Could not generate link: ' + e));
                  }}
                  </script>

                  <!-- Xero Invoice Payment Webhooks (nested) -->
                  <details class="mt-2 border border-gray-100 rounded-xl overflow-hidden group/xwh">
                    <summary class="flex items-center justify-between px-4 py-3 bg-gray-50 cursor-pointer list-none select-none hover:bg-gray-100 transition-colors">
                      <div class="flex items-center gap-2">
                        <svg class="w-3.5 h-3.5 text-blue-700" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"/>
                        </svg>
                        <span class="text-xs font-semibold text-gray-700">Invoice payment webhooks</span>
                      </div>
                      <div class="flex items-center gap-2">
                        {('<span class="text-xs font-medium text-emerald-700 bg-emerald-50 border border-emerald-200 px-2 py-0.5 rounded-full">&#10003; Verified</span>' if xero_wh_verified else ('<span class="text-xs font-medium text-amber-700 bg-amber-50 border border-amber-200 px-2 py-0.5 rounded-full">Key saved</span>' if xero_wh_key else '<span class="text-xs font-medium text-gray-500 bg-gray-100 px-2 py-0.5 rounded-full">Not set up</span>'))}
                        <svg class="w-3.5 h-3.5 text-gray-400 transition-transform duration-200 group-open/xwh:rotate-180 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"/>
                        </svg>
                      </div>
                    </summary>
                    <div class="px-4 py-3 space-y-3">
                      <p class="text-xs text-gray-500">When an invoice is marked paid in Xero, this app updates the sheet row to <strong>Paid</strong> automatically.</p>
                      <div class="p-3 bg-blue-50 border border-blue-100 rounded-lg text-xs text-blue-800 space-y-1">
                        <p class="font-semibold">One-time setup in Xero:</p>
                        <ol class="list-decimal list-inside space-y-0.5 text-blue-700">
                          <li>Go to <strong>developer.xero.com</strong> → your app → <strong>Webhooks</strong></li>
                          <li>Add webhook URL below, tick <strong>Invoices</strong></li>
                          <li>Copy the <strong>Webhooks Key</strong> shown, paste it below and save</li>
                        </ol>
                      </div>
                      <div>
                        <label class="block text-xs font-medium text-gray-600 mb-1">Webhook URL <span class="text-gray-400 font-normal">(paste into Xero portal)</span></label>
                        <div class="flex gap-2">
                          <input readonly value="{escape(xero_webhook_url)}"
                            class="flex-1 px-3 py-1.5 border border-gray-200 rounded-lg text-xs font-mono bg-gray-50 text-gray-600">
                          <button type="button"
                            onclick="navigator.clipboard.writeText('{xero_webhook_url.replace(chr(39), chr(92)+chr(39))}');this.textContent='Copied!';setTimeout(()=>this.textContent='Copy',2000)"
                            class="px-3 py-1.5 text-xs font-medium bg-gray-100 hover:bg-gray-200 text-gray-700 rounded-lg transition-colors">Copy</button>
                        </div>
                      </div>
                      <div>
                        <label class="block text-xs font-medium text-gray-600 mb-1">Xero Webhooks Key</label>
                        <input name="xero_webhook_key" type="password"
                          placeholder="{"••••••••  (saved)" if xero_wh_key else "Paste the signing key from Xero"}"
                          class="w-full px-3 py-1.5 border border-gray-300 rounded-lg text-xs font-mono focus:outline-none focus:ring-2 focus:ring-indigo-500">
                      </div>
                      <div class="flex flex-wrap gap-2">
                        <button type="submit" formaction="/save-xero-webhook-key"
                          class="px-3 py-1.5 text-xs font-medium text-white bg-indigo-600 hover:bg-indigo-700 rounded-lg transition-colors">
                          Save Webhook Key
                        </button>
                        <button type="submit" formaction="/test-xero-webhook"
                          class="px-3 py-1.5 text-xs font-medium text-indigo-700 bg-indigo-50 hover:bg-indigo-100 rounded-lg transition-colors">
                          Test Webhook
                        </button>
                      </div>
                    </div>
                  </details>
                </div>
              </details>

              <!-- Receipts Card -->
              <details id="receipts-parser" class="bg-white rounded-2xl shadow-sm border border-gray-200 group">
                <summary class="flex items-center justify-between p-5 cursor-pointer list-none select-none hover:bg-gray-50 rounded-2xl transition-colors">
                  <div class="flex items-center gap-3">
                    <div class="w-10 h-10 bg-violet-50 rounded-xl flex items-center justify-center shrink-0">
                      <svg class="w-5 h-5 text-violet-700" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12h6m-6 4h6M7 4h10a2 2 0 012 2v12a2 2 0 01-2 2H7a2 2 0 01-2-2V6a2 2 0 012-2z"/>
                      </svg>
                    </div>
                    <div>
                      <h3 class="font-semibold text-gray-900 text-sm">Receipts</h3>
                      <p class="text-xs text-gray-500">Google Document AI parser</p>
                    </div>
                  </div>
                  <div class="flex items-center gap-2">
                    {_status_badge(bool(receipts_settings.get("enabled")), "Enabled" if receipts_settings.get("enabled") else "Disabled")}
                    <svg class="w-4 h-4 text-gray-400 transition-transform duration-200 group-open:rotate-180 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"/>
                    </svg>
                  </div>
                </summary>
                <div class="px-5 pb-5 border-t border-gray-100 pt-4 space-y-4">
                  <div class="space-y-3">
                    <p class="text-xs text-gray-500">Reads the details off receipt photos. Used by the receipt dump, the email invoice importer and cashflows reconciliation. Enter the three values from Google Cloud, upload your service-account JSON file, then click <strong>Test connection</strong>.</p>
                    <div class="grid grid-cols-1 sm:grid-cols-3 gap-3">
                      <div>
                        <label class="block text-xs font-medium text-gray-600 mb-1">Project ID</label>
                        <input name="document_ai_project_id" value="{escape(str(receipts_settings.get('document_ai_project_id') or ''))}"
                          class="w-full px-3 py-1.5 border border-gray-300 rounded-lg text-xs font-mono"
                          placeholder="65322709611">
                      </div>
                      <div>
                        <label class="block text-xs font-medium text-gray-600 mb-1">Processor ID</label>
                        <input name="document_ai_processor_id" value="{escape(str(receipts_settings.get('document_ai_processor_id') or ''))}"
                          class="w-full px-3 py-1.5 border border-gray-300 rounded-lg text-xs font-mono"
                          placeholder="82c1804dc0bdc8b9">
                      </div>
                      <div>
                        <label class="block text-xs font-medium text-gray-600 mb-1">Location</label>
                        <input name="document_ai_location" value="{escape(str(receipts_settings.get('document_ai_location') or 'us'))}"
                          class="w-full px-3 py-1.5 border border-gray-300 rounded-lg text-xs font-mono"
                          placeholder="us">
                      </div>
                    </div>
                    <div>
                      <p class="text-xs font-medium text-gray-700 mb-1">Google Cloud service account file (.json)</p>
                      <input type="file" name="google_service_account" accept=".json,application/json"
                        class="block w-full text-xs text-gray-500 file:mr-3 file:py-1.5 file:px-3 file:rounded-lg file:border-0 file:text-xs file:font-medium file:bg-indigo-50 file:text-indigo-700 hover:file:bg-indigo-100">
                      <p class="text-xs text-gray-500 mt-1">Stored file: <code>{escape(receipt_sa_path)}</code> · {'found' if receipt_sa_exists else 'not found'}</p>
                    </div>
                    <div class="flex items-center gap-3 flex-wrap">
                      <label class="inline-flex items-center gap-2 text-xs text-gray-700">
                        <input type="checkbox" name="receipts_enabled_runtime" value="1" {"checked" if receipts_settings.get("enabled") else ""} class="w-4 h-4">
                        Enable receipts flow
                      </label>
                      <label class="inline-flex items-center gap-2 text-xs text-gray-700">
                        Retention days
                        <input type="number" min="1" max="30" name="retention_days" value="{int(receipts_settings.get('retention_days') or 2)}"
                          class="w-16 px-2 py-1 border border-gray-300 rounded-lg text-xs font-mono">
                      </label>
                    </div>
                    <div class="flex items-center gap-2 flex-wrap pt-2 border-t border-violet-100">
                      <button type="submit" formaction="/save-receipts-settings"
                        class="px-3 py-1.5 text-xs font-medium text-white bg-indigo-600 hover:bg-indigo-700 rounded-lg transition-colors">
                        Save settings
                      </button>
                      <button type="submit" formaction="/upload-google-service-account"
                        class="px-3 py-1.5 text-xs font-medium text-gray-700 bg-gray-100 hover:bg-gray-200 rounded-lg transition-colors">
                        Upload file
                      </button>
                      <button type="submit" formaction="/test-document-ai-connection"
                        class="px-3 py-1.5 text-xs font-medium text-indigo-700 bg-indigo-50 hover:bg-indigo-100 rounded-lg transition-colors">
                        Test connection
                      </button>
                      {parser_status_badge}
                    </div>
                    <p class="text-xs text-gray-600">{parser_status_text}{' · ' + parser_tested_at if parser_tested_at else ''}</p>
                  </div>
                </div>
              </details>

              <!-- Cashflows Card -->
              <details id="cashflows" class="bg-white rounded-2xl shadow-sm border border-gray-200 group">
                <summary class="flex items-center justify-between p-5 cursor-pointer list-none select-none hover:bg-gray-50 rounded-2xl transition-colors">
                  <div class="flex items-center gap-3">
                    <div class="w-10 h-10 bg-emerald-50 rounded-xl flex items-center justify-center shrink-0">
                      <svg class="w-5 h-5 text-emerald-700" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 7h6m-6 4h6m-6 4h4M5 3h14a2 2 0 012 2v14a2 2 0 01-2 2H5a2 2 0 01-2-2V5a2 2 0 012-2z"/>
                      </svg>
                    </div>
                    <div>
                      <h3 class="font-semibold text-gray-900 text-sm">Cashflows</h3>
                      <p class="text-xs text-gray-500">Settlement sync</p>
                    </div>
                  </div>
                  <div class="flex items-center gap-2">
                    {_status_badge(bool(cashflows_settings.get("enabled")), "Enabled" if cashflows_settings.get("enabled") else "Disabled")}
                    <svg class="w-4 h-4 text-gray-400 transition-transform duration-200 group-open:rotate-180 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"/>
                    </svg>
                  </div>
                </summary>
                <div class="px-5 pb-5 border-t border-gray-100 pt-4 space-y-4">
                  <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
                    <div>
                      <label class="block text-xs font-medium text-gray-600 mb-1">Cashflows environment</label>
                      <select name="cashflows_environment" class="w-full px-3 py-1.5 border border-gray-300 rounded-lg text-xs">
                        <option value="integration" {"selected" if str(cashflows_settings.get("environment")) == "integration" else ""}>Integration</option>
                        <option value="production" {"selected" if str(cashflows_settings.get("environment")) == "production" else ""}>Production</option>
                      </select>
                    </div>
                    <div>
                      <label class="block text-xs font-medium text-gray-600 mb-1">Cashflows base URL</label>
                      <input name="cashflows_base_url" value="{escape(str(cashflows_settings.get('base_url') or ''))}"
                        class="w-full px-3 py-1.5 border border-gray-300 rounded-lg text-xs font-mono"
                        placeholder="https://gateway-int.cashflows.com/api/gateway">
                    </div>
                    <div class="sm:col-span-2">
                      <label class="block text-xs font-medium text-gray-600 mb-1">Cashflows API Key <span class="font-normal text-gray-400">(from your Cashflows merchant account — not related to OpenAI)</span></label>
                      <input name="cashflows_api_key" type="password"
                        placeholder="{"••••••••  (saved)" if str(cashflows_settings.get('api_key') or '').strip() else "Paste your Cashflows API key"}"
                        class="w-full px-3 py-1.5 border border-gray-300 rounded-lg text-xs font-mono">
                    </div>
                    <div>
                      <label class="block text-xs font-medium text-gray-600 mb-1">Timeout (seconds)</label>
                      <input type="number" min="5" max="60" name="cashflows_timeout_seconds" value="{int(cashflows_settings.get('timeout_seconds') or 15)}"
                        class="w-full px-3 py-1.5 border border-gray-300 rounded-lg text-xs font-mono">
                    </div>
                    <div>
                      <label class="block text-xs font-medium text-gray-600 mb-1">Settlement read action</label>
                      <input name="cashflows_settlements_action" value="{escape(str(cashflows_settings.get('settlements_action') or 'GetSettlementPayouts'))}"
                        class="w-full px-3 py-1.5 border border-gray-300 rounded-lg text-xs font-mono"
                        placeholder="GetSettlementPayouts">
                    </div>
                  </div>
                  <div class="flex items-center gap-2 flex-wrap">
                    <label class="inline-flex items-center gap-2 text-xs text-gray-700">
                      <input type="checkbox" name="cashflows_enabled" value="1" {"checked" if cashflows_settings.get("enabled") else ""} class="w-4 h-4">
                      Enable cashflows sync
                    </label>
                    <button type="submit" formaction="/save-cashflows-settings"
                      class="px-3 py-1.5 text-xs font-medium text-white bg-violet-600 hover:bg-violet-700 rounded-lg transition-colors">
                      Save Cashflows Settings
                    </button>
                    <button type="submit" formaction="/test-cashflows-connection"
                      class="px-3 py-1.5 text-xs font-medium text-violet-700 bg-violet-50 hover:bg-violet-100 rounded-lg transition-colors">
                      Test Cashflows Connection
                    </button>
                    {cashflows_status_badge}
                  </div>
                  <p class="text-xs text-gray-600">{cashflows_status_text}{' · ' + cashflows_tested_at if cashflows_tested_at else ''}</p>
                  <p class="text-xs text-gray-500">
                    Cashflows tests are read-only/no-write. A 404 means the endpoint/action did not accept the probe; use Cashflows Sync diagnostics with a real date range.
                  </p>
                </div>
              </details>
            </div>
          </form>

          <!-- OpenAI Configuration -->
          <form method="post" action="/save-openai-settings" class="mt-4" id="openai">
            <details class="bg-white rounded-2xl shadow-sm border border-gray-200 overflow-hidden group" {"open" if _oa_details_open else ""}>
              <summary class="flex items-center justify-between px-5 py-4 cursor-pointer list-none select-none hover:bg-gray-50 transition-colors">
                <div class="flex items-center gap-3">
                  <div class="w-9 h-9 bg-purple-50 rounded-xl flex items-center justify-center shrink-0 text-lg">🤖</div>
                  <div>
                    <h3 class="text-sm font-semibold text-gray-900">OpenAI — AI calendar parsing</h3>
                    <p class="text-xs text-gray-500">Extracts customer names and invoice amounts from calendar event titles &amp; descriptions</p>
                  </div>
                </div>
                <div class="flex items-center gap-2 shrink-0" onclick="event.stopPropagation()">
                  <span id="oa-badge" class="px-2 py-1 rounded-full text-xs {openai_badge_cls}">{openai_badge_text}</span>
                  <button type="button" onclick="testOpenAI(this)"
                    class="px-2 py-1 text-xs font-medium text-purple-700 bg-purple-50 hover:bg-purple-100 border border-purple-200 rounded-lg transition-colors whitespace-nowrap">
                    Test connection
                  </button>
                </div>
              </summary>
              <div class="px-5 pb-5 border-t border-gray-100">
                <div class="grid grid-cols-1 sm:grid-cols-2 gap-3 mt-4">
                  <div class="sm:col-span-2">
                    <label class="block text-xs font-medium text-gray-600 mb-1">OpenAI API Key</label>
                    <input name="openai_api_key" type="password"
                      placeholder="{"••••••••  (saved)" if openai_settings.get('api_key') else "sk-..."}"
                      class="w-full px-3 py-1.5 border border-gray-300 rounded-lg text-xs font-mono"
                      autocomplete="new-password">
                    <p class="text-xs text-gray-400 mt-1">Leave blank to keep the current key. Get a key at <a href="https://platform.openai.com/api-keys" target="_blank" rel="noopener" class="underline text-indigo-600">platform.openai.com/api-keys</a>.</p>
                  </div>
                  <div>
                    <label class="block text-xs font-medium text-gray-600 mb-1">Model</label>
                    <input name="openai_model" value="{escape(openai_settings.get('model') or 'gpt-4o-mini')}"
                      class="w-full px-3 py-1.5 border border-gray-300 rounded-lg text-xs font-mono"
                      placeholder="gpt-4o-mini">
                    <p class="text-xs text-gray-400 mt-1">Recommended: <code>gpt-4o-mini</code> (fast, cheap).</p>
                  </div>
                </div>
                <div class="flex items-center gap-2 flex-wrap mt-3">
                  <button type="submit" class="px-3 py-1.5 text-xs font-medium text-white bg-purple-600 hover:bg-purple-700 rounded-lg transition-colors">
                    Save OpenAI Settings
                  </button>
                  <button type="button" onclick="testOpenAI(this)"
                    class="px-3 py-1.5 text-xs font-medium text-purple-700 bg-purple-50 hover:bg-purple-100 rounded-lg transition-colors">
                    Test OpenAI Connection
                  </button>
                  {"" if not openai_settings.get('api_key') else
                   '<button type="submit" name="clear_key" value="1" class="px-3 py-1.5 text-xs font-medium text-red-600 bg-red-50 hover:bg-red-100 rounded-lg border border-red-200 transition-colors">Remove saved key</button>'}
                </div>
                <div id="oa-test-result" class="hidden mt-3 p-3 rounded-lg text-xs"></div>
                <p class="text-xs text-gray-600 mt-2">{openai_test_status_text}{" · " + openai_tested_at if openai_tested_at else ""}</p>
              </div>
            </details>
          </form>
          <script>
          function testOpenAI(btn) {{
            var badge = document.getElementById('oa-badge');
            var result = document.getElementById('oa-test-result');
            var keyInput = document.querySelector('input[name="openai_api_key"]');
            var modelInput = document.querySelector('input[name="openai_model"]');
            var original = btn.textContent;
            btn.disabled = true;
            btn.textContent = 'Testing…';
            if (result) {{
              result.classList.remove('hidden');
              result.className = 'mt-3 p-3 rounded-lg text-xs bg-gray-50 border border-gray-200 text-gray-600';
              result.textContent = 'Testing OpenAI connection…';
            }}
            var params = new URLSearchParams();
            if (keyInput && keyInput.value) params.append('openai_api_key', keyInput.value);
            if (modelInput && modelInput.value) params.append('openai_model', modelInput.value);
            fetch('/test-openai-connection', {{
              method: 'POST',
              headers: {{'X-Requested-With': 'fetch', 'Content-Type': 'application/x-www-form-urlencoded', 'Accept': 'application/json'}},
              body: params.toString()
            }})
            .then(function(r) {{
              if (!r.ok) {{ throw new Error('server returned HTTP ' + r.status); }}
              return r.json();
            }})
            .then(function(data) {{
              btn.disabled = false;
              btn.textContent = original;
              if (badge) {{
                badge.className = 'px-2 py-1 rounded-full text-xs ' + (data.ok ? 'bg-green-100 text-green-700' : 'bg-red-100 text-red-700');
                badge.textContent = data.ok ? '✓ Connected' : '✗ Test failed';
              }}
              if (result) {{
                result.classList.remove('hidden');
                result.className = 'mt-3 p-3 rounded-lg text-xs ' + (data.ok ? 'bg-green-50 border border-green-200 text-green-800' : 'bg-red-50 border border-red-200 text-red-800');
                result.textContent = (data.ok ? '✓ ' : '✗ ') + (data.message || (data.ok ? 'Connected successfully.' : 'Test failed.'));
              }}
            }})
            .catch(function(err) {{
              btn.disabled = false;
              btn.textContent = original;
              if (result) {{
                result.classList.remove('hidden');
                result.className = 'mt-3 p-3 rounded-lg text-xs bg-red-50 border border-red-200 text-red-800';
                result.textContent = '✗ Request failed: ' + err;
              }}
            }});
          }}
          </script>

          <!-- Google Sheets Configuration - collapsible standalone form - directly under connectivity -->
          <form method="post" action="/save-sheet-target" class="mt-4">
            <details class="bg-white rounded-2xl shadow-sm border border-gray-200 overflow-hidden group" {"open" if not sheets_ok else ""}>
              <summary class="flex items-center justify-between px-5 py-4 cursor-pointer list-none select-none hover:bg-gray-50 transition-colors">
                <div class="flex items-center gap-3">
                  <div class="w-9 h-9 bg-blue-50 rounded-xl flex items-center justify-center shrink-0">
                    <svg class="w-4 h-4 text-blue-600" viewBox="0 0 24 24" fill="currentColor">
                      <path d="M12.545 10.239v3.821h5.445c-.712 2.315-2.647 3.972-5.445 3.972a6.033 6.033 0 110-12.064c1.498 0 2.866.549 3.921 1.453l2.814-2.814A9.969 9.969 0 0012.545 2C7.021 2 2.543 6.477 2.543 12s4.478 10 10.002 10c8.396 0 10.249-7.85 9.426-11.748l-9.426-.013z"/>
                    </svg>
                  </div>
                  <div>
                    <h2 class="font-semibold text-gray-900 text-sm">Google Sheets Configuration</h2>
                    <p class="text-xs text-gray-500">Invoice sheet · Sales tracking · Cash tracking</p>
                  </div>
                </div>
                <div class="flex items-center gap-2">
                  {_status_badge(sheets_ok, "Ready" if sheets_ok else "Not ready")}
                  <svg class="w-4 h-4 text-gray-400 transition-transform duration-200 group-open:rotate-180 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"/>
                  </svg>
                </div>
              </summary>
              <div class="px-5 pb-5 pt-4 border-t border-gray-100 space-y-4">
                <p class="text-sm text-gray-500">{escape(sheets_msg)}</p>
                <details class="border border-gray-200 rounded-xl overflow-hidden group/master" open>
                  <summary class="flex items-center justify-between px-4 py-3 bg-gray-50 cursor-pointer list-none select-none hover:bg-gray-100 transition-colors">
                    <div>
                      <h3 class="text-sm font-semibold text-gray-900">Master Sheet</h3>
                      <p class="text-xs text-gray-500">All invoice/payment rows (universal)</p>
                    </div>
                    <svg class="w-4 h-4 text-gray-400 transition-transform duration-200 group-open/master:rotate-180 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"/>
                    </svg>
                  </summary>
                  <div class="px-4 py-4 border-t border-gray-100 space-y-3">
                    {"" if not spreadsheets else f"""
                    <div>
                      <label class="block text-sm font-medium text-gray-700 mb-1.5">Pick from your spreadsheets</label>
                      <select name="spreadsheet_pick" class="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500">
                        {sheet_options}
                      </select>
                    </div>
                    """}
                    <div>
                      <label class="block text-sm font-medium text-gray-700 mb-1.5">Spreadsheet URL or ID</label>
                      <input name="spreadsheet_input" value="{escape(sheet_current_id)}"
                        placeholder="Paste a Google Sheets URL or spreadsheet ID"
                        class="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500">
                      {f'<p class="text-xs text-emerald-600 mt-1">&#10003; Saved: <span class="font-medium">{escape(sheet_current_id)}</span></p>' if sheet_current_id else '<p class="text-xs text-gray-400 mt-1">No spreadsheet saved yet.</p>'}
                    </div>
                    <div>
                      <label class="block text-sm font-medium text-gray-700 mb-1.5">Sheet tab name</label>
                      <input name="sheet_name" value="{escape(sheet_name_val)}"
                        placeholder="Sheet1"
                        class="w-48 px-3 py-2 border border-gray-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500">
                      {f'<p class="text-xs text-emerald-600 mt-1">&#10003; Saved: <span class="font-medium">{escape(sheet_name_val)}</span></p>' if sheet_current_id else ""}
                    </div>
                  </div>
                </details>

                <details class="border border-gray-200 rounded-xl overflow-hidden group/cashall">
                  <summary class="flex items-center justify-between px-4 py-3 bg-gray-50 cursor-pointer list-none select-none hover:bg-gray-100 transition-colors">
                    <div>
                      <h3 class="text-sm font-semibold text-gray-900">Cash (All)</h3>
                      <p class="text-xs text-gray-500">All cash payments in one sheet</p>
                    </div>
                    <svg class="w-4 h-4 text-gray-400 transition-transform duration-200 group-open/cashall:rotate-180 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"/>
                    </svg>
                  </summary>
                  <div class="px-4 py-4 border-t border-gray-100 space-y-3">
                    {"" if not spreadsheets else f"""
                    <div>
                      <label class="block text-sm font-medium text-gray-700 mb-1.5">Pick from your spreadsheets</label>
                      <select name="cash_spreadsheet_pick" class="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500">
                        {cash_sheet_options}
                      </select>
                    </div>
                    """}
                    <div>
                      <label class="block text-sm font-medium text-gray-700 mb-1.5">Spreadsheet URL or ID</label>
                      <input name="cash_spreadsheet_input" value="{escape(cash_sheet_current_id)}"
                        placeholder="Paste a Google Sheets URL or spreadsheet ID"
                        class="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500">
                    </div>
                    <div>
                      <label class="block text-sm font-medium text-gray-700 mb-1.5">Sheet tab name</label>
                      <input name="cash_sheet_name" value="{escape(cash_sheet_name_val)}"
                        placeholder="Cash"
                        class="w-48 px-3 py-2 border border-gray-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500">
                      {f'<p class="text-xs text-emerald-600 mt-1">&#10003; Saved: <span class="font-medium">{escape(cash_sheet_current_id)} / {escape(cash_sheet_name_val)}</span></p>' if cash_sheet_current_id else '<p class="text-xs text-gray-400 mt-1">No global cash sheet saved yet.</p>'}
                    </div>
                  </div>
                </details>

                <details class="border border-gray-200 rounded-xl overflow-hidden group/sales" open>
                  <summary class="flex items-center justify-between px-4 py-3 bg-gray-50 cursor-pointer list-none select-none hover:bg-gray-100 transition-colors">
                    <div>
                      <h3 class="text-sm font-semibold text-gray-900">Sales Tracking</h3>
                      <p class="text-xs text-gray-500">Logs lines below <strong>⬇Sales⬇</strong> in [invoice] — per calendar</p>
                    </div>
                    <svg class="w-4 h-4 text-gray-400 transition-transform duration-200 group-open/sales:rotate-180 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"/>
                    </svg>
                  </summary>
                  <div class="px-4 py-4 border-t border-gray-100 space-y-4">
                    {cal_sales_routing_html}
                  </div>
                </details>

                <details class="border border-gray-200 rounded-xl overflow-hidden group/cash">
                  <summary class="flex items-center justify-between px-4 py-3 bg-gray-50 cursor-pointer list-none select-none hover:bg-gray-100 transition-colors">
                    <div>
                      <h3 class="text-sm font-semibold text-gray-900">Cash Tracking</h3>
                      <p class="text-xs text-gray-500">Logs cash payments to a sheet — per calendar</p>
                    </div>
                    <svg class="w-4 h-4 text-gray-400 transition-transform duration-200 group-open/cash:rotate-180 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"/>
                    </svg>
                  </summary>
                  <div class="px-4 py-4 border-t border-gray-100 space-y-4">
                    {cal_cash_routing_html}
                  </div>
                </details>

                <button type="submit"
                  class="px-4 py-2 bg-indigo-600 hover:bg-indigo-700 text-white text-sm font-medium rounded-lg transition-colors">
                  Save Sheets Configuration
                </button>
              </div>
            </details>
          </form>

          <!-- Xero Organisations -->
          {_xero_tenant_cards(xero_ok, xero_tenant_account_data, xero_tenant_warning, global_sync_on=get_enabled(config.admin_db_file))}

          <form method="post" action="/save" enctype="multipart/form-data" class="space-y-6">

            <!-- Active Calendars -->
            <details class="group bg-white rounded-2xl shadow-sm border border-gray-200 overflow-hidden">
              <summary class="flex items-center justify-between px-6 py-4 cursor-pointer list-none select-none hover:bg-gray-50 transition-colors">
                <div class="flex items-center gap-3">
                  <h2 class="font-semibold text-gray-900">Active Calendars</h2>
                  <span class="text-xs text-gray-400">{len(active)} selected</span>
                </div>
                <svg class="w-4 h-4 text-gray-400 transition-transform duration-200 group-open:rotate-180 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"/></svg>
              </summary>
              <div class="px-6 pb-6 pt-2 border-t border-gray-100">
                <p class="text-sm text-gray-500 mb-4">Select which calendars to monitor for entries finalised with <strong>Y/N = Y</strong>.</p>
                <div class="divide-y divide-gray-50">
                  {cal_html}
                </div>
              </div>
            </details>

            <!-- Stats Fields -->
            <details class="bg-white rounded-2xl shadow-sm border border-gray-200 overflow-hidden group">
              <summary class="flex items-center justify-between px-6 py-4 cursor-pointer list-none select-none hover:bg-gray-50 transition-colors">
                <div class="flex items-center gap-3">
                  <div class="w-9 h-9 bg-blue-50 rounded-xl flex items-center justify-center shrink-0">
                    <svg class="w-4 h-4 text-blue-600" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z"/>
                    </svg>
                  </div>
                  <div>
                    <h2 class="font-semibold text-gray-900 text-sm">Stats to Post to Sheets</h2>
                    <p class="text-xs text-gray-500">Columns written to your Google Sheet per invoice</p>
                  </div>
                </div>
                <svg class="w-4 h-4 text-gray-400 transition-transform duration-200 group-open:rotate-180 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"/>
                </svg>
              </summary>
              <div class="px-6 pb-6 pt-4 border-t border-gray-100">
                <div class="grid grid-cols-1 sm:grid-cols-2 gap-1">
                  {stats_html}
                </div>
              </div>
            </details>

            <!-- Submitter Names -->
            <div class="bg-white rounded-2xl shadow-sm border border-gray-200 p-6">
              <h2 class="font-semibold text-gray-900 mb-1">Submitter Display Names</h2>
              <p class="text-sm text-gray-500 mb-4">Map a submitter's email address to a friendly display name used in calendar entries and sheet rows.</p>
              <div class="divide-y divide-gray-100">
                {alias_rows_html}
              </div>
              <div class="mt-4">
                <button type="submit" formaction="/apply-submitter-aliases"
                  class="px-4 py-2 bg-white hover:bg-gray-50 text-gray-700 text-sm font-medium rounded-lg border border-gray-300 transition-colors">
                  Save Names &amp; Apply to Existing Entries
                </button>
              </div>
            </div>

            <!-- Save -->
            <div class="flex justify-end pb-4">
              <button type="submit"
                class="px-6 py-2.5 bg-indigo-600 hover:bg-indigo-700 text-white font-medium rounded-lg text-sm transition-colors shadow-sm">
                Save Settings
              </button>
            </div>

          </form>
        </div>
        """)

    @app.post("/save")
    @require_login
    def save():
        calendars = request.form.getlist("active_calendars")
        if not calendars:
            calendars = [config.google_calendar_id]
        set_active_calendars(config.admin_db_file, calendars)
        trigger_watch_check()  # immediately register/remove watches for the new selection
        trigger_poll()         # wake poller so the watch check runs without delay

        stats_fields = request.form.getlist("stats_fields")
        valid = [k for k in stats_fields if k in {k for k, _ in STAT_OPTIONS}]
        if not valid:
            valid = DEFAULT_STATS_FIELDS
        set_stats_fields(config.admin_db_file, valid)

        _save_calendar_sales_sheets_from_form(config, request.form)
        _save_calendar_cash_sheets_from_form(config, request.form)

        aliases = _save_submitter_aliases_from_form(config, request.form)
        msg = "Settings saved."
        if aliases:
            msg += " Submitter names saved."
        session["save_notice"] = f"success:{msg}"

        set_json_setting(config.admin_db_file, "settings_version", {"updated": True})
        trigger_poll()
        return redirect(url_for("index"))

    @app.post("/save-sheet-target")
    @require_login
    def save_sheet_target():
        picked = (request.form.get("spreadsheet_pick") or "").strip()
        entered = (request.form.get("spreadsheet_input") or "").strip()
        spreadsheet_id = _extract_spreadsheet_id(picked or entered)
        sheet_name = (request.form.get("sheet_name") or "Sheet1").strip() or "Sheet1"
        set_sheet_target(config.admin_db_file, spreadsheet_id=spreadsheet_id, sheet_name=sheet_name)

        sales_picked = (request.form.get("sales_spreadsheet_pick") or "").strip()
        sales_entered = (request.form.get("sales_spreadsheet_input") or "").strip()
        sales_spreadsheet_id = _extract_spreadsheet_id(sales_picked or sales_entered)
        sales_sheet_name = (request.form.get("sales_sheet_name") or "Sales").strip() or "Sales"
        set_sales_sheet_target(
            config.admin_db_file,
            spreadsheet_id=sales_spreadsheet_id,
            sheet_name=sales_sheet_name,
        )
        cash_picked = (request.form.get("cash_spreadsheet_pick") or "").strip()
        cash_entered = (request.form.get("cash_spreadsheet_input") or "").strip()
        cash_spreadsheet_id = _extract_spreadsheet_id(cash_picked or cash_entered)
        cash_sheet_name = (request.form.get("cash_sheet_name") or "Cash").strip() or "Cash"
        set_cash_sheet_target(
            config.admin_db_file,
            spreadsheet_id=cash_spreadsheet_id,
            sheet_name=cash_sheet_name,
        )
        sales_fields = request.form.getlist("sales_stats_fields")
        valid_sales = [k for k in sales_fields if k in {k for k, _ in SALES_STAT_OPTIONS}]
        if not valid_sales:
            valid_sales = DEFAULT_SALES_STATS_FIELDS
        set_sales_stats_fields(config.admin_db_file, valid_sales)

        _save_sales_submitter_sheets_from_form(config, request.form)
        _save_cash_submitter_sheets_from_form(config, request.form)
        cal_sales_routes = _save_calendar_sales_sheets_from_form(config, request.form)
        cal_cash_routes = _save_calendar_cash_sheets_from_form(config, request.form)
        target = {"spreadsheet_id": spreadsheet_id, "sheet_name": sheet_name}
        creds = None
        ok = False
        try:
            creds = load_admin_credentials(config)
            ok, _ = _sheets_status_data(config, creds, target)
        except Exception:
            ok = False
        if ok:
            msg = "Sheets configuration saved. Connection is ready."
        else:
            msg = "Sheets configuration saved."
        if cal_sales_routes or cal_cash_routes:
            msg += " Calendar routing updated."
        if sales_spreadsheet_id:
            msg += " Sales sheet updated."
        if cash_spreadsheet_id:
            msg += " Global cash sheet updated."
        session["save_notice"] = f"success:{msg}"
        trigger_poll()
        return redirect(url_for("index"))

    @app.post("/apply-submitter-aliases")
    @require_login
    def apply_submitter_aliases():
        aliases = _save_submitter_aliases_from_form(config, request.form)
        if not aliases:
            session["save_notice"] = "error:No submitter name mappings set."
            return redirect(url_for("index"))

        updated, errors = _backfill_submitter_aliases(config, aliases)
        msg = f"Submitter names saved. Updated {updated} existing calendar entries."
        if errors:
            msg += f" ({errors} updates failed.)"

        creds = load_admin_credentials(config)
        sheet_target = get_sheet_target(config.admin_db_file)
        spreadsheet_id = (sheet_target.get("spreadsheet_id") or "").strip()
        sheet_name = (sheet_target.get("sheet_name") or "Sheet1").strip() or "Sheet1"
        sheet_updated = 0
        if creds and spreadsheet_id:
            try:
                sheet_updated = backfill_submitter_in_sheet(
                    creds,
                    spreadsheet_id=spreadsheet_id,
                    sheet_name=sheet_name,
                    aliases=aliases,
                )
            except Exception:
                pass
        if sheet_updated:
            msg += f" Updated {sheet_updated} sheet row(s)."

        session["save_notice"] = f"success:{msg}"
        set_json_setting(config.admin_db_file, "settings_version", {"updated": True})
        return redirect(url_for("index"))

    # ── Webhook endpoints ──────────────────────────────────────────────────────

    @app.post("/webhooks/google-calendar")
    def webhook_google_calendar():
        """Receive Google Calendar push notifications and wake the poller."""
        state_header = request.headers.get("X-Goog-Resource-State", "")
        token = request.headers.get("X-Goog-Channel-Token", "")
        channel_id = request.headers.get("X-Goog-Channel-ID", "")

        # Accept notifications from currently-registered channels.
        # Some legacy channels may miss our token but still be valid until renewal.
        known_channels = {
            str((w or {}).get("channel_id") or "").strip()
            for w in (get_google_watches(config.admin_db_file) or {}).values()
        }
        known_channels.discard("")
        channel_known = bool(channel_id and channel_id in known_channels)

        if token and token != "gcal-bridge" and not channel_known:
            return "", 200
        if not token and not channel_known:
            return "", 200

        calendar_hint = ""
        if channel_id:
            for _cal_id, _w in (get_google_watches(config.admin_db_file) or {}).items():
                if str((_w or {}).get("channel_id") or "").strip() == channel_id:
                    calendar_hint = _cal_id
                    break

        if state_header in {"exists", "not_exists", "sync"}:
            _feed.push("Google Calendar change detected — scanning calendars now", "event")
            if calendar_hint:
                queue_calendar_target(calendar_hint)
            else:
                trigger_poll()
            print(
                f"[webhook] Google Calendar change ({state_header}) — poll triggered"
                f"{' for ' + calendar_hint if calendar_hint else ''}",
                flush=True,
            )
        return "", 200

    @app.post("/webhooks/xero")
    def webhook_xero():
        """Receive Xero webhooks (invoice paid etc.) and update sheets + calendar status."""
        import hmac as _hmac
        import hashlib as _hashlib
        import base64 as _base64

        raw_body = request.get_data()
        webhook_key = get_xero_webhook_key(config.admin_db_file)

        sig = request.headers.get("x-xero-signature", "")
        if webhook_key:
            expected = _base64.b64encode(
                _hmac.new(webhook_key.encode("utf-8"), raw_body, _hashlib.sha256).digest()
            ).decode()
            if not _hmac.compare_digest(sig, expected):
                return "", 401

        try:
            payload = request.get_json(force=True, silent=True) or {}
        except Exception:
            return "", 200

        events = payload.get("events", [])
        if not events:
            # This is an intent-to-receive verification ping from Xero — mark as verified
            set_xero_webhook_verified(config.admin_db_file, True)
            print("[webhook] Xero intent-to-receive verified successfully", flush=True)
            return "", 200

        app_state = load_state(config.state_file)
        inv_map = app_state.get("event_invoice_map", {})
        inv_id_to_key = {v: k for k, v in inv_map.items()}

        if xero_is_disabled() or xero_lockout_is_active(config):
            print(
                "[webhook] Xero invoice webhook received while Xero is paused/locked; "
                "skipping Xero token refresh and invoice fetch",
                flush=True,
            )
            trigger_poll()
            return "", 200
        _busy = xero_busy_status(config.admin_db_file)
        if _busy.get("active"):
            print(
                "[webhook] Xero invoice webhook received while Xero is busy "
                f"({ _busy.get('reason') or _busy.get('owner') }); deferring invoice fetch",
                flush=True,
            )
            trigger_poll()
            return "", 200

        creds = load_admin_credentials(config)
        sheet_target = get_sheet_target(config.admin_db_file)
        sales_sheet_target = get_sales_sheet_target(config.admin_db_file)
        calendar_sales_sheets = get_calendar_sales_sheets(config.admin_db_file)
        sales_stats_fields = get_sales_stats_fields(config.admin_db_file)
        submitter_aliases = get_submitter_aliases(config.admin_db_file)
        spreadsheet_id = (sheet_target.get("spreadsheet_id") or "").strip()
        sheet_name = (sheet_target.get("sheet_name") or "Sheet1").strip() or "Sheet1"

        xero_tok = load_xero_token(config.xero_token_file)
        if token_is_expired(xero_tok) and xero_tok.get("refresh_token"):
            try:
                _cid, _csec = _get_xero_creds(config)
                if _cid and _csec:
                    refreshed = refresh_xero_token(
                        _cid,
                        _csec,
                        xero_tok["refresh_token"],
                    )
                    xero_tok = {**xero_tok, **refreshed}
                    save_xero_token(config.xero_token_file, xero_tok)
            except Exception as exc:
                print(
                    f"[webhook] Xero token refresh failed before invoice webhook handling: {exc}",
                    flush=True,
                )
        xero_at = (xero_tok or {}).get("access_token", "")
        xero_tenant = (xero_tok or {}).get("tenant_id", "")
        _webhook_429 = False  # set True if a Xero 429 is hit mid-batch

        def _fetch_invoice(invoice_id: str):
            nonlocal xero_at, xero_tok, _webhook_429
            if (
                xero_is_disabled()
                or xero_lockout_is_active(config)
                or xero_busy_status(config.admin_db_file).get("active")
            ):
                return None
            if not (xero_at and xero_tenant):
                return None
            url = f"https://api.xero.com/api.xro/2.0/Invoices/{invoice_id}"
            headers = {
                "Authorization": f"Bearer {xero_at}",
                "Xero-tenant-id": xero_tenant,
                "Accept": "application/json",
            }
            resp = requests.get(url, headers=headers, timeout=10)
            if resp.status_code == 401 and xero_tok.get("refresh_token"):
                try:
                    _cid, _csec = _get_xero_creds(config)
                    if _cid and _csec:
                        refreshed = refresh_xero_token(_cid, _csec, xero_tok["refresh_token"])
                        xero_tok = {**xero_tok, **refreshed}
                        save_xero_token(config.xero_token_file, xero_tok)
                        xero_at = (xero_tok or {}).get("access_token", "")
                        headers["Authorization"] = f"Bearer {xero_at}"
                        resp = requests.get(url, headers=headers, timeout=10)
                except Exception as exc:
                    print(f"[webhook] Xero token refresh failed while fetching invoice: {exc}", flush=True)
            if resp.status_code == 429:
                retry_after_s = 300
                raw_retry = str(resp.headers.get("Retry-After") or "").strip()
                if raw_retry.isdigit():
                    try:
                        retry_after_s = max(60, int(raw_retry))
                    except Exception:
                        retry_after_s = 300
                _lockout_until = time.time() + retry_after_s
                _ls = load_state(config.state_file)
                _ls["xero_lockout_until_ts"] = _lockout_until
                _ls["xero_lockout_reason"] = "Xero API rate limit (429) during webhook batch"
                _ls["xero_lockout_updated_at_ts"] = time.time()
                save_state_merged(config.state_file, _ls)
                print(
                    f"[webhook] Xero 429 — persisted lockout for {retry_after_s}s, "
                    "halting further webhook Xero calls this batch",
                    flush=True,
                )
                _webhook_429 = True
                return None
            if not resp.ok:
                return None
            invoices = resp.json().get("Invoices", [])
            return invoices[0] if invoices else None

        def _is_invoice_paid(invoice_obj: dict | None) -> bool:
            if not invoice_obj:
                return False
            status_raw = str(invoice_obj.get("Status") or "").upper()
            try:
                amount_due = float(invoice_obj.get("AmountDue") or 0.0)
            except Exception:
                amount_due = 0.0
            # Treat fully settled invoices as paid even if status text lags.
            return status_raw == "PAID" or amount_due <= 0.0001

        _invoice_cache: dict = {}  # one Xero API call per unique invoice ID per batch
        for ev in events:
            if ev.get("eventCategory") != "INVOICE":
                continue
            invoice_id = ev.get("resourceId", "")
            if not invoice_id:
                continue
            print(f"[webhook] Xero invoice event: {ev.get('eventType')} {invoice_id}", flush=True)

            try:
                if invoice_id not in _invoice_cache:
                    _invoice_cache[invoice_id] = _fetch_invoice(invoice_id)
                if _webhook_429:
                    print(
                        "[webhook] Xero rate-limited mid-batch — stopping; "
                        "remaining events will be picked up by the background poller",
                        flush=True,
                    )
                    break
                invoice = _invoice_cache[invoice_id]
                status_raw = str((invoice or {}).get("Status") or "").upper()
                is_paid_or_settled = _is_invoice_paid(invoice)
                is_sent_or_authorised = status_raw in {"AUTHORISED", "PAID"}
                inv_number = str((invoice or {}).get("InvoiceNumber") or "").strip()
                event_key = inv_id_to_key.get(invoice_id, "")

                # Keep mapped diary entry in sync with Xero-side invoice edits and status.
                if event_key and ":" in event_key and creds and invoice:
                    cal_id, event_id = event_key.split(":", 1)
                    queue_event_target(event_key)
                    try:
                        gsvc = build_calendar_service_from_creds(creds)
                        ge = gsvc.events().get(calendarId=cal_id, eventId=event_id).execute()
                        cur_desc = ge.get("description") or ""
                        synced_desc = sync_invoice_block_from_xero(
                            cur_desc,
                            (invoice.get("LineItems") or []),
                        )
                        cur_summary = ge.get("summary")
                        desired_status = ""
                        if is_paid_or_settled:
                            desired_status = "green"
                        elif is_sent_or_authorised:
                            pay_mode = payment_choice(synced_desc or cur_desc)
                            desired_status = "green" if pay_mode in {"card", "cash"} else "yellow"

                        next_summary = cur_summary
                        if desired_status:
                            next_summary = set_title_status_emoji(next_summary, desired_status)
                        next_summary = set_title_mail_emoji(
                            next_summary,
                            "invoice send failed" in (synced_desc or "").lower(),
                        )

                        if synced_desc != cur_desc or next_summary != cur_summary:
                            update_event_description(
                                config,
                                event_id=event_id,
                                description=synced_desc,
                                summary=next_summary,
                                calendar_id=cal_id,
                            )
                            print(
                                f"[webhook] Calendar synced from Xero for {event_key} "
                                f"(status={status_raw or 'UNKNOWN'})",
                                flush=True,
                            )

                        if is_sent_or_authorised:
                            app_state = mark_invoice_sent(app_state, event_key)
                        if is_paid_or_settled:
                            app_state = mark_invoice_paid(app_state, event_key)
                        app_state = mark_recent_xero_webhook(app_state, event_key, invoice_id)
                        app_state = save_state_merged(config.state_file, app_state)
                    except Exception as exc:
                        print(f"[webhook] Calendar sync failed for {event_key}: {exc}", flush=True)

                if is_paid_or_settled:
                    print(f"[webhook] Invoice {inv_number} is PAID — updating sheet + calendar", flush=True)
                    _feed.push(f"Invoice {inv_number} paid — marking sheet row as Paid", "paid")
                    if creds and spreadsheet_id and inv_number:
                        try:
                            sheet_update_status = update_invoice_paid_in_sheet(
                                creds,
                                spreadsheet_id=spreadsheet_id,
                                sheet_name=sheet_name,
                                invoice_number=inv_number,
                            )
                            if sheet_update_status == "updated":
                                print(f"[webhook] Sheet row updated for {inv_number}", flush=True)
                                _feed.push(f"Sheet updated: {inv_number} marked as Paid", "paid")
                            elif sheet_update_status == "already_paid":
                                print(f"[webhook] Sheet row already up-to-date for {inv_number}", flush=True)
                            else:
                                print(
                                    f"[webhook] Sheet row update skipped for {inv_number}: {sheet_update_status}",
                                    flush=True,
                                )
                        except Exception as exc:
                            print(f"[webhook] Sheet update failed: {exc}", flush=True)

                    if event_key and ":" in event_key:
                        cal_id, event_id = event_key.split(":", 1)
                        queue_event_target(event_key)
                        try:
                            gsvc = build_calendar_service_from_creds(creds) if creds else None
                            if gsvc:
                                ge = gsvc.events().get(calendarId=cal_id, eventId=event_id).execute()
                                cur_summary = ge.get("summary")
                                updated_summary = set_title_status_emoji(cur_summary, "green")
                                updated_summary = set_title_mail_emoji(
                                    updated_summary,
                                    "invoice send failed" in (ge.get("description") or "").lower(),
                                )
                                if updated_summary != cur_summary:
                                    update_event_description(
                                        config,
                                        event_id=event_id,
                                        description=ge.get("description") or "",
                                        summary=updated_summary,
                                        calendar_id=cal_id,
                                    )
                                    print(f"[webhook] Calendar title set to green for {event_key}", flush=True)
                                    _feed.push(f"Calendar updated to paid (green): {inv_number or event_id}", "paid")

                                # Invoice-mode sales logging happens only after payment is actually made.
                                if payment_choice(ge.get("description")) == "invoice":
                                    sales_lines = extract_sales_lines(ge.get("description"))
                                    effective_sales_stats_fields = [
                                        str(s) for s in (sales_stats_fields or DEFAULT_SALES_STATS_FIELDS)
                                    ]
                                    if sales_lines:
                                        route = calendar_sales_sheets.get(cal_id) or {}
                                        sales_spreadsheet_id = (
                                            str(route.get("spreadsheet_id") or "").strip()
                                            or str(sales_sheet_target.get("spreadsheet_id") or "").strip()
                                        )
                                        sales_sheet_name = (
                                            str(route.get("sheet_name") or "").strip()
                                            or str(sales_sheet_target.get("sheet_name") or "Sales").strip()
                                            or "Sales"
                                        )
                                        if sales_spreadsheet_id:
                                            sales_total_ex = round(
                                                sum(
                                                    float(li.get("UnitAmount") or 0.0)
                                                    * float(li.get("Quantity") or 1.0)
                                                    for li in sales_lines
                                                ),
                                                2,
                                            )
                                            sales_total_inc = round(
                                                sum(
                                                    (float(li.get("UnitAmount") or 0.0)
                                                    * float(li.get("Quantity") or 1.0))
                                                    * (
                                                        1.2
                                                        if (li.get("TaxType") or "").upper() == "OUTPUT2"
                                                        else 1.0
                                                    )
                                                    for li in sales_lines
                                                ),
                                                2,
                                            )
                                            sales_marker = (
                                                f"{invoice_id}:invoice:sales:{sales_spreadsheet_id}:"
                                                f"{sales_sheet_name}:{len(sales_lines)}:"
                                                f"{sales_total_ex:.2f}:{sales_total_inc:.2f}"
                                            ).upper()
                                            if get_sales_log_marker(app_state, event_key) != sales_marker:
                                                from zoneinfo import ZoneInfo

                                                london_tz = ZoneInfo("Europe/London")

                                                def _fmt_london(iso_str: str) -> str:
                                                    if not iso_str:
                                                        return ""
                                                    try:
                                                        if "T" in iso_str:
                                                            obj = dt.datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
                                                            if obj.tzinfo is None:
                                                                obj = obj.replace(tzinfo=dt.timezone.utc)
                                                            return obj.astimezone(london_tz).strftime("%d/%m/%Y %H:%M")
                                                        obj = dt.date.fromisoformat(iso_str)
                                                        return obj.strftime("%d/%m/%Y")
                                                    except Exception:
                                                        return iso_str

                                                start = (ge.get("start", {}) or {}).get("dateTime") or (ge.get("start", {}) or {}).get("date") or ""
                                                end = (ge.get("end", {}) or {}).get("dateTime") or (ge.get("end", {}) or {}).get("date") or ""
                                                start_fmt = _fmt_london(start)
                                                end_fmt = _fmt_london(end)
                                                slot_text = f"{start_fmt} – {end_fmt}".strip(" –") if start_fmt != end_fmt else start_fmt
                                                customer_fields = parse_customer_fields(ge.get("description"))
                                                submitter_email = (
                                                    ((ge.get("creator", {}) or {}).get("email"))
                                                    or ((ge.get("organizer", {}) or {}).get("email"))
                                                    or ""
                                                ).strip().lower()
                                                submitter_name = submitter_aliases.get(submitter_email, submitter_email)
                                                event_id_raw = ge.get("id") or ""
                                                date_part = start.split("T", 1)[0].replace("-", "") if start else ""
                                                suffix = event_id_raw[-4:] if event_id_raw else "0000"
                                                event_id_display = (
                                                    f"GC-{date_part}-{suffix}" if date_part else (event_id_raw or event_key)
                                                )
                                                ensure_header(
                                                    creds,
                                                    spreadsheet_id=sales_spreadsheet_id,
                                                    sheet_name=sales_sheet_name,
                                                    stats_fields=effective_sales_stats_fields,
                                                )
                                                sales_lines_text: list[str] = []
                                                for line in sales_lines:
                                                    ex_vat = round(
                                                        float(line.get("UnitAmount") or 0.0)
                                                        * float(line.get("Quantity") or 1.0),
                                                        2,
                                                    )
                                                    sales_lines_text.append(
                                                        f"{line.get('Description') or ''} = £{ex_vat:.2f}"
                                                    )
                                                sales_row_event_id = f"{event_id_display}-S"
                                                append_stats_row(
                                                    creds,
                                                    spreadsheet_id=sales_spreadsheet_id,
                                                    sheet_name=sales_sheet_name,
                                                    event_key=f"{event_key}:sales",
                                                    stats_fields=effective_sales_stats_fields,
                                                    payload={
                                                        "submitter": submitter_name,
                                                        "customer": customer_fields.get("name") or "",
                                                        "invoice_number": inv_number,
                                                        "slot_datetime": slot_text,
                                                        "payment_method": "INVOICE",
                                                        "sales_item_desc": "\n".join(sales_lines_text),
                                                        "sales_total_ex_vat": f"{sales_total_ex:.2f}",
                                                    },
                                                    event_id_display=sales_row_event_id,
                                                    dedupe_signature={"Event ID": sales_row_event_id},
                                                    update_existing=True,
                                                )
                                                app_state = set_sales_log_marker(app_state, event_key, sales_marker)
                                                app_state = save_state_merged(config.state_file, app_state)
                                                print(f"[webhook] Sales rows upserted for paid invoice {inv_number}", flush=True)
                                                _feed.push(
                                                    f"Sales logged after payment: {inv_number or event_id}",
                                                    "success",
                                                )
                        except Exception as exc:
                            print(f"[webhook] Calendar status update failed for {event_key}: {exc}", flush=True)
            except Exception as exc:
                print(f"[webhook] Xero invoice fetch failed: {exc}", flush=True)

        trigger_poll()
        return "", 200

    @app.post("/admin/sync-today-invoices")
    @require_login
    def admin_sync_today_invoices():
        """
        Manual backfill: check today's active-calendar events that already have
        mapped Xero invoices, then mirror Xero line/status changes into Calendar.
        """
        creds = load_admin_credentials(config)
        xero_client = build_xero_client(config)
        if not creds:
            session["save_notice"] = "error:Google is not connected. Cannot run invoice sync."
            return redirect(url_for("index"))
        if not xero_client:
            session["save_notice"] = "error:Xero is not connected. Cannot run invoice sync."
            return redirect(url_for("index"))

        try:
            from zoneinfo import ZoneInfo
        except Exception:
            ZoneInfo = None  # type: ignore[assignment]

        london_tz = ZoneInfo("Europe/London") if ZoneInfo else dt.timezone.utc
        today_london = dt.datetime.now(london_tz).date()

        def _event_is_today_london(event_obj: dict) -> bool:
            start = (event_obj.get("start", {}) or {}).get("dateTime") or (
                event_obj.get("start", {}) or {}
            ).get("date")
            if not start:
                return False
            try:
                if "T" in start:
                    obj = dt.datetime.fromisoformat(start.replace("Z", "+00:00"))
                    if obj.tzinfo is None:
                        obj = obj.replace(tzinfo=dt.timezone.utc)
                    return obj.astimezone(london_tz).date() == today_london
                return dt.date.fromisoformat(start) == today_london
            except Exception:
                return False

        def _invoice_paid(invoice_obj: dict | None) -> bool:
            if not invoice_obj:
                return False
            status_raw = str(invoice_obj.get("Status") or "").upper()
            try:
                amount_due = float(invoice_obj.get("AmountDue") or 0.0)
            except Exception:
                amount_due = 0.0
            return status_raw == "PAID" or amount_due <= 0.0001

        app_state = load_state(config.state_file)
        inv_map = dict(app_state.get("event_invoice_map") or {})
        active_cals = set(get_active_calendars(config.admin_db_file, config.google_calendar_id))
        gsvc = build_calendar_service_from_creds(creds)

        scanned = 0
        updated = 0
        status_only = 0
        for event_key, invoice_id in inv_map.items():
            if ":" not in event_key:
                continue
            cal_id, event_id = event_key.split(":", 1)
            if cal_id not in active_cals:
                continue
            if not invoice_id:
                continue
            try:
                ge = gsvc.events().get(calendarId=cal_id, eventId=event_id).execute()
            except Exception:
                continue
            if not _event_is_today_london(ge):
                continue
            scanned += 1

            try:
                invoice = xero_client.get_invoice(invoice_id)
            except Exception:
                continue
            if not invoice:
                continue

            synced_desc = sync_invoice_block_from_xero(
                ge.get("description") or "",
                invoice.get("LineItems") or [],
            )
            status_raw = str(invoice.get("Status") or "").upper()
            is_paid = _invoice_paid(invoice)
            is_sent_or_authorised = status_raw in {"AUTHORISED", "PAID"}
            desired_status = ""
            if is_paid:
                desired_status = "green"
            elif is_sent_or_authorised:
                pay_mode = payment_choice(synced_desc or ge.get("description") or "")
                desired_status = "green" if pay_mode in {"card", "cash"} else "yellow"

            cur_summary = ge.get("summary")
            next_summary = cur_summary
            if desired_status:
                next_summary = set_title_status_emoji(next_summary, desired_status)
            next_summary = set_title_mail_emoji(
                next_summary,
                "invoice send failed" in (synced_desc or "").lower(),
            )

            changed = synced_desc != (ge.get("description") or "") or next_summary != cur_summary
            if changed:
                try:
                    update_event_description(
                        config,
                        event_id=event_id,
                        description=synced_desc,
                        summary=next_summary,
                        calendar_id=cal_id,
                    )
                    updated += 1
                    queue_event_target(event_key)
                except Exception:
                    continue
            elif desired_status:
                status_only += 1

            if is_sent_or_authorised:
                app_state = mark_invoice_sent(app_state, event_key)
            if is_paid:
                app_state = mark_invoice_paid(app_state, event_key)

        app_state = save_state_merged(config.state_file, app_state)
        trigger_poll()
        session["save_notice"] = (
            f"success:Today Xero sync checked {scanned} event(s), "
            f"updated {updated}, status-confirmed {status_only}."
        )
        return redirect(url_for("index"))

    @app.post("/setup/register-google-watches")
    @require_login
    def register_google_watches():
        trigger_poll()
        session["save_notice"] = (
            "success:Google Calendar watches are auto-managed now. "
            "Save Active Calendars and the app will create/renew watches automatically."
        )
        return redirect(url_for("index"))

    @app.post("/setup/stop-google-watches")
    @require_login
    def stop_google_watches():
        session["save_notice"] = (
            "success:Manual stop is disabled. "
            "Untick calendars in Active Calendars to remove their watches."
        )
        return redirect(url_for("index"))

    @app.post("/save-xero-webhook-key")
    @require_login
    def save_xero_webhook_key():
        key = (request.form.get("xero_webhook_key") or "").strip()
        set_xero_webhook_key(config.admin_db_file, key)
        set_xero_webhook_verified(config.admin_db_file, False)
        session["save_notice"] = "success:Xero webhook key saved. Now click \"Send intent to receive\" in the Xero Developer portal to verify."
        return redirect(url_for("index"))

    @app.get("/cashflows-sync")
    @require_login
    def cashflows_sync_page():
        cset = get_cashflows_settings(config.admin_db_file)
        end = dt.date.today()
        try:
            lookback_days = max(int(os.getenv("CASHFLOWS_RECONCILE_DAYS", "14") or "14"), 1)
        except Exception:
            lookback_days = 14
        start = end - dt.timedelta(days=lookback_days)
        production_enabled = (
            os.getenv("CASHFLOWS_RECONCILE_PRODUCTION", "false").lower() == "true"
            and not bool(config.dry_run)
        )
        csv_submit_production_enabled = (
            os.getenv("CASHFLOWS_CSV_SUBMIT_PRODUCTION", "false").strip().lower()
            in {"1", "true", "yes", "on"}
            and not bool(config.dry_run)
        )
        cashflows_ready = bool(
            str(cset.get("base_url") or "").strip()
            and str(cset.get("api_key") or "").strip()
        )
        mode_badge = (
            '<span class="px-2 py-1 rounded-full bg-red-50 text-red-700 border border-red-200 text-xs font-semibold">Production writes enabled</span>'
            if production_enabled
            else '<span class="px-2 py-1 rounded-full bg-emerald-50 text-emerald-700 border border-emerald-200 text-xs font-semibold">Testing mode</span>'
        )
        csv_submit_badge = (
            '<span class="px-2 py-1 rounded-full bg-sky-50 text-sky-700 border border-sky-200 text-xs font-semibold whitespace-nowrap">Live submit enabled</span>'
            if csv_submit_production_enabled
            else '<span class="px-2 py-1 rounded-full bg-emerald-50 text-emerald-700 border border-emerald-200 text-xs font-semibold whitespace-nowrap">Submit guarded &middot; test mode</span>'
        )
        csv_submit_help = (
            "Upload the merchant-account statement you downloaded from Cashflows. Preview is read-only. Ticked batches can then be submitted from this page; selected batches will write to Xero when you press Submit selected to Xero."
            if csv_submit_production_enabled
            else "Upload the merchant-account statement you downloaded from Cashflows. Preview is read-only. Ticked batches can then be submitted from this page; test mode shows the Xero payloads without writing anything."
        )
        cashflows_badge = (
            '<span class="px-2 py-1 rounded-full bg-emerald-50 text-emerald-700 border border-emerald-200 text-xs font-semibold">Cashflows configured</span>'
            if cashflows_ready
            else '<a href="/settings" class="px-2 py-1 rounded-full bg-amber-50 text-amber-700 border border-amber-200 text-xs font-semibold hover:bg-amber-100">⚠ Cashflows settings missing →</a>'
        )
        _oa_cf = get_openai_settings(config.admin_db_file)
        openai_configured = bool(_oa_cf.get("api_key") or (os.getenv("OPENAI_API_KEY") or "").strip())
        preflight_items = []
        if not cashflows_ready:
            _cf_missing = []
            if not str(cset.get("base_url") or "").strip():
                _cf_missing.append("Base URL")
            if not str(cset.get("api_key") or "").strip():
                _cf_missing.append("Cashflows API Key")
            _cf_missing_str = " and ".join(_cf_missing) if _cf_missing else "API credentials"
            preflight_items.append(
                '<div class="flex items-start gap-3 rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-sm">'
                '<span class="shrink-0 text-base">❌</span>'
                f'<div class="flex-1 text-red-800"><span class="font-semibold">Cashflows not configured</span>'
                f' \u2014 missing: <strong>{_cf_missing_str}</strong>.'
                ' <a href="/settings" class="underline font-semibold">Add in Settings \u2192</a></div>'
                '</div>'
            )
        if not openai_configured:
            preflight_items.append(
                '<div class="flex items-start gap-3 rounded-lg border border-amber-200 bg-amber-50 px-4 py-3 text-sm">'
                '<span class="shrink-0 text-base">⚠️</span>'
                '<div class="flex-1 text-amber-800"><span class="font-semibold">AI calendar matching not configured</span>'
                ' \u2014 without an OpenAI API key, customer names and amounts cannot be extracted from calendar'
                ' events. Events will still appear by time proximity but name/amount matching is disabled.'
                ' <a href="/settings" class="underline font-semibold">Add OpenAI key in Settings \u2753</a></div>'
                '</div>'
            )
        if preflight_items:
            initial_panel = (
                '<div id="initial-panel" class="rounded-xl border border-gray-200 bg-white p-6">'
                '<div class="flex items-center justify-between gap-2 mb-1">'
                '<h2 class="text-sm font-semibold text-gray-900">Pre-scan checklist</h2>'
                '<span class="text-xs text-gray-400">No Xero changes are made during scan</span>'
                '</div>'
                '<div class="space-y-2 mt-3">' + '\n'.join(preflight_items) + '</div>'
                '</div>'
            )
        else:
            initial_panel = (
                '<div id="initial-panel" class="flex items-center gap-2 text-xs text-gray-400 px-1">'
                '<span class="inline-block w-2 h-2 rounded-full bg-emerald-500"></span>'
                'All integrations ready \u00b7 no Xero changes are made during scan'
                '</div>'
            )
        # Xero connection banner — check once on page load so the user knows
        # before they upload whether Xero is reachable.
        try:
            _xc = build_xero_client(config)
            xero_ok_banner = bool(_xc)
        except Exception:
            xero_ok_banner = False

        if xero_ok_banner:
            xero_banner = ""  # connected — no banner needed
        else:
            xero_banner = (
                '<div class="rounded-xl border border-red-200 bg-red-50 px-5 py-3 '
                'flex items-center gap-3">'
                '<span class="text-red-600 text-lg">⚠</span>'
                '<div class="flex-1 text-sm text-red-800">'
                '<strong>Xero is not connected.</strong> '
                'Invoice matching will be skipped until you reconnect. '
                '</div>'
                '<a href="/settings" class="shrink-0 px-3 py-1.5 rounded-lg bg-red-600 '
                'hover:bg-red-700 text-white text-xs font-semibold">Reconnect in Settings →</a>'
                '</div>'
            )

        corr_sheet_id = get_cashflows_correlation_sheet_id(config.admin_db_file)
        if corr_sheet_id:
            try:
                _lookup = fetch_card_lookup(corr_sheet_id, timeout=8)
                corr_sheet_msg = (
                    f"✓ Connected — {_lookup.total_card} CARD rows, "
                    f"{_lookup.total_rows} total"
                )
                corr_sheet_cls = "text-emerald-700"
            except Exception as _e:
                corr_sheet_msg = f"⚠ Could not reach sheet: {str(_e)[:80]}"
                corr_sheet_cls = "text-amber-700"
        else:
            corr_sheet_msg = "Not configured — using GC- prefix heuristic as fallback."
            corr_sheet_cls = "text-gray-400"
        body = f"""
        <main class="min-h-screen bg-gray-50">
          <header class="bg-white border-b border-gray-200">
            <div class="max-w-7xl mx-auto px-5 py-3 flex items-center justify-between gap-4">
              <div>
                <h1 class="text-lg font-semibold text-gray-950">Cashflows Sync</h1>
                <p class="text-xs text-gray-500">CFE SETT bank-line matching and settlement preview</p>
              </div>
              <div class="flex items-center gap-2">
                <details class="relative">
                  <summary class="list-none cursor-pointer w-8 h-8 rounded-full border border-gray-300 bg-white hover:bg-gray-50 text-gray-700 text-sm font-semibold flex items-center justify-center" title="Cashflows Sync information">i</summary>
                  <div class="absolute right-0 mt-2 w-80 max-w-[calc(100vw-2rem)] rounded-xl border border-gray-200 bg-white shadow-lg p-4 z-30 text-xs text-gray-600 space-y-2">
                    <p class="font-semibold text-gray-900">How to use this page</p>
                    <p>Download the merchant-account statement CSV from Cashflows for the recommended date range, then upload it here.</p>
                    <p>The preview reads Xero and the CSV, but it does not write to Xero while test mode is active.</p>
                  </div>
                </details>
                <a href="https://secure.cashflows.com/admin/login" target="_blank" rel="noopener noreferrer"
                  class="h-8 px-3 rounded-lg border border-gray-300 bg-white hover:bg-gray-50 text-gray-800 text-xs font-semibold inline-flex items-center">
                  Open Cashflows login
                </a>
              </div>
            </div>
          </header>

          <section class="max-w-7xl mx-auto px-5 py-6 space-y-5">
            {xero_banner}
            <div class="flex flex-col lg:flex-row lg:items-center lg:justify-between gap-4">
              <div class="flex flex-wrap items-center gap-2">
                {mode_badge}
                {cashflows_badge}
                <span id="scan-summary" class="text-xs text-gray-500"></span>
              </div>
              <div class="flex flex-wrap items-end gap-3">
                <label class="block">
                  <span class="block text-xs font-medium text-gray-600 mb-1">API scan from</span>
                  <input id="date-from" type="date" value="{start.isoformat()}" class="h-9 rounded-lg border border-gray-300 px-3 text-sm">
                </label>
                <label class="block">
                  <span class="block text-xs font-medium text-gray-600 mb-1">API scan to</span>
                  <input id="date-to" type="date" value="{end.isoformat()}" class="h-9 rounded-lg border border-gray-300 px-3 text-sm">
                </label>
                <button id="scan-btn" type="button" class="h-9 px-4 rounded-lg bg-indigo-600 hover:bg-indigo-700 text-white text-sm font-semibold shadow-sm">
                  API scan only
                </button>
                <button id="diagnostics-btn" type="button" class="h-9 px-4 rounded-lg border border-gray-300 bg-white hover:bg-gray-50 text-gray-800 text-sm font-semibold">
                  Test API Reads
                </button>
              </div>
            </div>

            {initial_panel}

            <div class="rounded-xl border border-indigo-200 bg-white p-6 space-y-4">
              <div class="flex items-start justify-between gap-4 flex-wrap">
                <div>
                  <h2 class="text-sm font-semibold text-gray-900">Reconcile from Cashflows CSV</h2>
                  <p class="text-sm text-gray-600 mt-1">{csv_submit_help}</p>
                </div>
                {csv_submit_badge}
              </div>

              <div id="recommended-range" class="hidden rounded-lg border border-indigo-100 bg-indigo-50 p-3 text-xs text-indigo-900">
                <span class="font-semibold">Recommended Cashflows export range:</span>
                <span id="rec-range-text"></span>
                <span id="rec-range-reason" class="block text-indigo-700 mt-0.5"></span>
              </div>

              <div class="rounded-lg border border-gray-200 bg-gray-50 p-4 space-y-2">
                <div class="flex items-start justify-between gap-3 flex-wrap">
                  <div>
                    <div class="text-xs font-semibold text-gray-700">Cashflows CSV export</div>
                    <p class="text-xs text-gray-500 mt-1">Log in to Cashflows, export the merchant-account statement CSV for the date range above, then upload it below.</p>
                  </div>
                  <a href="https://secure.cashflows.com/admin/login" target="_blank" rel="noopener noreferrer"
                    class="h-8 px-3 rounded-lg bg-white border border-gray-300 hover:bg-gray-50 text-gray-800 text-xs font-semibold inline-flex items-center whitespace-nowrap">
                    Open Cashflows
                  </a>
                </div>
              </div>

              <div class="rounded-lg border border-gray-200 bg-gray-50 p-4 space-y-2">
                <div class="flex items-center gap-2">
                  <div class="text-xs font-semibold text-gray-700">Payment Method correlation sheet</div>
                  {'<span id="sheet-saved-badge" class="px-2 py-0.5 rounded-full bg-emerald-100 text-emerald-700 text-[11px] font-semibold border border-emerald-200">✓ Saved</span>' if corr_sheet_id else '<span id="sheet-saved-badge" class="px-2 py-0.5 rounded-full bg-gray-100 text-gray-400 text-[11px] border border-gray-200">Not set</span>'}
                </div>
                <p class="text-xs text-gray-500">Paste the Google Sheet ID or full URL. Once saved it is remembered permanently — you only need to update it if the sheet changes.</p>
                <div class="flex gap-2 items-center flex-wrap">
                  <input id="correlation-sheet-input" type="text"
                    value="{corr_sheet_id}"
                    placeholder="Sheet ID or full Google Sheets URL"
                    class="flex-1 min-w-0 rounded-lg border border-gray-300 px-3 py-1.5 text-xs font-mono">
                  <button id="save-correlation-sheet-btn" type="button"
                    class="h-8 px-3 rounded-lg bg-indigo-600 hover:bg-indigo-700 text-white text-xs font-semibold shadow-sm disabled:opacity-50">
                    Save
                  </button>
                </div>
                <div id="correlation-sheet-status" class="text-xs {corr_sheet_cls}">{corr_sheet_msg}</div>
              </div>

              <div class="flex flex-wrap items-center gap-3">
                <input id="csv-file" type="file" accept=".csv,text/csv" class="block text-sm text-gray-700 file:mr-3 file:py-2 file:px-4 file:rounded-lg file:border-0 file:text-sm file:font-semibold file:bg-indigo-600 file:text-white hover:file:bg-indigo-700">
                <button id="upload-csv-btn" type="button" class="h-9 px-4 rounded-lg bg-indigo-600 hover:bg-indigo-700 text-white text-sm font-semibold shadow-sm disabled:opacity-50">
                  Upload CSV &amp; preview
                </button>
                <span id="csv-status" class="text-xs text-gray-500"></span>
              </div>

              <div id="csv-totals" class="hidden grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-3"></div>
              <div id="csv-error" class="hidden rounded-lg border border-red-200 bg-red-50 p-3 text-sm text-red-800"></div>
            </div>

            <div id="csv-submit-panel" class="hidden rounded-xl border border-emerald-200 bg-white p-4 space-y-3">
              <div class="flex items-start justify-between gap-3 flex-wrap">
                <div>
                  <h2 class="text-sm font-semibold text-gray-900">Submit approved batches to Xero</h2>
                  <p id="csv-submit-help" class="text-xs text-gray-500 mt-1">Tick the batches you are happy with, then submit only those selected batches.</p>
                </div>
                <div class="flex items-center gap-3 flex-wrap justify-end">
                  <label class="inline-flex items-center gap-2 rounded-lg border border-gray-200 bg-gray-50 px-3 py-2 text-xs font-semibold text-gray-700">
                    <input id="csv-preview-mode" type="checkbox" class="h-4 w-4 rounded border-gray-300 text-indigo-600">
                    Preview mode
                  </label>
                  <button id="submit-csv-btn" type="button" class="h-9 px-4 rounded-lg bg-emerald-600 hover:bg-emerald-700 text-white text-sm font-semibold shadow-sm disabled:opacity-50">
                    Submit selected to Xero
                  </button>
                </div>
              </div>
              <div id="csv-submit-status" class="text-xs text-gray-500"></div>
              <div id="csv-submit-progress" class="hidden rounded-xl border border-emerald-200 bg-emerald-50/60 p-4">
                <div class="flex items-center gap-3">
                  <svg id="csv-progress-spinner" class="animate-spin h-5 w-5 text-emerald-600 shrink-0" viewBox="0 0 24 24" fill="none">
                    <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                    <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"></path>
                  </svg>
                  <div class="min-w-0 flex-1">
                    <div id="csv-progress-title" class="text-sm font-semibold text-emerald-900">Reconciling with Xero…</div>
                    <div id="csv-progress-msg" class="text-xs text-emerald-800/90 mt-0.5 truncate"></div>
                  </div>
                  <div id="csv-progress-count" class="text-sm font-bold text-emerald-800 tabular-nums shrink-0">0 / 0</div>
                </div>
                <div class="mt-3 h-2.5 w-full rounded-full bg-emerald-100 overflow-hidden">
                  <div id="csv-progress-bar" class="h-full bg-emerald-600 transition-all duration-500 ease-out" style="width:0%"></div>
                </div>
                <p id="csv-progress-note" class="mt-2 text-[11px] text-emerald-700/80">You can safely close this page — the submission keeps running on the server and will pick up where it left off when you come back.</p>
              </div>
              <div id="csv-submit-output" class="hidden text-xs text-gray-700 bg-gray-50 border border-gray-200 rounded-lg p-3 overflow-x-auto"></div>
            </div>

            <div id="csv-results" class="hidden space-y-3"></div>

            <details class="rounded-xl border border-gray-200 bg-white">
              <summary class="cursor-pointer px-4 py-3 text-sm font-semibold text-gray-900">Manual Cashflows settlement JSON for API fallback testing</summary>
              <div class="px-4 pb-4 space-y-2">
                <p class="text-xs text-gray-600">Optional. If the Cashflows settlement endpoint is still returning 404, paste a settlement response or rows here to test matching/review/payloads using real Xero reads and manual Cashflows data.</p>
                <textarea id="manual-settlements-json" rows="7" class="w-full rounded-lg border border-gray-300 px-3 py-2 text-xs font-mono" placeholder='{{"Settlements":[{{"SettlementID":"sett-1","SettlementDate":"2026-06-18","GrossAmount":"120.00","NetAmount":"115.00","Fees":"5.00"}}]}}'></textarea>
              </div>
            </details>

            <div id="loading-panel" class="hidden rounded-xl border border-indigo-200 bg-indigo-50 p-5">
              <div class="flex items-center gap-3">
                <div class="w-5 h-5 border-2 border-indigo-200 border-t-indigo-600 rounded-full animate-spin"></div>
                <div>
                  <p class="text-sm font-semibold text-indigo-950">Scanning Cashflows and Xero</p>
                  <p class="text-xs text-indigo-700">Fetching CFE SETT lines, settlement batches, and open invoices.</p>
                </div>
              </div>
            </div>

            <div id="error-panel" class="hidden rounded-xl border border-red-200 bg-red-50 p-4 text-sm text-red-800"></div>

            <div id="diagnostics-panel" class="hidden rounded-xl border border-gray-200 bg-white p-4">
              <h2 class="text-sm font-semibold text-gray-950">API Diagnostics</h2>
              <pre id="diagnostics-output" class="mt-2 whitespace-pre-wrap text-xs text-gray-700"></pre>
            </div>

            <div id="results-panel" class="hidden space-y-3">
              <div class="grid grid-cols-12 gap-3 px-3 py-2 text-[11px] font-semibold uppercase tracking-wide text-gray-500">
                <div class="col-span-12 lg:col-span-3">Xero Bank Line</div>
                <div class="col-span-12 lg:col-span-4">Matched Cashflows Payout</div>
                <div class="col-span-12 lg:col-span-4">Linked Xero Invoices/Jobs</div>
                <div class="col-span-12 lg:col-span-1 text-right">Review</div>
              </div>
              <div id="match-rows" class="space-y-3"></div>
            </div>

            <div id="final-panel" class="hidden rounded-xl border border-emerald-200 bg-emerald-50 p-4">
              <h2 class="text-sm font-semibold text-emerald-950">Processing Summary</h2>
              <pre id="final-summary" class="mt-2 whitespace-pre-wrap text-xs text-emerald-900"></pre>
            </div>
          </section>

          <div id="review-modal" class="hidden fixed inset-0 z-50 bg-gray-950/60 items-center justify-center p-4">
            <div class="w-full max-w-2xl rounded-xl bg-white shadow-xl border border-gray-200">
              <div class="px-5 py-4 border-b border-gray-200 flex items-center justify-between">
                <h2 class="text-base font-semibold text-gray-950">Review Match</h2>
                <button type="button" id="modal-close" class="w-8 h-8 rounded-lg hover:bg-gray-100 text-gray-500">×</button>
              </div>
              <div class="p-5 space-y-4">
                <div class="rounded-lg border border-gray-200 bg-gray-50 p-4">
                  <p id="modal-logic" class="text-sm text-gray-800"></p>
                </div>
                <div id="modal-detail" class="text-xs text-gray-600 space-y-1"></div>
                <details class="rounded-lg border border-gray-200 bg-gray-50">
                  <summary class="cursor-pointer px-3 py-2 text-xs font-semibold text-gray-700">Submission payload preview</summary>
                  <pre id="modal-payloads" class="px-3 pb-3 whitespace-pre-wrap text-[11px] text-gray-700 overflow-x-auto"></pre>
                </details>
                <div class="flex items-center justify-end gap-2">
                  <button type="button" id="modal-cancel" class="px-3 py-2 rounded-lg border border-gray-300 text-sm text-gray-700 hover:bg-gray-50">Cancel</button>
                  <button type="button" id="confirm-btn" class="px-3 py-2 rounded-lg bg-emerald-600 hover:bg-emerald-700 text-sm font-semibold text-white">Confirm Match</button>
                </div>
              </div>
            </div>
          </div>
        </main>

        <script>
        let previewId = "";
        let matches = [];
        let activeMatch = null;

        const scanBtn = document.getElementById('scan-btn');
        const diagnosticsBtn = document.getElementById('diagnostics-btn');
        const dateFrom = document.getElementById('date-from');
        const dateTo = document.getElementById('date-to');
        const manualSettlementsJson = document.getElementById('manual-settlements-json');
        const initialPanel = document.getElementById('initial-panel');
        const loadingPanel = document.getElementById('loading-panel');
        const errorPanel = document.getElementById('error-panel');
        const resultsPanel = document.getElementById('results-panel');
        const rows = document.getElementById('match-rows');
        const summary = document.getElementById('scan-summary');
        const finalPanel = document.getElementById('final-panel');
        const finalSummary = document.getElementById('final-summary');
        const diagnosticsPanel = document.getElementById('diagnostics-panel');
        const diagnosticsOutput = document.getElementById('diagnostics-output');
        const modal = document.getElementById('review-modal');
        const modalLogic = document.getElementById('modal-logic');
        const modalDetail = document.getElementById('modal-detail');
        const modalPayloads = document.getElementById('modal-payloads');
        const confirmBtn = document.getElementById('confirm-btn');

        const CF_DRY_RUN = {'true' if config.dry_run else 'false'};

        function esc(s) {{
          return String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
        }}

        function money(n) {{
          const value = Number(n || 0);
          return '£' + value.toFixed(2);
        }}

        // British date display: "2026-05-09" -> "09/05/2026". Leaves any
        // already-formatted or empty value untouched.
        function gb(d) {{
          const m = String(d ?? '').match(/^(\\d{{4}})-(\\d{{2}})-(\\d{{2}})/);
          return m ? (m[3] + '/' + m[2] + '/' + m[1]) : String(d ?? '');
        }}

        function showError(message) {{
          errorPanel.textContent = message;
          errorPanel.classList.remove('hidden');
        }}

        function hideError() {{
          errorPanel.classList.add('hidden');
          errorPanel.textContent = '';
        }}

        function renderRows() {{
          rows.innerHTML = '';
          if (!matches.length) {{
            rows.innerHTML = '<div class="rounded-xl border border-gray-200 bg-white p-6 text-sm text-gray-500">No CFE SETT bank lines found for this date range.</div>';
            return;
          }}
          matches.forEach((m, idx) => {{
            const bank = m.bank_line || {{}};
            const settlementHtml = (m.settlements || []).length
              ? (m.settlements || []).map(s => `
                  <div class="rounded-lg border border-gray-200 bg-white p-3">
                    <div class="font-semibold text-gray-900">${{esc(s.id)}}</div>
                    <div class="text-xs text-gray-500">${{esc(s.settlement_date || '')}}</div>
                    <div class="mt-2 grid grid-cols-3 gap-2 text-xs">
                      <div><span class="text-gray-500">Gross</span><br><strong>${{money(s.gross_amount)}}</strong></div>
                      <div><span class="text-gray-500">Net</span><br><strong>${{money(s.net_amount)}}</strong></div>
                      <div><span class="text-gray-500">Fees</span><br><strong>${{money(s.fees)}}</strong></div>
                    </div>
                  </div>`).join('')
              : '<div class="rounded-lg border border-amber-200 bg-amber-50 p-3 text-sm text-amber-800">No Cashflows payout matched.</div>';
            const invoiceHtml = (m.invoices || []).length
              ? (m.invoices || []).map(inv => `
                  <div class="rounded-lg border border-gray-200 bg-white p-3">
                    <div class="font-semibold text-gray-900">${{esc(inv.number)}}</div>
                    <div class="text-xs text-gray-500">${{esc(inv.contact_name || '')}}</div>
                    <div class="mt-2 text-xs text-gray-700">Due ${{money(inv.amount_due)}} · Total ${{money(inv.total)}}</div>
                  </div>`).join('')
              : '<div class="rounded-lg border border-amber-300 bg-amber-50 p-3 text-sm font-semibold text-amber-900">⚠️ No Matching Invoice Found in Xero - Auto-Creation Required</div>';
            const methodColor = m.method === 'unmatched' ? 'text-red-700 bg-red-50 border-red-200' : 'text-emerald-700 bg-emerald-50 border-emerald-200';
            const row = document.createElement('div');
            row.className = 'grid grid-cols-12 gap-3 rounded-xl border border-gray-200 bg-white p-3 shadow-sm';
            row.innerHTML = `
              <div class="col-span-12 lg:col-span-3">
                <div class="text-sm font-semibold text-gray-950">${{esc(bank.description || '')}}</div>
                <div class="text-xs text-gray-500 mt-1">${{esc(gb(bank.date))}}</div>
                <div class="text-lg font-semibold text-gray-900 mt-2">${{money(bank.amount)}}</div>
                <div class="inline-flex mt-2 px-2 py-0.5 rounded-full border text-[11px] ${{methodColor}}">${{esc(m.method)}} · ${{Number(m.confidence || 0)}}%</div>
              </div>
              <div class="col-span-12 lg:col-span-4 space-y-2">
                ${{settlementHtml}}
                <div class="text-xs text-gray-600">Merchant Fee: <strong>${{money(m.merchant_fee)}}</strong> · Difference: <strong>${{money(m.difference)}}</strong></div>
              </div>
              <div class="col-span-12 lg:col-span-4 space-y-2">${{invoiceHtml}}</div>
              <div class="col-span-12 lg:col-span-1 flex lg:justify-end">
                <button type="button" class="review-btn h-9 px-3 rounded-lg bg-gray-900 hover:bg-gray-800 text-white text-sm" data-index="${{idx}}">Review</button>
              </div>
            `;
            rows.appendChild(row);
          }});
          document.querySelectorAll('.review-btn').forEach(btn => {{
            btn.addEventListener('click', () => openReview(Number(btn.dataset.index)));
          }});
        }}

        function openReview(index) {{
          activeMatch = matches[index];
          if (!activeMatch) return;
          modalLogic.textContent = activeMatch.logic || '';
          const aiText = activeMatch.ai_reason ? `<div>AI: ${{esc(activeMatch.ai_reason)}}</div>` : '';
          modalPayloads.textContent = JSON.stringify(activeMatch.submission_preview || {{}}, null, 2);
          modalDetail.innerHTML = `
            <div>Method: <strong>${{esc(activeMatch.method)}}</strong></div>
            <div>Confidence: <strong>${{Number(activeMatch.confidence || 0)}}%</strong></div>
            <div>Merchant fee: <strong>${{money(activeMatch.merchant_fee)}}</strong></div>
            ${{aiText}}
          `;
          modal.classList.remove('hidden');
          modal.classList.add('flex');
        }}

        function closeModal() {{
          modal.classList.add('hidden');
          modal.classList.remove('flex');
          activeMatch = null;
        }}

        document.getElementById('modal-close').addEventListener('click', closeModal);
        document.getElementById('modal-cancel').addEventListener('click', closeModal);

        diagnosticsBtn.addEventListener('click', async () => {{
          hideError();
          diagnosticsPanel.classList.remove('hidden');
          diagnosticsOutput.textContent = 'Running read-only diagnostics...';
          diagnosticsBtn.disabled = true;
          try {{
            const resp = await fetch('/cashflows-sync/diagnostics', {{
              method: 'POST',
              headers: {{'Content-Type': 'application/json'}},
              body: JSON.stringify({{date_from: dateFrom.value, date_to: dateTo.value}})
            }});
            const data = await resp.json();
            if (!resp.ok) throw new Error(data.error || 'Diagnostics failed');
            diagnosticsOutput.textContent = JSON.stringify(data, null, 2);
          }} catch (err) {{
            diagnosticsOutput.textContent = '';
            showError(err.message || String(err));
          }} finally {{
            diagnosticsBtn.disabled = false;
          }}
        }});

        scanBtn.addEventListener('click', async () => {{
          hideError();
          finalPanel.classList.add('hidden');
          initialPanel.classList.add('hidden');
          resultsPanel.classList.add('hidden');
          loadingPanel.classList.remove('hidden');
          scanBtn.disabled = true;
          try {{
            const resp = await fetch('/cashflows-sync/preview', {{
              method: 'POST',
              headers: {{'Content-Type': 'application/json'}},
              body: JSON.stringify({{
                date_from: dateFrom.value,
                date_to: dateTo.value,
                manual_settlements_json: manualSettlementsJson.value || ''
              }})
            }});
            const data = await resp.json();
            if (!resp.ok) throw new Error(data.error || 'Scan failed');
            previewId = data.preview_id || '';
            matches = data.matches || [];
            const c = data.counts || {{}};
            summary.textContent = `${{c.xero_cfe_bank_lines || 0}} CFE SETT lines · ${{c.cashflows_settlements || 0}} settlements · ${{c.open_xero_invoices || 0}} open invoices`;
            renderRows();
            resultsPanel.classList.remove('hidden');
          }} catch (err) {{
            showError(err.message || String(err));
            initialPanel.classList.remove('hidden');
          }} finally {{
            loadingPanel.classList.add('hidden');
            scanBtn.disabled = false;
          }}
        }});

        confirmBtn.addEventListener('click', async () => {{
          if (!activeMatch) return;
          confirmBtn.disabled = true;
          try {{
            const resp = await fetch('/cashflows-sync/confirm', {{
              method: 'POST',
              headers: {{'Content-Type': 'application/json'}},
              body: JSON.stringify({{preview_id: previewId, match_id: activeMatch.id}})
            }});
            const data = await resp.json();
            if (!resp.ok) throw new Error(data.error || 'Confirm failed');
            finalSummary.textContent = JSON.stringify(data, null, 2);
            finalPanel.classList.remove('hidden');
            closeModal();
          }} catch (err) {{
            showError(err.message || String(err));
          }} finally {{
            confirmBtn.disabled = false;
          }}
        }});

        // ---- Cashflows CSV upload (Phase 1: preview only, no writes) ----
        const csvFile = document.getElementById('csv-file');
        const uploadCsvBtn = document.getElementById('upload-csv-btn');
        const csvStatus = document.getElementById('csv-status');
        const csvError = document.getElementById('csv-error');
        const csvTotals = document.getElementById('csv-totals');
        const csvResults = document.getElementById('csv-results');
        const csvSubmitPanel = document.getElementById('csv-submit-panel');
        const csvSubmitBtn = document.getElementById('submit-csv-btn');
        const csvSubmitStatus = document.getElementById('csv-submit-status');
        const csvSubmitOutput = document.getElementById('csv-submit-output');
        const csvProgress = document.getElementById('csv-submit-progress');
        const csvPreviewMode = document.getElementById('csv-preview-mode');
        const csvProgressTitle = document.getElementById('csv-progress-title');
        const csvProgressMsg = document.getElementById('csv-progress-msg');
        const csvProgressCount = document.getElementById('csv-progress-count');
        const csvProgressBar = document.getElementById('csv-progress-bar');
        const csvProgressSpinner = document.getElementById('csv-progress-spinner');
        const csvProgressNote = document.getElementById('csv-progress-note');
        const recRange = document.getElementById('recommended-range');
        const recRangeText = document.getElementById('rec-range-text');
        const recRangeReason = document.getElementById('rec-range-reason');
        let _csvPreviewData = null;
        let _submitPollTimer = null;
        let _submitProgressStatus = '';

        function renderSubmitProgress(p) {{
          if (!csvProgress) return;
          const total = p.total || 0;
          const done = p.completed || 0;
          const pct = p.percent != null ? p.percent : (total ? Math.round((done / total) * 100) : 0);
          csvProgress.classList.remove('hidden');
          _submitProgressStatus = p.status || '';
          csvProgressCount.textContent = done + ' / ' + total;
          csvProgressBar.style.width = pct + '%';
          csvProgressMsg.textContent = p.message || '';
          if (p.status === 'paused') {{
            csvProgressTitle.textContent = 'Paused — waiting for Xero';
            csvProgressBar.className = 'h-full bg-amber-500 transition-all duration-500 ease-out';
            if (csvProgressNote) {{
              csvProgressNote.textContent = 'This is waiting for Xero to cool down. Leave the page open or come back later; it will continue when Xero allows it.';
              csvProgressNote.className = 'mt-2 text-[11px] text-amber-700/90';
            }}
          }} else if (p.status === 'done') {{
            csvProgressTitle.textContent = '✅ Reconciliation complete';
            csvProgressBar.className = 'h-full bg-emerald-600 transition-all duration-500 ease-out';
            if (csvProgressSpinner) csvProgressSpinner.classList.add('hidden');
            if (csvProgressNote) {{
              csvProgressNote.textContent = 'Finished batches are saved and will not be resent.';
              csvProgressNote.className = 'mt-2 text-[11px] text-emerald-700/80';
            }}
          }} else if (p.status === 'error') {{
            csvProgressTitle.textContent = '⚠ Submission stopped';
            csvProgressBar.className = 'h-full bg-red-500 transition-all duration-500 ease-out';
            if (csvProgressSpinner) csvProgressSpinner.classList.add('hidden');
            if (csvProgressNote) {{
              csvProgressNote.textContent = 'This is stopped. It will not retry by itself; refresh the preview, check the unfinished batch, then submit only the remaining batch.';
              csvProgressNote.className = 'mt-2 text-[11px] text-red-700/90';
            }}
          }} else {{
            csvProgressTitle.textContent = 'Reconciling with Xero…';
            csvProgressBar.className = 'h-full bg-emerald-600 transition-all duration-500 ease-out';
            if (csvProgressSpinner) csvProgressSpinner.classList.remove('hidden');
            if (csvProgressNote) {{
              csvProgressNote.textContent = 'You can safely close this page — the submission keeps running on the server and will pick up where it left off when you come back.';
              csvProgressNote.className = 'mt-2 text-[11px] text-emerald-700/80';
            }}
          }}
        }}

        function _markProgressBatchesSubmitted(p) {{
          (p.completed_batch_ids || []).forEach(function(bid) {{
            _setSubmitted(bid, true);
            _setChecked(bid, false);
          }});
          if (_csvPreviewData) renderCsvResults(_csvPreviewData);
          updateCsvSubmitPanel();
        }}

        async function pollSubmitProgress(showControls) {{
          try {{
            const resp = await fetch('/cashflows-sync/submit-progress');
            const p = await resp.json();
            if (!p || (p.active === false && !p.status)) {{
              if (csvProgress) csvProgress.classList.add('hidden');
              return;
            }}
            renderSubmitProgress(p);
            if (p.status === 'running' || p.status === 'paused') {{
              if (csvSubmitBtn) csvSubmitBtn.disabled = true;
              if (csvSubmitStatus) csvSubmitStatus.textContent = '';
              _submitPollTimer = setTimeout(function() {{ pollSubmitProgress(false); }}, 2000);
            }} else {{
              // done or error — finalise
              if (_submitPollTimer) {{ clearTimeout(_submitPollTimer); _submitPollTimer = null; }}
              _markProgressBatchesSubmitted(p);
              if (p.status === 'done') {{
                csvSubmitStatus.textContent = p.message || 'Reconciliation complete.';
              }} else {{
                csvShowError(p.message || p.error || 'Submission stopped.');
              }}
            }}
          }} catch (err) {{
            _submitPollTimer = setTimeout(function() {{ pollSubmitProgress(false); }}, 4000);
          }}
        }}

        const STATUS_META = {{
          ready: {{label: '✅ Invoices add up — ready to reconcile', cls: 'border-emerald-200 bg-emerald-50 text-emerald-800'}},
          needs_review: {{label: '🔎 Worth a quick check', cls: 'border-orange-200 bg-orange-50 text-orange-800'}},
          waiting_invoices: {{label: '⏳ An invoice is still missing', cls: 'border-amber-200 bg-amber-50 text-amber-800'}},
          no_bank_line: {{label: 'Not shown in Xero reconcile list', cls: 'border-gray-200 bg-gray-50 text-gray-700'}},
          prepared_in_xero: {{label: 'Prepared in Xero — press OK in bank reconciliation', cls: 'border-sky-200 bg-sky-50 text-sky-800'}},
          already_reconciled: {{label: '☑️ Already reconciled in Xero', cls: 'border-emerald-200 bg-emerald-50 text-emerald-700'}}
        }};

        function csvShowError(msg) {{
          csvError.textContent = msg;
          csvError.classList.remove('hidden');
        }}

        function statChip(label, value) {{
          return `<div class="rounded-lg border border-gray-200 bg-gray-50 p-3">
            <div class="text-[11px] uppercase tracking-wide text-gray-500">${{esc(label)}}</div>
            <div class="text-sm font-semibold text-gray-900 mt-0.5">${{value}}</div>
          </div>`;
        }}

        function renderCsvTotals(t) {{
          csvTotals.innerHTML = [
            statChip('Gross sales', money(t.gross_sales)),
            statChip('Merchant fees', money(t.merchant_fees)),
            statChip('Decline fees', money(t.decline_fees)),
            statChip('Remitted', money(t.remitted)),
            statChip('Sales', String(t.sale_count)),
            statChip('Payouts', String(t.payout_count))
          ].join('');
          csvTotals.classList.remove('hidden');
        }}

        // Storage key for confirmed batches (scoped to this preview upload)
        let _previewId = null;
        function _checkedKey(batchId) {{ return 'cf_confirmed_' + _previewId + '_' + batchId; }}
        function _isChecked(batchId) {{ return localStorage.getItem(_checkedKey(batchId)) === '1'; }}
        function _setChecked(batchId, v) {{
          if (v) localStorage.setItem(_checkedKey(batchId), '1');
          else localStorage.removeItem(_checkedKey(batchId));
        }}
        function _submittedKey(batchId) {{ return 'cf_submitted_' + _previewId + '_' + batchId; }}
        function _isSubmitted(batchId) {{ return localStorage.getItem(_submittedKey(batchId)) === '1'; }}
        function _setSubmitted(batchId, v) {{
          if (v) localStorage.setItem(_submittedKey(batchId), '1');
          else localStorage.removeItem(_submittedKey(batchId));
        }}
        function _batchHasPaidInvoices(batch) {{
          if (!batch) return false;
          if (batch.has_paid_invoices) return true;
          return (batch.sales || []).some(function(s, idx) {{
            const selected = _selectedInvoiceForSale(batch, s, idx);
            return selected && selected.is_open === false;
          }});
        }}
        function _batchCanSubmit(batch) {{
          return batch && !['already_reconciled', 'prepared_in_xero'].includes(batch.status);
        }}

        // Manual invoice picks for "missing" sales (scoped to this preview upload).
        // These are included when the user submits a checked batch, so the UI
        // must make the confirmed pick obvious before any Xero write happens.
        function _saleKey(batchId, sale, idx) {{
          // Always include the row index so duplicate sale_refs in one batch
          // can never collide and overwrite each other's manual pick.
          return batchId + '::' + idx + '::' + (sale.sale_ref || '');
        }}
        function _matchKey(saleKey) {{ return 'cf_match_' + _previewId + '_' + saleKey; }}
        function _getMatch(saleKey) {{
          try {{ return JSON.parse(localStorage.getItem(_matchKey(saleKey)) || 'null'); }}
          catch (e) {{ return null; }}
        }}
        function _setMatch(saleKey, cand) {{
          if (cand) localStorage.setItem(_matchKey(saleKey), JSON.stringify(cand));
          else localStorage.removeItem(_matchKey(saleKey));
        }}

        // Tied/ambiguous swap — user can switch which of the same-amount invoices
        // is shown when the app auto-picked one out of several equal candidates.
        // TESTING ONLY; no Xero writes happen.
        function _tiedSwapKey(bId, idx) {{ return 'cf_tswap_' + _previewId + '_' + bId + '::' + idx; }}
        function _getTiedSwap(bId, idx) {{
          try {{ return JSON.parse(localStorage.getItem(_tiedSwapKey(bId, idx)) || 'null'); }}
          catch (e) {{ return null; }}
        }}
        function _setTiedSwap(bId, idx, cand) {{
          if (cand) localStorage.setItem(_tiedSwapKey(bId, idx), JSON.stringify(cand));
          else localStorage.removeItem(_tiedSwapKey(bId, idx));
        }}

        // Calendar slot exclusivity — once a slot is confirmed for one tied row it
        // is hidden from every other row in this preview.
        function _calRowSlotKey(bId, si) {{ return 'cf_cal_slot_' + _previewId + '_' + bId + '_' + si; }}
        function _getCalRowSlot(bId, si) {{ return localStorage.getItem(_calRowSlotKey(bId, si)) || ''; }}
        function _setCalRowSlot(bId, si, slot) {{
          if (slot) localStorage.setItem(_calRowSlotKey(bId, si), slot);
          else localStorage.removeItem(_calRowSlotKey(bId, si));
        }}
        function _getAllCalSlots() {{
          // Returns {{slotKey: ownerRowKey}} for every slot claimed in this preview.
          const prefix = 'cf_cal_slot_' + _previewId + '_';
          const map = {{}};
          for (let i = 0; i < localStorage.length; i++) {{
            const k = localStorage.key(i);
            if (k && k.startsWith(prefix)) {{
              const v = localStorage.getItem(k);
              if (v) map[v] = k;
            }}
          }}
          return map;
        }}

        // Adjustment plans for rows where the card payment does not equal the
        // selected invoice total. TESTING ONLY for now: this changes preview
        // totals and instructions, but does not write to Xero.
        function _adjKey(saleKey) {{ return 'cf_adjust_' + _previewId + '_' + saleKey; }}
        function _getAdjustment(saleKey) {{
          try {{ return JSON.parse(localStorage.getItem(_adjKey(saleKey)) || 'null'); }}
          catch (e) {{ return null; }}
        }}
        function _setAdjustment(saleKey, adj) {{
          if (adj) localStorage.setItem(_adjKey(saleKey), JSON.stringify(adj));
          else localStorage.removeItem(_adjKey(saleKey));
        }}
        function _invAmount(inv) {{
          if (!inv) return 0;
          const raw = inv.total ?? inv.amount_due ?? 0;
          const val = Number(raw);
          return Number.isFinite(val) ? val : 0;
        }}
        function _expectedAdjustment(saleGross, invAmount) {{
          const diff = Number((Number(saleGross || 0) - Number(invAmount || 0)).toFixed(2));
          if (Math.abs(diff) < 0.02) return null;
          if (diff < 0) return {{type: 'discount', amount: Math.abs(diff)}};
          return {{type: 'extra_invoice', amount: diff}};
        }}
        function _adjustmentMatches(expected, actual) {{
          if (!expected) return true;
          if (!actual || actual.type !== expected.type) return false;
          return Math.abs(Number(actual.amount || 0) - Number(expected.amount || 0)) < 0.02;
        }}
        function _adjustmentLabel(adj) {{
          if (!adj) return '';
          if (adj.type === 'discount') return 'discount / credit adjustment ' + money(adj.amount);
          if (adj.type === 'extra_invoice') return 'extra Materials invoice ' + money(adj.amount);
          return 'adjustment ' + money(adj.amount);
        }}
        function _selectedInvoiceForSale(batch, sale, idx) {{
          const saleKey = _saleKey(batch.id, sale, idx);
          if (sale.invoice) return _getTiedSwap(batch.id, idx) || sale.invoice;
          return _getMatch(saleKey) || sale.quick_invoice || null;
        }}
        function _submissionSale(batch, sale, idx) {{
          const saleKey = _saleKey(batch.id, sale, idx);
          const selected = _selectedInvoiceForSale(batch, sale, idx);
          const adjustment = _getAdjustment(saleKey);
          return {{
            sale_ref: sale.sale_ref || '',
            sale_index: idx,
            selected_invoice_id: selected ? (selected.id || '') : '',
            selected_invoice_number: selected ? (selected.number || '') : '',
            adjustment: adjustment || null,
          }};
        }}
        function updateCsvSubmitPanel() {{
          if (!_csvPreviewData || !_csvPreviewData.batches) {{
            csvSubmitPanel.classList.add('hidden');
            return;
          }}
          const active = (_csvPreviewData.batches || []).filter(b => _batchCanSubmit(b) && !_isSubmitted(b.id));
          const checked = active.filter(b => _isChecked(b.id));
          csvSubmitPanel.classList.remove('hidden');
          csvSubmitStatus.textContent = checked.length
            ? checked.length + ' batch' + (checked.length === 1 ? '' : 'es') + ' selected. Submit will only process those selected rows.'
            : 'No batches selected yet.';
          csvSubmitBtn.disabled = checked.length === 0;
        }}
        function collectCsvSubmission() {{
          if (!_csvPreviewData || !_csvPreviewData.batches) return [];
          return (_csvPreviewData.batches || [])
            .filter(b => _isChecked(b.id))
            .filter(b => _batchCanSubmit(b))
            .filter(b => !_isSubmitted(b.id))
            .map(b => ({{
              batch_id: b.id,
              payout_ref: (b.payout || {{}}).csv_ref || '',
              sales: (b.sales || []).map((s, idx) => _submissionSale(b, s, idx)),
            }}));
        }}
        async function refreshCsvPreview(focusBatchId) {{
          if (!_csvPreviewData || !_previewId) {{
            csvShowError('Upload the CSV first, then refresh the batch.');
            return;
          }}
          csvError.classList.add('hidden');
          csvStatus.textContent = 'Refreshing Xero and calendar matches…';
          const buttons = Array.from(document.querySelectorAll('.batch-refresh-btn'));
          buttons.forEach(btn => btn.disabled = true);
          try {{
            const resp = await fetch('/cashflows-sync/refresh-csv-preview', {{
              method: 'POST',
              headers: {{'Content-Type': 'application/json'}},
              body: JSON.stringify({{preview_id: _previewId, focus_batch_id: focusBatchId || ''}}),
            }});
            const data = await resp.json();
            if (!resp.ok || data.error) throw new Error(data.error || 'Refresh failed');
            _previewId = data.preview_id || _previewId;
            renderCsvTotals(data.totals || {{}});
            renderCsvResults(data);
            csvStatus.textContent = 'Preview refreshed from Xero and calendar.';
          }} catch (err) {{
            csvShowError(err.message || String(err));
            csvStatus.textContent = '';
          }} finally {{
            buttons.forEach(btn => btn.disabled = false);
            updateCsvSubmitPanel();
          }}
        }}
        function renderCsvSubmitSummary(data) {{
          const plans = data.plans || [];
          const blockers = data.production_blockers || [];
          const mode = data.mode === 'production' ? 'Production write' : 'Test mode';
          const modeCls = data.mode === 'production'
            ? 'bg-sky-50 border-sky-200 text-sky-800'
            : 'bg-emerald-50 border-emerald-200 text-emerald-800';
          const planRows = plans.map(function(p) {{
            const invs = (p.chosen_invoices || []).map(function(i) {{
              return esc(i.number || i.id || 'invoice') + (i.contact_name ? ' · ' + esc(i.contact_name) : '');
            }}).join('<br>');
            const extra = (p.extra_invoice_payloads || []).length
              ? '<div class="mt-1 text-indigo-700 font-semibold">Extra invoice plan: ' + (p.extra_invoice_payloads || []).map(x => esc(x.contact_name || 'Parking') + ' ' + money(x.amount)).join(', ') + '</div>'
              : '';
            const discounts = (p.discount_actions_required || []).length
              ? '<div class="mt-1 text-amber-700 font-semibold">Credit note plan: ' + (p.discount_actions_required || []).map(x => money(x.amount)).join(', ') + '</div>'
              : '';
            const paidExtras = (p.paid_overpayment_adjustments || []).length
              ? '<div class="mt-1 text-indigo-700 font-semibold">Already-paid overpayment adjustment: ' + (p.paid_overpayment_adjustments || []).map(x => esc(x.invoice_number || x.sale_ref || 'sale') + ' +' + money(x.amount)).join(', ') + '</div>'
              : '';
            const paid = (p.already_paid_invoices || []).length
              ? '<div class="mt-1 text-emerald-700 font-semibold">Already paid in Xero: app will move the payment into Cashflow reconciliation and create one net Cashflows bank match.</div>'
              : '';
            const matchPack = p.paid_matching_adjustment
              ? '<div class="mt-1 text-emerald-700 font-semibold">Match pack: one net Cashflows bank transaction will be ready for Xero bank reconciliation.</div>'
              : '';
            const clearing = p.clearing_receipt
              ? '<div class="mt-1 text-emerald-700 font-semibold">Clearing receipt: bank deposit coded to Cashflow reconciliation account.</div>'
              : '';
            return `<tr class="border-t border-gray-200">
              <td class="px-3 py-2 font-mono text-[11px]">${{esc(p.payout_ref || p.batch_id || '')}}</td>
              <td class="px-3 py-2 text-right font-semibold">${{money(p.gross)}}</td>
              <td class="px-3 py-2 text-right text-gray-500">${{money(p.fee_or_charge_total)}}</td>
              <td class="px-3 py-2 text-right font-semibold">${{money(p.net)}}</td>
              <td class="px-3 py-2">${{invs || '<span class="text-amber-700">No invoices selected</span>'}}${{extra}}${{discounts}}${{paidExtras}}${{paid}}${{matchPack}}${{clearing}}</td>
            </tr>`;
          }}).join('');
          const blockerHtml = blockers.length
            ? '<div class="rounded-lg border border-amber-200 bg-amber-50 text-amber-800 p-3 mt-3"><div class="font-semibold mb-1">Production blockers</div><ul class="list-disc pl-5 space-y-1">' + blockers.map(b => '<li>' + esc(b) + '</li>').join('') + '</ul></div>'
            : '';
          csvSubmitOutput.innerHTML = `
            <div class="inline-flex px-2 py-1 rounded-full border text-[11px] font-semibold ${{modeCls}}">${{esc(mode)}}</div>
            <div class="mt-2 text-sm font-semibold text-gray-900">${{esc(data.message || 'Submission prepared.')}}</div>
            <div class="mt-3 overflow-x-auto rounded-lg border border-gray-200 bg-white">
              <table class="w-full text-xs">
                <thead>
                  <tr class="bg-gray-50 text-[11px] uppercase tracking-wide text-gray-400 text-left">
                    <th class="px-3 py-2">Payout</th>
                    <th class="px-3 py-2 text-right">Gross</th>
                    <th class="px-3 py-2 text-right">Fees</th>
                    <th class="px-3 py-2 text-right">Bank net</th>
                    <th class="px-3 py-2">Invoices / actions</th>
                  </tr>
                </thead>
                <tbody>${{planRows}}</tbody>
              </table>
            </div>
            ${{blockerHtml}}
            <details class="mt-3 rounded-lg border border-gray-200 bg-white">
              <summary class="cursor-pointer px-3 py-2 text-[11px] font-semibold text-gray-600">Technical Xero payload preview</summary>
              <pre class="px-3 pb-3 whitespace-pre-wrap text-[11px] text-gray-600">${{esc(JSON.stringify(data, null, 2))}}</pre>
            </details>`;
          csvSubmitOutput.classList.remove('hidden');
        }}

        // Name match strict enough to avoid surname-only false positives:
        // "Tim Johnson" must not match "Tracey Johnson" just because both
        // contain Johnson.
        function _nameOverlap(a, b) {{
          const aw = (a||'').toLowerCase().split(/[^a-z0-9]+/).filter(function(w){{ return w.length >= 3; }});
          const bw = (b||'').toLowerCase().split(/[^a-z0-9]+/).filter(function(w){{ return w.length >= 3; }});
          if (!aw.length || !bw.length) return false;
          const overlap = aw.filter(function(w){{ return bw.indexOf(w) >= 0; }});
          if (!overlap.length) return false;
          if (aw.length >= 2 && bw.length >= 2) return aw[0] === bw[0] || overlap.length >= 2;
          if (aw.length === 1) return aw[0] === bw[0];
          return bw[0] === aw[0];
        }}
        // Format a calendar entry as "5 May 2026, 09:00-10:30".
        function _fmtCalEntry(c) {{
          let ds = c.event_date || '';
          try {{
            const d = new Date(c.event_date + 'T00:00:00');
            if (!isNaN(d.getTime())) ds = d.toLocaleDateString('en-GB', {{day:'numeric', month:'short', year:'numeric'}});
          }} catch(e) {{}}
          let t = '';
          if (c.event_start && c.event_end) t = c.event_start + '\u2013' + c.event_end;
          else t = c.event_end || c.event_start || '';
          return ds + (t ? ', ' + t : '');
        }}

        function renderBatch(b) {{
          const meta = STATUS_META[b.status] || STATUS_META.no_bank_line;
          const batchHasPaidInvoices = _batchHasPaidInvoices(b);
          const displayMeta = batchHasPaidInvoices && !['already_reconciled', 'prepared_in_xero'].includes(b.status)
            ? {{label: 'Ready to prepare Xero match pack', cls: 'border-emerald-200 bg-emerald-50 text-emerald-800'}}
            : meta;
          const payout = b.payout || {{}};
          const bank = b.bank_line;

          // ── Bank line header (left side of Xero reconciliation screen) ──
          const bankDesc = bank ? esc(bank.description || 'CFE SETT CASHFLOWS FPI') : 'CFE SETT CASHFLOWS FPI';
          const bankDate = bank ? esc(gb(bank.date || payout.date)) : esc(gb(payout.date));
          const bankAmt  = bank ? bank.amount : (b.net || 0);

          // Bank line confirmation pill
          let bankPill;
          if (b.status === 'already_reconciled') {{
            bankPill = `<span class="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-emerald-50 border border-emerald-200 text-emerald-700 text-[11px] font-semibold">☑ Already reconciled in Xero</span>`;
          }} else if (b.status === 'prepared_in_xero') {{
            bankPill = `<span class="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-sky-50 border border-sky-200 text-sky-700 text-[11px] font-semibold">Prepared in Xero</span>`;
          }} else if (bank) {{
            bankPill = `<span class="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-emerald-50 border border-emerald-200 text-emerald-700 text-[11px] font-semibold">✅ Confirmed in Xero bank feed</span>`;
          }} else if (b.status === 'no_bank_line') {{
            bankPill = `<span class="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-gray-50 border border-gray-200 text-gray-500 text-[11px]">⏳ Not shown in Xero reconcile list</span>`;
          }} else {{
            bankPill = `<span class="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-amber-50 border border-amber-200 text-amber-700 text-[11px]">⚠ Bank scope missing — using CSV as proxy</span>`;
          }}

          // ── Invoice rows (right side of Xero reconciliation — "Find & select") ──
          const sales = b.sales || [];
          const rowStates = sales.map((s, idx) => {{
            const saleKey = _saleKey(b.id, s, idx);
            const selected = _selectedInvoiceForSale(b, s, idx);
            const invTotal = _invAmount(selected);
            const expectedAdj = selected ? _expectedAdjustment(Number(s.gross || 0), invTotal) : null;
            const adjustment = _getAdjustment(saleKey);
            const adjustmentOk = _adjustmentMatches(expectedAdj, adjustment);
            const selectedStatus = String((selected && selected.status) || '').toUpperCase();
            const invoiceReady = selectedStatus !== 'DRAFT';
            const ready = !!selected && adjustmentOk && invoiceReady;
            return {{saleKey, selected, invTotal, expectedAdj, adjustment, adjustmentOk, invoiceReady, ready}};
          }});
          // A sale counts toward the total only if it has a selected invoice and
          // any under/over difference has an explicit adjustment plan.
          const matchedGrossEff = sales.reduce((sum, r, idx) =>
            rowStates[idx].ready ? sum + Number(r.gross || 0) : sum, 0);
          // Cashflows deducts its fees before paying the bank, so the correct
          // check is: gross − fees − absorbed declines ≈ bank net (not gross ≈ net).
          const allEffective = sales.every((s, idx) => rowStates[idx].ready);
          const matchedFees = sales.reduce((sum, r, idx) =>
            rowStates[idx].ready ? sum + Number(r.fee||0) : sum, 0);
          const declineAbs = Number(b.decline_absorbed || 0);
          const netOfFees = matchedGrossEff - matchedFees - declineAbs;
          const netVariance = Math.abs(Number(b.net||0) - netOfFees);
          const balanced = allEffective && netVariance < 0.02;
          const allMatched = b.missing_invoice_count === 0 && b.sale_count > 0;

          const invRows = sales.map((s, idx) => {{
            const sTime = s.time ? '<div class="text-[10px] text-gray-400">' + esc(s.time) + '</div>' : '';
            const isMissing = !s.invoice;
            const saleKey = _saleKey(b.id, s, idx);
            const rowState = rowStates[idx];
            const expectedAdj = rowState.expectedAdj;
            const adjustment = rowState.adjustment;
            const adjustmentOk = rowState.adjustmentOk;
            const invoiceReady = rowState.invoiceReady;

            // Option list: matched -> [app pick, ...same-amount alts]; missing -> ranked candidates.
            const allOptions = isMissing
              ? (s.candidates || [])
              : [s.invoice].concat(s.tied_candidates || []);

            // User overrides.
            const tswap = isMissing ? null : _getTiedSwap(b.id, idx);
            const manualPick = isMissing ? _getMatch(saleKey) : null;

            // Calendar suggestions minus slots already claimed by other rows.
            const _allCalSlots = _getAllCalSlots();
            const _myRowKey = _calRowSlotKey(b.id, idx);
            const calSuggs = (s.calendar_suggestions || []).filter(function(sg) {{
              if (!sg.event_date || !sg.event_start) return true;
              const owner = _allCalSlots[sg.event_date + 'T' + sg.event_start];
              return !owner || owner === _myRowKey;
            }});
            // Assign each calendar suggestion to at most ONE option, so the
            // same appointment is never pasted onto every candidate. Name
            // overlap is the only reliable per-candidate signal; amount/date
            // alone cannot tell apart several invoices of the same value
            // (e.g. five different £426 jobs share one £426 calendar entry).
            const optCal = allOptions.map(function(){{ return null; }});
            const _usedSugg = new Set();
            const _nameMatches = [];
            allOptions.forEach(function(opt, i){{
              if (!opt) return;
              calSuggs.forEach(function(sg, sgi){{
                if (sg.customer && _nameOverlap(sg.customer, opt.contact_name || '')) {{
                  _nameMatches.push({{ i: i, sgi: sgi, sg: sg, score: sg.score || 0 }});
                }}
              }});
            }});
            _nameMatches.sort(function(a, b){{ return b.score - a.score; }});
            const _nameMatchedIdx = new Set();
            _nameMatches.forEach(function(m){{
              if (optCal[m.i] || _usedSugg.has(m.sgi)) return;
              optCal[m.i] = m.sg; _usedSugg.add(m.sgi); _nameMatchedIdx.add(m.i);
            }});
            // Primary pick (index 0) falls back to the best remaining suggestion
            // when no name overlap — e.g. calendar records the tenant while Xero
            // invoices the letting company. NOT added to _nameMatchedIdx so it
            // renders as "proximity only", not "confirmed".
            if (allOptions[0] && !optCal[0]) {{
              let best = null, bestScore = -1, bestSgi = -1;
              calSuggs.forEach(function(sg, sgi){{
                if (_usedSugg.has(sgi)) return;
                const sc = sg.score || 0;
                if (sc > bestScore) {{ bestScore = sc; best = sg; bestSgi = sgi; }}
              }});
              if (best) {{ optCal[0] = best; _usedSugg.add(bestSgi); }}
            }}
            // Favoured option: app pick / top candidate, unless the user overrode it,
            // or a tied price is broken by the closest calendar appointment.
            let favIdx = 0;
            if (tswap) {{
              const ti = allOptions.findIndex(function(o){{ return o.id === tswap.id; }});
              favIdx = ti >= 0 ? ti : 0;
            }} else if (manualPick) {{
              const mi = allOptions.findIndex(function(o){{ return o.id === manualPick.id; }});
              favIdx = mi >= 0 ? mi : 0;
            }} else if ((isMissing && allOptions.length > 1) || (!isMissing && s.ambiguous)) {{
              // Break a tie by the closest calendar appointment. Price stays
              // primary: on missing rows only exact-price candidates compete,
              // so calendar never overrides a better price match.
              let bestI = -1, bestScore = -1;
              allOptions.forEach(function(o, i){{
                if (isMissing && !o.amount_match) return;
                const c = optCal[i];
                if (c && c.score >= 0.45 && c.score > bestScore) {{ bestScore = c.score; bestI = i; }}
              }});
              if (bestI >= 0) favIdx = bestI;
            }}

            const favoured = allOptions[favIdx] || null;
            const favCal = favoured ? optCal[favIdx] : null;
            const favCalNameMatch = !!(favCal && favoured && _nameOverlap(favCal.customer || '', favoured.contact_name || ''));
            const favDateMismatch = !!(favCalNameMatch && favoured && Number(favoured.days_apart || 0) > 3);
            const noMatch = !favoured;
            const userChosen = !!(tswap || manualPick);
            const ambiguous = !isMissing && !!s.ambiguous;
            // On a "missing" row, a favoured candidate whose amount does NOT
            // match the sale is not a real match \u2014 it's just the nearest
            // invoice by date. Presenting it as a confident "suggested" match
            // (with an unrelated calendar entry) misleads the user. Treat it
            // as "no matching invoice" and show the raw CSV sale facts, while
            // still keeping the candidates below for manual cross-referencing.
            const favAmountMatch = !!(favoured && favoured.amount_match);
            const noAmountMatch = isMissing && !userChosen && !!favoured && !favAmountMatch;
            const needsSuggestionConfirm = isMissing && !userChosen && !!favoured && favAmountMatch;
            const likelyCal = (s.calendar_suggestions || []).find(function(c) {{
              const eventGross = Number(c.event_gross);
              const saleGross = Number(s.gross || 0);
              const amountOk = !isNaN(eventGross) && Math.abs(eventGross - saleGross) < 0.02;
              return amountOk && Number(c.score || 0) >= 0.6;
            }}) || null;
            const likelyCalLine = likelyCal
              ? '<div class="mt-1 rounded border border-teal-200 bg-teal-50 px-2 py-1.5 text-[11px] text-teal-800">'
                  + '<div class="font-semibold">&#128197; Likely calendar job: ' + esc(likelyCal.customer || likelyCal.event_summary || 'Calendar entry') + '</div>'
                  + '<div>' + esc(_fmtCalEntry(likelyCal))
                    + ((likelyCal.event_gross !== null && likelyCal.event_gross !== undefined) ? ' <span class="font-bold">(\u00a3' + Number(likelyCal.event_gross).toFixed(2) + ')</span>' : '')
                    + '</div>'
                + '</div>'
              : '';

            // Favoured candidate cell.
            const custName = noMatch
              ? '<span class="text-amber-700">No match found</span>'
              : noAmountMatch
                ? (likelyCal
                    ? '<span class="text-teal-800">' + esc(likelyCal.customer || 'Calendar job') + '</span><div class="text-[10px] text-teal-600 font-normal">calendar/card-sale clue</div>'
                    : '<span class="text-amber-700">' + money(s.gross) + ' card sale</span>')
                : esc(favoured.contact_name || favoured.number || '\u2014');
            const calLineTone = needsSuggestionConfirm ? 'text-amber-700' : 'text-teal-700';
            const calLineAmountTone = needsSuggestionConfirm ? 'text-amber-700' : 'text-teal-700';
            const calLine = (favCal && !noAmountMatch)
              ? '<div class="text-[11px] ' + calLineTone + ' mt-0.5">&#128197; Calendar entry \u2014 ' + esc(_fmtCalEntry(favCal))
                  + ((favCal.event_gross !== null && favCal.event_gross !== undefined) ? ' <span class="text-xs font-bold ' + calLineAmountTone + '">(\u00a3' + favCal.event_gross.toFixed(2) + ')</span>' : '')
                  + (needsSuggestionConfirm ? ' <span class="text-[10px] font-semibold bg-amber-100 border border-amber-200 rounded px-1">needs confirmation</span>' : '')
                  + (favDateMismatch ? ' <span class="text-[10px] font-semibold bg-amber-100 border border-amber-200 rounded px-1">invoice date differs</span>' : '')
                  + '</div>'
              : '';
            const suggestedConfirmBtn = needsSuggestionConfirm
              ? '<div class="mt-2 rounded border border-amber-200 bg-amber-50 px-2 py-1.5 text-[11px] text-amber-800 flex items-center justify-between gap-2">'
                  + '<span><span class="font-semibold">Suggested invoice only.</span> Confirm this row before it counts toward the batch total.</span>'
                  + '<button class="cf-cand-pick shrink-0 px-2 py-1 rounded bg-amber-600 text-white text-[11px] font-semibold hover:bg-amber-700" data-si="' + idx + '" data-ci="' + favIdx + '" data-cal-slot="' + esc((favCal && favCal.event_date && favCal.event_start) ? (favCal.event_date + 'T' + favCal.event_start) : '') + '">Confirm this invoice</button>'
                + '</div>'
              : '';
            const suggestionLine = noAmountMatch && favoured
              ? '<div class="mt-2 rounded border border-amber-200 bg-white px-2 py-2 text-[11px] text-gray-700">'
                  + '<div class="flex items-start justify-between gap-3">'
                    + '<div class="min-w-0">'
                      + '<div class="text-[10px] uppercase tracking-widest text-amber-700 font-semibold">Top invoice suggestion</div>'
                      + '<div class="mt-0.5"><span class="font-semibold text-gray-900">' + esc(favoured.contact_name || favoured.number || '') + '</span> '
                        + '<span class="text-indigo-700 font-mono">' + esc(favoured.number || '') + '</span> '
                        + '<span class="text-gray-400 font-mono">' + esc(favoured.reference || '') + '</span></div>'
                      + (favCalNameMatch
                          ? '<div class="mt-0.5 text-teal-700">&#128197; ' + esc(_fmtCalEntry(favCal))
                              + ((favCal.event_gross !== null && favCal.event_gross !== undefined) ? ' <span class="font-bold">(\u00a3' + Number(favCal.event_gross).toFixed(2) + ')</span>' : '')
                              + (favDateMismatch ? ' <span class="text-[10px] font-semibold text-amber-700 bg-amber-100 border border-amber-200 rounded px-1">invoice date differs</span>' : '')
                              + '</div>'
                          : '')
                      + '<div class="mt-0.5 text-gray-500">Invoice total ' + money(favoured.total) + ' vs card payment ' + money(s.gross) + '. Confirming will show the adjustment needed.</div>'
                    + '</div>'
                    + '<button class="cf-cand-pick shrink-0 px-2 py-1 rounded bg-amber-600 text-white text-[11px] font-semibold hover:bg-amber-700" data-si="' + idx + '" data-ci="' + favIdx + '" data-cal-slot="' + esc((favCal && favCal.event_date && favCal.event_start) ? (favCal.event_date + 'T' + favCal.event_start) : '') + '">Confirm</button>'
                  + '</div>'
                + '</div>'
              : '';
            // Quick-invoice control: for a stray card payment with no invoice,
            // one click raises a tidy standalone invoice. It is intentionally
            // shown after candidate review, because a real Xero match is safer
            // than creating a new invoice.
            const qi = s.quick_invoice;
            const qiContact = likelyCal && likelyCal.customer ? likelyCal.customer : '';
            const qiDesc = likelyCal
              ? ((likelyCal.event_summary || likelyCal.customer || 'Calendar card payment') + ' - card payment')
              : 'Materials';
            const qiBtn = qi
              ? '<div class="mt-1 text-[11px] text-emerald-700 font-semibold">'
                  + (qi.dry_run
                      ? '\u2713 Quick invoice simulated (dry-run on)'
                      : '\u2713 Quick invoice created' + (qi.number ? ': <span class="font-mono">' + esc(qi.number) + '</span>' : ''))
                  + ' \u00b7 ' + esc(qi.contact_name || '') + ' \u00b7 ' + esc(qi.description || '')
                  + '</div>'
              : '<button class="cf-quick-invoice mt-1 inline-flex items-center gap-1 px-2 py-1 rounded bg-emerald-600 text-white text-[11px] font-semibold hover:bg-emerald-700" data-ref="' + esc(s.sale_ref || '') + '" data-amt="' + (s.gross || 0) + '" data-date="' + esc(s.date || '') + '" data-si="' + idx + '" data-contact="' + esc(qiContact) + '" data-desc="' + esc(qiDesc) + '">+ Quick invoice (' + money(s.gross) + ')</button>';

            let invCell = noMatch
              ? '<span class="text-[11px] text-amber-600">create an invoice in Xero to reconcile this payment</span>'
              : noAmountMatch
                ? suggestionLine
                : '<span class="text-indigo-700 font-mono">' + esc(favoured.number || '') + '</span> '
                    + '<span class="text-gray-400 font-mono">' + esc(favoured.reference || '') + '</span>' + calLine + suggestedConfirmBtn;
            if (noMatch) {{ invCell += '<div class="mt-1">' + qiBtn + '</div>'; }}
            if (favoured && expectedAdj) {{
              const expLabel = _adjustmentLabel(expectedAdj);
              const activeLabel = adjustmentOk ? '&#10003; ' + _adjustmentLabel(adjustment) : '';
              const actionText = expectedAdj.type === 'discount'
                ? 'Add discount plan'
                : 'Add Materials invoice plan';
              const helpText = expectedAdj.type === 'discount'
                ? 'Card payment is lower than invoice total. Xero needs a discount/credit-style adjustment before this can fully match.'
                : 'Card payment is higher than this invoice. If the invoice is missing a line, amend the invoice and refresh this batch. If you have checked it and the extra money is separate, add a Materials invoice plan for the difference.';
              invCell += '<div class="mt-2 rounded border ' + (adjustmentOk ? 'border-emerald-200 bg-emerald-50 text-emerald-800' : 'border-amber-200 bg-amber-50 text-amber-800') + ' p-2">'
                + '<div class="text-[11px] font-semibold">Adjustment needed: ' + expLabel + '</div>'
                + '<div class="text-[10px] mt-0.5">' + helpText + '</div>'
                + (adjustmentOk
                    ? '<div class="mt-1 text-[11px] font-semibold">' + activeLabel + ' <button class="cf-adjust-clear ml-1 underline text-gray-500 hover:text-red-600" data-si="' + idx + '">clear</button></div>'
                    : '<button class="cf-adjust-set mt-1 inline-flex px-2 py-1 rounded bg-amber-600 text-white text-[11px] font-semibold hover:bg-amber-700" data-si="' + idx + '" data-type="' + expectedAdj.type + '" data-amt="' + expectedAdj.amount.toFixed(2) + '">' + actionText + ' (' + money(expectedAdj.amount) + ')</button>')
                + '</div>';
            }}
            if (favoured && String(favoured.status || '').toUpperCase() === 'DRAFT') {{
              invCell += '<div class="mt-2 rounded border border-amber-200 bg-amber-50 p-2 text-[11px] text-amber-800">'
                + '<div class="font-semibold">Invoice needs finalising: process/approve this Xero draft first.</div>'
                + '<div class="mt-0.5">It is the best match, but it cannot be included in a Cashflows submission while still DRAFT.</div>'
                + '</div>';
            }}

            // Status badge.
            let statusBadge;
            if (noMatch) {{
              statusBadge = '<span class="text-amber-600 font-semibold">\u23f3 no matches found</span>';
            }} else if (favoured && String(favoured.status || '').toUpperCase() === 'DRAFT') {{
              statusBadge = '<span class="text-amber-700 font-semibold">\u26a0 invoice needs finalising</span>';
            }} else if (userChosen) {{
              statusBadge = expectedAdj && !adjustmentOk
                ? '<span class="text-amber-700 font-semibold">\u26a0 adjustment needed</span>'
                : '<span class="text-indigo-600 font-semibold">\u2713 your choice</span>'
                + ' <button class="cf-row-reset ml-1 text-[10px] text-gray-400 hover:text-red-500 underline" data-si="' + idx + '" data-missing="' + (isMissing?'1':'0') + '">reset</button>';
            }} else if (noAmountMatch) {{
              statusBadge = '<span class="text-amber-700 font-semibold">\u26a0 no matching invoice</span>';
            }} else if (isMissing) {{
              statusBadge = '<span class="text-amber-700 font-semibold">\u23f3 suggested \u2014 confirm this row</span>';
            }} else if (expectedAdj && !adjustmentOk) {{
              statusBadge = '<span class="text-amber-700 font-semibold">\u26a0 adjustment needed</span>';
            }} else if (ambiguous) {{
              statusBadge = favCal
                ? '<span class="text-teal-700 font-semibold">&#128197; calendar match</span>'
                : '<span class="text-orange-600 font-semibold">\u26a0 tied on price</span>';
            }} else if (favoured && favoured.is_open === false) {{
              statusBadge = '<span class="text-amber-700 font-semibold">↳ already paid — match existing Xero payment</span>';
            }} else {{
              statusBadge = '<span class="text-emerald-600 font-semibold">\u2713 matched by app</span>';
            }}

            // "other options" dropdown toggle.
            const altCount = noMatch ? 0 : (allOptions.length - 1);
            const hasSecondaryAction = noAmountMatch && !qi;
            const showPanel = altCount > 0 || hasSecondaryAction;
            const toggle = showPanel
              ? '<button class="cf-row-toggle ml-2 inline-flex items-center gap-1 text-[11px] text-gray-500 hover:text-gray-800" data-si="' + idx + '">'
                  + (altCount > 0 ? altCount + ' other option' + (altCount===1?'':'s') : 'review options')
                  + ' <span class="text-[9px]">\u25be</span></button>'
              : '';

            // Options panel (favoured first).
            let order = allOptions.map(function(_, i){{ return i; }});
            if (favIdx > 0) {{ order.splice(order.indexOf(favIdx), 1); order.unshift(favIdx); }}
            const optionRows = order.map(function(origIdx) {{
              const opt = allOptions[origIdx];
              const oc = optCal[origIdx];
              const isFav = origIdx === favIdx;
              const amtBadge = (opt.amount_match || opt.total === null || opt.total === undefined)
                ? '<span class="text-[10px] font-semibold text-emerald-700 bg-emerald-100 rounded px-1">exact \u00a3</span>'
                : '<span class="text-[10px] text-gray-500 bg-gray-100 rounded px-1">\u00a3 near</span>';
              const dayTxt = (opt.days_apart === null || opt.days_apart === undefined) ? ''
                : '<span class="text-[10px] text-gray-400">' + (opt.days_apart===0?'same day':opt.days_apart+'d apart') + '</span>';
              const assigned = opt.assigned_to ? ' <span class="text-[10px] text-orange-500 italic">(now on ' + esc(opt.assigned_to) + ')</span>' : '';
              const openBadge = (opt.is_open === true)
                ? (String(opt.status || '').toUpperCase() === 'DRAFT'
                    ? '<span class="text-[10px] font-semibold text-amber-700 bg-amber-100 rounded px-1">draft \u2014 process first</span>'
                    : '<span class="text-[10px] font-semibold text-indigo-700 bg-indigo-100 rounded px-1">unpaid \u2014 reconciling marks paid</span>')
                : (opt.is_open === false ? '<span class="text-[10px] text-gray-400 bg-gray-100 rounded px-1">already paid</span>' : '');
              const optTotal = Number(opt.total ?? opt.amount ?? opt.amount_due ?? 0);
              const saleGross = Number(s.gross || 0);
              const optDiff = optTotal - saleGross;
              const optDiffAbs = Math.abs(optDiff);
              const optDiffLabel = optDiffAbs < 0.005
                ? 'exact'
                : (optDiff > 0 ? 'invoice higher by ' : 'invoice lower by ') + money(optDiffAbs);
              const priceLine = '<div class="mt-1 flex flex-wrap items-center gap-1.5 text-[11px]">'
                + '<span class="font-semibold text-gray-900">Invoice ' + money(optTotal) + '</span>'
                + '<span class="text-gray-400">vs card</span>'
                + '<span class="font-semibold text-gray-900">' + money(saleGross) + '</span>'
                + '<span class="' + (optDiffAbs < 0.005 ? 'text-emerald-700 bg-emerald-50 border-emerald-100' : 'text-amber-700 bg-amber-50 border-amber-100') + ' border rounded px-1">'
                + esc(optDiffLabel) + '</span>'
                + '</div>';
              const calConfirmed = _nameMatchedIdx.has(origIdx);
              const displayCal = oc;
              const ocLine = displayCal
                ? '<div class="text-[11px] mt-0.5 ' + (calConfirmed ? 'text-teal-700' : 'text-gray-400') + '">&#128197; ' + esc(_fmtCalEntry(displayCal))
                    + ((displayCal.event_gross !== null && displayCal.event_gross !== undefined) ? ' <span class="text-xs font-bold ' + (calConfirmed ? 'text-teal-700' : 'text-gray-500') + '">(\u00a3' + displayCal.event_gross.toFixed(2) + ')</span>' : '')
                    + (calConfirmed
                        ? ' <span class="text-[10px] text-teal-700 font-semibold bg-teal-50 rounded px-1">\u2713 name match</span>'
                        : ' <span class="text-[10px] text-gray-400 bg-gray-100 rounded px-1">? proximity only</span>')
                    + (calConfirmed && Number(opt.days_apart || 0) > 3
                        ? ' <span class="text-[10px] text-amber-700 font-semibold bg-amber-100 border border-amber-200 rounded px-1">invoice date differs</span>'
                        : '')
                    + '</div>'
                : '';
              const slot = (displayCal && displayCal.event_date && displayCal.event_start) ? (displayCal.event_date + 'T' + displayCal.event_start) : '';
              const btn = isFav
                ? '<span class="text-[11px] text-gray-400 italic shrink-0 self-center">'
                    + (noAmountMatch ? 'top suggestion' : 'currently shown')
                  + '</span>'
                : '<button class="' + (isMissing ? 'cf-cand-pick' : 'cf-tied-pick') + ' shrink-0 self-center px-2 py-1 rounded bg-indigo-600 text-white text-[11px] font-semibold hover:bg-indigo-700" data-si="' + idx + '" data-' + (isMissing?'ci':'oi') + '="' + origIdx + '" data-cal-slot="' + esc(slot) + '">Use this</button>';
              return '<div class="flex items-start justify-between gap-2 py-1.5 px-2 rounded border-b border-gray-100 last:border-0 hover:bg-white ' + (isFav?'bg-teal-50/40':'') + '">'
                + '<div class="min-w-0">'
                +   '<div class="text-xs"><span class="font-medium text-gray-900">' + esc(opt.contact_name || opt.number || '\u2014') + '</span> '
                +     '<span class="text-indigo-700 font-mono">' + esc(opt.number||'') + '</span> '
                +     '<span class="text-gray-400 font-mono">' + esc(opt.reference||'') + '</span>'
                +     (isFav?' <span class="text-[10px] text-gray-400 italic">(favoured)</span>':'') + assigned + '</div>'
                +   priceLine
                +   '<div class="flex items-center gap-1.5 mt-0.5 flex-wrap">' + amtBadge + ' ' + dayTxt + ' ' + openBadge + '</div>' + ocLine
                + '</div>' + btn + '</div>';
            }}).join('');
            const quickFallback = hasSecondaryAction
              ? '<div class="mt-2 rounded border border-emerald-200 bg-white p-2">'
                  + '<div class="text-[10px] uppercase tracking-widest text-emerald-700 font-semibold">Fallback if no Xero invoice is right</div>'
                  + (likelyCal
                      ? '<div class="text-[11px] text-gray-600 mt-1">Prefilled from calendar: <span class="font-semibold text-gray-900">' + esc(likelyCal.customer || '') + '</span> ' + esc(_fmtCalEntry(likelyCal)) + '</div>'
                      : '<div class="text-[11px] text-gray-600 mt-1">Use only if this card payment really has no matching Xero invoice.</div>')
                  + qiBtn
                + '</div>'
              : (qi ? '<div class="mt-2">' + qiBtn + '</div>' : '');
            const panelRow = showPanel
              ? '<tr class="cf-row-panel hidden" id="cf-row-panel-' + b.id + '-' + idx + '"><td colspan="6" class="px-3 pb-3 pt-0 bg-gray-50/60">'
                  + '<div class="rounded-lg border border-gray-200 bg-gray-50 p-2">'
                  + '<div class="text-[10px] uppercase tracking-widest text-gray-400 font-semibold mb-1 px-1">Candidates \u00b7 best price &amp; calendar match first</div>'
                  + optionRows
                  + '<div class="text-[10px] text-gray-400 mt-1 px-1">Picking one is for cross-referencing only \u2014 nothing changes in Xero.</div>'
                  + quickFallback
                  + '</div></td></tr>'
              : '';

            const rowCls = (noMatch || noAmountMatch) ? 'border-amber-100 bg-amber-50/60'
              : userChosen ? 'border-indigo-100 bg-indigo-50/40'
              : (ambiguous && !favCal) ? 'border-orange-100 bg-orange-50/40'
              : 'border-gray-100 hover:bg-gray-50';

            return '<tr class="border-t ' + rowCls + '">'
              + '<td class="px-3 py-2 text-xs text-gray-500 whitespace-nowrap">' + esc(gb(s.date)) + sTime + '</td>'
              + '<td class="px-3 py-2 text-xs font-medium text-gray-900">' + custName + '</td>'
              + '<td class="px-3 py-2 text-xs">' + invCell + '</td>'
              + '<td class="px-3 py-2 text-xs text-right font-semibold text-gray-900">' + money(s.gross) + '</td>'
              + '<td class="px-3 py-2 text-xs text-right text-gray-400">' + money(s.fee) + '</td>'
              + '<td class="px-3 py-2 text-xs">' + statusBadge + toggle + '</td>'
              + '</tr>' + panelRow;
          }}).join('');

          // ── Running total row ──
          // The bank line is AFTER Cashflows deducts its fees, so "balanced"
          // means: gross matched − fees = bank net (not gross = bank net).
          const totalRowCls = balanced ? 'bg-emerald-50 text-emerald-800' : 'bg-amber-50 text-amber-800';
          const missingCount = sales.filter((s, i) => !rowStates[i].ready).length;
          let missingGross = 0, missingFees = 0;
          sales.forEach(function(s, i){{ if (!rowStates[i].ready) {{ missingGross += Number(s.gross||0); missingFees += Number(s.fee||0); }} }});
          const missingRow = (missingCount > 0 && missingGross > 0.005)
            ? '<tr class="border-t border-dashed border-amber-300 bg-amber-50 text-xs text-amber-800">'
                + '<td class="px-3 py-2.5" colspan="3">'
                +   '<span class="font-semibold">&#128161; Action needed to balance</span>'
                +   ' <span class="text-[11px] text-amber-600">\u2014 '
                +   missingCount + ' CSV payment' + (missingCount===1?'':'s') + ' need a confirmed invoice or adjustment plan</span>'
                + '</td>'
                + '<td class="px-3 py-2.5 text-right font-bold text-amber-900">' + money(missingGross) + '</td>'
                + '<td class="px-3 py-2.5 text-right text-amber-700">' + money(missingFees) + '</td>'
                + '<td class="px-3 py-2.5 text-[11px] text-amber-700">Confirm the orange suggested row(s), or choose another option if the suggestion is wrong.</td>'
                + '</tr>'
            : '';
          const totalCheck = balanced
            ? `✅ Balanced — gross ${{money(matchedGrossEff)}} less CF fees ${{money(matchedFees)}} = net ${{money(Number(b.net||0))}}`
            : allEffective
              ? `⚠ Small discrepancy of £${{netVariance.toFixed(2)}} — check for absorbed declined payments`
              : `⚠ ${{money(Math.abs(Number(b.net||0) - netOfFees))}} still unreconciled — ${{missingCount}} row${{missingCount===1?'':'s'}} need action`;
          const totalRow = `<tr class="border-t-2 border-gray-200 ${{totalRowCls}} font-semibold text-xs">
            <td class="px-3 py-2" colspan="3">Total invoices matched</td>
            <td class="px-3 py-2 text-right">${{money(matchedGrossEff)}}</td>
            <td class="px-3 py-2"></td>
            <td class="px-3 py-2 text-xs">${{totalCheck}}</td>
          </tr>`;

          const checked = _isChecked(b.id);
          const checkBg = checked ? 'bg-emerald-50 border-emerald-300' : 'bg-gray-50 border-gray-200';
          const checkLabel = batchHasPaidInvoices
            ? (checked
                ? '<span class="text-emerald-700 font-semibold">✓ Confirmed — prepare net Cashflows match in Xero</span>'
                : '<span class="text-gray-600">Mark as confirmed — prepare net Cashflows match</span>')
            : (checked
                ? '<span class="text-emerald-700 font-semibold">✓ Confirmed — ready to reconcile in Xero</span>'
                : '<span class="text-gray-600">Mark as confirmed — all invoices look correct</span>');

          const borderCls = b.status === 'ready' ? 'border-emerald-200'
                          : b.status === 'needs_review' ? 'border-orange-200'
                          : b.status === 'waiting_invoices' ? 'border-amber-200'
                          : 'border-gray-200';

          const wrap = document.createElement('div');
          wrap.className = `rounded-xl border bg-white shadow-sm ${{borderCls}}`;
          wrap.dataset.batchId = b.id;
          wrap.innerHTML = `
            <!-- Bank line header -->
            <div class="flex items-start justify-between gap-3 flex-wrap px-4 pt-4 pb-3 border-b border-gray-100">
              <div>
                <div class="text-[10px] uppercase tracking-widest text-gray-400 font-semibold mb-0.5">Xero bank line to reconcile</div>
                <div class="text-base font-bold text-gray-900">${{bankDesc}}</div>
                <div class="flex items-center gap-3 mt-1">
                  <span class="text-sm font-semibold text-gray-700">${{money(bankAmt)}}</span>
                  <span class="text-xs text-gray-400">${{bankDate}}</span>
                  ${{bankPill}}
                </div>
              </div>
              <div class="flex items-center gap-2">
                <button type="button" class="batch-refresh-btn h-7 w-7 rounded-full border border-gray-200 bg-white hover:bg-gray-50 text-gray-500 text-sm leading-none disabled:opacity-50" data-batch-id="${{b.id}}" title="Refresh this batch after updating the calendar or Xero">↻</button>
                <span class="px-2 py-1 rounded-full border text-xs font-semibold ${{displayMeta.cls}}">${{displayMeta.label}}</span>
              </div>
            </div>

            <!-- Invoice list — mirrors Xero "Find & select" -->
            <div class="px-4 py-3">
              <div class="text-[10px] uppercase tracking-widest text-gray-400 font-semibold mb-0.5">Invoices to select in Xero (${{b.sale_count}} in this batch)</div>
              <div class="text-[11px] text-gray-400 mb-2">${{batchHasPaidInvoices ? 'These invoices are already paid in Xero. Submitting moves the invoice payment into Cashflow reconciliation and creates one net Cashflows bank transaction for the payout.' : 'These were matched by this app. Xero is not changed by preview or ticking; only the separate submit button prepares the selected batches for Xero.'}}</div>
              <div class="overflow-x-auto rounded-lg border border-gray-100">
                <table class="w-full text-xs">
                  <thead>
                    <tr class="bg-gray-50 text-[11px] uppercase tracking-wide text-gray-400 text-left">
                      <th class="px-3 py-2">Date / Time</th>
                      <th class="px-3 py-2">Sale / customer</th>
                      <th class="px-3 py-2">Invoice / Ref</th>
                      <th class="px-3 py-2 text-right">Gross</th>
                      <th class="px-3 py-2 text-right">CF fee</th>
                      <th class="px-3 py-2">Status</th>
                    </tr>
                  </thead>
                  <tbody>${{invRows}}</tbody>
                  ${{totalRow}}
                  ${{missingRow}}
                </table>
              </div>
            </div>

            <!-- Confirm checkbox -->
            <label class="flex items-center gap-3 px-4 py-3 border-t border-gray-100 rounded-b-xl cursor-pointer ${{checkBg}} transition-colors">
              <input type="checkbox" class="batch-confirm-cb w-4 h-4 accent-emerald-600" data-batch-id="${{b.id}}" ${{checked ? 'checked' : ''}}>
              ${{checkLabel}}
            </label>`;

          // Checkbox behaviour
          wrap.querySelector('.batch-refresh-btn').addEventListener('click', function() {{
            refreshCsvPreview(b.id);
          }});
          wrap.querySelector('.batch-confirm-cb').addEventListener('change', function() {{
            _setChecked(b.id, this.checked);
            const lbl = wrap.querySelector('label');
            if (this.checked) {{
              lbl.className = lbl.className.replace('bg-gray-50 border-gray-200', 'bg-emerald-50 border-emerald-300');
              lbl.querySelector('span').outerHTML = batchHasPaidInvoices
                ? '<span class="text-emerald-700 font-semibold">✓ Confirmed — prepare net Cashflows match in Xero</span>'
                : '<span class="text-emerald-700 font-semibold">✓ Confirmed — ready to reconcile in Xero</span>';
            }} else {{
              lbl.className = lbl.className.replace('bg-emerald-50 border-emerald-300', 'bg-gray-50 border-gray-200');
              lbl.querySelector('span').outerHTML = batchHasPaidInvoices
                ? '<span class="text-gray-600">Mark as confirmed — prepare net Cashflows match</span>'
                : '<span class="text-gray-600">Mark as confirmed — all invoices look correct</span>';
            }}
            updateCsvSubmitPanel();
          }});

          // Expand/collapse the "other options" panel for a transaction.
          wrap.querySelectorAll('.cf-row-toggle').forEach(btn => {{
            btn.addEventListener('click', () => {{
              const panel = wrap.querySelector('#cf-row-panel-' + b.id + '-' + btn.dataset.si);
              if (panel) panel.classList.toggle('hidden');
            }});
          }});

          // Pick an alternative for a matched/ambiguous sale (display only).
          wrap.querySelectorAll('.cf-tied-pick').forEach(btn => {{
            btn.addEventListener('click', () => {{
              const si = Number(btn.dataset.si);
              const oi = Number(btn.dataset.oi);
              const s = sales[si];
              const allOpts = [s.invoice].concat(s.tied_candidates||[]);
              const chosen = allOpts[oi];
              if (!chosen) return;
              // Claim the calendar slot so it disappears from every other row.
              _setCalRowSlot(b.id, si, btn.dataset.calSlot || '');
              _setAdjustment(_saleKey(b.id, sales[si], si), null);
              _setTiedSwap(b.id, si, chosen);
              wrap.replaceWith(renderBatch(b));
            }});
          }});

          // Pick a candidate invoice for a missing sale (testing only — no submit).
          wrap.querySelectorAll('.cf-cand-pick').forEach(btn => {{
            btn.addEventListener('click', () => {{
              const si = Number(btn.dataset.si);
              const ci = Number(btn.dataset.ci);
              const s = sales[si];
              const cand = (s.candidates || [])[ci];
              if (!cand) return;
              _setCalRowSlot(b.id, si, btn.dataset.calSlot || '');
              _setAdjustment(_saleKey(b.id, s, si), null);
              _setMatch(_saleKey(b.id, s, si), cand);
              wrap.replaceWith(renderBatch(b));
            }});
          }});

          // Reset a transaction back to the app's automatic choice.
          wrap.querySelectorAll('.cf-row-reset').forEach(btn => {{
            btn.addEventListener('click', () => {{
              const si = Number(btn.dataset.si);
              _setCalRowSlot(b.id, si, '');
              _setAdjustment(_saleKey(b.id, sales[si], si), null);
              if (btn.dataset.missing === '1') {{
                _setMatch(_saleKey(b.id, sales[si], si), null);
              }} else {{
                _setTiedSwap(b.id, si, null);
              }}
              wrap.replaceWith(renderBatch(b));
            }});
          }});

          // Mark how a selected invoice/card mismatch should be made to balance.
          wrap.querySelectorAll('.cf-adjust-set').forEach(btn => {{
            btn.addEventListener('click', () => {{
              const si = Number(btn.dataset.si);
              const amount = Number(btn.dataset.amt || 0);
              if (!amount || amount <= 0) return;
              _setAdjustment(_saleKey(b.id, sales[si], si), {{
                type: btn.dataset.type || '',
                amount: amount,
              }});
              wrap.replaceWith(renderBatch(b));
            }});
          }});
          wrap.querySelectorAll('.cf-adjust-clear').forEach(btn => {{
            btn.addEventListener('click', () => {{
              const si = Number(btn.dataset.si);
              _setAdjustment(_saleKey(b.id, sales[si], si), null);
              wrap.replaceWith(renderBatch(b));
            }});
          }});

          // Cheat action: raise a tidy standalone invoice for a stray card payment.
          wrap.querySelectorAll('.cf-quick-invoice').forEach(btn => {{
            btn.addEventListener('click', async () => {{
              const ref = btn.dataset.ref || '';
              const amt = Number(btn.dataset.amt);
              const date = btn.dataset.date || '';
              const si = Number(btn.dataset.si);
              const s = sales[si];
              const contact = (btn.dataset.contact || '').trim();
              const defaultDesc = (btn.dataset.desc || 'Materials').trim() || 'Materials';
              const targetContact = contact || 'Materials';
              if (!CF_DRY_RUN) {{
                if (!window.confirm('This will create a REAL invoice in Xero for ' + money(amt) + ' to "' + targetContact + '". Continue?')) return;
              }}
              const desc = (window.prompt('Description for this quick invoice:', defaultDesc) || '').trim();
              if (desc === '') return;
              const orig = btn.innerHTML;
              btn.disabled = true;
              btn.innerHTML = 'Creating\u2026';
              try {{
                const resp = await fetch('/cashflows-sync/create-quick-invoice', {{
                  method: 'POST',
                  headers: {{ 'Content-Type': 'application/json' }},
                  body: JSON.stringify({{ sale_ref: ref, amount: amt, date: date, description: desc, contact_name: contact }})
                }});
                const data = await resp.json();
                if (!resp.ok || data.error) {{ throw new Error(data.error || 'Request failed'); }}
                if (s) s.quick_invoice = {{ id: data.id, number: data.number, contact_name: data.contact_name, description: data.description, amount: data.amount, dry_run: data.dry_run }};
                wrap.replaceWith(renderBatch(b));
              }} catch (err) {{
                btn.disabled = false;
                btn.innerHTML = orig;
                alert('Could not create invoice: ' + err.message);
              }}
            }});
          }});

          return wrap;
        }}

        function sourceRow(ok, label, detail) {{
          const icon = ok === true ? '✅' : ok === false ? '❌' : '⚠️';
          const colour = ok === true ? 'text-emerald-700' : ok === false ? 'text-red-600' : 'text-amber-700';
          return `<div class="flex items-start gap-2 text-xs">
            <span>${{icon}}</span>
            <span><span class="font-semibold ${{colour}}">${{esc(label)}}</span>${{detail ? ' &mdash; ' + detail : ''}}</span>
          </div>`;
        }}

        function renderBatchCompactSection(title, batches, note, tone) {{
          if (!batches.length) return null;
          const section = document.createElement('details');
          section.open = false;
          const toneCls = tone === 'submitted' || tone === 'reconciled'
            ? 'border-emerald-200 bg-emerald-50 text-emerald-900'
            : tone === 'prepared'
              ? 'border-sky-200 bg-sky-50 text-sky-900'
            : 'border-gray-200 bg-gray-50 text-gray-800';
          section.className = 'rounded-xl border ' + toneCls;
          const rows = batches.map(function(b) {{
            const payout = b.payout || {{}};
            const names = (b.sales || []).map(function(s) {{
              const inv = s.invoice || s.quick_invoice || {{}};
              return inv.contact_name || inv.number || s.sale_ref || '';
            }}).filter(Boolean).slice(0, 3).join(', ');
            const date = gb((b.bank_line || {{}}).date || payout.date || '');
            const label = tone === 'submitted'
              ? 'Submitted'
              : tone === 'prepared'
                ? 'Prepared in Xero'
                : tone === 'waiting'
                  ? 'Not in Xero reconcile list'
                  : 'Already reconciled in Xero';
            return `<div class="grid grid-cols-12 gap-2 items-center px-3 py-2 border-t border-white/60 first:border-t-0 text-xs">
              <div class="col-span-12 sm:col-span-3 font-semibold">${{esc(payout.csv_ref || b.id || '')}}</div>
              <div class="col-span-6 sm:col-span-2">${{money(b.net || payout.amount || 0)}}</div>
              <div class="col-span-6 sm:col-span-2 text-gray-500">${{esc(date)}}</div>
              <div class="col-span-12 sm:col-span-3 truncate">${{esc(names || 'Cashflows batch')}}</div>
              <div class="col-span-12 sm:col-span-2 text-right font-semibold">${{label}}</div>
            </div>`;
          }}).join('');
          section.innerHTML = `
            <summary class="cursor-pointer list-none px-4 py-3 flex items-center justify-between gap-3">
              <span class="text-sm font-semibold">${{esc(title)}} <span class="text-xs font-normal opacity-70">(${{batches.length}})</span></span>
              <span class="text-xs opacity-70">${{esc(note || '')}}</span>
            </summary>
            <div class="bg-white/70">${{rows}}</div>`;
          return section;
        }}

        function renderCsvResults(data) {{
          _csvPreviewData = data;
          csvResults.innerHTML = '';
          const counts = data.status_counts || {{}};
          const alreadyDone = counts.already_reconciled || 0;
          const preparedInXero = counts.prepared_in_xero || 0;
          const submittedBatches = (data.batches || []).filter(b => !['already_reconciled', 'prepared_in_xero'].includes(b.status) && _isSubmitted(b.id));
          const reconciledBatches = (data.batches || []).filter(b => b.status === 'already_reconciled');
          const preparedBatches = (data.batches || []).filter(b => b.status === 'prepared_in_xero');
          const activeBatches = (data.batches || []).filter(b => !['already_reconciled', 'prepared_in_xero'].includes(b.status) && !_isSubmitted(b.id));
          const workBatches = activeBatches;
          const total = data.active_batch_count || activeBatches.length;
          const bankMatched = data.bank_matched_count || 0;

          // ── Data source status panel ──────────────────────────────────────
          const srcPanel = document.createElement('div');
          srcPanel.className = 'rounded-xl border border-gray-200 bg-white p-4 space-y-2';

          const xeroInvCount = data.xero_invoice_count;
          let xeroOk, xeroDetail;
          if (!data.xero_connected) {{
            xeroOk = false;
            xeroDetail = data.xero_error
              ? esc(data.xero_error)
              : 'Not connected &mdash; <a href="/settings" class="underline">reconnect in Settings</a>';
          }} else {{
            xeroOk = true;
            xeroDetail = xeroInvCount != null ? `${{xeroInvCount}} invoice(s) fetched` : 'Connected';
          }}

          let bankOk, bankDetail;
          if (data.bank_scope_missing) {{
            bankOk = null;
            bankDetail = 'Scope missing &mdash; tick <em>accounting.banktransactions</em> in your Xero developer app then reconnect';
          }} else if (!data.xero_connected) {{
            bankOk = false;
            bankDetail = 'Unavailable (Xero not connected)';
          }} else {{
            const n = data.xero_bank_tx_count;
            bankOk = true;
            bankDetail = n != null ? `${{n}} unreconciled bank transaction(s) fetched` : 'Fetched';
          }}

          const sheetCount = data.sheet_card_count;
          const sheetOk = sheetCount != null;
          const sheetDetail = sheetOk
            ? `${{sheetCount}} CARD rows loaded`
            : (data.sheet_status ? esc(data.sheet_status) : 'Not configured &mdash; invoice matching uses heuristic fallback');

          const aiCalOk = data.ai_calendar_configured == null ? null : !!data.ai_calendar_configured;
          const aiCalDetail = aiCalOk === false
            ? 'No OpenAI key &mdash; calendar events matched by time only; customer names &amp; amounts not extracted. '
              + '<a href="/settings" class="underline font-semibold">Set up &#x2753;</a>'
            : aiCalOk === true
              ? 'OpenAI key configured &mdash; event names and amounts will be extracted from calendar entries'
              : 'Status unknown';

          srcPanel.innerHTML = `
            <div class="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-1">Data sources</div>
            ${{sourceRow(true, 'CSV upload', `${{(data.totals||{{}}).sale_count||0}} sales &middot; ${{(data.totals||{{}}).payout_count||0}} payouts &middot; ${{money((data.totals||{{}}).gross_sales||0)}} gross`)}}
            ${{sourceRow(xeroOk, 'Xero invoices', xeroDetail)}}
            ${{sourceRow(bankOk, 'Xero bank transactions', bankDetail)}}
            ${{sourceRow(sheetOk, 'Google Sheets (CARD lookup)', sheetDetail)}}
            ${{sourceRow(aiCalOk, 'AI calendar matching', aiCalDetail)}}`;
          csvResults.appendChild(srcPanel);

          // ── Matching summary ──────────────────────────────────────────────
          const matchPanel = document.createElement('div');
          matchPanel.className = 'rounded-xl border border-gray-200 bg-white p-4';
          const pct = total > 0 ? Math.round(bankMatched / total * 100) : 0;
          const barCls = pct === 100 ? 'bg-emerald-500' : pct > 0 ? 'bg-amber-400' : 'bg-gray-300';
          matchPanel.innerHTML = `
            <div class="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-2">Cashflows batches from this CSV</div>
            <div class="flex items-center gap-3 flex-wrap">
              <div class="flex-1 min-w-32 h-2 rounded-full bg-gray-100 overflow-hidden">
                <div class="h-2 rounded-full ${{barCls}}" style="width:${{pct}}%"></div>
              </div>
              <span class="text-sm font-semibold text-gray-900">${{bankMatched}} of ${{total}} settlement batches found in your Xero bank feed</span>
            </div>
            <div class="mt-1 text-xs text-gray-500">Already reconciled, prepared, and submitted batches are collapsed below so the active work stays visible without losing anything.</div>
            <div class="mt-2 flex flex-wrap gap-2 text-xs">
              <span class="px-2 py-1 rounded-full ${{STATUS_META.ready.cls}}">${{counts.ready||0}} ready to reconcile</span>
              <span class="px-2 py-1 rounded-full ${{STATUS_META.needs_review.cls}}">${{counts.needs_review||0}} worth a check</span>
              <span class="px-2 py-1 rounded-full ${{STATUS_META.waiting_invoices.cls}}">${{counts.waiting_invoices||0}} missing an invoice</span>
              <span class="px-2 py-1 rounded-full ${{STATUS_META.no_bank_line.cls}}">${{counts.no_bank_line||0}} not in Xero reconcile list</span>
              ${{preparedInXero ? `<span class="px-2 py-1 rounded-full ${{STATUS_META.prepared_in_xero.cls}}">${{preparedInXero}} prepared in Xero</span>` : ''}}
              ${{alreadyDone ? `<span class="px-2 py-1 rounded-full ${{STATUS_META.already_reconciled.cls}}">${{alreadyDone}} already reconciled in Xero</span>` : ''}}
            </div>`;
          csvResults.appendChild(matchPanel);

          // ── Main work list ──────────────────────────────────────────────────
          workBatches.slice().reverse().forEach(b => csvResults.appendChild(renderBatch(b)));

          const submittedSection = renderBatchCompactSection(
            'Submitted this session',
            submittedBatches.slice().reverse(),
            'Prepared for Xero Find & Match',
            'submitted'
          );
          if (submittedSection) csvResults.appendChild(submittedSection);

          const preparedSection = renderBatchCompactSection(
            'Prepared in Xero',
            preparedBatches.slice().reverse(),
            'Open Xero bank reconciliation and press OK',
            'prepared'
          );
          if (preparedSection) csvResults.appendChild(preparedSection);

          const reconciledSection = renderBatchCompactSection(
            'Already reconciled in Xero',
            reconciledBatches.slice().reverse(),
            'Hidden from the active work list',
            'reconciled'
          );
          if (reconciledSection) csvResults.appendChild(reconciledSection);

          // ── Unpaid sales footer ───────────────────────────────────────────
          const unpaid = data.unpaid_sales || [];
          if (unpaid.length) {{
            const u = document.createElement('div');
            u.className = 'rounded-xl border border-sky-200 bg-sky-50 p-4';
            u.innerHTML = `<div class="text-sm font-semibold text-sky-900">💤 Not paid out yet (${{unpaid.length}})</div>
              <div class="text-xs text-sky-800 mt-1">These sales have matured but no remittance appears in this file yet. They will appear automatically on a later upload once Cashflows pays them out.</div>
              <div class="text-xs text-sky-700 mt-2">${{unpaid.map(s => esc(s.sale_ref) + ' (' + money(s.gross) + ')').join(', ')}}</div>`;
            csvResults.appendChild(u);
          }}
          csvResults.classList.remove('hidden');
          updateCsvSubmitPanel();
        }}

        async function loadRecommendedRange() {{
          try {{
            const resp = await fetch('/cashflows-sync/recommended-range');
            const data = await resp.json();
            if (data.date_from && data.date_to) {{
              recRangeText.textContent = ' ' + data.date_from + ' → ' + data.date_to;
              recRangeReason.textContent = data.reason || '';
              recRange.classList.remove('hidden');
            }}
          }} catch (err) {{ /* non-fatal */ }}
        }}

        uploadCsvBtn.addEventListener('click', async () => {{
          csvError.classList.add('hidden');
          const file = csvFile.files && csvFile.files[0];
          if (!file) {{ csvShowError('Choose a CSV file first.'); return; }}
          uploadCsvBtn.disabled = true;
          csvStatus.textContent = 'Reading Xero and matching…';
          try {{
            const form = new FormData();
            form.append('csv_file', file);
            const resp = await fetch('/cashflows-sync/upload-csv', {{method: 'POST', body: form}});
            const data = await resp.json();
            if (!resp.ok) throw new Error(data.error || 'Upload failed');
            _previewId = data.preview_id || Date.now().toString();
            renderCsvTotals(data.totals || {{}});
            renderCsvResults(data);
            if (!['running', 'paused'].includes(_submitProgressStatus) && csvProgress) {{
              csvProgress.classList.add('hidden');
            }}
            const sheetEl = document.getElementById('correlation-sheet-status');
            if (sheetEl && data.sheet_status) {{
              sheetEl.textContent = data.sheet_status;
              sheetEl.className = 'text-xs ' + (data.sheet_card_count != null ? 'text-emerald-700' : 'text-amber-700');
            }}
            csvStatus.textContent = 'Preview ready · no Xero changes were made.';
            if ((data.warnings || []).length) {{
              csvShowError('Notes: ' + data.warnings.join(' '));
            }}
          }} catch (err) {{
            csvShowError(err.message || String(err));
            csvStatus.textContent = '';
          }} finally {{
            uploadCsvBtn.disabled = false;
          }}
        }});

        csvSubmitBtn.addEventListener('click', async () => {{
          csvError.classList.add('hidden');
          csvSubmitOutput.classList.add('hidden');
          const batches = collectCsvSubmission();
          if (!batches.length) {{
            csvSubmitStatus.textContent = 'Tick at least one batch first.';
            return;
          }}
          if (!window.confirm('Submit ' + batches.length + ' selected Cashflows batch' + (batches.length === 1 ? '' : 'es') + ' for Xero reconciliation preparation?')) {{
            return;
          }}
          csvSubmitBtn.disabled = true;
          csvSubmitStatus.textContent = csvPreviewMode && csvPreviewMode.checked
            ? 'Preview mode: preparing payloads without Xero writes...'
            : 'Preparing Xero submission payloads...';
          try {{
            const resp = await fetch('/cashflows-sync/submit-csv-batches', {{
              method: 'POST',
              headers: {{'Content-Type': 'application/json'}},
              body: JSON.stringify({{
                preview_id: _previewId,
                batches: batches,
                preview_mode: !!(csvPreviewMode && csvPreviewMode.checked),
              }}),
            }});
            const data = await resp.json();
            if (!resp.ok || data.error) throw new Error(data.error || 'Submit failed');
            if (data.async) {{
              // Background job started — switch to the live progress screen.
              csvSubmitStatus.textContent = '';
              renderSubmitProgress({{status: 'running', total: data.total || batches.length, completed: 0, percent: 0, message: data.message || 'Reconciling in the background…'}});
              if (_submitPollTimer) clearTimeout(_submitPollTimer);
              pollSubmitProgress(true);
              return;
            }}
            // Test mode (no async) — show the payload summary as before.
            csvSubmitStatus.textContent = data.message || 'Submission prepared.';
            renderCsvSubmitSummary(data);
          }} catch (err) {{
            csvSubmitStatus.textContent = '';
            csvShowError(err.message || String(err));
          }} finally {{
            updateCsvSubmitPanel();
          }}
        }});

        // Reattach to any submission already running on the server (e.g. after
        // the page was closed and reopened mid-run).
        pollSubmitProgress(true);
        loadRecommendedRange();

        document.getElementById('save-correlation-sheet-btn').addEventListener('click', async () => {{
          const btn = document.getElementById('save-correlation-sheet-btn');
          const input = document.getElementById('correlation-sheet-input');
          const statusEl = document.getElementById('correlation-sheet-status');
          btn.disabled = true;
          statusEl.textContent = 'Saving…';
          statusEl.className = 'text-xs text-gray-500';
          try {{
            const resp = await fetch('/cashflows-sync/save-correlation-sheet', {{
              method: 'POST',
              headers: {{'Content-Type': 'application/json'}},
              body: JSON.stringify({{sheet_id: input.value.trim()}}),
            }});
            const data = await resp.json();
            if (!resp.ok) throw new Error(data.error || 'Save failed');
            statusEl.textContent = data.status_msg || 'Saved.';
            statusEl.className = 'text-xs ' + (data.ok ? 'text-emerald-700' : 'text-amber-700');
            if (data.ok && input.value.trim()) {{
              btn.textContent = '✓ Saved';
              setTimeout(() => {{ btn.textContent = 'Save'; }}, 2500);
              const badge = document.getElementById('sheet-saved-badge');
              if (badge) {{
                badge.textContent = '✓ Saved';
                badge.className = 'px-2 py-0.5 rounded-full bg-emerald-100 text-emerald-700 text-[11px] font-semibold border border-emerald-200';
              }}
            }}
          }} catch (err) {{
            statusEl.textContent = '⚠ ' + (err.message || String(err));
            statusEl.className = 'text-xs text-red-700';
          }} finally {{
            btn.disabled = false;
          }}
        }});
        </script>
        """
        return _page(body)

    @app.post("/cashflows-sync/save-correlation-sheet")
    @require_login
    def cashflows_sync_save_correlation_sheet():
        import flask as _flask
        raw = (request.get_json(silent=True) or {}).get("sheet_id", "").strip()
        # Accept either a full URL or a bare sheet ID
        sheet_id = _extract_spreadsheet_id(raw) if raw else ""
        set_cashflows_correlation_sheet_id(config.admin_db_file, sheet_id)
        if not sheet_id:
            return _flask.jsonify({"ok": True, "status_msg": "Cleared — using GC- prefix heuristic as fallback."})
        try:
            lookup = fetch_card_lookup(sheet_id, timeout=10)
            msg = (
                f"✓ Connected — {lookup.total_card} CARD rows, "
                f"{lookup.total_rows} total rows"
            )
            return _flask.jsonify({"ok": True, "status_msg": msg})
        except Exception as exc:
            return _flask.jsonify({
                "ok": False,
                "status_msg": f"Saved, but could not verify sheet: {str(exc)[:120]}",
            })

    @app.post("/cashflows-sync/diagnostics")
    @require_login
    def cashflows_sync_diagnostics():
        import flask as _flask

        payload = request.get_json(silent=True) or {}
        start_date = _date(payload.get("date_from"))
        end_date = _date(payload.get("date_to"))
        if not start_date or not end_date:
            end_date = dt.date.today()
            start_date = end_date - dt.timedelta(days=14)
        try:
            xero_client = build_xero_client(config)
            if not xero_client:
                raise RuntimeError("Xero is not connected.")
            cashflows_client = CashflowsClient.from_config(config)
            bank_payload = xero_client.get_bank_transactions(
                start_date=start_date,
                end_date=end_date,
            )
            invoice_payload = xero_client.get_open_invoices()
            from .cashflows_reconciliation import parse_xero_bank_lines, parse_xero_invoices

            cashflows_diag = cashflows_client.diagnose_settlements(start_date, end_date)
            result = {
                "date_from": start_date.isoformat(),
                "date_to": end_date.isoformat(),
                "safe_mode": {
                    "xero_writes_enabled": bool(
                        os.getenv("CASHFLOWS_RECONCILE_PRODUCTION", "false").lower() == "true"
                        and not bool(config.dry_run)
                    ),
                    "ai_enabled": bool(
                        os.getenv("CASHFLOWS_RECONCILE_AI_ENABLED", "false").lower() == "true"
                    ),
                },
                "xero": {
                    "bank_transactions_read_ok": True,
                    "cfe_sett_unreconciled_count": len(parse_xero_bank_lines(bank_payload)),
                    "open_invoice_count": len(parse_xero_invoices(invoice_payload)),
                },
                "cashflows": cashflows_diag,
            }
            set_json_setting(config.admin_db_file, "cashflows_reconcile_diagnostics", result)
            return _flask.jsonify(result)
        except Exception as exc:
            return _flask.jsonify({"error": str(exc).splitlines()[0][:300]}), 400

    @app.post("/cashflows-sync/preview")
    @require_login
    def cashflows_sync_preview():
        import flask as _flask

        payload = request.get_json(silent=True) or {}
        start_date = _date(payload.get("date_from"))
        end_date = _date(payload.get("date_to"))
        manual_settlements_json = str(payload.get("manual_settlements_json") or "").strip()
        try:
            xero_client = build_xero_client(config)
            cashflows_client = None
            manual_count = 0
            if manual_settlements_json:
                try:
                    manual_payload = json.loads(manual_settlements_json)
                except Exception as exc:
                    return _flask.jsonify({"error": f"Manual settlement JSON is invalid: {exc}"}), 400
                manual_settlements = parse_cashflows_settlements(manual_payload)
                manual_count = len(manual_settlements)

                class _ManualCashflowsClient:
                    def fetch_settlements(self, _start_date, _end_date):
                        return manual_settlements

                cashflows_client = _ManualCashflowsClient()
            svc = CashflowsReconciliationService(
                config,
                xero_client=xero_client,
                cashflows_client=cashflows_client,
            )
            result = svc.scan(start_date=start_date, end_date=end_date)
            if manual_settlements_json:
                result["cashflows_source"] = "manual_json"
                result["manual_settlement_count"] = manual_count
            else:
                result["cashflows_source"] = "api"
            set_json_setting(config.admin_db_file, "cashflows_reconcile_preview", result)
            _feed.push(
                f"Cashflows preview scanned {result.get('counts', {}).get('xero_cfe_bank_lines', 0)} CFE SETT line(s)",
                "system",
            )
            return _flask.jsonify(result)
        except Exception as exc:
            return _flask.jsonify({"error": str(exc).splitlines()[0][:300]}), 400

    @app.post("/cashflows-sync/confirm")
    @require_login
    def cashflows_sync_confirm():
        import flask as _flask

        payload = request.get_json(silent=True) or {}
        preview_id = str(payload.get("preview_id") or "").strip()
        match_id = str(payload.get("match_id") or "").strip()
        preview = get_json_setting(config.admin_db_file, "cashflows_reconcile_preview", {})
        if not preview_id or preview_id != str(preview.get("preview_id") or ""):
            return _flask.jsonify({"error": "Preview expired. Run Scan & Preview Matches again."}), 409
        match = next(
            (m for m in (preview.get("matches") or []) if str(m.get("id") or "") == match_id),
            None,
        )
        if not match:
            return _flask.jsonify({"error": "Match not found in the latest preview."}), 404
        try:
            xero_client = build_xero_client(config)
            svc = CashflowsReconciliationService(config, xero_client=xero_client)
            result = svc.confirm(match)
            _feed.push(
                f"Cashflows match confirmed in {result.get('mode', 'unknown')} mode",
                "success" if result.get("mode") == "production" else "system",
            )
            return _flask.jsonify(result)
        except Exception as exc:
            return _flask.jsonify({"error": str(exc).splitlines()[0][:300]}), 400

    @app.post("/cashflows-sync/upload-csv")
    @require_login
    def cashflows_sync_upload_csv():
        import flask as _flask

        upload = request.files.get("csv_file")
        if upload is None or not (upload.filename or "").strip():
            return _flask.jsonify({"error": "No CSV file was uploaded."}), 400
        try:
            raw_bytes = upload.read()
            csv_text = raw_bytes.decode("utf-8-sig", errors="replace")
        except Exception as exc:
            return _flask.jsonify({"error": f"Could not read the uploaded file: {exc}"}), 400
        try:
            from .admin_store import get_active_calendars as _get_active_cals
            xero_client = build_xero_client(config)
            sheet_id = get_cashflows_correlation_sheet_id(config.admin_db_file)
            cal_ids = _get_active_cals(config.admin_db_file, config.google_calendar_id)
            result = build_csv_reconciliation_preview(
                config, csv_text,
                xero_client=xero_client,
                correlation_sheet_id=sheet_id,
                calendar_ids=cal_ids or None,
            )
            if not result.get("xero_connected"):
                msg = str(result.get("xero_error") or "Xero is not connected.").strip()
                return _flask.jsonify({
                    "error": f"Cashflows preview needs Xero data. {msg}",
                    "xero_connected": False,
                    "xero_error": msg,
                }), 503
            result["source_filename"] = (upload.filename or "").strip()
            cached_preview = {**result, "_source_csv_text": csv_text}
            set_json_setting(config.admin_db_file, "cashflows_csv_preview", cached_preview)
            _clear_finished_cashflows_submit_job(config.admin_db_file)
            counts = result.get("status_counts", {})
            _feed.push(
                "Cashflows CSV preview: "
                f"{counts.get('ready', 0)} ready, "
                f"{counts.get('waiting_invoices', 0)} waiting, "
                f"{counts.get('no_bank_line', 0)} no bank line (test mode, no writes)",
                "system",
            )
            return _flask.jsonify(result)
        except CsvParseError as exc:
            return _flask.jsonify({"error": str(exc)}), 400
        except Exception as exc:
            return _flask.jsonify({"error": str(exc).splitlines()[0][:300]}), 400

    @app.post("/cashflows-sync/refresh-csv-preview")
    @require_login
    def cashflows_sync_refresh_csv_preview():
        import flask as _flask

        payload = request.get_json(silent=True) or {}
        preview = get_json_setting(config.admin_db_file, "cashflows_csv_preview", {})
        preview_id = str(payload.get("preview_id") or "").strip()
        if not preview or not isinstance(preview, dict):
            return _flask.jsonify({"error": "No Cashflows CSV preview is cached. Upload the CSV again first."}), 409
        if preview_id and str(preview.get("preview_id") or "") and preview_id != str(preview.get("preview_id")):
            return _flask.jsonify({"error": "This preview is out of date. Upload the CSV again before refreshing."}), 409
        csv_text = str(preview.get("_source_csv_text") or "")
        if not csv_text.strip():
            return _flask.jsonify({"error": "The original CSV is not cached. Upload the CSV again first."}), 409
        try:
            from .admin_store import get_active_calendars as _get_active_cals
            xero_client = build_xero_client(config)
            sheet_id = get_cashflows_correlation_sheet_id(config.admin_db_file)
            cal_ids = _get_active_cals(config.admin_db_file, config.google_calendar_id)
            result = build_csv_reconciliation_preview(
                config, csv_text,
                xero_client=xero_client,
                correlation_sheet_id=sheet_id,
                calendar_ids=cal_ids or None,
            )
            if not result.get("xero_connected"):
                msg = str(result.get("xero_error") or "Xero is not connected.").strip()
                return _flask.jsonify({
                    "error": f"Cashflows refresh kept the previous preview because Xero data is unavailable. {msg}",
                    "xero_connected": False,
                    "xero_error": msg,
                }), 503
            result["source_filename"] = str(preview.get("source_filename") or "").strip()
            cached_preview = {**result, "_source_csv_text": csv_text}
            set_json_setting(config.admin_db_file, "cashflows_csv_preview", cached_preview)
            _clear_finished_cashflows_submit_job(config.admin_db_file)
            counts = result.get("status_counts", {})
            _feed.push(
                "Cashflows CSV refreshed: "
                f"{counts.get('ready', 0)} ready, "
                f"{counts.get('prepared_in_xero', 0)} prepared, "
                f"{counts.get('already_reconciled', 0)} reconciled",
                "system",
            )
            return _flask.jsonify(result)
        except CsvParseError as exc:
            return _flask.jsonify({"error": str(exc)}), 400
        except Exception as exc:
            return _flask.jsonify({"error": str(exc).splitlines()[0][:300]}), 400

    @app.post("/cashflows-sync/create-quick-invoice")
    @require_login
    def cashflows_sync_create_quick_invoice():
        import flask as _flask

        payload = request.get_json(silent=True) or {}
        sale_ref = str(payload.get("sale_ref") or "").strip()
        if not sale_ref:
            return _flask.jsonify({"error": "Missing sale reference."}), 400
        description = (str(payload.get("description") or "").strip() or "Materials")[:200]
        contact_name = (
            str(payload.get("contact_name") or "").strip()
            or str(
                get_json_setting(
                    config.admin_db_file, "cashflows_quick_invoice_contact", "Materials"
                )
                or "Materials"
            )
        )[:200]

        # Server is the source of truth for amount/date: locate the sale in the
        # cached preview and require it to be a genuine, still-unmatched card
        # payment. This stops the endpoint from minting arbitrary invoices.
        preview = get_json_setting(config.admin_db_file, "cashflows_csv_preview", {})
        target = None
        for b in preview.get("batches", []) or []:
            for s in b.get("sales", []) or []:
                if str(s.get("sale_ref") or "") == sale_ref:
                    target = s
                    break
            if target is not None:
                break
        if target is None:
            return _flask.jsonify(
                {"error": "That sale is no longer in the current preview. Re-upload the CSV and try again."}
            ), 409
        if target.get("invoice"):
            return _flask.jsonify({"error": "That sale already has a matching invoice."}), 409
        if target.get("quick_invoice"):
            return _flask.jsonify(
                {"error": "A quick invoice has already been created for that sale."}
            ), 409
        try:
            amount = round(float(target.get("gross")), 2)
        except (TypeError, ValueError):
            return _flask.jsonify({"error": "That sale has no valid amount."}), 400
        if amount <= 0:
            return _flask.jsonify({"error": "Amount must be greater than zero."}), 400
        sale_date = _date(target.get("date"))
        invoice_date = sale_date.isoformat() if sale_date else None
        reference = "Card terminal " + sale_ref
        try:
            xero_client = build_xero_client(config)
            if xero_client is None:
                return _flask.jsonify(
                    {"error": "Xero is not connected. Connect Xero first."}
                ), 400
            result = xero_client.create_simple_invoice(
                contact_name=contact_name,
                description=description,
                amount=amount,
                reference=reference,
                invoice_date=invoice_date,
            )
        except Exception as exc:
            return _flask.jsonify({"error": str(exc).splitlines()[0][:300]}), 400

        dry = bool(isinstance(result, dict) and result.get("dry_run"))
        number = None
        invoice_id = None
        if not dry:
            try:
                created_invoice = ((result.get("Invoices") or [{}])[0])
                number = created_invoice.get("InvoiceNumber")
                invoice_id = created_invoice.get("InvoiceID")
            except Exception:
                number = None
                invoice_id = None

        # Reflect on the cached CSV preview so the row shows as resolved on re-render.
        try:
            target["quick_invoice"] = {
                "id": invoice_id,
                "number": number,
                "contact_name": contact_name,
                "description": description,
                "amount": amount,
                "dry_run": dry,
            }
            set_json_setting(config.admin_db_file, "cashflows_csv_preview", preview)
        except Exception:
            pass

        _feed.push(
            ("Quick invoice (simulated, dry-run): " if dry else "Quick invoice created in Xero: ")
            + f"\u00a3{amount:.2f} {contact_name}"
            + (f" \u2014 {number}" if number else ""),
            "system" if dry else "success",
        )
        return _flask.jsonify(
            {
                "ok": True,
                "dry_run": dry,
                "id": invoice_id,
                "number": number,
                "contact_name": contact_name,
                "description": description,
                "amount": amount,
            }
        )

    @app.post("/cashflows-sync/submit-csv-batches")
    @require_login
    def cashflows_sync_submit_csv_batches():
        import flask as _flask

        payload = request.get_json(silent=True) or {}
        preview = get_json_setting(config.admin_db_file, "cashflows_csv_preview", {})
        preview_id = str(payload.get("preview_id") or "").strip()
        if not preview or not isinstance(preview, dict):
            return _flask.jsonify({"error": "No Cashflows CSV preview is cached. Upload the CSV again first."}), 409
        if preview_id and str(preview.get("preview_id") or "") and preview_id != str(preview.get("preview_id")):
            return _flask.jsonify({"error": "This preview is out of date. Upload the CSV again before submitting."}), 409

        requested_batches = payload.get("batches") or []
        if not isinstance(requested_batches, list) or not requested_batches:
            return _flask.jsonify({"error": "No checked batches were submitted."}), 400

        xero_client = build_xero_client(config)
        preview_mode = bool(payload.get("preview_mode"))
        production_enabled = (
            os.getenv("CASHFLOWS_CSV_SUBMIT_PRODUCTION", "false").strip().lower()
            in ("1", "true", "yes", "on")
            and not bool(config.dry_run)
            and not preview_mode
        )
        if production_enabled and xero_client is None:
            return _flask.jsonify({
                "error": (
                    "Xero is temporarily unavailable to the app. Refresh the page and try again first; "
                    "only reconnect Xero if this keeps happening."
                )
            }), 503

        payment_account = str(getattr(xero_client, "payment_account_code", "") or "").strip() if xero_client else ""
        if not payment_account:
            payment_account = "090"
        clearing_account = (os.getenv("CASHFLOWS_CLEARING_ACCOUNT_CODE") or "780").strip()
        bank_fees_account = (os.getenv("CASHFLOWS_BANK_FEES_ACCOUNT_CODE") or "404").strip()

        def _money_value(value: object) -> float:
            try:
                return round(float(value or 0), 2)
            except (TypeError, ValueError):
                return 0.0

        def _account_ref(value: str) -> dict[str, str]:
            account_value = str(value or "").strip()
            if account_value.lower().startswith("id:"):
                return {"AccountID": account_value[3:].strip()}
            if len(account_value) == 36 and account_value.count("-") == 4:
                return {"AccountID": account_value}
            return {"Code": account_value}

        def _candidate_options(sale: dict) -> list[dict]:
            options: list[dict] = []
            for key in ("invoice", "quick_invoice"):
                item = sale.get(key)
                if isinstance(item, dict) and item:
                    options.append(item)
            for key in ("tied_candidates", "candidates"):
                for item in sale.get(key) or []:
                    if isinstance(item, dict) and item:
                        options.append(item)
            return options

        def _find_candidate(sale: dict, invoice_id: str, invoice_number: str) -> dict | None:
            invoice_id = str(invoice_id or "").strip()
            invoice_number = str(invoice_number or "").strip()
            for cand in _candidate_options(sale):
                cand_id = str(cand.get("id") or "").strip()
                cand_number = str(cand.get("number") or "").strip()
                if invoice_id and cand_id == invoice_id:
                    return cand
                if invoice_number and cand_number == invoice_number:
                    return cand
            return None

        def _existing_account_payments(invoice_id: str) -> list[dict]:
            """Return Xero payments already posted to an account for this invoice.

            If an already-paid invoice has an ACCRECPAYMENT posted to the bank
            account, creating a clearing receipt would duplicate the bank/account
            balance. In that case the user must match/reconcile the existing
            payment or undo it first.
            """
            if not production_enabled or not xero_client or not invoice_id:
                return []
            try:
                invoice = xero_client.get_invoice(invoice_id)
            except Exception:
                return []
            found: list[dict] = []
            for payment in invoice.get("Payments") or []:
                payment_id = str(payment.get("PaymentID") or "").strip()
                if not payment_id or not hasattr(xero_client, "_request"):
                    continue
                try:
                    resp = xero_client._request("GET", f"{xero_client.base_url}/Payments/{payment_id}")
                    if not resp.ok:
                        continue
                    payment_detail = ((resp.json() or {}).get("Payments") or [{}])[0]
                except Exception:
                    continue
                account = payment_detail.get("Account") or {}
                if payment_detail.get("HasAccount") and account:
                    found.append(
                        {
                            "payment_id": payment_id,
                            "amount": _money_value(payment_detail.get("Amount")),
                            "status": payment_detail.get("Status"),
                            "account_name": account.get("Name"),
                            "account_id": account.get("AccountID"),
                            "account_code": account.get("Code"),
                            "is_reconciled": payment_detail.get("IsReconciled"),
                        }
                    )
            return found

        batch_map = {str(b.get("id") or ""): b for b in (preview.get("batches") or []) if isinstance(b, dict)}
        submitted_by_id = {
            str(b.get("batch_id") or ""): b
            for b in requested_batches
            if isinstance(b, dict)
        }

        plans: list[dict] = []
        blocking_errors: list[str] = []
        production_blockers: list[str] = []

        for batch_id, req in submitted_by_id.items():
            batch = batch_map.get(batch_id)
            if not batch:
                blocking_errors.append(f"Selected batch {batch_id} is no longer in the preview.")
                continue
            if batch.get("already_reconciled") or batch.get("status") == "already_reconciled":
                blocking_errors.append(f"Batch {batch_id} has already been marked reconciled.")
                continue
            sales = batch.get("sales") or []
            req_sales: dict[int, dict] = {}
            for submitted_sale in req.get("sales") or []:
                if not isinstance(submitted_sale, dict):
                    continue
                try:
                    submitted_idx = int(submitted_sale.get("sale_index"))
                except (TypeError, ValueError):
                    continue
                req_sales[submitted_idx] = submitted_sale
            if len(req_sales) < len(sales):
                blocking_errors.append(f"Batch {batch_id} was incomplete. Re-open the preview and submit again.")
                continue

            reference = f"Cashflows {((batch.get('payout') or {}).get('csv_ref') or batch_id)}"
            payout_date = ((batch.get("payout") or {}).get("date") or dt.date.today().isoformat())
            gross_total = _money_value(batch.get("gross"))
            net_total = _money_value(batch.get("net"))
            fee_total = round(max(0.0, gross_total - net_total), 2)
            payments: list[dict] = []
            chosen_invoices: list[dict] = []
            extra_invoice_payloads: list[dict] = []
            credit_note_payloads: list[dict] = []
            discount_actions: list[dict] = []
            already_paid: list[dict] = []
            existing_account_payments: list[dict] = []
            open_invoice_count = 0
            paid_invoice_payment_total = 0.0
            paid_invoice_discount_total = 0.0
            paid_invoice_extra_total = 0.0
            paid_overpayment_adjustments: list[dict] = []

            for idx, sale in enumerate(sales):
                req_sale = req_sales.get(idx) or {}
                selected = _find_candidate(
                    sale,
                    str(req_sale.get("selected_invoice_id") or ""),
                    str(req_sale.get("selected_invoice_number") or ""),
                )
                sale_ref = str(sale.get("sale_ref") or "")
                sale_gross = _money_value(sale.get("gross"))
                if not selected:
                    blocking_errors.append(
                        f"Batch {batch_id} sale {sale_ref or idx + 1} has no selected invoice."
                    )
                    continue
                if str(selected.get("status") or "").strip().upper() == "DRAFT":
                    blocking_errors.append(
                        f"Batch {batch_id} invoice {selected.get('number') or selected.get('id') or 'invoice'} is still DRAFT in Xero. Process/approve the invoice first, then refresh this batch."
                    )
                    continue
                selected_id = str(selected.get("id") or "").strip()
                selected_total = _money_value(selected.get("total") or selected.get("amount") or selected.get("amount_due"))
                selected_due = _money_value(selected.get("amount_due"))
                payable_amount = selected_due if selected_due > 0 else selected_total
                adjustment = req_sale.get("adjustment") if isinstance(req_sale.get("adjustment"), dict) else None
                chosen_invoices.append(
                    {
                        "id": selected_id,
                        "number": selected.get("number"),
                        "contact_name": selected.get("contact_name"),
                        "contact_id": selected.get("contact_id"),
                        "total": selected_total,
                        "amount_due": selected_due,
                        "payable_amount": payable_amount,
                        "is_open": selected.get("is_open"),
                    }
                )
                is_open = selected.get("is_open")
                if is_open is False:
                    already_paid.append(selected)
                    for payment in _existing_account_payments(selected_id):
                        existing_account_payments.append(
                            {
                                **payment,
                                "invoice_id": selected_id,
                                "invoice_number": selected.get("number"),
                                "contact_name": selected.get("contact_name"),
                                "expected_amount": payable_amount,
                                "payment_date": str(sale.get("date") or payout_date),
                            }
                        )
                else:
                    open_invoice_count += 1
                if not selected_id:
                    blocking_errors.append(
                        f"Batch {batch_id} invoice {selected.get('number') or sale_ref or idx + 1} has no Xero InvoiceID."
                    )
                    continue

                diff = round(sale_gross - payable_amount, 2)
                if is_open is False:
                    paid_invoice_payment_total = round(paid_invoice_payment_total + payable_amount, 2)
                    if diff < -0.01:
                        if not adjustment or adjustment.get("type") != "discount":
                            blocking_errors.append(
                                f"Batch {batch_id} invoice {selected.get('number') or selected_id} needs a discount/credit adjustment."
                            )
                            continue
                        discount_amount = round(abs(diff), 2)
                        paid_invoice_discount_total = round(paid_invoice_discount_total + discount_amount, 2)
                        discount_actions.append(
                            {
                                "invoice": {"InvoiceID": selected_id, "InvoiceNumber": selected.get("number")},
                                "amount": discount_amount,
                                "reason": "Card payment is lower than an already-paid invoice; a matching-pack adjustment will be created.",
                            }
                        )
                    elif diff > 0.01:
                        if not adjustment or adjustment.get("type") != "extra_invoice":
                            blocking_errors.append(
                                f"Batch {batch_id} sale {sale_ref or idx + 1} is £{diff:.2f} higher than already-paid invoice {selected.get('number') or selected_id} (£{payable_amount:.2f} paid vs £{sale_gross:.2f} card sale). If the invoice is missing a line, edit the invoice and refresh this batch. Only confirm a separate adjustment if the extra money is genuinely not part of that customer invoice."
                            )
                            continue
                        paid_invoice_extra_total = round(paid_invoice_extra_total + diff, 2)
                        paid_overpayment_adjustments.append(
                            {
                                "sale_ref": sale_ref,
                                "invoice_id": selected_id,
                                "invoice_number": selected.get("number"),
                                "contact_name": selected.get("contact_name"),
                                "amount": diff,
                                "invoice_paid_amount": payable_amount,
                                "card_sale_amount": sale_gross,
                                "reason": "Card sale is higher than an already-paid Xero invoice; the app will add a positive Cashflows adjustment line to the match pack.",
                            }
                        )
                    continue

                if diff < -0.01:
                    if not adjustment or adjustment.get("type") != "discount":
                        blocking_errors.append(
                            f"Batch {batch_id} invoice {selected.get('number') or selected_id} needs a discount/credit adjustment."
                        )
                        continue
                    discount_amount = round(abs(diff), 2)
                    discount_actions.append(
                        {
                            "invoice": {"InvoiceID": selected_id, "InvoiceNumber": selected.get("number")},
                            "amount": discount_amount,
                            "reason": "Card payment is lower than invoice total.",
                        }
                    )
                    contact_id = str(selected.get("contact_id") or "").strip()
                    contact_name = str(selected.get("contact_name") or "Customer").strip() or "Customer"
                    contact_ref = {"ContactID": contact_id} if contact_id else {"Name": contact_name}
                    credit_note_payloads.append(
                        {
                            "credit_note": {
                                "CreditNotes": [
                                    {
                                        "Type": "ACCRECCREDIT",
                                        "Contact": contact_ref,
                                        "Date": payout_date,
                                        "Reference": f"{reference} discount {selected.get('number') or sale_ref}"[:255],
                                        "Status": "AUTHORISED",
                                        "LineAmountTypes": "Inclusive",
                                        "LineItems": [
                                            {
                                                "Description": f"Cashflows card underpayment adjustment for {selected.get('number') or sale_ref}"[:4000],
                                                "Quantity": 1,
                                                "UnitAmount": discount_amount,
                                                "AccountCode": getattr(xero_client, "sales_account_code", "200") if xero_client else "200",
                                            }
                                        ],
                                    }
                                ]
                            },
                            "allocation": {
                                "Allocations": [
                                    {
                                        "Invoice": {"InvoiceID": selected_id},
                                        "Amount": discount_amount,
                                        "Date": payout_date,
                                    }
                                ]
                            },
                            "invoice": {"InvoiceID": selected_id, "InvoiceNumber": selected.get("number")},
                            "amount": discount_amount,
                        }
                    )
                    payments.append(
                        {
                            "Invoice": {"InvoiceID": selected_id},
                            "Amount": sale_gross,
                        }
                    )
                else:
                    payments.append(
                        {
                            "Invoice": {"InvoiceID": selected_id},
                            "Amount": payable_amount if payable_amount > 0 else sale_gross,
                        }
                    )
                    if diff > 0.01:
                        if not adjustment or adjustment.get("type") != "extra_invoice":
                            blocking_errors.append(
                                f"Batch {batch_id} sale {sale_ref or idx + 1} is higher than the selected invoice. If the invoice is missing a line, edit the invoice and refresh this batch. Only confirm a separate adjustment if the extra money is genuinely not part of that customer invoice."
                            )
                            continue
                        extra_amount = round(diff, 2)
                        extra_invoice_payloads.append(
                            {
                                "contact_name": "Materials",
                                "description": f"Cashflows materials balance {sale_ref or selected.get('number') or batch_id}",
                                "amount": extra_amount,
                                "reference": f"{reference} extra {sale_ref}".strip()[:255],
                                "invoice_date": str(sale.get("date") or payout_date),
                            }
                        )

            paid_matching_adjustment = False
            if already_paid:
                names = ", ".join(
                    str(inv.get("number") or inv.get("id") or "invoice")
                    for inv in already_paid[:3]
                )
                if already_paid and open_invoice_count:
                    blocking_errors.append(
                        f"Batch {batch_id} mixes already-paid and unpaid invoices ({names}). Submit unpaid invoices only; already-paid invoices must be handled in Xero by matching the existing payment, not by creating another payment."
                    )
                    continue
                if len(existing_account_payments) < len(already_paid):
                    blocking_errors.append(
                        f"Batch {batch_id} includes already-paid invoice(s): {names}, but Xero did not return enough existing bank-account payments to package a safe match."
                    )
                    continue
                payment_account_ref = _account_ref(payment_account)
                clearing_account_ref = _account_ref(clearing_account)

                def _matches_account_ref(payment: dict, account_ref: dict) -> bool:
                    if account_ref.get("AccountID"):
                        return payment.get("account_id") == account_ref.get("AccountID")
                    if account_ref.get("Code"):
                        return str(payment.get("account_code") or "") == account_ref.get("Code")
                    return False

                def _payment_problem(payment: dict) -> str:
                    invoice_label = payment.get("invoice_number") or payment.get("invoice_id") or "invoice"
                    account_label = payment.get("account_name") or payment.get("account_code") or "unknown account"
                    status = str(payment.get("status") or "").strip() or "unknown"
                    amount = _money_value(payment.get("amount"))
                    expected = _money_value(payment.get("expected_amount"))
                    if status != "AUTHORISED":
                        return f"{invoice_label}: payment is {status}, not AUTHORISED"
                    if payment.get("is_reconciled"):
                        return f"{invoice_label}: existing payment is already reconciled in Xero"
                    if abs(amount - expected) > 0.01:
                        return (
                            f"{invoice_label}: existing payment is £{amount:.2f}, "
                            f"expected £{expected:.2f}"
                        )
                    if not (
                        _matches_account_ref(payment, payment_account_ref)
                        or _matches_account_ref(payment, clearing_account_ref)
                    ):
                        return f"{invoice_label}: existing payment is in {account_label}, not the configured card/Cashflows account"
                    return f"{invoice_label}: existing payment cannot be safely moved"

                payment_moves_to_clearing: list[dict] = []
                payments_already_in_clearing: list[dict] = []
                bad_existing_payments: list[dict] = []
                for payment in existing_account_payments:
                    basic_safe = (
                        payment.get("status") == "AUTHORISED"
                        and not payment.get("is_reconciled")
                        and abs(_money_value(payment.get("amount")) - _money_value(payment.get("expected_amount"))) <= 0.01
                    )
                    if not basic_safe:
                        bad_existing_payments.append({**payment, "problem": _payment_problem(payment)})
                    elif _matches_account_ref(payment, payment_account_ref):
                        payment_moves_to_clearing.append(payment)
                    elif _matches_account_ref(payment, clearing_account_ref):
                        payments_already_in_clearing.append(payment)
                    else:
                        payment_moves_to_clearing.append(
                            {
                                **payment,
                                "source_account_warning": (
                                    f"Moved from {payment.get('account_name') or payment.get('account_code') or 'unknown account'}"
                                ),
                            }
                        )
                covered_invoice_ids = {
                    str(payment.get("invoice_id") or "").strip()
                    for payment in (payment_moves_to_clearing + payments_already_in_clearing)
                    if str(payment.get("invoice_id") or "").strip()
                }
                expected_invoice_ids = {
                    str(inv.get("id") or "").strip()
                    for inv in already_paid
                    if str(inv.get("id") or "").strip()
                }
                if bad_existing_payments:
                    details = "; ".join(
                        str(payment.get("problem") or _payment_problem(payment))
                        for payment in bad_existing_payments[:3]
                    )
                    blocking_errors.append(
                        f"Batch {batch_id} includes already-paid invoice(s): {names}, but Xero cannot package one existing payment safely: {details}."
                    )
                    continue
                if not expected_invoice_ids.issubset(covered_invoice_ids):
                    blocking_errors.append(
                        f"Batch {batch_id} includes already-paid invoice(s): {names}, but Xero did not return enough existing bank-account payments to package a safe match."
                    )
                    continue
                expected_net = round(paid_invoice_payment_total + paid_invoice_extra_total - fee_total - paid_invoice_discount_total, 2)
                if abs(expected_net - net_total) > 0.02:
                    blocking_errors.append(
                        f"Batch {batch_id} match pack would total £{expected_net:.2f}, not the bank amount £{net_total:.2f}."
                    )
                    continue
                paid_matching_adjustment = True
            if not payments and not paid_matching_adjustment:
                blocking_errors.append(f"Batch {batch_id} has no payment lines to submit.")
                continue

            batch_payment_payload = None
            if payments:
                batch_payment_payload = {
                    "BatchPayments": [
                        {
                            "Account": _account_ref(payment_account),
                            "Date": payout_date,
                            "Reference": reference[:255],
                            "Payments": payments,
                            "Details": f"Cashflows CSV settlement. Gross £{gross_total:.2f}; fees/adjustments £{fee_total:.2f}; net bank deposit £{net_total:.2f}.",
                        }
                    ]
                }
            bank_fee_payload = None
            clearing_receive_payload = None
            if paid_matching_adjustment:
                clearing_lines = [
                    {
                        "Description": f"Cashflows gross card takings for {reference}"[:4000],
                        "Quantity": 1,
                        "UnitAmount": round(paid_invoice_payment_total, 2),
                        "AccountCode": clearing_account,
                        "TaxType": "NONE",
                    }
                ]
                if fee_total > 0:
                    clearing_lines.append(
                        {
                            "Description": f"Cashflows merchant fees for {reference}"[:4000],
                            "Quantity": 1,
                            "UnitAmount": -round(fee_total, 2),
                            "AccountCode": bank_fees_account,
                            "TaxType": "NONE",
                        }
                    )
                if paid_invoice_discount_total > 0:
                    clearing_lines.append(
                        {
                            "Description": f"Cashflows underpayment adjustment for {reference}"[:4000],
                            "Quantity": 1,
                            "UnitAmount": -round(paid_invoice_discount_total, 2),
                            "AccountCode": getattr(xero_client, "sales_account_code", "200") if xero_client else "200",
                            "TaxType": "NONE",
                        }
                    )
                if paid_invoice_extra_total > 0:
                    detail = ", ".join(
                        f"{item.get('invoice_number') or item.get('sale_ref') or 'sale'} +£{_money_value(item.get('amount')):.2f}"
                        for item in paid_overpayment_adjustments[:4]
                    )
                    clearing_lines.append(
                        {
                            "Description": f"Cashflows overpayment adjustment for {reference}: {detail}"[:4000],
                            "Quantity": 1,
                            "UnitAmount": round(paid_invoice_extra_total, 2),
                            "AccountCode": getattr(xero_client, "sales_account_code", "200") if xero_client else "200",
                            "TaxType": "NONE",
                        }
                    )
                expected_receive_total = _money_value(sum(_money_value(line.get("UnitAmount")) for line in clearing_lines))
                if abs(expected_receive_total - net_total) > 0.02:
                    blocking_errors.append(
                        f"Batch {batch_id} Cashflows clearing item would total £{expected_receive_total:.2f}, not the bank amount £{net_total:.2f}."
                    )
                    continue
                clearing_receive_payload = {
                    "BankTransactions": [
                        {
                            "Type": "RECEIVE",
                            "Contact": {"Name": "Cashflows"},
                            "Date": payout_date,
                            "Reference": reference[:255],
                            "BankAccount": _account_ref(payment_account),
                            "LineAmountTypes": "NoTax",
                            "LineItems": clearing_lines,
                        }
                    ]
                }
            elif fee_total > 0:
                bank_fee_payload = {
                    "BankTransactions": [
                        {
                            "Type": "SPEND",
                            "Contact": {"Name": "Cashflows"},
                            "Date": payout_date,
                            "Reference": f"{reference} merchant fees"[:255],
                            "BankAccount": _account_ref(payment_account),
                            "LineItems": [
                                {
                                    "Description": f"Cashflows merchant fees for {reference}"[:4000],
                                    "Quantity": 1,
                                    "UnitAmount": fee_total,
                                    "AccountCode": bank_fees_account,
                                }
                            ],
                        }
                    ]
                }
            plans.append(
                {
                    "batch_id": batch_id,
                    "payout_ref": (batch.get("payout") or {}).get("csv_ref"),
                    "payout_date": payout_date,
                    "bank_line": batch.get("bank_line"),
                    "gross": gross_total,
                    "net": net_total,
                    "fee_or_charge_total": fee_total,
                    "chosen_invoices": chosen_invoices,
                    "already_paid_invoices": already_paid,
                    "discount_actions_required": discount_actions,
                    "paid_overpayment_adjustments": paid_overpayment_adjustments,
                    "credit_note_payloads": credit_note_payloads,
                    "extra_invoice_payloads": extra_invoice_payloads,
                    "paid_matching_adjustment": paid_matching_adjustment,
                    "payment_moves_to_clearing": payment_moves_to_clearing if paid_matching_adjustment else [],
                    "payments_already_in_clearing": payments_already_in_clearing if paid_matching_adjustment else [],
                    "clearing_receipt": bool(clearing_receive_payload),
                    "payloads": {
                        "batch_payment": batch_payment_payload,
                        "bank_fee": bank_fee_payload,
                        "clearing_receive": clearing_receive_payload,
                    },
                }
            )

        if blocking_errors:
            return _flask.jsonify({"error": " ".join(blocking_errors[:4])}), 400

        if not production_enabled:
            message = (
                f"Test mode: prepared {len(plans)} selected batch(es). No Xero writes were sent."
            )
            if production_blockers:
                message += " Some rows are blocked from production until reviewed."
            print(
                "[cashflows-csv-submit] TEST MODE payloads:\n"
                + json.dumps({"plans": plans, "production_blockers": production_blockers}, indent=2, sort_keys=True),
                flush=True,
            )
            return _flask.jsonify(
                {
                    "ok": True,
                    "mode": "testing",
                    "message": message,
                    "production_enabled": False,
                    "production_blockers": production_blockers,
                    "plans": plans,
                }
            )

        # ── Production writes run in a background daemon thread ───────────────
        # This survives the browser closing and dodges the gunicorn worker
        # timeout.  Calls are paced under Xero's rate limit; progress is
        # persisted after each batch so the page can reattach a live progress
        # screen and a resume never re-writes a completed batch.
        def _lockout_until_ts() -> float:
            # Combine BOTH lockout signals: the persisted state-file timestamp
            # (set by the webhook/poll paths) AND the in-memory 429 cooldown set
            # by xero_client._request when it actually sees a 429. Our own bulk
            # posts trip the in-memory one, so relying on the state file alone
            # would miss them and the pause/resume loop would never fire.
            persisted = 0.0
            try:
                persisted = float(load_state(config.state_file).get("xero_lockout_until_ts") or 0.0)
            except Exception:
                persisted = 0.0
            try:
                in_memory = float(get_xero_rate_limit_until_ts() or 0.0)
            except Exception:
                in_memory = 0.0
            return max(persisted, in_memory)

        def _lockout_active() -> bool:
            return _lockout_until_ts() > time.time()

        _last_call_ts = {"t": 0.0}

        def _pace() -> None:
            while _lockout_active():
                wait = max(1, int(_lockout_until_ts() - time.time()))
                time.sleep(min(10, wait))
            wait = _CF_SUBMIT_PACE_SECONDS - (time.time() - _last_call_ts["t"])
            if wait > 0:
                time.sleep(wait)
            _last_call_ts["t"] = time.time()

        def _xero_write(callable_obj):
            last_exc = None
            for _attempt in range(4):
                try:
                    _pace()
                    return callable_obj()
                except RuntimeError as exc:
                    msg = str(exc)
                    if "429" not in msg and "rate-limit" not in msg and "rate-limited" not in msg:
                        raise
                    last_exc = exc
                    while _lockout_active():
                        wait = max(1, int(_lockout_until_ts() - time.time()))
                        time.sleep(min(10, wait))
            if last_exc:
                raise last_exc
            raise RuntimeError("Xero write failed before it could be sent.")

        def _post_xero(path: str, payload: dict) -> dict:
            if not xero_client or not hasattr(xero_client, "_request"):
                raise RuntimeError("Xero client cannot post the required Cashflows adjustment.")
            def _send():
                response = xero_client._request("POST", f"{xero_client.base_url}{path}", json=payload)
                if response.status_code == 429:
                    raise RuntimeError(f"Xero post failed: 429 {response.text}")
                return response
            resp = _xero_write(_send)
            if not resp.ok:
                raise RuntimeError(f"Xero post failed: {resp.status_code} {resp.text}")
            return resp.json() if resp.text else {}

        def _execute_plan(plan: dict) -> tuple[dict, tuple | None]:
            created_extra: list[dict] = []
            created_credit_notes: list[dict] = []
            credit_note_allocations: list[dict] = []
            moved_payments: list[dict] = []
            for credit_plan in plan.get("credit_note_payloads") or []:
                credit_resp = _xero_write(lambda: xero_client.create_credit_note_payload(credit_plan["credit_note"]))
                created_credit = ((credit_resp.get("CreditNotes") or [{}])[0]) if isinstance(credit_resp, dict) else {}
                credit_note_id = str(created_credit.get("CreditNoteID") or "").strip()
                if not credit_note_id:
                    raise RuntimeError("Xero created a credit note but did not return a CreditNoteID.")
                allocation_resp = _xero_write(
                    lambda: xero_client.allocate_credit_note_payload(
                        credit_note_id,
                        credit_plan["allocation"],
                    )
                )
                created_credit_notes.append(created_credit)
                credit_note_allocations.append(allocation_resp)
            for inv_payload in plan.get("extra_invoice_payloads") or []:
                resp = _xero_write(lambda: xero_client.create_simple_invoice(**inv_payload))
                created = ((resp.get("Invoices") or [{}])[0]) if isinstance(resp, dict) else {}
                invoice_id = str(created.get("InvoiceID") or "").strip()
                if not invoice_id:
                    raise RuntimeError("Xero created an extra invoice but did not return an InvoiceID.")
                amount = _money_value(created.get("AmountDue") or created.get("Total") or inv_payload.get("amount"))
                batch = ((plan["payloads"]["batch_payment"].get("BatchPayments") or [{}])[0])
                batch.setdefault("Payments", []).append(
                    {"Invoice": {"InvoiceID": invoice_id}, "Amount": amount}
                )
                created_extra.append(created)
            batch_resp = None
            clearing_resp = None
            if plan.get("paid_matching_adjustment"):
                for payment_move in plan.get("payment_moves_to_clearing") or []:
                    payment_id = str(payment_move.get("payment_id") or "").strip()
                    invoice_id = str(payment_move.get("invoice_id") or "").strip()
                    amount = _money_value(payment_move.get("amount"))
                    if not payment_id or not invoice_id or amount <= 0:
                        raise RuntimeError("Cashflows clearing move is missing a payment or invoice ID.")
                    _post_xero(f"/Payments/{payment_id}", {"Status": "DELETED"})
                    created_payment = _post_xero(
                        "/Payments",
                        {
                            "Payments": [
                                {
                                    "Invoice": {"InvoiceID": invoice_id},
                                    "Account": _account_ref(clearing_account),
                                    "Date": str(payment_move.get("payment_date") or plan.get("payout_date") or dt.date.today().isoformat()),
                                    "Amount": amount,
                                }
                            ]
                        },
                    )
                    moved_payments.append(
                        {
                            "deleted_payment_id": payment_id,
                            "created_payment": ((created_payment.get("Payments") or [{}])[0]),
                            "invoice_number": payment_move.get("invoice_number"),
                        }
                    )
            if plan["payloads"].get("batch_payment"):
                batch_resp = _xero_write(lambda: xero_client.create_batch_payment_payload(plan["payloads"]["batch_payment"]))
            if plan["payloads"].get("clearing_receive"):
                clearing_resp = _xero_write(lambda: xero_client.create_bank_transaction_payload(plan["payloads"]["clearing_receive"]))
            fee_resp = None
            if plan["payloads"].get("bank_fee"):
                fee_resp = _xero_write(lambda: xero_client.create_bank_transaction_payload(plan["payloads"]["bank_fee"]))
            response = {
                "batch_id": plan["batch_id"],
                "created_credit_notes": created_credit_notes,
                "credit_note_allocations": credit_note_allocations,
                "created_extra_invoices": created_extra,
                "moved_payments_to_clearing": moved_payments,
                "batch_payment": batch_resp,
                "clearing_receive": clearing_resp,
                "bank_fee": fee_resp,
            }
            payout_ref = str(plan.get("payout_ref") or "").strip()
            ref = None
            if payout_ref and not plan.get("paid_matching_adjustment"):
                ref = (
                    payout_ref,
                    {
                        "date": dt.datetime.now(dt.timezone.utc).isoformat(),
                        "amount": plan.get("net"),
                        "batch_id": plan["batch_id"],
                    },
                )
            return response, ref

        job_id = uuid.uuid4().hex
        total = len(plans)
        now_iso = dt.datetime.now(dt.timezone.utc).isoformat()
        job_state = {
            "job_id": job_id,
            "status": "running",  # running | paused | done | error
            "message": f"Starting reconciliation of {total} batch(es)…",
            "total": total,
            "completed": 0,
            "completed_batch_ids": [],
            "current_batch_ref": "",
            "plans": plans,
            "responses": [],
            "error": "",
            "resume_after_ts": 0.0,
            "started_at": now_iso,
            "updated_at": now_iso,
            "preview_id": str(preview.get("preview_id") or ""),
        }
        busy_owner = f"cashflows_csv:{job_id}"

        def _persist_job(state: dict) -> None:
            state["updated_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
            try:
                slim = {k: v for k, v in state.items() if k not in ("plans", "responses")}
                slim["response_count"] = len(state.get("responses") or [])
                set_json_setting(config.admin_db_file, _CF_SUBMIT_JOB_KEY, slim)
            except Exception:
                pass

        def _run_submit_job() -> None:
            state = _cf_submit_job.get(job_id) or job_state
            try:
                mark_xero_busy(
                    config.admin_db_file,
                    owner=busy_owner,
                    reason="Cashflows CSV submission is preparing Xero reconciliation",
                )
                for plan in state["plans"]:
                    bid = plan.get("batch_id")
                    if bid in state["completed_batch_ids"]:
                        continue
                    # Pre-flight: wait out any active Xero cooldown BEFORE we make
                    # a single write for this batch. A batch's plan performs several
                    # dependent writes (credit note, payment move, batch payment,
                    # bank txns); if we hit a 429 halfway through and blindly retried
                    # the whole batch we would DUPLICATE the writes already committed.
                    # So we only ever pause between batches, never mid-batch.
                    while _lockout_active():
                        until = _lockout_until_ts()
                        secs = max(1, int(until - time.time()))
                        state["status"] = "paused"
                        state["resume_after_ts"] = until
                        state["message"] = (
                            f"Xero is taking a short breather — resuming "
                            f"automatically in about {secs}s…"
                        )
                        _persist_job(state)
                        time.sleep(min(10, max(1, secs)))
                    state["status"] = "running"
                    state["resume_after_ts"] = 0.0
                    state["current_batch_ref"] = str(plan.get("payout_ref") or bid or "")
                    state["message"] = (
                        f"Reconciling batch {state['completed'] + 1} of "
                        f"{state['total']} ({state['current_batch_ref']})…"
                    )
                    mark_xero_busy(
                        config.admin_db_file,
                        owner=busy_owner,
                        reason=(
                            f"Cashflows CSV submission batch {state['completed'] + 1} "
                            f"of {state['total']} ({state['current_batch_ref']})"
                        ),
                    )
                    _persist_job(state)
                    # Execute exactly once. On failure we do NOT replay this batch,
                    # because partial Xero writes cannot be safely re-sent. The
                    # already-completed batches stay saved and are skipped on resume.
                    resp, ref = _execute_plan(plan)
                    state["responses"].append(resp)
                    if ref:
                        try:
                            add_cashflows_reconciled_refs(config.admin_db_file, {ref[0]: ref[1]})
                        except Exception:
                            pass
                    state["completed_batch_ids"].append(bid)
                    state["completed"] += 1
                    _persist_job(state)
                state["status"] = "done"
                state["current_batch_ref"] = ""
                state["message"] = (
                    f"Submitted {state['completed']} batch(es) to Xero. Open Xero "
                    "bank reconciliation and press OK on the matching Cashflows bank line."
                )
                _persist_job(state)
                _feed.push(
                    f"Cashflows CSV submitted {state['completed']} batch(es) to Xero reconciliation preparation.",
                    "success",
                )
            except Exception as exc:
                state["status"] = "error"
                state["error"] = str(exc).splitlines()[0][:300]
                _ref = state.get("current_batch_ref") or ""
                is_xero_rate_limit = (
                    "429" in state["error"]
                    or "rate-limit" in state["error"]
                    or "rate-limited" in state["error"]
                )
                if is_xero_rate_limit:
                    state["message"] = (
                        f"Xero asked the app to wait while processing batch "
                        f"{state['completed'] + 1} of {state['total']}"
                        + (f" ({_ref})" if _ref else "")
                        + ". The "
                        f"{state['completed']} batch(es) already finished are saved and "
                        "will NOT be resent. This batch may have been partly prepared; "
                        "refresh the Cashflows preview before retrying so the app reads "
                        "the current Xero state. "
                        f"Problem: {state['error']}"
                    )
                else:
                    state["message"] = (
                        f"Stopped on batch {state['completed'] + 1} of {state['total']}"
                        + (f" ({_ref})" if _ref else "")
                        + ". The "
                        f"{state['completed']} batch(es) already finished are saved and "
                        "will NOT be resent. This batch may be part-done in Xero — please "
                        "check it in Xero before re-running. "
                        f"Problem: {state['error']}"
                    )
                _persist_job(state)
            finally:
                try:
                    clear_xero_busy(config.admin_db_file, owner=busy_owner)
                except Exception:
                    pass

        with _cf_submit_lock:
            existing = next(
                (j for j in _cf_submit_job.values() if j.get("status") in ("running", "paused")),
                None,
            )
            if existing:
                return _flask.jsonify(
                    {
                        "ok": True,
                        "mode": "production",
                        "async": True,
                        "already_running": True,
                        "job_id": existing["job_id"],
                        "total": existing.get("total"),
                        "message": "A Cashflows submission is already running — showing its progress.",
                        "production_enabled": True,
                    }
                )
            _cf_submit_job.clear()
            _cf_submit_job[job_id] = job_state
            mark_xero_busy(
                config.admin_db_file,
                owner=busy_owner,
                reason="Cashflows CSV submission is starting",
            )
            _persist_job(job_state)
            threading.Thread(
                target=_run_submit_job, name="cashflows-submit", daemon=True
            ).start()

        return _flask.jsonify(
            {
                "ok": True,
                "mode": "production",
                "async": True,
                "job_id": job_id,
                "total": total,
                "message": (
                    f"Reconciling {total} batch(es) in the background. You can safely "
                    "leave this page — progress is saved and it keeps running."
                ),
                "production_enabled": True,
            }
        )

    @app.get("/cashflows-sync/submit-progress")
    @require_login
    def cashflows_sync_submit_progress():
        import flask as _flask

        job = None
        stale = False
        with _cf_submit_lock:
            live = [j for j in _cf_submit_job.values()]
        if live:
            job = live[-1]
        else:
            # Fall back to the persisted copy (e.g. after a worker restart).
            job = get_json_setting(config.admin_db_file, _CF_SUBMIT_JOB_KEY, None)
            # Single-worker app: a live job is always in memory. If the DB says a
            # job is still running/paused but it is NOT in memory, its worker
            # thread died (process restarted). Plans are not persisted so it
            # cannot resume — report it as stopped so the UI re-enables submit.
            if isinstance(job, dict) and str(job.get("status") or "") in ("running", "paused"):
                stale = True
        if not job or not isinstance(job, dict):
            return _flask.jsonify({"active": False})
        total = int(job.get("total") or 0)
        completed = int(job.get("completed") or 0)
        status = str(job.get("status") or "")
        resume_after = float(job.get("resume_after_ts") or 0.0)
        message = job.get("message") or ""
        if stale:
            status = "error"
            message = (
                f"The submission was interrupted by a server restart after "
                f"{completed} of {total} batch(es). Completed batches are saved and "
                "will not be resent — re-run to finish the rest."
            )
            resume_after = 0.0
        return _flask.jsonify(
            {
                "active": status in ("running", "paused"),
                "job_id": job.get("job_id"),
                "status": status,
                "message": message,
                "total": total,
                "completed": completed,
                "percent": int(round((completed / total) * 100)) if total else 0,
                "current_batch_ref": job.get("current_batch_ref") or "",
                "completed_batch_ids": job.get("completed_batch_ids") or [],
                "error": job.get("error") or "",
                "resume_in": max(0, int(resume_after - time.time())) if resume_after else 0,
                "preview_id": job.get("preview_id") or "",
                "started_at": job.get("started_at") or "",
                "updated_at": job.get("updated_at") or "",
            }
        )

    @app.get("/cashflows-sync/recommended-range")
    @require_login
    def cashflows_sync_recommended_range():
        import flask as _flask

        return _flask.jsonify(recommend_export_range(config))

    @app.get("/receipts")
    @require_login
    def receipts_scaffold():
        svc = ReceiptService(config)
        settings = get_receipts_settings(config.admin_db_file)
        flow_enabled = bool(config.receipts_enabled and settings.get("enabled"))
        records = svc.list_recent(limit=50) if svc.is_enabled else []
        rows = []
        for rec in records:
            file_name = escape(str((rec.metadata or {}).get("filename") or ""))
            rows.append(
                "<tr class='border-b border-gray-100'>"
                f"<td class='px-3 py-2 text-xs text-gray-700'>{escape(rec.created_at)}</td>"
                f"<td class='px-3 py-2 text-xs text-gray-700'>{escape(rec.status)}</td>"
                f"<td class='px-3 py-2 text-xs text-gray-700'>{escape(rec.merchant)}</td>"
                f"<td class='px-3 py-2 text-xs text-gray-700'>{escape(rec.transaction_ref)}</td>"
                f"<td class='px-3 py-2 text-xs text-gray-700'>{escape(rec.event_key)}</td>"
                f"<td class='px-3 py-2 text-xs text-gray-700'>{'' if rec.amount is None else f'£{rec.amount:.2f}'}</td>"
                f"<td class='px-3 py-2 text-xs text-gray-500'>{file_name}</td>"
                "</tr>"
            )
        table_html = (
            "<div class='overflow-x-auto border border-gray-200 rounded-xl bg-white'>"
            "<table class='min-w-full text-left'>"
            "<thead class='bg-gray-50 text-[11px] uppercase tracking-wide text-gray-500'>"
            "<tr>"
            "<th class='px-3 py-2'>Created</th>"
            "<th class='px-3 py-2'>Status</th>"
            "<th class='px-3 py-2'>Merchant</th>"
            "<th class='px-3 py-2'>Reference</th>"
            "<th class='px-3 py-2'>Event Key</th>"
            "<th class='px-3 py-2'>Amount</th>"
            "<th class='px-3 py-2'>File</th>"
            "</tr></thead><tbody>"
            + ("".join(rows) if rows else "<tr><td colspan='7' class='px-3 py-6 text-sm text-gray-500'>No receipts yet.</td></tr>")
            + "</tbody></table></div>"
        )
        enabled_checked = "checked" if settings.get("enabled") else ""
        sample_event_key = "primary:sample-event-id"
        sample_link = svc.create_upload_url(sample_event_key, base_url=_current_base_url())
        body = f"""
        <main class="max-w-5xl mx-auto p-6 space-y-6">
          <div class="flex items-center justify-between gap-4">
            <div>
              <h1 class="text-2xl font-semibold text-gray-900">Receipt Processing</h1>
              <p class="text-sm text-gray-600 mt-1">Signed upload links + Google Document AI parser (feature-flagged).</p>
            </div>
            <a href="/" class="px-3 py-2 text-sm rounded-lg bg-gray-100 hover:bg-gray-200 text-gray-800">Back</a>
          </div>
          <div class="rounded-xl border border-blue-200 bg-blue-50 px-4 py-3 text-sm text-blue-900">
            <strong>Code feature flag:</strong> RECEIPTS_ENABLED={'true' if config.receipts_enabled else 'false'} ·
            <strong>Runtime toggle:</strong> {'on' if settings.get("enabled") else 'off'} ·
            <strong>Write confirm required:</strong> {'yes' if svc.write_confirmation_required else 'no'}
          </div>
          <form method="post" action="/receipts/save-settings" class="rounded-xl border border-gray-200 bg-white p-4 space-y-4">
            <h2 class="text-sm font-semibold text-gray-900">Receipt Settings</h2>
            <label class="flex items-center gap-2 text-sm text-gray-800">
              <input type="checkbox" name="enabled" value="1" class="w-4 h-4" {enabled_checked}>
              Enable receipts flow
            </label>
            <div class="grid grid-cols-1 md:grid-cols-2 gap-3">
              <div>
                <label class="block text-xs font-medium text-gray-600 mb-1">Document AI project ID</label>
                <input name="document_ai_project_id" value="{escape(str(settings.get('document_ai_project_id') or ''))}" class="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm" placeholder="65322709611">
              </div>
              <div>
                <label class="block text-xs font-medium text-gray-600 mb-1">Document AI location</label>
                <input name="document_ai_location" value="{escape(str(settings.get('document_ai_location') or 'us'))}" class="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm" placeholder="us">
              </div>
              <div>
                <label class="block text-xs font-medium text-gray-600 mb-1">Document AI processor ID</label>
                <input name="document_ai_processor_id" value="{escape(str(settings.get('document_ai_processor_id') or ''))}" class="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm" placeholder="82c1804dc0bdc8b9">
              </div>
              <div>
                <label class="block text-xs font-medium text-gray-600 mb-1">Google project name (optional)</label>
                <input name="document_ai_project_name" value="{escape(str(settings.get('document_ai_project_name') or ''))}" class="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm" placeholder="rich-involution-492420-n8">
              </div>
              <div>
                <label class="block text-xs font-medium text-gray-600 mb-1">Retention days</label>
                <input type="number" min="1" max="30" name="retention_days" value="{int(settings.get('retention_days') or 2)}" class="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm">
              </div>
              <div>
                <label class="block text-xs font-medium text-gray-600 mb-1">Receipt sheet tab name</label>
                <input name="sheet_name" value="{escape(str(settings.get('sheet_name') or 'Receipt_Reconciliation'))}" class="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm">
              </div>
            </div>
            <div>
              <label class="block text-xs font-medium text-gray-600 mb-1">Receipt sheet URL or ID</label>
              <input name="sheet_spreadsheet_id" value="{escape(str(settings.get('sheet_spreadsheet_id') or ''))}" class="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm" placeholder="Paste Google Sheets URL or ID">
            </div>
            <button type="submit" class="px-3 py-2 text-sm rounded-lg bg-indigo-600 hover:bg-indigo-700 text-white">Save Receipt Settings</button>
          </form>
          <div class="rounded-xl border border-gray-200 bg-white p-4 space-y-2">
            <h2 class="text-sm font-semibold text-gray-900">Signed Link Preview</h2>
            <p class="text-xs text-gray-600">This is the secure signed upload URL format used in calendar entries.</p>
            <code class="block text-xs bg-gray-50 border border-gray-200 rounded p-2 break-all">{escape(sample_link)}</code>
          </div>
          <form method="post" action="/receipts/create" class="rounded-xl border border-gray-200 bg-white p-4 space-y-3">
            <label class="block text-sm font-medium text-gray-800">Paste receipt text</label>
            <textarea name="raw_text" rows="6" class="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm" placeholder="Merchant\\nTotal £12.34\\nRef ABC123"></textarea>
            <button type="submit" class="px-3 py-2 text-sm rounded-lg bg-indigo-600 hover:bg-indigo-700 text-white" {'disabled' if not flow_enabled else ''}>Create Draft Receipt</button>
          </form>
          {table_html}
        </main>
        """
        return _page(body)

    @app.post("/receipts/save-settings")
    @require_login
    def receipts_save_settings():
        enabled = bool(request.form.get("enabled"))
        current = get_receipts_settings(config.admin_db_file)
        project_id = (request.form.get("document_ai_project_id") or "").strip()
        location = (request.form.get("document_ai_location") or "us").strip() or "us"
        processor_id = (request.form.get("document_ai_processor_id") or "").strip()
        project_name = (request.form.get("document_ai_project_name") or "").strip()
        retention_days_raw = (request.form.get("retention_days") or "2").strip()
        sheet_spreadsheet_id = _extract_spreadsheet_id(
            (request.form.get("sheet_spreadsheet_id") or "").strip()
        )
        sheet_name = (request.form.get("sheet_name") or "Receipt_Reconciliation").strip() or "Receipt_Reconciliation"
        try:
            retention_days = max(int(retention_days_raw or "2"), 1)
        except Exception:
            retention_days = 2
        set_receipts_settings(
            config.admin_db_file,
            {
                "enabled": enabled,
                "document_ai_project_id": project_id,
                "document_ai_location": location,
                "document_ai_processor_id": processor_id,
                "document_ai_project_name": project_name,
                "google_service_account_file": str(
                    current.get("google_service_account_file")
                    or _default_sa_path(config.admin_db_file)
                ).strip()
                or _default_sa_path(config.admin_db_file),
                "retention_days": retention_days,
                "sheet_spreadsheet_id": sheet_spreadsheet_id,
                "sheet_name": sheet_name,
            },
        )
        session["save_notice"] = "success:Receipt settings saved."
        return redirect(url_for("receipts_scaffold"))

    @app.post("/receipts/create")
    @require_login
    def receipts_create_scaffold():
        svc = ReceiptService(config)
        if not svc.is_enabled:
            session["save_notice"] = "error:Receipts flow is currently paused."
            return redirect(url_for("receipts_scaffold"))
        raw_text = (request.form.get("raw_text") or "").strip()
        if not raw_text:
            session["save_notice"] = "error:Paste receipt text before creating a draft."
            return redirect(url_for("receipts_scaffold"))
        created = svc.create_draft(raw_text, source="admin_scaffold")
        session["save_notice"] = f"success:Receipt draft created ({created.id})."
        return redirect(url_for("receipts_scaffold"))

    @app.get("/r/<code>")
    def receipt_short_link(code: str):
        """Expand a short receipt-upload code into the full /receipts/submit?token= URL."""
        import time as _time
        links = get_json_setting(config.admin_db_file, "receipt_short_links", {})
        entry = links.get((code or "").strip())
        if not entry or not isinstance(entry, dict):
            return _page(
                "<main class='max-w-xl mx-auto p-6'><div class='rounded-xl border border-red-200 bg-red-50 p-4 text-sm text-red-800'>Upload link not found. It may have already been used or never existed.</div></main>"
            ), 404
        if entry.get("exp", 0) < int(_time.time()):
            return _page(
                "<main class='max-w-xl mx-auto p-6'><div class='rounded-xl border border-amber-200 bg-amber-50 p-4 text-sm text-amber-800'>This upload link has expired. Ask your admin to resend it.</div></main>"
            ), 410
        return redirect(f"/receipts/submit?token={entry['token']}")

    @app.get("/receipts/submit")
    def receipts_submit_page():
        svc = ReceiptService(config)
        token = (request.args.get("token") or "").strip()
        if not svc.is_enabled:
            return _page(
                "<main class='max-w-xl mx-auto p-6'><div class='rounded-xl border border-red-200 bg-red-50 p-4 text-sm text-red-800'>Receipts flow is currently paused.</div></main>"
            )
        try:
            event_key = svc.verify_upload_token(token)
        except Exception as exc:
            return _page(
                f"<main class='max-w-xl mx-auto p-6'><div class='rounded-xl border border-red-200 bg-red-50 p-4 text-sm text-red-800'>Invalid upload link: {escape(str(exc))}</div></main>"
            )
        return _page(
            f"""
<style>
body {{ background:#f7f6f3 !important; }}
.rcpt-corner {{ position:absolute; width:20px; height:20px; border-color:#4f46e5; border-style:solid; }}
</style>
<div style="max-width:430px;margin:0 auto;min-height:100vh;display:flex;flex-direction:column;">
  <div style="background:white;border-bottom:1px solid #e5e7eb;padding:12px 18px;display:flex;
              align-items:center;justify-content:space-between;position:sticky;top:0;z-index:10;">
    <span style="font-size:16px;font-weight:800;color:#1e1b4b;letter-spacing:-.03em;">Powwash</span>
    <span style="font-size:11px;font-weight:600;color:#4f46e5;background:#eef2ff;
                 padding:3px 10px;border-radius:99px;">Receipt upload</span>
  </div>
  <div style="flex:1;padding:20px 16px 32px;display:flex;flex-direction:column;gap:14px;">
    <form id="rcpt-form" method="post" action="/receipts/submit?token={escape(token)}"
          enctype="multipart/form-data">
      <div id="cam-zone" onclick="document.getElementById('rcpt-file').click()"
           style="position:relative;background:white;border:1.5px solid #e5e7eb;border-radius:18px;
                  min-height:300px;display:flex;flex-direction:column;align-items:center;
                  justify-content:center;cursor:pointer;overflow:hidden;">
        <span class="rcpt-corner" style="top:12px;left:12px;border-width:2.5px 0 0 2.5px;border-radius:3px 0 0 0;"></span>
        <span class="rcpt-corner" style="top:12px;right:12px;border-width:2.5px 2.5px 0 0;border-radius:0 3px 0 0;"></span>
        <span class="rcpt-corner" style="bottom:12px;left:12px;border-width:0 0 2.5px 2.5px;border-radius:0 0 0 3px;"></span>
        <span class="rcpt-corner" style="bottom:12px;right:12px;border-width:0 2.5px 2.5px 0;border-radius:0 0 3px 0;"></span>
        <div id="cam-idle" style="display:flex;flex-direction:column;align-items:center;gap:12px;padding:32px;text-align:center;">
          <svg width="56" height="56" viewBox="0 0 24 24" fill="none" stroke="#4f46e5" stroke-width="1.5"
               stroke-linecap="round" stroke-linejoin="round">
            <path d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z"/>
            <circle cx="12" cy="13" r="4"/>
          </svg>
          <div>
            <div style="font-size:18px;font-weight:700;color:#111827;margin-bottom:4px;">Tap to photograph</div>
            <div style="font-size:13px;color:#9ca3af;line-height:1.5;">Lay the receipt flat in good light<br>and fill the frame</div>
          </div>
        </div>
        <img id="rcpt-img" src="" alt="Receipt preview"
             style="display:none;width:100%;height:100%;object-fit:contain;position:absolute;inset:0;padding:12px;">
      </div>
      <input id="rcpt-file" type="file" name="receipt_file" accept="image/*" capture="environment" required
             style="position:absolute;width:1px;height:1px;opacity:0;pointer-events:none;">
      <div id="rcpt-actions" style="display:none;flex-direction:column;gap:10px;margin-top:12px;">
        <button id="rcpt-submit" type="submit"
                style="width:100%;padding:15px;background:#4f46e5;color:white;font-size:16px;
                       font-weight:700;border:none;border-radius:13px;cursor:pointer;">
          Send receipt &nbsp;&#10003;
        </button>
        <button type="button" id="rcpt-retake"
                style="width:100%;padding:13px;background:white;color:#4b5563;font-size:14px;
                       font-weight:600;border:1.5px solid #e5e7eb;border-radius:13px;cursor:pointer;">
          Retake photo
        </button>
      </div>
    </form>
    <div style="background:white;border:1px solid #e5e7eb;border-radius:14px;padding:14px 16px;">
      <div style="font-size:10.5px;font-weight:700;text-transform:uppercase;letter-spacing:.07em;
                  color:#9ca3af;margin-bottom:10px;">Make sure we can see</div>
      <div style="display:flex;flex-wrap:wrap;gap:6px;">
        <span style="font-size:12.5px;color:#374151;background:#f9fafb;border:1px solid #e5e7eb;border-radius:7px;padding:4px 10px;">&#10003;&nbsp;Merchant name</span>
        <span style="font-size:12.5px;color:#374151;background:#f9fafb;border:1px solid #e5e7eb;border-radius:7px;padding:4px 10px;">&#10003;&nbsp;Total amount</span>
        <span style="font-size:12.5px;color:#374151;background:#f9fafb;border:1px solid #e5e7eb;border-radius:7px;padding:4px 10px;">&#10003;&nbsp;Date</span>
        <span style="font-size:12.5px;color:#374151;background:#f9fafb;border:1px solid #e5e7eb;border-radius:7px;padding:4px 10px;">&#10003;&nbsp;VAT amount</span>
      </div>
    </div>
  </div>
</div>
<script>
(function() {{
  var inp  = document.getElementById('rcpt-file');
  var zone = document.getElementById('cam-zone');
  var idle = document.getElementById('cam-idle');
  var img  = document.getElementById('rcpt-img');
  var acts = document.getElementById('rcpt-actions');
  var sub  = document.getElementById('rcpt-submit');
  var retk = document.getElementById('rcpt-retake');
  var form = document.getElementById('rcpt-form');
  function showPreview(file) {{
    img.src = URL.createObjectURL(file);
    img.style.display = 'block';
    idle.style.display = 'none';
    acts.style.display = 'flex';
    zone.style.cursor  = 'default';
    zone.onclick = null;
  }}
  retk.addEventListener('click', function() {{
    img.style.display  = 'none'; img.src = '';
    idle.style.display = 'flex';
    acts.style.display = 'none';
    zone.style.cursor  = 'pointer';
    zone.onclick = function() {{ inp.click(); }};
    inp.value = '';
    setTimeout(function() {{ try {{ inp.click(); }} catch(e) {{}} }}, 50);
  }});
  inp.addEventListener('change', function() {{
    if (!inp.files || !inp.files.length) return;
    showPreview(inp.files[0]);
  }});
  form.addEventListener('submit', function() {{
    sub.textContent = 'Sending\u2026'; sub.disabled = true;
    sub.style.background = '#6366f1'; retk.style.display = 'none';
  }});
  try {{ inp.click(); }} catch(e) {{}}
}})();
</script>
"""
        )

    @app.get("/receipts/job/<job_id>")
    def receipts_job_status(job_id: str):
        """Polling endpoint — returns JSON {status, message} for the background job."""
        with _receipt_jobs_lock:
            job = _receipt_jobs.get((job_id or "").strip())
        if not job:
            return jsonify({"status": "processing", "message": "Queued…"})
        return jsonify(job)

    @app.post("/receipts/submit")
    def receipts_submit_upload():
        svc = ReceiptService(config)
        token = (request.args.get("token") or "").strip()
        if not svc.is_enabled:
            return _page(
                "<main class='max-w-xl mx-auto p-6'><div class='rounded-xl border border-red-200 bg-red-50 p-4 text-sm text-red-800'>Receipts flow is currently paused.</div></main>"
            )
        try:
            event_key = svc.verify_upload_token(token)
        except Exception as exc:
            return _page(
                f"<main class='max-w-xl mx-auto p-6'><div class='rounded-xl border border-red-200 bg-red-50 p-4 text-sm text-red-800'>Invalid upload link: {escape(str(exc))}</div></main>"
            )

        file = request.files.get("receipt_file")
        if not file or not file.filename:
            return _page(
                "<main class='max-w-xl mx-auto p-6'><div class='rounded-xl border border-red-200 bg-red-50 p-4 text-sm text-red-800'>Please select a file.</div></main>"
            )

        # Read everything from the request NOW, before we hand off to the thread.
        file_bytes = file.read() or b""
        filename = file.filename or "receipt.jpg"
        content_type = (file.content_type or "").strip() or "image/jpeg"
        if filename.lower().endswith(".pdf"):
            content_type = content_type or "application/pdf"
        elif filename.lower().endswith(".png"):
            content_type = content_type or "image/png"

        # Register the job and start the background thread immediately.
        job_id = uuid.uuid4().hex
        now = time.time()
        with _receipt_jobs_lock:
            # Evict finished jobs older than 10 minutes so the dict can't grow
            # unbounded (entries are only needed while the phone is polling).
            stale = [
                jid
                for jid, j in _receipt_jobs.items()
                if now - j.get("created_at", now) > 600
            ]
            for jid in stale:
                _receipt_jobs.pop(jid, None)
            _receipt_jobs[job_id] = {
                "status": "processing",
                "message": "Uploading…",
                "created_at": now,
            }

        t = threading.Thread(
            target=_receipt_bg_worker,
            args=(job_id, config, _feed.push, file_bytes, filename, content_type, event_key),
            daemon=True,
        )
        t.start()

        # Return the "submitted" page straight away — the phone can be pocketed.
        return _page(
            f"""
<style>body {{ background:#f7f6f3 !important; }}</style>
<div style="max-width:430px;margin:0 auto;min-height:100vh;display:flex;flex-direction:column;">
  <div style="background:white;border-bottom:1px solid #e5e7eb;padding:12px 18px;display:flex;
              align-items:center;justify-content:space-between;position:sticky;top:0;">
    <span style="font-size:16px;font-weight:800;color:#1e1b4b;letter-spacing:-.03em;">Powwash</span>
    <span style="font-size:11px;font-weight:600;color:#059669;background:#ecfdf5;
                 padding:3px 10px;border-radius:99px;">&#10003;&nbsp;Received</span>
  </div>
  <div style="flex:1;padding:24px 16px;display:flex;flex-direction:column;gap:14px;">
    <div id="status-box" data-job="{job_id}"
         style="background:white;border:1.5px solid #e5e7eb;border-radius:18px;
                padding:36px 24px;text-align:center;">
      <div id="spinner-state">
        <svg class="animate-spin" style="display:inline-block;width:40px;height:40px;color:#6366f1;margin-bottom:16px;"
             xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24">
          <circle style="opacity:.2" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"/>
          <path style="opacity:.8" fill="currentColor" d="M4 12a8 8 0 018-8v8z"/>
        </svg>
        <div style="font-size:17px;font-weight:700;color:#111827;margin-bottom:6px;">Reading your receipt</div>
        <div id="status-msg" style="font-size:13px;color:#6b7280;">Processing&hellip;</div>
      </div>
    </div>
    <div style="background:white;border:1px solid #e5e7eb;border-radius:14px;padding:16px;text-align:center;">
      <div style="font-size:13px;color:#9ca3af;line-height:1.6;">
        &#128100;&nbsp;You can put your phone away.<br>We&rsquo;ll save this in the background.
      </div>
    </div>
  </div>
</div>
<script>
(function() {{
  var box = document.getElementById('status-box');
  var msg = document.getElementById('status-msg');
  var jobId = box.getAttribute('data-job');
  var iv = setInterval(function() {{
    fetch('/receipts/job/' + jobId)
      .then(function(r) {{ return r.json(); }})
      .then(function(d) {{
        if (d.status === 'success') {{
          clearInterval(iv);
          box.style.borderColor = '#6ee7b7'; box.style.background = '#ecfdf5';
          box.innerHTML = '<div style="font-size:52px;margin-bottom:12px;">&#10003;</div>'
            + '<div style="font-size:19px;font-weight:800;color:#065f46;margin-bottom:6px;">All done!</div>'
            + '<div style="font-size:13px;color:#047857;line-height:1.5;">' + (d.message||'Receipt saved.') + '</div>';
        }} else if (d.status === 'failed') {{
          clearInterval(iv);
          box.style.borderColor = '#fca5a5'; box.style.background = '#fef2f2';
          box.innerHTML = '<div style="font-size:52px;margin-bottom:12px;">&#9888;</div>'
            + '<div style="font-size:19px;font-weight:700;color:#7f1d1d;margin-bottom:6px;">Couldn\'t read it</div>'
            + '<div style="font-size:13px;color:#991b1b;">' + (d.message||'Please try again.') + '</div>';
        }} else {{
          msg.textContent = d.message || 'Processing\u2026';
        }}
      }})
      .catch(function() {{}});
  }}, 2000);
}})();
</script>
"""
        )

    # ── Field Expenses: engineer-facing pages (PUBLIC — token only) ───────────

    def _exp_error_page(message: str, code: int = 404):
        return (
            _page(
                "<main class='max-w-xl mx-auto p-6'>"
                "<div class='rounded-xl border border-red-200 bg-red-50 p-4 "
                f"text-sm text-red-800'>{escape(message)}</div></main>"
            ),
            code,
        )

    # ── Engineer / subcontractor portal login (admin-set credentials) ──────
    def _portal_login_page(error: str = "", username: str = ""):
        err_html = ""
        if error:
            err_html = (
                "<div class='rounded-xl border border-red-200 bg-red-50 p-3 "
                f"text-sm text-red-800 text-center'>{escape(error)}</div>"
            )
        return _page(f"""
        <div class="min-h-screen flex items-center justify-center bg-gray-50 px-4">
          <div class="w-full max-w-sm">
            <div class="bg-white rounded-2xl shadow-sm border border-gray-200 p-6">
              <div class="text-center mb-6">
                <div class="text-3xl mb-2">&#128247;</div>
                <h1 class="text-xl font-bold text-gray-900">Powwash Expenses</h1>
                <p class="text-gray-500 text-sm mt-1">Sign in to upload receipts</p>
              </div>
              {err_html}
              <form method="post" action="/portal" class="space-y-4 mt-4">
                <div>
                  <label class="block text-sm font-medium text-gray-700 mb-1">Username</label>
                  <input name="username" autocomplete="username" autocapitalize="none"
                    value="{escape(username)}"
                    class="w-full px-4 py-3 border border-gray-300 rounded-xl text-base focus:outline-none focus:ring-2 focus:ring-indigo-500"
                    placeholder="Your username">
                </div>
                <div>
                  <label class="block text-sm font-medium text-gray-700 mb-1">Password</label>
                  <input name="password" type="password" autocomplete="current-password"
                    class="w-full px-4 py-3 border border-gray-300 rounded-xl text-base focus:outline-none focus:ring-2 focus:ring-indigo-500"
                    placeholder="Your password">
                </div>
                <button type="submit"
                  class="w-full py-3 px-4 bg-indigo-600 hover:bg-indigo-700 text-white font-semibold rounded-xl text-base">
                  Sign in
                </button>
              </form>
              <p class="text-center mt-4 text-xs text-gray-400">
                Forgotten your details? Ask the office.
              </p>
            </div>
          </div>
        </div>
        """)

    @app.get("/portal")
    def portal_login():
        eid = session.get("engineer_id")
        if eid:
            eng = exp_store.get_engineer(config.admin_db_file, int(eid))
            if eng and eng.get("active"):
                return redirect(f"/expenses/{eng['token']}")
            session.pop("engineer_id", None)
        return _portal_login_page()

    @app.post("/portal")
    def portal_login_post():
        db = config.admin_db_file
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        eng = exp_store.get_engineer_by_username(db, username)
        if (
            eng
            and eng.get("active")
            and (eng.get("password_hash") or "")
            and check_password_hash(eng["password_hash"], password)
        ):
            session["engineer_id"] = eng["id"]
            return redirect(f"/expenses/{eng['token']}")
        return _portal_login_page(
            "Sorry, that username or password wasn't right.", username
        )

    @app.get("/portal/logout")
    def portal_logout():
        session.pop("engineer_id", None)
        return redirect("/portal")

    def _engineer_reconciled_lines(start, end):
        """Reconciled SPEND lines (amount, iso_date) from Xero in a date window.

        Returns ``None`` when Xero is paused/unavailable (status unknown), so
        the feed can keep lines visible rather than silently dropping them.
        Cached ~10 minutes in admin settings to avoid hammering the API on
        every phone page-load.
        """
        if xero_is_disabled():
            return None
        import time as _t
        now = _t.time()
        cache = get_json_setting(config.admin_db_file, "engineer_recon_cache", {}) or {}
        if cache.get("until", 0) > now and isinstance(cache.get("lines"), list):
            return cache["lines"]
        try:
            client = build_xero_client(config)
            payload = client.get_bank_transactions(start_date=start, end_date=end)
            raw = (payload or {}).get("BankTransactions") or []
        except Exception as e:
            print(f"[engineer-card-feed] Xero reconciliation fetch failed: {e}",
                  flush=True)
            return None
        lines = []
        for t in raw:
            if str(t.get("Type") or "").upper() != "SPEND":
                continue
            if not t.get("IsReconciled"):
                continue
            try:
                amt = float(t.get("Total") or 0)
            except (TypeError, ValueError):
                continue
            lines.append([amt, _xero_date_to_iso(t.get("Date"))])
        set_json_setting(
            config.admin_db_file, "engineer_recon_cache",
            {"until": now + 600, "lines": lines},
        )
        return lines

    def _engineer_card_feed_html(eng, receipts):
        """Card-holder feed for a company-card engineer.

        Shows the engineer's OWN card transactions (filtered by their linked
        Plaid account) for roughly the last month. A green tick appears once a
        matching receipt has been uploaded; a line moves to a muted
        "Reconciled" list (and out of the active feed) once it is reconciled in
        Xero. Read-only and degrades gracefully when Plaid/Xero are off.
        """
        if (eng.get("kind") or "") != "company_card":
            return ""
        acct = (eng.get("plaid_account_id") or "").strip()
        if not acct:
            return ""
        db = config.admin_db_file
        if not cardfeed.is_connected(db):
            return (
                "<div class='rounded-xl border border-dashed border-gray-300 bg-white "
                "p-4 text-center text-xs text-gray-500'>Card feed paused &mdash; the "
                "office hasn&rsquo;t linked the bank yet.</div>"
            )

        def _d(v):
            try:
                return dt.date.fromisoformat(str(v or "")[:10])
            except (ValueError, TypeError):
                return None

        try:
            all_tx = cardfeed.get_cached_transactions(db) or []
        except Exception:
            all_tx = []
        today = dt.datetime.now(dt.timezone.utc).date()
        cutoff = today - dt.timedelta(days=35)
        txs = []
        for t in all_tx:
            if (t.get("account_id") or "") != acct:
                continue
            try:
                amt = float(t.get("amount") or 0)
            except (TypeError, ValueError):
                continue
            if amt <= 0:  # ignore refunds / incoming credits
                continue
            d = _d(t.get("date"))
            if d is None or d < cutoff:
                continue
            txs.append((d, amt, str(t.get("name") or "Card payment")))
        txs.sort(key=lambda x: x[0], reverse=True)
        if not txs:
            return (
                "<div class='rounded-xl border border-dashed border-gray-300 bg-white "
                "p-4 text-center text-xs text-gray-500'>No card payments in the last "
                "month.</div>"
            )

        recon = _engineer_reconciled_lines(cutoff, today)

        def _reconciled(amt, d):
            if not recon:
                return False
            for ln in recon:
                try:
                    ra = float(ln[0])
                except (TypeError, ValueError, IndexError):
                    continue
                rd = _d(ln[1]) if len(ln) > 1 else None
                if abs(ra - amt) <= 1.0 and (rd is None or abs((rd - d).days) <= 10):
                    return True
            return False

        def _ticked(amt, d):
            for r in receipts:
                if (r.get("payment_source") or "company_card") != "company_card":
                    continue
                try:
                    ra = float(r.get("amount_inc") or 0)
                except (TypeError, ValueError):
                    continue
                if abs(ra - amt) > 1.0:
                    continue
                rd = _d(r.get("purchased_on") or (r.get("created_at") or "")[:10])
                if rd is None or abs((rd - d).days) <= 31:
                    return True
            return False

        def _row(d, amt, name, ticked, muted=False):
            tick = (
                "<span class='text-emerald-600 text-lg leading-none' "
                "title='Receipt uploaded'>&#10003;</span>"
                if ticked else
                "<span class='text-xs text-amber-600'>no receipt</span>"
            )
            opacity = " opacity-60" if muted else ""
            return (
                "<div class='px-4 py-3 border-b border-gray-100 last:border-0 flex "
                f"items-center justify-between gap-3{opacity}'>"
                "<div class='min-w-0'>"
                f"<div class='font-medium text-gray-900 truncate'>{escape(name)}</div>"
                f"<div class='text-xs text-gray-500'>"
                f"{escape(_exp_day_label(d.isoformat()))}</div>"
                "</div>"
                "<div class='text-right whitespace-nowrap'>"
                f"<div class='font-semibold text-gray-900'>{_exp_money(amt)}</div>"
                f"<div class='mt-0.5'>{tick}</div>"
                "</div></div>"
            )

        active_rows, done_rows = [], []
        for (d, amt, name) in txs:
            if _reconciled(amt, d):
                done_rows.append(_row(d, amt, name, _ticked(amt, d), muted=True))
            else:
                active_rows.append(_row(d, amt, name, _ticked(amt, d)))

        out = [
            "<div class='space-y-2'>"
            "<h2 class='text-sm font-semibold text-gray-500 uppercase tracking-wide "
            "px-1'>Your card &mdash; needs reconciling</h2>"
        ]
        if recon is None:
            out.append(
                "<p class='text-xs text-gray-400 px-1'>Reconciliation status paused "
                "&mdash; lines stay here until the office turns Xero back on.</p>"
            )
        if active_rows:
            out.append(
                "<div class='rounded-xl border border-gray-200 bg-white "
                "overflow-hidden'>" + "".join(active_rows) + "</div>"
            )
        else:
            out.append(
                "<div class='rounded-xl border border-dashed border-gray-300 bg-white "
                "p-4 text-center text-xs text-gray-500'>All caught up &mdash; nothing "
                "waiting.</div>"
            )
        if done_rows:
            out.append(
                "<h2 class='text-sm font-semibold text-gray-500 uppercase tracking-wide "
                "px-1 pt-2'>Reconciled (last month)</h2>"
                "<div class='rounded-xl border border-gray-200 bg-white "
                "overflow-hidden'>" + "".join(done_rows) + "</div>"
            )
        out.append("</div>")
        return "".join(out)

    def _subcontractor_reference(eng):
        """Stable payment reference the office quotes when paying a
        subcontractor. The Plaid feed is scanned for this string to recognise
        the payment automatically."""
        return f"PWSUB{eng.get('id')}"

    def _exp_purge_old_photos():
        """Best-effort disk cleanup: clear local photos of submitted/settled
        receipts older than ~30 days, but only when the image is recoverable
        from Xero (xero_id present). The figures are always kept. Throttled to
        run at most once a day via a stored timestamp."""
        db = config.admin_db_file
        import time as _t
        now = _t.time()
        try:
            if float(get_json_setting(db, "exp_photo_purge_next", 0) or 0) > now:
                return
        except (TypeError, ValueError):
            pass
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30)
        done = {"submitted", "settled"}
        try:
            recs = exp_store.list_receipts_with_images(db)
        except Exception:
            recs = []
        for rec in recs:
            if (rec.get("status") or "") not in done:
                continue
            if not (rec.get("xero_id") or "").strip():
                continue  # only purge when recoverable from Xero
            stamp = str(rec.get("updated_at") or rec.get("created_at") or "")
            try:
                d = dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                continue
            if d.tzinfo is None:
                d = d.replace(tzinfo=dt.timezone.utc)
            if d > cutoff:
                continue
            old = exp_store.clear_receipt_stored_file(db, rec["id"])
            if old:
                _exp_safe_remove_file(db, old)
        set_json_setting(db, "exp_photo_purge_next", now + 86400)

    def _allocate_receipts_to_payment(receipts, amount):
        """Decide which of a subcontractor's owed receipts a single bank payment
        covers. One payment is usually the COMBINATION of several receipts, so we
        match by amount (a 0/1 subset-sum in pennies, ±2p tolerance for rounding):

          • payment >= total owed  -> covers everything (possible overpayment)
          • payment == a subset    -> covers exactly those receipts
          • otherwise              -> covers the largest subset that fits

        Returns (chosen_receipts, settled_pennies, target_pennies, total_pennies).
        """
        items = []
        for r in receipts:
            try:
                p = int(round(float(r.get("amount_inc") or 0) * 100))
            except (TypeError, ValueError):
                p = 0
            if p > 0:
                items.append((p, r))
        total = sum(p for p, _ in items)
        try:
            target = int(round(float(amount) * 100))
        except (TypeError, ValueError):
            target = 0
        # Whole payment clears (or exceeds) everything owed.
        if target >= total - 2:
            return [r for _, r in items], total, target, total
        # 0/1 subset-sum up to target; remember a parent so we can reconstruct.
        reachable = {0: None}
        for idx, (p, _) in enumerate(items):
            for s in list(reachable.keys()):
                ns = s + p
                if ns <= target and ns not in reachable:
                    reachable[ns] = (s, idx)
        best = max(reachable.keys())
        chosen_idx, s = [], best
        while s and reachable.get(s) is not None:
            prev, idx = reachable[s]
            chosen_idx.append(idx)
            s = prev
        chosen = [items[i][1] for i in chosen_idx]
        return chosen, best, target, total

    def _create_xero_bill_for_settlement(eng, settlement, receipts):
        """Raise a Xero ACCPAY purchase bill for the receipts a subcontractor
        payment has just settled, then record the payment against it so the bill
        shows as paid. One line per receipt (its own account code + amount), so
        the P&L keeps the per-expense breakdown even though one lump sum was paid.

        Best-effort: the local settlement already exists, so any Xero failure is
        logged + stored on the settlement (xero_error) rather than raised. No-op
        when Xero is paused or there is nothing to bill. Returns the bill id or "".
        """
        if xero_is_disabled():
            exp_store.update_settlement(
                config.admin_db_file, settlement["id"],
                xero_error="Xero paused (XERO_DISABLED) — bill not raised.",
            )
            return ""
        billable = [r for r in receipts if r]
        if not billable:
            return ""

        # Contact: prefer an explicit Xero contact id, then a contact name,
        # falling back to the engineer's display name.
        if (eng.get("xero_contact_id") or "").strip():
            contact = {"ContactID": eng["xero_contact_id"].strip()}
        else:
            contact = {"Name": (eng.get("xero_contact_name") or eng.get("name") or "").strip()
                       or f"Subcontractor {eng.get('id')}"}

        default_acct = (eng.get("expense_account_code") or "").strip()
        line_items = []
        for r in billable:
            try:
                unit = round(float(r.get("amount_inc") or 0), 2)
            except (TypeError, ValueError):
                unit = 0.0
            if unit <= 0:
                continue
            merchant = (r.get("merchant") or r.get("ocr_merchant") or "Receipt").strip()
            day = (r.get("purchased_on") or "")[:10]
            desc = f"{merchant} {day}".strip() if day else merchant
            acct = (r.get("category_account_code") or "").strip() or default_acct
            line = {
                "Description": desc[:4000],
                "Quantity": 1,
                "UnitAmount": unit,
            }
            if acct:
                line["AccountCode"] = acct
            line_items.append(line)

        if not line_items:
            return ""

        ref = settlement.get("reference") or _subcontractor_reference(eng)
        paid_on = (settlement.get("paid_on") or "")[:10] or None
        # Pay the BILL's own total (sum of the receipts it covers), NOT the raw
        # bank payment — those differ on over/under payments, and Xero rejects a
        # payment larger than the amount due. The over/under is already flagged
        # on the settlement note; the remainder stays owed locally.
        amount_paid = round(sum(li["UnitAmount"] for li in line_items), 2)

        try:
            client = build_xero_client(config)
            if client is None:
                exp_store.update_settlement(
                    config.admin_db_file, settlement["id"],
                    xero_error="Xero not connected — bill not raised.",
                )
                return ""
            bill = client.create_bill(
                contact=contact,
                line_items=line_items,
                reference=ref,
                bill_date=paid_on,
                status="AUTHORISED",
            )
            bill_id = str((bill or {}).get("InvoiceID") or "").strip()
            if client.dry_run:
                # Dry-run: nothing was actually written to Xero.
                exp_store.update_settlement(
                    config.admin_db_file, settlement["id"],
                    xero_error="DRY_RUN — bill + payment simulated, not written.",
                )
                print(f"[subcontractor-bill] DRY_RUN engineer={eng.get('id')} "
                      f"ref={ref} lines={len(line_items)} amount={amount_paid}",
                      flush=True)
                return ""

            if not bill_id:
                # Xero replied without an InvoiceID — treat as a failure rather
                # than silently clearing linkage / marking the settlement synced.
                exp_store.update_settlement(
                    config.admin_db_file, settlement["id"],
                    xero_error="Bill create returned no InvoiceID — not recorded.",
                )
                print(f"[subcontractor-bill] no InvoiceID engineer={eng.get('id')} "
                      f"ref={ref}", flush=True)
                return ""

            # Record the payment against the new bill so it shows as paid.
            pay_acct = (eng.get("payment_account_code") or "").strip()
            if bill_id and amount_paid > 0:
                try:
                    client.record_invoice_payment(
                        bill_id, amount_paid,
                        account_code=pay_acct, when=paid_on,
                    )
                except Exception as pe:
                    exp_store.update_settlement(
                        config.admin_db_file, settlement["id"],
                        xero_bill_id=bill_id,
                        xero_error=f"Bill raised but payment failed: {pe}",
                    )
                    print(f"[subcontractor-bill] payment failed bill={bill_id}: {pe}",
                          flush=True)
                    return bill_id

            exp_store.update_settlement(
                config.admin_db_file, settlement["id"],
                xero_bill_id=bill_id, xero_error="",
            )
            # Link the bill onto each settled receipt for traceability/pull-back.
            for r in billable:
                try:
                    exp_store.update_receipt(
                        config.admin_db_file, r["id"],
                        xero_type="ACCPAY", xero_id=bill_id,
                    )
                except Exception:
                    pass
            print(f"[subcontractor-bill] engineer={eng.get('id')} ref={ref} "
                  f"bill={bill_id} lines={len(line_items)} paid={amount_paid}",
                  flush=True)
            return bill_id
        except Exception as e:
            exp_store.update_settlement(
                config.admin_db_file, settlement["id"],
                xero_error=f"Bill create failed: {e}",
            )
            print(f"[subcontractor-bill] failed engineer={eng.get('id')}: {e}",
                  flush=True)
            return ""

    def _maybe_settle_subcontractor(eng):
        """Recognise the company's payment to a subcontractor in the Plaid feed
        by the app's payment reference (PWSUB<id>), then reconcile it against the
        actual receipts it covers — one payment is usually a COMBINATION of a few
        receipts. Only the matched receipts are settled; the rest stay owed.

        Over/under payments are handled automatically and flagged on the
        settlement note so the admin is warned:
          • overpaid  -> settle all, note the surplus.
          • partial   -> settle the best-matching subset, note the shortfall.

        Reference-based only (never amount-only), idempotent per Plaid
        transaction. No-op when not applicable or Plaid is off.
        """
        if (eng.get("kind") or "") != "subcontractor":
            return
        db = config.admin_db_file
        if not cardfeed.is_connected(db):
            return
        if exp_store.amount_owed_to_engineer(db, eng["id"]) <= 0:
            return
        eng_id = eng.get("id")
        if not eng_id:
            return
        # Normalise to alphanumerics, then require the reference to appear as a
        # whole token: "pwsub12" must NOT be matched by "pwsub1" (or vice versa),
        # so the id is followed by a non-digit or the end of the string.
        ref = re.sub(r"[^a-z0-9]+", "", _subcontractor_reference(eng).lower())
        if not ref:
            return
        ref_re = re.compile(re.escape(ref) + r"(?![0-9])")
        # Idempotency: never re-consume a Plaid transaction already settled.
        try:
            used_tx = {
                (s.get("plaid_tx_id") or "").strip()
                for s in exp_store.list_settlements_for_engineer(db, eng_id)
                if (s.get("plaid_tx_id") or "").strip()
            }
        except Exception:
            used_tx = set()
        try:
            txs = cardfeed.get_cached_transactions(db) or []
        except Exception:
            return
        for t in txs:
            tx_id = str(t.get("transaction_id") or "").strip()
            if tx_id and tx_id in used_tx:
                continue
            name = re.sub(r"[^a-z0-9]+", "", str(t.get("name") or "").lower())
            if not ref_re.search(name):
                continue
            paid_on = str(t.get("date") or "")[:10]
            try:
                amt = abs(float(t.get("amount") or 0))
            except (TypeError, ValueError):
                amt = None
            if not amt or amt <= 0:
                continue

            owed = [
                r for r in exp_store.list_receipts_for_engineer(db, eng_id)
                if r.get("status") in ("approved", "submitted")
                and not r.get("settlement_id")
            ]
            chosen, settled_p, target_p, total_p = _allocate_receipts_to_payment(
                owed, amt
            )
            # Classify the payment and build a warning note for the admin.
            status, note = "paid", ""
            if target_p > total_p + 2:
                status = "overpaid"
                note = (
                    f"Overpaid: paid {_exp_money(amt)} but only "
                    f"{_exp_money(total_p / 100)} was owed "
                    f"(+{_exp_money((target_p - total_p) / 100)})."
                )
            elif settled_p < target_p - 2:
                # Payment didn't land on a clean combination of receipts.
                status = "review"
                note = (
                    f"Doesn't match receipts exactly: paid {_exp_money(amt)}, "
                    f"auto-settled {_exp_money(settled_p / 100)} "
                    f"({len(chosen)} receipt(s)); "
                    f"{_exp_money((total_p - settled_p) / 100)} still owed. "
                    "Please check."
                )

            try:
                s = exp_store.create_settlement(
                    db, engineer_id=eng_id,
                    reference=_subcontractor_reference(eng),
                    amount=amt, paid_on=paid_on, status=status,
                    plaid_tx_id=tx_id, note=note,
                )
            except Exception as e:
                # Unique-index race: this tx was already consumed elsewhere.
                print(f"[subcontractor-settle] skip dup tx={tx_id}: {e}",
                      flush=True)
                return
            for r in chosen:
                exp_store.update_receipt(
                    db, r["id"], settlement_id=s["id"], status="settled"
                )
            print(f"[subcontractor-settle] engineer={eng_id} ref={ref} "
                  f"tx={tx_id} paid_on={paid_on} amount={amt} "
                  f"settled={settled_p/100:.2f} of owed={total_p/100:.2f} "
                  f"status={status}", flush=True)
            # Raise + pay the Xero bill for exactly the receipts this payment
            # settled (best-effort; never undoes the local settlement above).
            _create_xero_bill_for_settlement(eng, s, chosen)
            return

    def _exp_pull_receipt_image_from_xero(rec):
        """Pull a previously-submitted receipt image back from Xero when the
        local photo has been purged. Returns a local temp-file path, or '' when
        unavailable (Xero paused, no linkage, no attachment, or any error)."""
        if xero_is_disabled():
            return ""
        xtype = str((rec or {}).get("xero_type") or "").strip().lower()
        xid = str((rec or {}).get("xero_id") or "").strip()
        if not xid:
            return ""
        if "invoice" in xtype or "accpay" in xtype or "accrec" in xtype or "bill" in xtype:
            endpoint = "Invoices"
        else:
            endpoint = "BankTransactions"
        try:
            client = build_xero_client(config)
            attachments = client.get_attachments(endpoint, xid)
            chosen = None
            for a in attachments or []:
                if str(a.get("MimeType") or "").lower().startswith("image/"):
                    chosen = a
                    break
            if not chosen and attachments:
                chosen = attachments[0]
            name = str((chosen or {}).get("FileName") or "").strip()
            if not name:
                return ""
            content, _mime = client.get_attachment_content(endpoint, xid, name)
            if not content:
                return ""
            import tempfile as _tempfile
            suffix = os.path.splitext(name)[1] or ".jpg"
            fd, tmp_path = _tempfile.mkstemp(prefix="xero_exp_", suffix=suffix)
            with os.fdopen(fd, "wb") as fh:
                fh.write(content)
            return tmp_path
        except Exception as e:
            print(f"[expense-photo-pullback] failed: {e}", flush=True)
            return ""

    @app.get("/expenses/<token>")
    def expense_engineer_home(token: str):
        db = config.admin_db_file
        eng = exp_store.get_engineer_by_token(db, token)
        if not eng or not eng.get("active"):
            return _exp_error_page(
                "This expenses link is not active. Please contact the office."
            )
        if session.get("engineer_id") != eng["id"]:
            return redirect(url_for("portal_login"))

        _exp_purge_old_photos()
        receipts = exp_store.list_receipts_for_engineer(db, eng["id"])
        card_feed_html = _engineer_card_feed_html(eng, receipts)
        today = dt.datetime.now(dt.timezone.utc).date().isoformat()

        from collections import OrderedDict
        groups: "OrderedDict[str, list]" = OrderedDict()
        for r in receipts:
            day = (r.get("created_at") or "")[:10]
            groups.setdefault(day, []).append(r)

        # Flash message after an action.
        flash = request.args.get("flash", "")
        flash_html = ""
        if flash == "approved":
            flash_html = (
                "<div class='rounded-xl border border-emerald-200 bg-emerald-50 "
                "p-3 text-sm text-emerald-800 text-center'>&#10003; Receipt approved "
                "&amp; saved.</div>"
            )
        elif flash == "deleted":
            flash_html = (
                "<div class='rounded-xl border border-gray-200 bg-gray-50 "
                "p-3 text-sm text-gray-700 text-center'>Receipt removed.</div>"
            )

        paid_with_html = ""
        if _expense_owner_paid_enabled(eng):
            paid_with_html = """
                <div class="rounded-xl border border-gray-200 bg-white p-2 grid grid-cols-2 gap-2">
                  <label class="cursor-pointer">
                    <input type="radio" name="payment_source" value="company_card"
                           class="peer sr-only" checked>
                    <span class="block text-center rounded-lg border border-indigo-200
                                 bg-indigo-50 px-3 py-2 text-sm font-semibold text-indigo-800
                                 peer-checked:bg-indigo-600 peer-checked:text-white
                                 peer-checked:border-indigo-600">
                      Company card
                    </span>
                  </label>
                  <label class="cursor-pointer">
                    <input type="radio" name="payment_source" value="owner_paid"
                           class="peer sr-only">
                    <span class="block text-center rounded-lg border border-gray-200
                                 bg-white px-3 py-2 text-sm font-semibold text-gray-700
                                 peer-checked:bg-emerald-600 peer-checked:text-white
                                 peer-checked:border-emerald-600">
                      Personal card
                    </span>
                  </label>
                </div>
            """
        elif eng.get("kind") == "company_card":
            paid_with_html = '<input type="hidden" name="payment_source" value="company_card">'
        else:
            paid_with_html = '<input type="hidden" name="payment_source" value="owner_paid">'

        # Subcontractor: recognise any payment in the feed, then show the
        # running balance + the reference the office should use to pay.
        owed_html = ""
        if eng.get("kind") == "subcontractor":
            _maybe_settle_subcontractor(eng)
            owed = exp_store.amount_owed_to_engineer(db, eng["id"])
            ref = _subcontractor_reference(eng)
            settlements = exp_store.list_settlements_for_engineer(db, eng["id"])
            paid_line = ""
            if settlements and (settlements[0].get("paid_on") or ""):
                last = settlements[0]
                paid_line = (
                    "<div class='text-xs opacity-80 mt-1'>Last payment "
                    f"{_exp_money(last.get('amount') or 0)} on "
                    f"{escape(_exp_day_label(str(last.get('paid_on'))[:10]))}</div>"
                )
                _last_note = (last.get("note") or "").strip()
                if _last_note:
                    paid_line += (
                        "<div class='text-xs mt-1 bg-white/20 rounded px-2 py-1'>"
                        f"{escape(_last_note)}</div>"
                    )
            owed_html = (
                "<div class='rounded-xl border border-indigo-200 bg-indigo-600 "
                "text-white p-4 text-center'>"
                "<div class='text-xs uppercase tracking-wide opacity-80'>You're owed</div>"
                f"<div class='text-3xl font-bold mt-1'>{_exp_money(owed)}</div>"
                "<div class='text-xs opacity-80 mt-1'>Approved receipts not yet paid</div>"
                f"{paid_line}"
                "</div>"
                "<div class='rounded-xl border border-gray-200 bg-white p-3 text-center "
                "text-xs text-gray-600'>When the office pays you, ask them to use "
                "reference <span class='font-mono font-semibold text-gray-900'>"
                f"{escape(ref)}</span> so it&rsquo;s matched automatically.</div>"
            )

        # Today's receipts (full list + total).
        today_list = groups.get(today, [])
        if today_list:
            rows = []
            for r in today_list:
                merchant = r.get("merchant") or r.get("ocr_merchant") or "Receipt"
                amount = _exp_money(r.get("amount_inc"), r.get("currency") or "GBP")
                cat_name = (r.get("category_account_name") or "").strip()
                cat_html = (
                    f"<span class='inline-block text-xs px-2 py-0.5 rounded-full "
                    f"bg-indigo-50 text-indigo-700 mt-0.5'>{escape(cat_name)}</span>"
                    if cat_name else ""
                )
                source_html = (
                    "<span class='inline-block text-xs px-2 py-0.5 rounded-full "
                    "bg-emerald-50 text-emerald-700 mt-0.5'>Personal card</span>"
                    if (r.get("payment_source") or "company_card") == "owner_paid"
                    else ""
                )
                rows.append(
                    f"<a href='/expenses/{escape(token)}/review/{escape(r['id'])}' "
                    "class='flex items-center justify-between gap-3 px-4 py-3 "
                    "hover:bg-gray-50 border-b border-gray-100 last:border-0'>"
                    "<div class='min-w-0'>"
                    f"<div class='font-medium text-gray-900 truncate'>{escape(merchant)}</div>"
                    f"<div class='mt-0.5 flex items-center gap-2'>"
                    f"{_exp_status_badge(r.get('status'))}{cat_html}{source_html}</div>"
                    "</div>"
                    f"<div class='text-right font-semibold text-gray-900 "
                    f"whitespace-nowrap'>{amount}</div></a>"
                )
            today_total = sum(float(r.get("amount_inc") or 0) for r in today_list)
            today_html = (
                "<div class='rounded-xl border border-gray-200 bg-white overflow-hidden'>"
                + "".join(rows)
                + "<div class='flex items-center justify-between px-4 py-3 "
                "bg-gray-50 border-t border-gray-200'>"
                "<span class='text-sm font-medium text-gray-600'>Today's total</span>"
                f"<span class='font-bold text-gray-900'>{_exp_money(today_total)}</span>"
                "</div></div>"
            )
        else:
            today_html = (
                "<div class='rounded-xl border border-dashed border-gray-300 "
                "bg-white p-6 text-center text-sm text-gray-500'>"
                "No receipts yet today. Tap the button above to add one.</div>"
            )

        # Previous days (single-line totals).
        prev_lines = []
        for day, rows_ in groups.items():
            if day == today:
                continue
            total = sum(float(x.get("amount_inc") or 0) for x in rows_)
            prev_lines.append(
                "<div class='flex items-center justify-between px-4 py-3 "
                "border-b border-gray-100 last:border-0'>"
                f"<div><div class='font-medium text-gray-900'>{escape(_exp_day_label(day))}</div>"
                f"<div class='text-xs text-gray-500'>{len(rows_)} receipt(s)</div></div>"
                f"<div class='font-semibold text-gray-900'>{_exp_money(total)}</div></div>"
            )
        prev_html = ""
        if prev_lines:
            prev_html = (
                "<div class='mt-6'><h2 class='text-sm font-semibold text-gray-500 "
                "uppercase tracking-wide mb-2 px-1'>Previous days</h2>"
                "<div class='rounded-xl border border-gray-200 bg-white overflow-hidden'>"
                + "".join(prev_lines)
                + "</div></div>"
            )

        return _page(
            f"""
            <main class="max-w-xl mx-auto p-4 space-y-4">
              <div class="pt-2 flex items-start justify-between">
                <div>
                  <div class="text-xs text-gray-500">Field Expenses</div>
                  <h1 class="text-xl font-bold text-gray-900">{escape(eng['name'])}</h1>
                </div>
                <a href="/portal/logout"
                   class="text-xs text-gray-400 hover:text-gray-600 px-2 py-1">Log out</a>
              </div>
              {flash_html}
              {owed_html}
              <form id="exp-form" method="post"
                    action="/expenses/{escape(token)}/upload"
                    enctype="multipart/form-data">
                {paid_with_html}
                <label for="exp-file"
                       class="flex flex-col items-center justify-center w-full h-36
                              rounded-2xl bg-indigo-600 text-white cursor-pointer
                              active:bg-indigo-700 shadow-sm">
                  <span class="text-4xl">&#128247;</span>
                  <span class="mt-2 text-base font-semibold">Add a receipt</span>
                  <span class="text-xs opacity-80 mt-0.5">Take a photo</span>
                </label>
                <input id="exp-file" type="file" name="receipt_file"
                       accept="image/*" capture="environment"
                       style="position:absolute;width:1px;height:1px;opacity:0;pointer-events:none">
              </form>
              {card_feed_html}
              <div>
                <h2 class="text-sm font-semibold text-gray-500 uppercase tracking-wide
                           mb-2 px-1">Today</h2>
                {today_html}
              </div>
              {prev_html}
            </main>
            <div id="exp-overlay"
                 style="display:none;position:fixed;inset:0;background:rgba(255,255,255,.92);
                        z-index:50;align-items:center;justify-content:center;flex-direction:column;">
              <svg class="animate-spin h-8 w-8 text-indigo-600" xmlns="http://www.w3.org/2000/svg"
                   fill="none" viewBox="0 0 24 24">
                <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8z"></path>
              </svg>
              <div class="mt-3 text-sm font-medium text-gray-700">Reading your receipt&hellip;</div>
            </div>
            <script>
            (function() {{
              var inp = document.getElementById('exp-file');
              var form = document.getElementById('exp-form');
              var overlay = document.getElementById('exp-overlay');
              inp.addEventListener('change', function() {{
                if (!inp.files || !inp.files.length) return;
                if (overlay) overlay.style.display = 'flex';
                if (form.requestSubmit) {{ form.requestSubmit(); }} else {{ form.submit(); }}
              }});
            }})();
            </script>
            """
        )

    @app.post("/expenses/<token>/upload")
    def expense_engineer_upload(token: str):
        db = config.admin_db_file
        eng = exp_store.get_engineer_by_token(db, token)
        if not eng or not eng.get("active"):
            return _exp_error_page("This expenses link is not active.")
        if session.get("engineer_id") != eng["id"]:
            return redirect(url_for("portal_login"))

        file = request.files.get("receipt_file")
        if not file or not file.filename:
            return redirect(f"/expenses/{token}")
        payment_source, owner_paid_account_code = _expense_normalise_payment_source(
            eng, request.form.get("payment_source")
        )

        file_bytes = file.read() or b""
        filename = file.filename or "receipt.jpg"
        # Trust the file's magic bytes, never the client-supplied Content-Type.
        content_type = _exp_sniff_mime(file_bytes[:16])
        if content_type is None:
            return _exp_error_page(
                "Please upload a photo (JPG, PNG, GIF, WebP) or a PDF receipt.",
                400,
            )

        svc = ReceiptService(config)
        # Shrink big phone photos before OCR — same speed-up as the dump tool.
        file_bytes, filename, content_type = _exp_resize_for_ocr(
            file_bytes, filename, content_type
        )
        try:
            result = svc.analyze_upload(
                file_bytes=file_bytes, filename=filename, mime_type=content_type
            )
        except Exception as exc:  # OCR/save failure shouldn't lose the claim
            result = {
                "stored_file": "",
                "merchant": "",
                "total": None,
                "net": None,
                "tax": None,
                "date": "",
                "currency": "GBP",
                "raw_text": "",
                "ocr_error": str(exc).splitlines()[0][:200],
            }

        settings = get_expense_settings(db)
        vat_rate = settings["vat_rate"]
        total = result.get("total")
        net = result.get("net")
        tax = result.get("tax")
        total, net, tax, _zero_rated = _exp_reconcile_amounts(
            total, net, tax, vat_rate
        )

        # Auto-categorise: ask the AI to code the receipt against one of the
        # real Xero expense accounts so engineers usually just confirm it.
        # Falls back to the engineer's / global default account if AI can't decide.
        cat_code, cat_name = "", ""
        try:
            _at, _tid, _ = _load_xero_at_tid(config)
            _exp_accounts, _ = _get_xero_expense_accounts(_at, _tid, config.admin_db_file)
            cat_code, cat_name = _ai_categorize_receipt(
                db,
                result.get("merchant", ""),
                result.get("raw_text", ""),
                total,
                _exp_accounts,
            )
            if not cat_code:
                fallback = (
                    (eng.get("expense_account_code") or "").strip()
                    or (settings.get("default_expense_account") or "").strip()
                )
                if fallback:
                    cat_code = fallback
                    for _a in _exp_accounts:
                        if str(_a.get("Code") or "").strip() == fallback:
                            cat_name = str(_a.get("Name") or "").strip()
                            break
            _, cat_code, cat_name = _apply_fuel_threshold(
                [], cat_code, cat_name, total, _exp_accounts,
                result.get("raw_text", ""),
            )
        except Exception:
            cat_code, cat_name = "", ""

        today = dt.datetime.now(dt.timezone.utc).date().isoformat()
        rec = exp_store.create_receipt(
            db,
            engineer_id=eng["id"],
            merchant=result.get("merchant", ""),
            purchased_on=(result.get("date") or today),
            amount_inc=total,
            amount_ex=net,
            vat_amount=tax,
            currency=result.get("currency", "GBP"),
            ocr_merchant=result.get("merchant", ""),
            ocr_amount=total,
            ocr_date=result.get("date", ""),
            ocr_raw=(result.get("raw_text", "") or "")[:5000],
            ocr_error=result.get("ocr_error", ""),
            stored_file=result.get("stored_file", ""),
            filename=filename,
            mime_type=content_type,
            category_account_code=cat_code,
            category_account_name=cat_name,
            payment_source=payment_source,
            owner_paid_account_code=owner_paid_account_code,
            status="pending_review",
        )
        return redirect(f"/expenses/{token}/review/{rec['id']}")

    @app.get("/expenses/<token>/review/<rid>")
    def expense_engineer_review(token: str, rid: str):
        db = config.admin_db_file
        eng = exp_store.get_engineer_by_token(db, token)
        if not eng or not eng.get("active"):
            return _exp_error_page("This expenses link is not active.")
        if session.get("engineer_id") != eng["id"]:
            return redirect(url_for("portal_login"))
        rec = exp_store.get_receipt(db, rid)
        if not rec or rec.get("engineer_id") != eng["id"]:
            return _exp_error_page("Receipt not found.")

        _settings = get_expense_settings(db)
        vat_rate = _settings["vat_rate"]
        merchant = escape(rec.get("merchant") or "")
        purchased_on = escape(rec.get("purchased_on") or "")

        # Category = which Xero expense account this receipt is coded against
        # (fuel, advertising, plant & hire, etc.). Default to the engineer's
        # account, then the global default, so engineers usually just confirm it.
        _at, _tid, _ = _load_xero_at_tid(config)
        exp_accounts, _ = _get_xero_expense_accounts(_at, _tid, config.admin_db_file)
        category_html = ""
        if exp_accounts:
            selected_cat = (
                (rec.get("category_account_code") or "").strip()
                or (eng.get("expense_account_code") or "").strip()
                or (_settings.get("default_expense_account") or "").strip()
            )
            # If the AI already coded this receipt, tell the engineer so they
            # know they're just confirming rather than choosing from scratch.
            ai_hint = ""
            if (rec.get("category_account_code") or "").strip():
                ai_hint = (
                    "<span class='ml-1 text-indigo-600 font-normal'>"
                    "&mdash; suggested automatically, change if it's wrong</span>"
                )
            category_html = (
                "<div>"
                "<label class='block text-xs font-medium text-gray-500 mb-1'>"
                f"What was this for?{ai_hint}</label>"
                "<select name='category_account_code' "
                "class='w-full rounded-lg border border-gray-300 px-3 py-2 text-base bg-white'>"
                + _exp_acct_options(
                    exp_accounts, selected_cat, default_label="— choose a category —"
                )
                + "</select>"
                "<p class='text-xs text-gray-400 mt-1'>e.g. Fuel, Advertising, "
                "Plant &amp; hire, Materials</p>"
                "</div>"
            )
        inc = rec.get("amount_inc")
        ex = rec.get("amount_ex")
        vat = rec.get("vat_amount")
        inc_v = "" if inc is None else f"{float(inc):.2f}"
        ex_v = "" if ex is None else f"{float(ex):.2f}"
        vat_v = "" if vat is None else f"{float(vat):.2f}"

        ocr_note = ""
        if rec.get("ocr_error"):
            ocr_note = (
                "<div class='rounded-lg bg-amber-50 border border-amber-200 p-3 "
                "text-xs text-amber-800'>We couldn't read this receipt automatically &mdash; "
                "please fill in the details below.</div>"
            )

        photo_html = (
            f"<a href='/expenses/{escape(token)}/photo/{escape(rid)}' target='_blank' "
            "class='block'>"
            f"<img src='/expenses/{escape(token)}/photo/{escape(rid)}' "
            "alt='Receipt photo' class='w-full max-h-72 object-contain rounded-xl "
            "border border-gray-200 bg-gray-50'></a>"
            if rec.get("stored_file")
            else ""
        )

        already = rec.get("status") not in ("pending_review",)
        status_note = ""
        if already:
            status_note = (
                "<div class='rounded-lg bg-blue-50 border border-blue-200 p-3 "
                f"text-xs text-blue-800'>This receipt is already "
                f"{escape(rec.get('status'))}. You can still correct the details.</div>"
            )

        source = rec.get("payment_source") or "company_card"
        source_html = ""
        if _expense_owner_paid_enabled(eng):
            company_checked = "checked" if source != "owner_paid" else ""
            owner_checked = "checked" if source == "owner_paid" else ""
            owner_code = escape(str(eng.get("owner_paid_account_code") or "").strip())
            source_html = (
                "<div>"
                "<label class='block text-xs font-medium text-gray-500 mb-1'>Paid with</label>"
                "<div class='grid grid-cols-2 gap-2'>"
                "<label class='cursor-pointer'>"
                f"<input type='radio' name='payment_source' value='company_card' class='peer sr-only' {company_checked}>"
                "<span class='block text-center rounded-lg border border-gray-200 px-3 py-2 text-sm font-semibold text-gray-700 peer-checked:bg-indigo-600 peer-checked:text-white peer-checked:border-indigo-600'>Company card</span>"
                "</label>"
                "<label class='cursor-pointer'>"
                f"<input type='radio' name='payment_source' value='owner_paid' class='peer sr-only' {owner_checked}>"
                "<span class='block text-center rounded-lg border border-gray-200 px-3 py-2 text-sm font-semibold text-gray-700 peer-checked:bg-emerald-600 peer-checked:text-white peer-checked:border-emerald-600'>Personal card</span>"
                "</label>"
                "</div>"
                f"<p class='text-xs text-gray-400 mt-1'>Personal card receipts are saved as owner-paid to Xero account {owner_code} and do not expect a company-card bank match.</p>"
                "</div>"
            )
        elif eng.get("kind") == "company_card":
            source_html = "<input type='hidden' name='payment_source' value='company_card'>"
        else:
            source_html = "<input type='hidden' name='payment_source' value='owner_paid'>"

        return _page(
            f"""
            <main class="max-w-xl mx-auto p-4 space-y-4">
              <div class="flex items-center justify-between pt-2">
                <a href="/expenses/{escape(token)}" class="text-sm text-indigo-600">&larr; Back</a>
                <span class="text-xs text-gray-500">Check the details</span>
              </div>
              {photo_html}
              {ocr_note}
              {status_note}
              <form method="post" action="/expenses/{escape(token)}/review/{escape(rid)}"
                    class="space-y-4 rounded-xl border border-gray-200 bg-white p-4">
                <div id="vat_meta" data-rate="{vat_rate}"></div>
                {source_html}
                <div>
                  <label class="block text-xs font-medium text-gray-500 mb-1">Shop / supplier</label>
                  <input type="text" name="merchant" value="{merchant}"
                         class="w-full rounded-lg border border-gray-300 px-3 py-2 text-base"
                         placeholder="e.g. Screwfix">
                </div>
                <div>
                  <label class="block text-xs font-medium text-gray-500 mb-1">Date</label>
                  <input type="date" name="purchased_on" value="{purchased_on}"
                         class="w-full rounded-lg border border-gray-300 px-3 py-2 text-base">
                </div>
                {category_html}
                <div>
                  <label class="block text-xs font-medium text-gray-500 mb-1">Total paid (inc VAT)</label>
                  <input id="amount_inc" type="number" step="0.01" inputmode="decimal"
                         name="amount_inc" value="{inc_v}"
                         class="w-full rounded-lg border border-gray-300 px-3 py-2 text-lg font-semibold"
                         placeholder="0.00">
                </div>
                <div class="grid grid-cols-2 gap-3">
                  <div>
                    <label class="block text-xs font-medium text-gray-500 mb-1">Ex VAT</label>
                    <input id="amount_ex" type="number" step="0.01" inputmode="decimal"
                           name="amount_ex" value="{ex_v}"
                           class="w-full rounded-lg border border-gray-300 px-3 py-2 text-base">
                  </div>
                  <div>
                    <label class="block text-xs font-medium text-gray-500 mb-1">VAT</label>
                    <input id="vat_amount" type="number" step="0.01" inputmode="decimal"
                           name="vat_amount" value="{vat_v}"
                           class="w-full rounded-lg border border-gray-300 px-3 py-2 text-base">
                  </div>
                </div>
                <button type="submit"
                        class="w-full py-4 rounded-xl bg-emerald-600 text-white font-semibold text-base
                               active:bg-emerald-700">
                  Approve &amp; Save &#10003;
                </button>
              </form>
            </main>
            <script>
            (function() {{
              var inc = document.getElementById('amount_inc');
              var ex = document.getElementById('amount_ex');
              var vat = document.getElementById('vat_amount');
              var meta = document.getElementById('vat_meta');
              var rate = parseFloat(meta.getAttribute('data-rate')) || 0;
              inc.addEventListener('input', function() {{
                var v = parseFloat(inc.value) || 0;
                if (v <= 0) {{ return; }}
                var exVal = rate ? (v / (1 + rate / 100)) : v;
                ex.value = exVal.toFixed(2);
                vat.value = (v - exVal).toFixed(2);
              }});
            }})();
            </script>
            """
        )

    @app.post("/expenses/<token>/review/<rid>")
    def expense_engineer_review_save(token: str, rid: str):
        db = config.admin_db_file
        eng = exp_store.get_engineer_by_token(db, token)
        if not eng or not eng.get("active"):
            return _exp_error_page("This expenses link is not active.")
        if session.get("engineer_id") != eng["id"]:
            return redirect(url_for("portal_login"))
        rec = exp_store.get_receipt(db, rid)
        if not rec or rec.get("engineer_id") != eng["id"]:
            return _exp_error_page("Receipt not found.")

        def _num(name):
            raw = (request.form.get(name) or "").strip()
            if not raw:
                return None
            try:
                return round(float(raw), 2)
            except ValueError:
                return None

        updates = dict(
            merchant=(request.form.get("merchant") or "").strip()[:120],
            purchased_on=(request.form.get("purchased_on") or "").strip(),
            amount_inc=_num("amount_inc"),
            amount_ex=_num("amount_ex"),
            vat_amount=_num("vat_amount"),
            status="approved",
        )
        payment_source, owner_paid_account_code = _expense_normalise_payment_source(
            eng,
            request.form.get("payment_source")
            or rec.get("payment_source")
            or "company_card",
        )
        updates["payment_source"] = payment_source
        updates["owner_paid_account_code"] = owner_paid_account_code

        # Only touch the category when the dropdown was actually rendered/submitted.
        # If Xero was unavailable the field is absent, so we preserve any existing
        # category instead of silently clearing it.
        if "category_account_code" in request.form:
            category_code = (request.form.get("category_account_code") or "").strip()
            category_name = ""
            if category_code:
                _at, _tid, _ = _load_xero_at_tid(config)
                _exp_accounts, _ = _get_xero_expense_accounts(_at, _tid, config.admin_db_file)
                for _a in _exp_accounts:
                    if str(_a.get("Code") or "").strip() == category_code:
                        category_name = str(_a.get("Name") or "").strip()
                        break
            updates["category_account_code"] = category_code
            updates["category_account_name"] = category_name

        exp_store.update_receipt(db, rid, **updates)
        return redirect(f"/expenses/{token}?flash=approved")

    @app.get("/expenses/<token>/photo/<rid>")
    def expense_engineer_photo(token: str, rid: str):
        db = config.admin_db_file
        eng = exp_store.get_engineer_by_token(db, token)
        if not eng or not eng.get("active"):
            return _exp_error_page("Not found.")
        if session.get("engineer_id") != eng["id"]:
            return redirect(url_for("portal_login"))
        rec = exp_store.get_receipt(db, rid)
        if not rec or rec.get("engineer_id") != eng["id"]:
            return _exp_error_page("Not found.")
        path = os.path.abspath(rec.get("stored_file") or "")
        if not path or not os.path.exists(path):
            # Local photo purged after submission — try pulling it back from
            # Xero. Read it into memory and delete the temp file immediately so
            # repeated views can't accumulate temp files on disk.
            pulled = _exp_pull_receipt_image_from_xero(rec)
            if pulled:
                try:
                    with open(pulled, "rb") as fh:
                        data = fh.read()
                finally:
                    try:
                        os.remove(pulled)
                    except OSError:
                        pass
                mime = _exp_sniff_mime(data[:16]) or "application/octet-stream"
                resp = Response(data, mimetype=mime)
                resp.headers["X-Content-Type-Options"] = "nosniff"
                return resp
            return _exp_error_page("Photo not available.")
        with open(path, "rb") as fh:
            head = fh.read(16)
        safe_mime = _exp_sniff_mime(head)
        if safe_mime and safe_mime.startswith("image/"):
            resp = send_file(path, mimetype=safe_mime)
        elif safe_mime == "application/pdf":
            resp = send_file(
                path,
                mimetype="application/pdf",
                as_attachment=True,
                download_name="receipt.pdf",
            )
        else:
            resp = send_file(
                path,
                mimetype="application/octet-stream",
                as_attachment=True,
                download_name="receipt",
            )
        resp.headers["X-Content-Type-Options"] = "nosniff"
        return resp

    # ── Field Expenses: LIVE parser test mode (no submission to Xero) ─────────
    #
    # An admin starts a test session and shows a QR code. A tester scans it with
    # their phone, photographs a real receipt, and the app shows what the parser
    # extracted AND which Xero account/options it WOULD choose. Nothing is saved
    # as a claim and nothing is submitted to Xero.

    _TEST_POLL_JS = """
<script>
(function(){
  var url = "__RESULT_URL__";
  var box = document.getElementById("test-result");
  function esc(s){ var d=document.createElement('div'); d.textContent=(s==null?'':String(s)); return d.innerHTML; }
  function money(v,c){ if(v===null||v===undefined||v==="") return "\u2014"; return (c||"\u00a3")+Number(v).toFixed(2); }
  function row(label,val){ return "<div class='flex justify-between gap-3 py-1.5 border-b border-gray-100'><span class='text-gray-500'>"+esc(label)+"</span><span class='font-medium text-gray-900 text-right'>"+esc(val)+"</span></div>"; }
  function render(d){
    if(!d || d.status==="waiting"){ box.innerHTML="<div class='text-sm text-gray-500'>Waiting for a receipt photo\u2026 keep this screen open.</div>"; return; }
    if(d.status==="expired"){ box.innerHTML="<div class='rounded-lg border border-amber-200 bg-amber-50 p-3 text-sm text-amber-800'>This test session expired. Start a new one.</div>"; return; }
    if(d.status==="error"){ box.innerHTML="<div class='rounded-lg border border-red-200 bg-red-50 p-3 text-sm text-red-800'>Could not read that receipt. Try again.</div>"; return; }
    var r=d.result||{}, cat=r.category||{}, pay=r.payment_account||{};
    var segs=r.segments||[];
    var rows="";
    rows+=row("Merchant", r.merchant||"\u2014");
    rows+=row("Date", r.purchased_on_uk||r.purchased_on||"\u2014");
    rows+=row("Total (inc VAT)", money(r.amount_inc, r.currency_symbol));
    rows+=row("Net (ex VAT)", money(r.amount_ex, r.currency_symbol));
    rows+=row("VAT", money(r.vat_amount, r.currency_symbol));
    if(r.zero_rated!==null && r.zero_rated!==undefined && Number(r.zero_rated)>0){
      rows+=row("Zero-rated portion", money(r.zero_rated, r.currency_symbol));
    }
    rows+=row(r.is_split?"Main Xero account":"Xero account it would choose", (cat.name?cat.name:"\u2014")+(cat.code?" ("+cat.code+")":""));
    rows+=row("How it was chosen", cat.source_label||"\u2014");
    rows+=row("Payment / bank account", (pay.name?pay.name:"\u2014")+(pay.code?" ("+pay.code+")":""));
    rows+=row("Would create in Xero", r.would_create||"\u2014");
    var segHtml="";
    if(segs.length>1){
      segHtml="<div class='mt-3'><div class='text-xs font-semibold text-gray-700 mb-1.5'>Split receipt \u2014 "+segs.length+" parts, coded separately</div>";
      for(var i=0;i<segs.length;i++){
        var s=segs[i];
        segHtml+="<div class='rounded-lg border border-indigo-100 bg-indigo-50 p-2 mb-1.5 text-xs'>"
          +"<div class='font-medium text-indigo-900'>"+esc(s.label)+"</div>"
          +"<div class='flex justify-between text-indigo-700'><span>"+esc(s.account_name)+" ("+esc(s.account_code)+")</span><span class='font-medium'>"+money(s.gross,r.currency_symbol)+"</span></div>"
          +"<div class='text-indigo-500 mt-0.5'>net "+money(s.net,r.currency_symbol)+" + VAT "+money(s.vat,r.currency_symbol)+" @ "+esc(s.vat_rate)+"%</div>"
          +"</div>";
      }
      segHtml+="</div>";
    }
    var warn=r.ocr_error?"<div class='rounded-lg border border-amber-200 bg-amber-50 p-2 text-xs text-amber-800 mt-2'>Parser note: "+esc(r.ocr_error)+"</div>":"";
    box.innerHTML="<div class='rounded-xl border border-emerald-200 bg-emerald-50 p-3 text-sm text-emerald-800 mb-3 font-medium'>\u2713 Receipt read \u2014 nothing was sent to Xero.</div><div class='text-sm'>"+rows+"</div>"+segHtml+warn;
  }
  function tick(){ fetch(url,{headers:{"X-Requested-With":"fetch"}}).then(function(r){return r.json();}).then(render).catch(function(){}); }
  tick(); setInterval(tick, 3000);
})();
</script>
"""

    def _exp_test_session_page(token, session_obj):
        base = _current_base_url()
        mobile_url = f"{base}/expenses/test/{token}"
        result_url = f"{base}/receipts/expenses/test/result/{token}"
        try:
            import io as _io
            import segno as _segno
            _qr = _segno.make(mobile_url, error="m")
            _b = _io.BytesIO()
            _qr.save(_b, kind="svg", scale=6, border=2, dark="#312e81", xmldecl=False)
            qr_svg = _b.getvalue().decode("utf-8")
        except Exception:
            qr_svg = "<div class='text-xs text-red-600'>Could not render QR code.</div>"
        eng_label = "Generic test (no engineer)"
        eid = session_obj.get("engineer_id")
        if eid:
            _e = exp_store.get_engineer(config.admin_db_file, int(eid))
            if _e:
                k = _e.get("kind") or "company_card"
                eng_label = (
                    f"{_e['name']} "
                    f"({'Company card' if k == 'company_card' else 'Subcontractor'})"
                )
        poll = _TEST_POLL_JS.replace("__RESULT_URL__", result_url)
        body = (
            "<main class='max-w-xl mx-auto p-6 space-y-5'>"
            "<div class='flex items-center justify-between'>"
            "<h1 class='text-xl font-bold text-gray-900'>Test receipt scanner</h1>"
            "<a href='/receipts/expenses' class='text-sm text-indigo-600'>&larr; Back</a></div>"
            f"<p class='text-sm text-gray-600'>Testing as: <span class='font-medium'>{escape(eng_label)}</span></p>"
            "<div class='rounded-2xl border border-gray-200 bg-white p-5 text-center space-y-3'>"
            "<p class='text-sm text-gray-700'>Scan this with your phone camera, then photograph a real receipt.</p>"
            f"<div class='flex justify-center'>{qr_svg}</div>"
            "<div class='flex items-center gap-2'>"
            f"<input readonly value='{escape(mobile_url)}' class='flex-1 text-xs bg-gray-50 border border-gray-200 rounded px-2 py-1 text-gray-600'>"
            f"<button type='button' onclick=\"navigator.clipboard.writeText('{escape(mobile_url)}')\" class='text-xs px-2 py-1 rounded bg-indigo-600 text-white'>Copy</button>"
            "</div>"
            "<p class='text-xs text-gray-400'>This link works for 30 minutes. Nothing is submitted to Xero.</p>"
            "</div>"
            "<div class='rounded-2xl border border-gray-200 bg-white p-5'>"
            "<h2 class='text-sm font-semibold text-gray-900 mb-3'>Result</h2>"
            "<div id='test-result'><div class='text-sm text-gray-500'>Waiting for a receipt photo&hellip; keep this screen open.</div></div>"
            "</div>"
            "<div><a href='/receipts/expenses/test' class='text-sm text-gray-500'>Start a new test session</a></div>"
            "</main>" + poll
        )
        return _page(body)

    def _exp_test_run(session_obj, *, file_bytes, filename, content_type):
        db = config.admin_db_file
        svc = ReceiptService(config)
        # Shrink big phone photos before OCR — same speed-up as the dump tool.
        file_bytes, filename, content_type = _exp_resize_for_ocr(
            file_bytes, filename, content_type
        )
        try:
            result = svc.analyze_upload(
                file_bytes=file_bytes, filename=filename, mime_type=content_type
            )
        except Exception as exc:
            result = {
                "stored_file": "", "merchant": "", "total": None, "net": None,
                "tax": None, "date": "", "currency": "GBP", "raw_text": "",
                "ocr_error": str(exc).splitlines()[0][:200],
            }
        settings = get_expense_settings(db)
        vat_rate = settings["vat_rate"]
        ocr_net = result.get("net")
        ocr_tax = result.get("tax")
        ocr_had_breakdown = ocr_net is not None or ocr_tax is not None
        total, net, tax, zero_rated = _exp_reconcile_amounts(
            result.get("total"), ocr_net, ocr_tax, vat_rate
        )

        eng = None
        eid = session_obj.get("engineer_id")
        if eid:
            eng = exp_store.get_engineer(db, int(eid))

        exp_accounts: list = []
        bank_accounts: list = []
        segments: list = []
        cat_code = cat_name = ""
        cat_source = "none"
        try:
            _at, _tid, _ = _load_xero_at_tid(config)
            exp_accounts, _ = _get_xero_expense_accounts(_at, _tid, config.admin_db_file)
            try:
                _r, bank_accounts, _t, _bw = (
                    _get_tenant_acct_themes(_at, _tid) if (_at and _tid) else ([], [], [], "")
                )
            except Exception:
                bank_accounts = []
            # Split-aware: ask the AI to break the receipt into per-account
            # segments (e.g. fuel + food). Falls back to single-account coding.
            segments = _ai_analyze_receipt(
                db, result.get("merchant", ""), result.get("raw_text", ""),
                total, exp_accounts, vat_rate,
            )
            if segments:
                cat_source = "ai"
                primary = max(segments, key=lambda s: s.get("gross") or 0)
                cat_code = primary["account_code"]
                cat_name = primary["account_name"]
                # When the receipt is genuinely split and OCR gave us no explicit
                # net/VAT, let the per-segment VAT drive the headline figures.
                if len(segments) > 1 and not ocr_had_breakdown:
                    net = round(sum(s["net"] for s in segments), 2)
                    tax = round(sum(s["vat"] for s in segments), 2)
                    total = round(sum(s["gross"] for s in segments), 2)
                    zero_rated = round(
                        sum(s["gross"] - s["net"] - s["vat"] for s in segments), 2
                    )
            else:
                cat_code, cat_name = _ai_categorize_receipt(
                    db, result.get("merchant", ""), result.get("raw_text", ""),
                    total, exp_accounts,
                )
                if cat_code:
                    cat_source = "ai"
                else:
                    fallback = (
                        ((eng.get("expense_account_code") if eng else "") or "").strip()
                        or (settings.get("default_expense_account") or "").strip()
                    )
                    if fallback:
                        cat_code = fallback
                        cat_source = "default"
                        for _a in exp_accounts:
                            if str(_a.get("Code") or "").strip() == fallback:
                                cat_name = str(_a.get("Name") or "").strip()
                                break
            segments, cat_code, cat_name = _apply_fuel_threshold(
                segments, cat_code, cat_name, total, exp_accounts,
                result.get("raw_text", ""),
            )
        except Exception:
            pass

        pay_code = (
            ((eng.get("payment_account_code") if eng else "") or "").strip()
            or (settings.get("default_payment_account") or "").strip()
        )
        pay_name = ""
        for _a in bank_accounts:
            if str(_a.get("Code") or "").strip() == pay_code:
                pay_name = str(_a.get("Name") or "").strip()
                break

        if eng:
            kind = eng.get("kind") or "company_card"
            would = (
                "Spend Money bank transaction (already paid by company card)"
                if kind == "company_card"
                else "Purchase bill \u2014 money owed to subcontractor (ACCPAY)"
            )
        else:
            would = (
                "Company card \u2192 Spend Money; Subcontractor \u2192 Purchase bill "
                "(pick an engineer to see exactly)"
            )
        source_labels = {
            "ai": "Chosen automatically by AI",
            "default": "Fell back to the default account (AI unsure)",
            "none": "No account chosen (configure AI key or a default account)",
        }
        today = dt.datetime.now(dt.timezone.utc).date().isoformat()
        purchased_on = result.get("date") or today
        return {
            "merchant": result.get("merchant", ""),
            "purchased_on": purchased_on,
            "purchased_on_uk": _exp_uk_date(purchased_on),
            "currency": result.get("currency", "GBP"),
            "currency_symbol": "\u00a3",
            "amount_inc": total, "amount_ex": net, "vat_amount": tax,
            "zero_rated": zero_rated,
            "vat_rate": vat_rate,
            "is_split": len(segments) > 1,
            "segments": segments,
            "ocr_error": result.get("ocr_error", ""),
            "category": {
                "code": cat_code, "name": cat_name, "source": cat_source,
                "source_label": source_labels.get(cat_source, ""),
            },
            "payment_account": {"code": pay_code, "name": pay_name},
            "engineer": ({"name": eng["name"], "kind": eng.get("kind")} if eng else None),
            "would_create": would,
        }

    @app.get("/expenses/test/<token>")
    def expense_test_capture(token: str):
        s = get_expense_test_session(config.admin_db_file, token)
        if s is None:
            return _exp_error_page(
                "This test link has expired. Ask the office for a new QR code."
            )
        body = f"""
<style>body {{ background:#f7f6f3 !important; }}</style>
<div style="max-width:430px;margin:0 auto;min-height:100vh;display:flex;flex-direction:column;">
  <div style="background:white;border-bottom:1px solid #e5e7eb;padding:12px 18px;display:flex;
              align-items:center;justify-content:space-between;position:sticky;top:0;z-index:10;">
    <span style="font-size:16px;font-weight:800;color:#1e1b4b;letter-spacing:-.03em;">Powwash</span>
    <span style="font-size:11px;font-weight:600;color:#92400e;background:#fffbeb;border:1px solid #fde68a;
                 padding:3px 10px;border-radius:99px;">Test mode</span>
  </div>
  <div style="flex:1;padding:20px 16px 32px;display:flex;flex-direction:column;gap:14px;">
    <div style="background:#fffbeb;border:1px solid #fde68a;border-radius:12px;padding:12px 14px;">
      <div style="font-size:13px;font-weight:600;color:#92400e;">Nothing will be saved or sent to Xero.</div>
      <div style="font-size:12px;color:#b45309;margin-top:2px;">Take a photo to see how the AI reads this receipt.</div>
    </div>
    <form id="tst-form" method="post" action="/expenses/test/{escape(token)}" enctype="multipart/form-data">
      <div id="cam-zone" onclick="document.getElementById('tst-file').click()"
           style="position:relative;background:white;border:1.5px solid #e5e7eb;border-radius:18px;
                  min-height:300px;display:flex;flex-direction:column;align-items:center;
                  justify-content:center;cursor:pointer;overflow:hidden;">
        <span style="position:absolute;top:12px;left:12px;width:20px;height:20px;border-top:2.5px solid #d97706;border-left:2.5px solid #d97706;border-radius:3px 0 0 0;"></span>
        <span style="position:absolute;top:12px;right:12px;width:20px;height:20px;border-top:2.5px solid #d97706;border-right:2.5px solid #d97706;border-radius:0 3px 0 0;"></span>
        <span style="position:absolute;bottom:12px;left:12px;width:20px;height:20px;border-bottom:2.5px solid #d97706;border-left:2.5px solid #d97706;border-radius:0 0 0 3px;"></span>
        <span style="position:absolute;bottom:12px;right:12px;width:20px;height:20px;border-bottom:2.5px solid #d97706;border-right:2.5px solid #d97706;border-radius:0 0 3px 0;"></span>
        <div id="cam-idle" style="display:flex;flex-direction:column;align-items:center;gap:12px;padding:32px;text-align:center;">
          <svg width="56" height="56" viewBox="0 0 24 24" fill="none" stroke="#d97706" stroke-width="1.5"
               stroke-linecap="round" stroke-linejoin="round">
            <path d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z"/>
            <circle cx="12" cy="13" r="4"/>
          </svg>
          <div>
            <div style="font-size:18px;font-weight:700;color:#111827;margin-bottom:4px;">Tap to photograph</div>
            <div style="font-size:13px;color:#9ca3af;line-height:1.5;">Point camera at the receipt</div>
          </div>
        </div>
        <img id="tst-img" src="" alt="Receipt preview"
             style="display:none;width:100%;height:100%;object-fit:contain;position:absolute;inset:0;padding:12px;">
      </div>
      <input id="tst-file" type="file" name="receipt_file" accept="image/*" capture="environment" required
             style="position:absolute;width:1px;height:1px;opacity:0;pointer-events:none;">
      <div id="tst-actions" style="display:none;flex-direction:column;gap:10px;margin-top:12px;">
        <button id="tst-submit" type="submit"
                style="width:100%;padding:15px;background:#d97706;color:white;font-size:16px;
                       font-weight:700;border:none;border-radius:13px;cursor:pointer;">
          Analyse this receipt &nbsp;&#10003;
        </button>
        <button type="button" id="tst-retake"
                style="width:100%;padding:13px;background:white;color:#4b5563;font-size:14px;
                       font-weight:600;border:1.5px solid #e5e7eb;border-radius:13px;cursor:pointer;">
          Retake photo
        </button>
      </div>
    </form>
  </div>
</div>
<script>
(function() {{
  var inp  = document.getElementById('tst-file');
  var zone = document.getElementById('cam-zone');
  var idle = document.getElementById('cam-idle');
  var img  = document.getElementById('tst-img');
  var acts = document.getElementById('tst-actions');
  var sub  = document.getElementById('tst-submit');
  var retk = document.getElementById('tst-retake');
  var form = document.getElementById('tst-form');
  function showPreview(file) {{
    img.src = URL.createObjectURL(file);
    img.style.display = 'block';
    idle.style.display = 'none';
    acts.style.display = 'flex';
    zone.style.cursor  = 'default';
    zone.onclick = null;
  }}
  retk.addEventListener('click', function() {{
    img.style.display  = 'none'; img.src = '';
    idle.style.display = 'flex';
    acts.style.display = 'none';
    zone.style.cursor  = 'pointer';
    zone.onclick = function() {{ inp.click(); }};
    inp.value = '';
    setTimeout(function() {{ try {{ inp.click(); }} catch(e) {{}} }}, 50);
  }});
  inp.addEventListener('change', function() {{
    if (!inp.files || !inp.files.length) return;
    showPreview(inp.files[0]);
  }});
  form.addEventListener('submit', function() {{
    sub.textContent = 'Analysing\u2026'; sub.disabled = true;
    sub.style.background = '#f59e0b'; retk.style.display = 'none';
  }});
  try {{ inp.click(); }} catch(e) {{}}
}})();
</script>
"""
        return _page(body)

    @app.post("/expenses/test/<token>")
    def expense_test_capture_upload(token: str):
        db = config.admin_db_file
        s = get_expense_test_session(db, token)
        if s is None:
            return _exp_error_page(
                "This test link has expired. Ask the office for a new QR code."
            )
        file = request.files.get("receipt_file")
        if not file or not file.filename:
            return redirect(f"/expenses/test/{token}")
        file_bytes = file.read() or b""
        filename = file.filename or "receipt.jpg"
        content_type = _exp_sniff_mime(file_bytes[:16])
        if content_type is None:
            return _exp_error_page(
                "Please upload a photo (JPG, PNG, GIF, WebP) or a PDF receipt.", 400
            )
        try:
            result = _exp_test_run(
                s, file_bytes=file_bytes, filename=filename, content_type=content_type
            )
            set_expense_test_result(db, token, status="done", result=result)
        except Exception as exc:
            set_expense_test_result(db, token, status="error", result=None)
            return _exp_error_page(
                f"Could not read that receipt: {str(exc).splitlines()[0][:200]}", 500
            )

        def _row(label, val):
            return (
                "<div class='flex justify-between gap-3 py-1.5 border-b border-gray-100'>"
                f"<span class='text-gray-500'>{escape(label)}</span>"
                f"<span class='font-medium text-gray-900 text-right'>{escape(val)}</span></div>"
            )

        def _m(v):
            return "\u2014" if v is None else f"\u00a3{float(v):.2f}"

        cat = result["category"]
        pay = result["payment_account"]
        cat_disp = (cat["name"] or "\u2014") + (f" ({cat['code']})" if cat["code"] else "")
        pay_disp = (pay["name"] or "\u2014") + (f" ({pay['code']})" if pay["code"] else "")
        rows = (
            _row("Merchant", result["merchant"] or "\u2014")
            + _row("Date", result["purchased_on"] or "\u2014")
            + _row("Total (inc VAT)", _m(result["amount_inc"]))
            + _row("Net (ex VAT)", _m(result["amount_ex"]))
            + _row("VAT", _m(result["vat_amount"]))
            + _row("Xero account it would choose", cat_disp)
            + _row("How it was chosen", cat["source_label"])
            + _row("Payment / bank account", pay_disp)
            + _row("Would create in Xero", result["would_create"])
        )
        warn = (
            "<div class='rounded-lg border border-amber-200 bg-amber-50 p-2 text-xs "
            f"text-amber-800 mt-2'>Parser note: {escape(result['ocr_error'])}</div>"
            if result.get("ocr_error") else ""
        )
        body = (
            "<style>body { background:#f7f6f3 !important; }</style>"
            "<div style='max-width:430px;margin:0 auto;min-height:100vh;display:flex;flex-direction:column;'>"
            "<div style='background:white;border-bottom:1px solid #e5e7eb;padding:12px 18px;display:flex;"
            "align-items:center;justify-content:space-between;position:sticky;top:0;'>"
            "<span style='font-size:16px;font-weight:800;color:#1e1b4b;letter-spacing:-.03em;'>Powwash</span>"
            "<span style='font-size:11px;font-weight:600;color:#92400e;background:#fffbeb;border:1px solid #fde68a;"
            "padding:3px 10px;border-radius:99px;'>Test mode</span>"
            "</div>"
            "<div style='flex:1;padding:20px 16px 32px;display:flex;flex-direction:column;gap:14px;'>"
            "<div style='background:#ecfdf5;border:1px solid #6ee7b7;border-radius:12px;padding:12px 14px;'>"
            "<div style='font-size:13px;font-weight:600;color:#065f46;'>&#10003;&nbsp;Receipt read &mdash; nothing was sent to Xero.</div>"
            "</div>"
            "<div style='background:white;border:1px solid #e5e7eb;border-radius:16px;overflow:hidden;'>"
            "<div style='padding:14px 16px;border-bottom:1px solid #f3f4f6;'>"
            "<div style='font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.07em;color:#9ca3af;margin-bottom:2px;'>AI reading</div>"
            "</div>"
            "<div style='padding:0 16px;'>" + rows + "</div>"
            "</div>"
            + warn
            + "<a href='/expenses/test/" + escape(token) + "' "
            "style='display:block;text-align:center;padding:13px;background:white;color:#4f46e5;"
            "font-size:14px;font-weight:600;border:1.5px solid #e5e7eb;border-radius:13px;text-decoration:none;'>"
            "Test another receipt</a>"
            "</div></div>"
        )
        return _page(body)

    @app.get("/receipts/expenses/test")
    @require_login
    def expense_admin_test():
        db = config.admin_db_file
        token = (request.args.get("token") or "").strip()
        session_obj = get_expense_test_session(db, token) if token else None
        if token and session_obj:
            return _exp_test_session_page(token, session_obj)
        engineers = exp_store.list_engineers(db, include_inactive=False)
        opts = ["<option value=''>&mdash; No engineer (generic test) &mdash;</option>"]
        for e in engineers:
            kind = e.get("kind") or "company_card"
            klabel = "Company card" if kind == "company_card" else "Subcontractor"
            opts.append(
                f"<option value='{e['id']}'>{escape(e['name'])} ({klabel})</option>"
            )
        expired_note = ""
        if token and session_obj is None:
            expired_note = (
                "<div class='rounded-lg border border-amber-200 bg-amber-50 p-3 text-sm "
                "text-amber-800'>That test session expired. Start a new one below.</div>"
            )
        body = (
            "<main class='max-w-xl mx-auto p-6 space-y-5'>"
            "<div class='flex items-center justify-between'>"
            "<h1 class='text-xl font-bold text-gray-900'>Test receipt scanner</h1>"
            "<a href='/receipts/expenses' class='text-sm text-indigo-600'>&larr; Back</a></div>"
            + expired_note +
            "<p class='text-sm text-gray-600'>Generate a QR code, scan it with a phone, and "
            "photograph a real receipt. You'll see what the parser extracted and which Xero "
            "account it would choose &mdash; <span class='font-medium'>nothing is submitted to "
            "Xero</span>.</p>"
            "<form method='post' action='/receipts/expenses/test' "
            "class='rounded-2xl border border-gray-200 bg-white p-5 space-y-4'>"
            "<div><label class='block text-sm text-gray-600 mb-1'>Test as engineer (optional)</label>"
            f"<select name='engineer_id' class='w-full rounded border border-gray-300 px-3 py-2 text-sm'>{''.join(opts)}</select>"
            "<p class='text-xs text-gray-400 mt-1'>Pick an engineer to see exactly which Xero "
            "action (Spend Money vs Bill) and accounts would be used.</p></div>"
            "<button type='submit' class='w-full rounded-lg bg-indigo-600 text-white py-3 "
            "font-medium'>Start test session</button>"
            "</form></main>"
        )
        return _page(body)

    @app.post("/receipts/expenses/test")
    @require_login
    def expense_admin_test_start():
        eid_raw = (request.form.get("engineer_id") or "").strip()
        eid = int(eid_raw) if eid_raw.isdigit() else None
        token = create_expense_test_session(config.admin_db_file, engineer_id=eid)
        return redirect(f"/receipts/expenses/test?token={token}")

    @app.get("/receipts/expenses/test/result/<token>")
    @require_login
    def expense_admin_test_result(token: str):
        s = get_expense_test_session(config.admin_db_file, token)
        if s is None:
            return jsonify({"status": "expired"})
        return jsonify({"status": s.get("status", "waiting"), "result": s.get("result")})

    # ── Field Expenses: admin management (require_login) ──────────────────────

    @app.get("/receipts/expenses")
    @require_login
    def expense_admin_home():
        db = config.admin_db_file
        _exp_purge_old_photos()
        engineers = exp_store.list_engineers(db)
        settings = get_expense_settings(db)
        base_url = request.url_root.rstrip("/")

        # Live Xero account lists so dropdowns reflect the real chart of accounts.
        _at, _tid, _acct_warn = _load_xero_at_tid(config)
        exp_accounts, _exp_warn = _get_xero_expense_accounts(_at, _tid, config.admin_db_file)
        bank_accounts: list = []
        owner_paid_accounts: list = []
        _owner_warn = ""
        try:
            _r, bank_accounts, _t, _bw = _get_tenant_acct_themes(_at, _tid) if (_at and _tid) else ([], [], [], "")
        except Exception:
            bank_accounts = []
        try:
            owner_paid_accounts, _owner_warn = (
                _get_xero_active_accounts(_at, _tid, config.admin_db_file)
                if (_at and _tid) else ([], "")
            )
        except Exception:
            owner_paid_accounts, _owner_warn = [], ""
        acct_warning = " ".join(
            dict.fromkeys([
                w.strip() for w in (_acct_warn, _exp_warn, _owner_warn)
                if w and w.strip()
            ])
        )
        acct_warning_html = ""
        if acct_warning:
            acct_warning_html = (
                "<div class='rounded-lg border border-amber-200 bg-amber-50 p-3 text-xs "
                f"text-amber-800'>{escape(acct_warning)} You can still type an account code "
                "directly.</div>"
            )

        # ── Setup & connection status (parser / AI / Xero) ──────────────────
        rcs = get_receipts_settings(db)
        _sa_file = (rcs.get("google_service_account_file") or "").strip()
        docai_has_ids = bool(
            rcs.get("document_ai_project_id") and rcs.get("document_ai_processor_id")
        )
        sa_exists = bool(_sa_file) and os.path.exists(_sa_file)
        docai_test = get_json_setting(db, "receipts_parser_test_status", {}) or {}
        _oa = get_openai_settings(db)
        openai_has_key = bool(
            (_oa.get("api_key") or "").strip() or (os.getenv("OPENAI_API_KEY") or "").strip()
        )
        openai_test = get_json_setting(db, "openai_test_status", {}) or {}
        xero_ok = bool(_at and _tid)

        def _conn_status(*, has_config, test, missing_msg, configured_msg):
            """Return (badge_html, detail_text). Uses last live-test result when present."""
            tested = bool(test) and test.get("ok") is not None
            ok = bool(test.get("ok")) if tested else False
            when = escape(str(test.get("tested_at") or "")) if tested else ""
            if not has_config:
                badge = (
                    "<span class='text-xs font-semibold px-2 py-0.5 rounded-full "
                    "bg-gray-100 text-gray-600'>Not configured</span>"
                )
                return badge, missing_msg
            if tested and ok:
                badge = (
                    "<span class='text-xs font-semibold px-2 py-0.5 rounded-full "
                    "bg-emerald-100 text-emerald-700'>Connected</span>"
                )
                return badge, (f"Last checked {when}." if when else "Connection verified.")
            if tested and not ok:
                badge = (
                    "<span class='text-xs font-semibold px-2 py-0.5 rounded-full "
                    "bg-red-100 text-red-700'>Not connected</span>"
                )
                msg = escape(str(test.get("message") or "Last connection test failed."))
                return badge, msg[:200]
            badge = (
                "<span class='text-xs font-semibold px-2 py-0.5 rounded-full "
                "bg-amber-100 text-amber-700'>Configured &mdash; not tested</span>"
            )
            return badge, configured_msg

        docai_detail_missing = (
            "Add your Google Document AI project ID, processor ID and service-account "
            "file to read receipts automatically."
            if not docai_has_ids
            else "Project details are set, but the Google service-account file is missing. "
            "Upload it in the parser settings."
        )
        docai_badge, docai_detail = _conn_status(
            has_config=(docai_has_ids and sa_exists),
            test=docai_test,
            missing_msg=docai_detail_missing,
            configured_msg="Settings look complete. Click Test to confirm the connection.",
        )
        openai_badge, openai_detail = _conn_status(
            has_config=openai_has_key,
            test=openai_test,
            missing_msg="Add your own OpenAI API key so receipts are auto-categorised "
            "into the right Xero account.",
            configured_msg="API key is set. Click Test to confirm the connection.",
        )
        if xero_ok:
            xero_badge = (
                "<span class='text-xs font-semibold px-2 py-0.5 rounded-full "
                "bg-emerald-100 text-emerald-700'>Connected</span>"
            )
            xero_detail = "Xero is connected and the chart of accounts is available."
        else:
            xero_badge = (
                "<span class='text-xs font-semibold px-2 py-0.5 rounded-full "
                "bg-red-100 text-red-700'>Not connected</span>"
            )
            xero_detail = "Connect Xero so invoices and bills can be created."

        def _conn_row(title, badge, detail, *, cfg_href, cfg_label, test_action=None):
            test_btn = ""
            if test_action:
                test_btn = (
                    f"<form method='post' action='{test_action}' class='inline'>"
                    "<input type='hidden' name='return_to' value='/receipts/expenses'>"
                    "<button type='submit' class='text-xs px-3 py-1.5 rounded-lg border "
                    "border-gray-300 text-gray-700 hover:bg-gray-50'>Test now</button></form>"
                )
            return (
                "<div class='flex flex-col sm:flex-row sm:items-center sm:justify-between "
                "gap-2 py-3 border-b border-gray-100 last:border-0'>"
                "<div class='min-w-0'>"
                f"<div class='flex items-center gap-2'><span class='font-medium text-gray-900 "
                f"text-sm'>{escape(title)}</span>{badge}</div>"
                f"<p class='text-xs text-gray-500 mt-0.5'>{detail}</p></div>"
                "<div class='flex items-center gap-2 shrink-0'>"
                f"{test_btn}"
                f"<a href='{cfg_href}' class='text-xs px-3 py-1.5 rounded-lg bg-indigo-600 "
                f"text-white'>{escape(cfg_label)}</a>"
                "</div></div>"
            )

        setup_all_ready = bool(docai_has_ids and sa_exists) and openai_has_key and xero_ok
        setup_summary_badge = (
            "<span class='text-xs font-semibold px-2 py-0.5 rounded-full "
            "bg-emerald-100 text-emerald-700'>Ready</span>"
            if setup_all_ready else
            "<span class='text-xs font-semibold px-2 py-0.5 rounded-full "
            "bg-amber-100 text-amber-700'>Check setup</span>"
        )
        connections_html = (
            "<details class='rounded-xl border border-gray-200 bg-white overflow-hidden group'>"
            "<summary class='flex items-center justify-between px-4 py-3 cursor-pointer "
            "list-none select-none hover:bg-gray-50'>"
            "<div class='flex items-center gap-2'>"
            "<h2 class='font-semibold text-gray-900'>Setup &amp; connections</h2>"
            f"{setup_summary_badge}</div>"
            "<svg class='w-4 h-4 text-gray-400 transition-transform duration-200 "
            "group-open:rotate-180 shrink-0' fill='none' stroke='currentColor' "
            "viewBox='0 0 24 24'><path stroke-linecap='round' stroke-linejoin='round' "
            "stroke-width='2' d='M19 9l-7 7-7-7'/></svg></summary>"
            "<div class='px-4 pb-4 pt-1 border-t border-gray-100'>"
            "<p class='text-xs text-gray-500 mb-2'>Everything the receipt-claim flow needs. "
            "Green means it's ready to use.</p>"
            + _conn_row(
                "Google receipt parser (Document AI)", docai_badge, docai_detail,
                cfg_href="/settings#receipts-parser", cfg_label="Configure",
                test_action="/test-document-ai-connection" if (docai_has_ids and sa_exists) else None,
            )
            + _conn_row(
                "AI categorisation (OpenAI)", openai_badge, openai_detail,
                cfg_href="/settings#openai", cfg_label="Configure",
                test_action="/test-openai-connection" if openai_has_key else None,
            )
            + _conn_row(
                "Xero accounting", xero_badge, xero_detail,
                cfg_href="/settings#xero", cfg_label="Configure",
            )
            + "</div></details>"
        )

        flash = request.args.get("flash", "")
        flash_n = request.args.get("n", "")
        flash_html = ""
        if flash:
            if flash == "card_changed":
                _from = session.pop("card_change_from", "")
                _to = session.pop("card_change_to", "")
                if _from and _to:
                    msg = f"Linked card updated from {_from} to {_to}."
                elif _to:
                    msg = f"Linked card set to {_to}."
                else:
                    msg = "Linked card updated."
            else:
                _flash_labels = {
                    "created": "Engineer created.",
                    "updated": "Saved.",
                    "settings": "Settings saved.",
                    "img_cleared": "Image file removed.",
                    "deleted": "Receipt deleted.",
                    "not_found": "Receipt not found.",
                    "username_taken": "That username is already in use by another person.",
                    "bulk_cleared": (
                        f"Images cleared from {flash_n} submitted/settled receipt(s)."
                        if flash_n else "Images cleared."
                    ),
                }
                msg = _flash_labels.get(flash, "Done.")
            err_flashes = {"not_found", "username_taken"}
            if flash in err_flashes:
                flash_html = (
                    "<div class='rounded-lg border border-red-200 bg-red-50 "
                    f"p-3 text-sm text-red-800'>{escape(msg)}</div>"
                )
            else:
                flash_html = (
                    "<div class='rounded-lg border border-emerald-200 bg-emerald-50 "
                    f"p-3 text-sm text-emerald-800'>&#10003; {escape(msg)}</div>"
                )

        # Plaid card accounts so each company-card engineer links to one card.
        try:
            _plaid_status = cardfeed.connection_status(db)
        except Exception:
            _plaid_status = {"connected": False}
        plaid_cards = (
            (_plaid_status.get("accounts") or [])
            if _plaid_status.get("connected") else []
        )

        try:
            _acct_labels = cardfeed.get_account_labels(db)
        except Exception:
            _acct_labels = {}

        def _card_options_html(selected):
            opts = ["<option value=''>— not linked —</option>"]
            for _a in plaid_cards:
                _aid = _a.get("account_id") or ""
                _mask = _a.get("mask") or _aid[-4:] if _aid else ""
                _lbl_meta = _acct_labels.get(_aid) or {}
                _xero_name = (_lbl_meta.get("xero_account_name") or "").strip()
                if _xero_name:
                    _label = f"{_xero_name} (••{_mask})" if _mask else _xero_name
                else:
                    _label = _a.get("name") or "Card"
                    if _mask:
                        _label = f"{_label} ••{_mask}"
                _sel = "selected" if _aid == (selected or "") else ""
                opts.append(
                    f"<option value='{escape(_aid)}' {_sel}>{escape(_label)}</option>"
                )
            return "".join(opts)

        def _card_label_for(account_id: str) -> str:
            """Return the friendly display name for a card account_id."""
            if not account_id:
                return "none"
            _lbl_meta = _acct_labels.get(account_id) or {}
            _xero_name = (_lbl_meta.get("xero_account_name") or "").strip()
            _mask = account_id[-4:] if account_id else ""
            if _xero_name:
                return f"{_xero_name} (••{_mask})"
            for _a in plaid_cards:
                if _a.get("account_id") == account_id:
                    _n = _a.get("name") or "Card"
                    return f"{_n} ••{_mask}" if _mask else _n
            return f"••{_mask}" if _mask else account_id

        eng_cards = []
        for e in engineers:
            link = f"{base_url}/expenses/{e['token']}"
            receipts = exp_store.list_receipts_for_engineer(db, e["id"])
            count = len(receipts)
            if receipts:
                _rcpt_lines = []
                for _r in receipts[:50]:
                    _m = escape(_r.get("merchant") or _r.get("ocr_merchant") or "Receipt")
                    _d = escape((_r.get("purchased_on") or _r.get("created_at") or "")[:10])
                    _amt = _exp_money(_r.get("amount_inc"), _r.get("currency") or "GBP")
                    _badge = _exp_status_badge(_r.get("status") or "")
                    _acct_name = (_r.get("category_account_name") or "").strip()
                    _acct_code = (_r.get("category_account_code") or "").strip()
                    if _acct_name or _acct_code:
                        _acct_disp = escape(" ".join(x for x in [_acct_code, _acct_name] if x))
                        _acct_html = (
                            "<span class='text-xs text-gray-500'>&rarr; "
                            f"{_acct_disp}</span>"
                        )
                    else:
                        _acct_html = (
                            "<span class='text-xs text-gray-400 italic'>not yet coded</span>"
                        )
                    _rcpt_lines.append(
                        "<div class='flex flex-wrap items-center justify-between gap-2 "
                        "py-2 border-b border-gray-100 last:border-0'>"
                        "<div class='min-w-0'>"
                        "<div class='flex items-center gap-2 flex-wrap'>"
                        f"<span class='font-medium text-gray-900 text-sm'>{_m}</span>{_badge}</div>"
                        f"<div class='mt-0.5'>{_acct_html}</div></div>"
                        "<div class='text-right shrink-0'>"
                        f"<div class='text-sm font-medium text-gray-900'>{_amt}</div>"
                        f"<div class='text-xs text-gray-400'>{_d}</div></div></div>"
                    )
                _more = (
                    f"<div class='text-xs text-gray-400 pt-1'>Showing latest 50 of {count}.</div>"
                    if count > 50 else ""
                )
                receipts_summary_html = (
                    "<details class='group/receipts rounded-lg border border-gray-200 bg-gray-50/50'>"
                    "<summary class='flex items-center justify-between gap-2 px-3 py-2 "
                    "cursor-pointer list-none select-none text-sm font-medium text-gray-700 "
                    "hover:bg-gray-100 rounded-lg'>"
                    f"<span>Submitted receipts &amp; AI coding ({count})</span>"
                    "<svg class='w-4 h-4 text-gray-400 transition-transform duration-200 "
                    "group-open/receipts:rotate-180 shrink-0' fill='none' stroke='currentColor' "
                    "viewBox='0 0 24 24'><path stroke-linecap='round' stroke-linejoin='round' "
                    "stroke-width='2' d='M19 9l-7 7-7-7'/></svg></summary>"
                    "<div class='px-3 pb-2'>"
                    + "".join(_rcpt_lines)
                    + _more
                    + "</div></details>"
                )
            else:
                receipts_summary_html = (
                    "<div class='rounded-lg border border-dashed border-gray-200 "
                    "bg-gray-50/50 px-3 py-2 text-xs text-gray-500'>"
                    "No receipts submitted yet.</div>"
                )
            kind = e.get("kind") or "company_card"
            kind_label = "Company card" if kind == "company_card" else "Subcontractor"
            owed_html = ""
            if kind == "subcontractor":
                _maybe_settle_subcontractor(e)
                owed = exp_store.amount_owed_to_engineer(db, e["id"])
                _setts = exp_store.list_settlements_for_engineer(db, e["id"])
                _pay_line = (
                    f"<span class='text-xs text-indigo-700 font-medium'>Pay "
                    f"{_exp_money(owed)} to settle</span>"
                    if owed > 0 else
                    "<span class='text-xs text-emerald-700 font-medium'>"
                    "Fully settled</span>"
                )
                _paid = ""
                if _setts and (_setts[0].get("paid_on") or ""):
                    _paid = (
                        "<span class='text-xs text-gray-500'>· last paid "
                        f"{escape(str(_setts[0].get('paid_on'))[:10])}</span>"
                    )
                _warn = ""
                _last_note = (_setts[0].get("note") or "").strip() if _setts else ""
                if _last_note:
                    _warn = (
                        "<div class='mt-1 text-xs rounded-md border "
                        "border-amber-300 bg-amber-50 text-amber-800 px-2 py-1'>"
                        f"&#9888; {escape(_last_note)}</div>"
                    )
                owed_html = (
                    f"{_pay_line}"
                    f"<span class='text-xs text-gray-400'>· ref "
                    f"<span class='font-mono'>{escape(_subcontractor_reference(e))}</span></span>"
                    f"{_paid}{_warn}"
                )
            active = bool(e.get("active"))
            sel_card = "selected" if kind == "company_card" else ""
            sel_sub = "selected" if kind == "subcontractor" else ""
            _card_style = "" if kind == "company_card" else "display:none"
            active_checked = "checked" if active else ""
            inactive_badge = (
                "" if active
                else "<span class='text-xs px-2 py-0.5 rounded-full bg-gray-200 "
                "text-gray-600'>Inactive</span>"
            )
            _sel_cls = "w-full rounded border border-gray-300 px-2 py-1 text-sm"
            if exp_accounts:
                exp_field = (
                    f"<select name='expense_account_code' class='{_sel_cls}'>"
                    + _exp_acct_options(
                        exp_accounts, e.get("expense_account_code") or "",
                        default_label="Use default account",
                    )
                    + "</select>"
                )
            else:
                exp_field = (
                    f"<input name='expense_account_code' "
                    f"value='{escape(e.get('expense_account_code') or '')}' "
                    f"class='{_sel_cls}' placeholder='blank = default'>"
                )
            if bank_accounts:
                pay_field = (
                    f"<select name='payment_account_code' class='{_sel_cls}'>"
                    + _exp_acct_options(
                        bank_accounts, e.get("payment_account_code") or "",
                        default_label="Use default account",
                    )
                    + "</select>"
                )
            else:
                pay_field = (
                    f"<input name='payment_account_code' "
                    f"value='{escape(e.get('payment_account_code') or '')}' "
                    f"class='{_sel_cls}' placeholder='blank = default'>"
                )
            owner_checked = "checked" if e.get("allow_owner_paid") else ""
            owner_account_code = e.get("owner_paid_account_code") or ""
            if owner_paid_accounts:
                owner_account_field = (
                    f"<select name='owner_paid_account_code' class='{_sel_cls} bg-white'>"
                    + _exp_acct_options(
                        owner_paid_accounts, owner_account_code,
                        default_label="Choose Xero account",
                    )
                    + "</select>"
                )
            else:
                owner_account_field = (
                    f"<input name='owner_paid_account_code' "
                    f"value='{escape(owner_account_code)}' "
                    f"class='{_sel_cls}' placeholder='e.g. Ben personal account code'>"
                )
            owner_picker_hint = (
                "Choose the Xero account these personal receipts should post to."
                if owner_paid_accounts else
                "Xero account options are unavailable right now; type the account code here."
            )
            has_pw = bool((e.get("password_hash") or "").strip())
            pw_hint = (
                "<span class='text-emerald-600 font-normal'>(set)</span>" if has_pw
                else "<span class='text-amber-600 font-normal'>(not set)</span>"
            )
            if plaid_cards:
                card_field = (
                    f"<select name='plaid_account_id' class='{_sel_cls}'>"
                    + _card_options_html(e.get("plaid_account_id") or "")
                    + "</select>"
                )
            else:
                card_field = (
                    "<input type='hidden' name='plaid_account_id' "
                    f"value='{escape(e.get('plaid_account_id') or '')}'>"
                    "<p class='text-xs text-gray-400 mt-1'>Connect the company bank on the "
                    "<a href='/cardfeed' class='text-indigo-600'>Card feed</a> page to link a card.</p>"
                )
            eng_cards.append(
                "<details class='rounded-xl border border-gray-200 bg-white overflow-hidden group'>"
                "<summary class='flex items-center justify-between gap-2 p-4 cursor-pointer "
                "list-none select-none hover:bg-gray-50'>"
                f"<div class='font-semibold text-gray-900 flex items-center gap-2'>{escape(e['name'])} "
                f"{inactive_badge}</div>"
                "<div class='flex items-center gap-2'>"
                f"<span class='text-xs text-gray-500'>{count} receipt(s)</span>"
                "<svg class='w-4 h-4 text-gray-400 transition-transform duration-200 "
                "group-open:rotate-180 shrink-0' fill='none' stroke='currentColor' "
                "viewBox='0 0 24 24'><path stroke-linecap='round' stroke-linejoin='round' "
                "stroke-width='2' d='M19 9l-7 7-7-7'/></svg></div></summary>"
                "<div class='px-4 pb-4 pt-1 border-t border-gray-100 space-y-3'>"
                "<div class='flex items-center gap-2'>"
                f"<input readonly value='{escape(link)}' "
                "class='flex-1 text-xs bg-gray-50 border border-gray-200 rounded px-2 py-1 "
                f"text-gray-600' id='lnk-{e['id']}'>"
                f"<button type='button' onclick=\"navigator.clipboard.writeText('{escape(link)}')\" "
                "class='text-xs px-2 py-1 rounded bg-indigo-600 text-white'>Copy</button>"
                f"<a href='{escape(link)}' target='_blank' "
                "class='text-xs px-2 py-1 rounded border border-gray-300 text-gray-700'>Open</a>"
                "</div>"
                f"<div class='flex items-center gap-3 text-sm'><span>{escape(kind_label)}</span>{owed_html}</div>"
                f"{receipts_summary_html}"
                f"<form method='post' action='/receipts/expenses/{e['id']}/update' "
                "class='grid grid-cols-1 sm:grid-cols-2 gap-2 pt-2 border-t border-gray-100'>"
                "<div><label class='block text-xs text-gray-500 mb-1'>Type</label>"
                f"<select name='kind' onchange=\"document.getElementById('card-{e['id']}').style.display=this.value==='company_card'?'block':'none'\" "
                "class='w-full rounded border border-gray-300 px-2 py-1 text-sm'>"
                f"<option value='company_card' {sel_card}>Company card</option>"
                f"<option value='subcontractor' {sel_sub}>Subcontractor</option>"
                "</select></div>"
                f"<div class='sm:col-span-2' id='card-{e['id']}' style='{_card_style}'>"
                "<label class='block text-xs text-gray-500 mb-1'>"
                "Linked card <span class='text-gray-400 font-normal'>(which Plaid card does this person hold?)</span></label>"
                f"{card_field}</div>"
                "<div><label class='block text-xs text-gray-500 mb-1'>Name</label>"
                f"<input name='name' value='{escape(e['name'])}' "
                "class='w-full rounded border border-gray-300 px-2 py-1 text-sm'></div>"
                "<div><label class='block text-xs text-gray-500 mb-1'>Xero contact name</label>"
                f"<input name='xero_contact_name' value='{escape(e.get('xero_contact_name') or '')}' "
                "class='w-full rounded border border-gray-300 px-2 py-1 text-sm' "
                "placeholder='e.g. John Smith'></div>"
                "<div><label class='block text-xs text-gray-500 mb-1'>Expense account "
                "<span class='text-gray-400 font-normal'>(default for this person)</span></label>"
                f"{exp_field}</div>"
                "<div><label class='block text-xs text-gray-500 mb-1'>Payment/bank account</label>"
                f"{pay_field}</div>"
                "<div class='sm:col-span-2 rounded-lg border border-emerald-300 bg-emerald-50 p-3 space-y-2'>"
                "<label class='flex items-start gap-2 text-sm font-semibold text-emerald-950'>"
                f"<input type='checkbox' name='allow_owner_paid' value='1' {owner_checked} class='mt-1'> "
                "<span>Allow personal / owner-paid receipt option</span></label>"
                "<p class='text-xs text-emerald-700 mt-1'>Only enabled engineers will see a personal-card choice on the photo screen.</p>"
                "<div class='rounded-md border border-emerald-200 bg-white/80 p-2'>"
                "<label class='block text-xs font-semibold text-emerald-900 mb-1'>"
                "Xero account to post these personal receipts to</label>"
                f"{owner_account_field}"
                f"<p class='text-[11px] text-emerald-700 mt-1'>{escape(owner_picker_hint)}</p>"
                "</div></div>"
                "<div><label class='block text-xs text-gray-500 mb-1'>Login username</label>"
                f"<input name='username' value='{escape(e.get('username') or '')}' "
                "autocapitalize='none' class='w-full rounded border border-gray-300 px-2 py-1 "
                "text-sm' placeholder='e.g. dave'></div>"
                "<div><label class='block text-xs text-gray-500 mb-1'>Set / reset password "
                f"{pw_hint}</label>"
                "<input name='password' type='text' value='' autocomplete='new-password' "
                "class='w-full rounded border border-gray-300 px-2 py-1 text-sm' "
                "placeholder='blank = leave unchanged'></div>"
                "<label class='flex items-center gap-2 text-sm mt-5'>"
                f"<input type='checkbox' name='active' value='1' {active_checked}> Active</label>"
                "<div class='sm:col-span-2'>"
                "<button type='submit' class='px-3 py-1.5 rounded bg-gray-900 text-white text-sm'>"
                "Save</button></div>"
                "</form>"
                "</div>"
                "</details>"
            )
        engineers_html = (
            "".join(eng_cards)
            if eng_cards
            else "<div class='rounded-xl border border-dashed border-gray-300 bg-white "
            "p-6 text-center text-sm text-gray-500'>No engineers yet. Add one below.</div>"
        )

        _set_cls = "w-full rounded border border-gray-300 px-3 py-2 text-sm"
        if exp_accounts:
            default_exp_field = (
                f"<select name='default_expense_account' class='{_set_cls}'>"
                + _exp_acct_options(
                    exp_accounts, settings["default_expense_account"],
                    default_label="— none —",
                )
                + "</select>"
            )
        else:
            default_exp_field = (
                f"<input name='default_expense_account' "
                f"value='{escape(settings['default_expense_account'])}' "
                f"class='{_set_cls}' placeholder='e.g. 320'>"
            )
        if bank_accounts:
            default_pay_field = (
                f"<select name='default_payment_account' class='{_set_cls}'>"
                + _exp_acct_options(
                    bank_accounts, settings["default_payment_account"],
                    default_label="— none —",
                )
                + "</select>"
            )
        else:
            default_pay_field = (
                f"<input name='default_payment_account' "
                f"value='{escape(settings['default_payment_account'])}' "
                f"class='{_set_cls}' placeholder='e.g. 090'>"
            )

        # ── Storage section ─────────────────────────────────────────────────
        receipts_with_images = exp_store.list_receipts_with_images(db)
        eng_by_id = {e["id"]: e for e in engineers}
        done_statuses = {"submitted", "settled", "failed"}
        clearable_count = sum(
            1 for r in receipts_with_images if r.get("status") in done_statuses
        )

        if receipts_with_images:
            bulk_btn = ""
            if clearable_count:
                bulk_btn = (
                    "<form method='post' action='/receipts/expenses/bulk-clear-images' "
                    "class='inline' onsubmit=\"return confirm('Remove image files from all "
                    f"{clearable_count} submitted/settled/failed receipt(s)? "
                    "The receipt records will be kept.')\">"
                    "<button type='submit' class='text-xs px-3 py-1.5 rounded-lg border "
                    "border-amber-300 bg-amber-50 text-amber-800 hover:bg-amber-100'>"
                    f"Clear {clearable_count} image(s)</button></form>"
                )

            receipt_rows = []
            return_to = "/receipts/expenses"
            for r in receipts_with_images:
                eng = eng_by_id.get(r.get("engineer_id") or 0)
                eng_name = escape(eng["name"]) if eng else "—"
                merchant = escape(r.get("merchant") or r.get("ocr_merchant") or "Receipt")
                date_str = escape((r.get("purchased_on") or r.get("created_at") or "")[:10])
                amount = _exp_money(r.get("amount_inc"), r.get("currency") or "GBP")
                status = r.get("status") or ""
                badge = _exp_status_badge(status)
                rid = r["id"]
                img_btn = (
                    "<form method='post' "
                    f"action='/receipts/expenses/receipt/{rid}/delete-image' "
                    "class='inline'>"
                    f"<input type='hidden' name='return_to' value='{return_to}'>"
                    "<button type='submit' class='text-xs px-2 py-1 rounded border "
                    "border-gray-300 text-gray-600 hover:bg-gray-50' "
                    "title='Remove image file, keep record'>Clear image</button></form>"
                )
                del_btn = (
                    "<form method='post' "
                    f"action='/receipts/expenses/receipt/{rid}/delete' "
                    "class='inline' "
                    f"onsubmit=\"return confirm('Delete this receipt record for {merchant}? "
                    "This cannot be undone.')\">"
                    f"<input type='hidden' name='return_to' value='{return_to}'>"
                    "<button type='submit' class='text-xs px-2 py-1 rounded border "
                    "border-red-200 text-red-600 hover:bg-red-50'>Delete</button></form>"
                )
                receipt_rows.append(
                    "<div class='flex flex-wrap items-center justify-between gap-2 "
                    "py-2.5 border-b border-gray-100 last:border-0'>"
                    "<div class='min-w-0 flex-1'>"
                    f"<div class='flex items-center gap-2 flex-wrap'>"
                    f"<span class='font-medium text-gray-900 text-sm'>{merchant}</span>"
                    f"{badge}"
                    f"<span class='text-xs text-gray-400'>{eng_name}</span>"
                    "</div>"
                    f"<div class='text-xs text-gray-500 mt-0.5'>{date_str} &middot; {amount}</div>"
                    "</div>"
                    f"<div class='flex items-center gap-1.5 shrink-0'>{img_btn}{del_btn}</div>"
                    "</div>"
                )

            storage_html = (
                "<section class='rounded-xl border border-gray-200 bg-white p-4'>"
                "<div class='flex items-center justify-between mb-1'>"
                "<h2 class='font-semibold text-gray-900'>Receipt images</h2>"
                f"{bulk_btn}"
                "</div>"
                "<p class='text-xs text-gray-500 mb-3'>"
                f"{len(receipts_with_images)} receipt(s) still have a stored image file. "
                "Clearing an image frees disk space — the receipt record and Xero entry "
                "are not affected.</p>"
                + "".join(receipt_rows)
                + "</section>"
            )
        else:
            storage_html = (
                "<section class='rounded-xl border border-dashed border-gray-200 "
                "bg-white p-4 text-sm text-gray-500'>"
                "No stored receipt images &mdash; nothing to clean up.</section>"
            )

        # Card field for the "Add an engineer" create form (shown/hidden by JS).
        # "Company card" is the default, so this starts VISIBLE and is hidden
        # when the user switches to Subcontractor.
        if plaid_cards:
            _create_card_inner = (
                "<select name='plaid_account_id' "
                "class='w-full rounded border border-gray-300 px-3 py-2 text-sm'>"
                + _card_options_html("")
                + "</select>"
            )
        else:
            _create_card_inner = (
                "<p class='text-xs text-gray-500'>No bank connected yet &mdash; "
                "<a href='/cardfeed' class='text-indigo-600'>connect one on the Card feed page</a>, "
                "then come back to link a card.</p>"
            )
        _create_card_field_html = (
            "<div class='sm:col-span-3' id='create-card-row'>"
            "<label class='block text-xs text-gray-500 mb-1'>Linked card "
            "<span class='text-gray-400 font-normal'>"
            "(company-card staff only)</span></label>"
            + _create_card_inner
            + "</div>"
        )
        if owner_paid_accounts:
            _create_owner_account_inner = (
                "<select name='owner_paid_account_code' "
                "class='w-full rounded border border-gray-300 bg-white px-3 py-2 text-sm'>"
                + _exp_acct_options(
                    owner_paid_accounts, "",
                    default_label="Choose Xero account",
                )
                + "</select>"
            )
        else:
            _create_owner_account_inner = (
                "<input name='owner_paid_account_code' "
                "class='w-full rounded border border-gray-300 px-3 py-2 text-sm' "
                "placeholder='e.g. Ben personal account code'>"
            )
        _create_owner_picker_hint = (
            "Choose the Xero account these personal receipts should post to."
            if owner_paid_accounts else
            "Xero account options are unavailable right now; type the account code here."
        )
        _create_owner_field_html = (
            "<div class='sm:col-span-3 rounded-lg border border-emerald-300 bg-emerald-50 p-3 space-y-2'>"
            "<label class='flex items-start gap-2 text-sm font-semibold text-emerald-950'>"
            "<input type='checkbox' name='allow_owner_paid' value='1' class='mt-1'>"
            "<span>Allow personal / owner-paid receipt option</span></label>"
            "<p class='text-xs text-emerald-700 mt-1'>Use this only for people who should be able to submit receipts paid from a non-company card.</p>"
            "<div class='rounded-md border border-emerald-200 bg-white/80 p-2'>"
            "<label class='block text-xs font-semibold text-emerald-900 mb-1'>"
            "Xero account to post these personal receipts to</label>"
            + _create_owner_account_inner
            + f"<p class='text-[11px] text-emerald-700 mt-1'>{escape(_create_owner_picker_hint)}</p>"
            + "</div></div>"
        )

        return _page(
            f"""
            <header class="bg-white border-b border-gray-200">
              <div class="max-w-4xl mx-auto px-4 py-3 flex items-center gap-4">
                <a href="/" class="text-sm text-indigo-600">&larr; Dashboard</a>
                <a href="/receipts" class="text-sm text-gray-600">Receipts</a>
                <span class="text-sm font-semibold text-gray-900">Field Expenses</span>
              </div>
            </header>
            <main class="max-w-4xl mx-auto p-4 space-y-6">
              {flash_html}
              {acct_warning_html}

              <div class="space-y-2">
                <a href="/receipts/expenses/dump"
                   class="flex items-center justify-center px-4 py-3 rounded-xl bg-emerald-600 hover:bg-emerald-700 text-white text-sm font-semibold shadow-sm">
                  Receipt dump
                </a>
                <a href="/cardfeed"
                   class="flex items-center justify-center gap-2 px-4 py-3 rounded-xl bg-white border border-gray-200 hover:bg-gray-50 text-gray-700 text-sm font-semibold shadow-sm">
                  <svg class="w-4 h-4 text-gray-500" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-8l-4-4m0 0L8 8m4-4v12"/></svg>
                  Upload bank statement (CSV)
                </a>
                <div class="text-center">
                  <a href="/receipts/expenses/test"
                     class="inline-flex items-center gap-1.5 text-xs text-gray-500 hover:text-gray-700">
                    <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2m-3 7h3m-3 4h3m-6-4h.01M9 16h.01"/></svg>
                    Test receipt scanner
                  </a>
                </div>
              </div>

              {connections_html}

              <section class="space-y-3">
                <h2 class="text-sm font-semibold text-gray-500 uppercase tracking-wide">Engineers</h2>
                {engineers_html}
              </section>

              <section class="rounded-xl border border-gray-200 bg-white p-4">
                <h2 class="font-semibold text-gray-900 mb-3">Add an engineer</h2>
                <form method="post" action="/receipts/expenses/create"
                      class="grid grid-cols-1 sm:grid-cols-3 gap-3">
                  <div class="sm:col-span-2">
                    <label class="block text-xs text-gray-500 mb-1">Name</label>
                    <input name="name" required
                           class="w-full rounded border border-gray-300 px-3 py-2 text-sm"
                           placeholder="e.g. Dave Jones">
                  </div>
                  <div>
                    <label class="block text-xs text-gray-500 mb-1">Type</label>
                    <select name="kind" onchange="document.getElementById('create-card-row').style.display=this.value==='company_card'?'block':'none'" class="w-full rounded border border-gray-300 px-3 py-2 text-sm">
                      <option value="company_card">Company card</option>
                      <option value="subcontractor">Subcontractor</option>
                    </select>
                  </div>
                  {_create_card_field_html}
                  {_create_owner_field_html}
                  <div class="sm:col-span-3">
                    <button type="submit"
                            class="px-4 py-2 rounded bg-indigo-600 text-white text-sm font-medium">
                      Create &amp; get link
                    </button>
                  </div>
                </form>
              </section>

              {storage_html}

              <details class="rounded-xl border border-gray-200 bg-white overflow-hidden group">
                <summary class="flex items-center justify-between px-4 py-3 cursor-pointer list-none select-none hover:bg-gray-50">
                  <h2 class="font-semibold text-gray-900">Settings</h2>
                  <svg class="w-4 h-4 text-gray-400 transition-transform duration-200 group-open:rotate-180 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"/></svg>
                </summary>
                <form method="post" action="/receipts/expenses/save-settings"
                      class="grid grid-cols-1 sm:grid-cols-3 gap-3 px-4 pb-4 pt-2 border-t border-gray-100">
                  <div>
                    <label class="block text-xs text-gray-500 mb-1">Default VAT rate (%)</label>
                    <input name="vat_rate" type="number" step="0.1" value="{settings['vat_rate']}"
                           class="w-full rounded border border-gray-300 px-3 py-2 text-sm">
                  </div>
                  <div>
                    <label class="block text-xs text-gray-500 mb-1">Fallback expense account</label>
                    {default_exp_field}
                    <p class="text-xs text-gray-400 mt-1">Only used as a fallback when the AI can&rsquo;t work out a receipt&rsquo;s account &mdash; those receipts are held for you to confirm or change before importing.</p>
                  </div>
                  <div>
                    <label class="block text-xs text-gray-500 mb-1">Default payment/bank account</label>
                    {default_pay_field}
                  </div>
                  <div class="sm:col-span-3">
                    <button type="submit"
                            class="px-4 py-2 rounded bg-gray-900 text-white text-sm font-medium">
                      Save settings
                    </button>
                  </div>
                </form>
              </details>
            </main>
            """
        )

    @app.post("/receipts/expenses/create")
    @require_login
    def expense_admin_create():
        db = config.admin_db_file
        name = (request.form.get("name") or "").strip()
        kind = (request.form.get("kind") or "company_card").strip()
        if not name:
            return redirect("/receipts/expenses")
        eng = exp_store.create_engineer(
            db,
            name=name,
            kind=kind,
            allow_owner_paid=1 if request.form.get("allow_owner_paid") else 0,
            owner_paid_account_code=(
                request.form.get("owner_paid_account_code") or ""
            ).strip(),
        )
        plaid_account_id = (request.form.get("plaid_account_id") or "").strip()
        if plaid_account_id and kind == "company_card":
            exp_store.update_engineer(db, eng["id"], plaid_account_id=plaid_account_id)
        return redirect("/receipts/expenses?flash=created")

    @app.post("/receipts/expenses/<int:engineer_id>/update")
    @require_login
    def expense_admin_update(engineer_id: int):
        db = config.admin_db_file
        username = (request.form.get("username") or "").strip()
        if username and exp_store.username_taken(db, username, exclude_id=engineer_id):
            return redirect("/receipts/expenses?flash=username_taken")
        # Capture old engineer state to detect card changes.
        try:
            old_eng = exp_store.get_engineer(db, engineer_id) or {}
        except Exception:
            old_eng = {}
        old_card = (old_eng.get("plaid_account_id") or "").strip()
        new_card = (request.form.get("plaid_account_id") or "").strip()
        updates = dict(
            name=(request.form.get("name") or "").strip(),
            kind=(request.form.get("kind") or "company_card").strip(),
            xero_contact_name=(request.form.get("xero_contact_name") or "").strip(),
            expense_account_code=(request.form.get("expense_account_code") or "").strip(),
            payment_account_code=(request.form.get("payment_account_code") or "").strip(),
            plaid_account_id=new_card,
            allow_owner_paid=1 if request.form.get("allow_owner_paid") else 0,
            owner_paid_account_code=(
                request.form.get("owner_paid_account_code") or ""
            ).strip(),
            username=username,
            active=1 if request.form.get("active") else 0,
        )
        new_password = (request.form.get("password") or "").strip()
        if new_password:
            updates["password_hash"] = generate_password_hash(new_password)
        exp_store.update_engineer(db, engineer_id, **updates)
        # Build a card-change notice when the linked card changed.
        if old_card != new_card:
            try:
                _labels = cardfeed.get_account_labels(db)
                def _card_disp(cid):
                    if not cid:
                        return "none"
                    lm = _labels.get(cid) or {}
                    nm = (lm.get("xero_account_name") or "").strip()
                    mask = cid[-4:] if cid else ""
                    return f"{nm} (••{mask})" if nm else (f"••{mask}" if mask else cid)
                session["card_change_from"] = _card_disp(old_card)
                session["card_change_to"] = _card_disp(new_card)
            except Exception:
                pass
            return redirect("/receipts/expenses?flash=card_changed")
        return redirect("/receipts/expenses?flash=updated")

    @app.post("/receipts/expenses/save-settings")
    @require_login
    def expense_admin_save_settings():
        db = config.admin_db_file
        try:
            vat_rate = float((request.form.get("vat_rate") or "20").strip())
        except ValueError:
            vat_rate = 20.0
        set_expense_settings(
            db,
            {
                "vat_rate": vat_rate,
                "default_expense_account": (
                    request.form.get("default_expense_account") or ""
                ).strip(),
                "default_payment_account": (
                    request.form.get("default_payment_account") or ""
                ).strip(),
            },
        )
        return redirect("/receipts/expenses?flash=settings")

    def _exp_safe_remove_file(db: str, stored_file: str) -> bool:
        """Remove *stored_file* from disk only when no dump item or expense receipt still
        references it.  Returns True if the file was actually removed."""
        if not stored_file:
            return False
        if dump_store.stored_file_in_use(db, stored_file):
            return False
        if exp_store.stored_file_in_use(db, stored_file):
            return False
        try:
            os.remove(os.path.abspath(stored_file))
            return True
        except OSError:
            return False

    @app.post("/receipts/expenses/receipt/<rid>/delete-image")
    @require_login
    def expense_receipt_delete_image(rid: str):
        """Clear the stored image for a single receipt, freeing disk space.

        The DB record is kept intact.  The file is only removed from disk when
        no other expense receipt or dump item still references the same path.
        """
        db = config.admin_db_file
        rec = exp_store.get_receipt(db, rid)
        if not rec:
            return redirect("/receipts/expenses?flash=not_found")
        old_file = rec.get("stored_file") or ""
        exp_store.clear_receipt_stored_file(db, rid)
        _exp_safe_remove_file(db, old_file)
        return_to = request.form.get("return_to") or "/receipts/expenses"
        return redirect(return_to + ("&" if "?" in return_to else "?") + "flash=img_cleared")

    @app.post("/receipts/expenses/receipt/<rid>/delete")
    @require_login
    def expense_receipt_delete(rid: str):
        """Delete a single expense receipt row and its image file (when safe).

        The image file is only removed from disk when no other expense receipt
        or dump item references the same path.
        """
        db = config.admin_db_file
        rec = exp_store.get_receipt(db, rid)
        if not rec:
            return redirect("/receipts/expenses?flash=not_found")
        stored_file = rec.get("stored_file") or ""
        exp_store.delete_receipt(db, rid)
        _exp_safe_remove_file(db, stored_file)
        return_to = request.form.get("return_to") or "/receipts/expenses"
        return redirect(return_to + ("&" if "?" in return_to else "?") + "flash=deleted")

    @app.post("/receipts/expenses/bulk-clear-images")
    @require_login
    def expense_bulk_clear_images():
        """Clear stored image files from all submitted and settled receipts.

        The DB record stays — only the on-disk image is freed.  Files shared
        with other receipts or dump items are skipped (not orphaned yet).
        """
        db = config.admin_db_file
        all_with_images = exp_store.list_receipts_with_images(db)
        done_statuses = {"submitted", "settled", "failed"}
        cleared = 0
        for rec in all_with_images:
            if (rec.get("status") or "") not in done_statuses:
                continue
            old_file = exp_store.clear_receipt_stored_file(db, rec["id"])
            if old_file:
                _exp_safe_remove_file(db, old_file)
                cleared += 1
        return redirect(f"/receipts/expenses?flash=bulk_cleared&n={cleared}")

    @app.post("/test-xero-webhook")
    @require_login
    def test_xero_webhook():
        """
        Manual test for Xero webhook handling from settings page.
        1) Sends an intent-style ping (events=[]) to verify route/signature path.
        2) Sends one sample INVOICE event using a known mapped invoice id (if available).
        """
        import hmac as _hmac
        import hashlib as _hashlib

        webhook_key = get_xero_webhook_key(config.admin_db_file)
        base_url = request.host_url.rstrip("/")  # keep local http for localhost dev
        webhook_url = f"{base_url}/webhooks/xero"

        def _post_payload(payload: dict) -> requests.Response:
            raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            headers = {"Content-Type": "application/json"}
            if webhook_key:
                sig = base64.b64encode(
                    _hmac.new(webhook_key.encode("utf-8"), raw, _hashlib.sha256).digest()
                ).decode("utf-8")
                headers["x-xero-signature"] = sig
            return requests.post(webhook_url, data=raw, headers=headers, timeout=12)

        try:
            ping_resp = _post_payload({"events": []})
            if ping_resp.status_code != 200:
                session["save_notice"] = (
                    f"error:Webhook test failed (intent ping HTTP {ping_resp.status_code})."
                )
                return redirect(url_for("index"))

            # Optional sample invoice event to exercise paid/settled path.
            app_state = load_state(config.state_file)
            inv_map = app_state.get("event_invoice_map", {}) or {}
            sample_invoice_id = next(
                (v for v in reversed(list(inv_map.values())) if str(v).strip()),
                "",
            )
            if sample_invoice_id:
                _post_payload(
                    {
                        "events": [
                            {
                                "resourceId": sample_invoice_id,
                                "eventCategory": "INVOICE",
                                "eventType": "UPDATE",
                            }
                        ]
                    }
                )
                session["save_notice"] = (
                    "success:Xero webhook test passed. Intent ping accepted and sample invoice event submitted."
                )
            else:
                session["save_notice"] = (
                    "success:Xero webhook test passed. Intent ping accepted."
                )
        except Exception as exc:
            session["save_notice"] = f"error:Xero webhook test failed: {str(exc)[:220]}"
        return redirect(url_for("index"))

    # ── Receipt Dump (bulk past-receipt upload & reconciliation) ────────────

    def _dump_tooltip(text: str) -> str:
        """A subtle inline '?' help marker with a hover tooltip."""
        return (
            "<span class='inline-flex items-center justify-center w-4 h-4 ml-1 "
            "text-[10px] font-bold text-gray-400 border border-gray-300 rounded-full "
            "cursor-help align-middle' title='" + escape(text) + "'>?</span>"
        )

    def _dump_help_panel() -> str:
        steps = [
            ("Upload a batch", "Drag in many past receipt photos at once. Pick the "
             "person/card they belong to. Optionally enter a subcontractor account "
             "number to balance their batch."),
            ("AI reads & codes each one", "Every receipt is OCR'd and the AI matches "
             "it to the right Xero expense account (splitting fuel + food etc.)."),
            ("Duplicates are removed", "Identical images, and receipts already "
             "submitted or reconciled, are skipped automatically."),
            ("Suspicious cross-claims are checked", "If a receipt looks like one "
             "already claimed by someone else, the two images are compared to "
             "confirm whether it's genuinely the same receipt."),
            ("Card-feed gaps are flagged", "Receipts that should be on a card feed "
             "but aren't get listed so you can say which account paid them "
             "(e.g. a personal card)."),
            ("Review & import", "Check the groups, fix any accounts, then import the "
             "clean receipts into Field Expenses."),
        ]
        items = "".join(
            "<li class='mb-2'><span class='font-semibold text-gray-800'>"
            + escape(t) + ".</span> <span class='text-gray-600'>" + escape(d)
            + "</span></li>"
            for t, d in steps
        )
        return (
            "<details class='rounded-xl border border-indigo-200 bg-indigo-50/60 "
            "p-4 mb-6'>"
            "<summary class='cursor-pointer text-sm font-semibold text-indigo-800 "
            "select-none'>How the Receipt Dump works (click to open guide)</summary>"
            "<ol class='list-decimal ml-5 mt-3 text-sm'>" + items + "</ol>"
            "</details>"
        )

    # Generic stop-words that carry no identity for a merchant ("Esso Service
    # Station" vs "Esso") so a token overlap means a genuine merchant match.
    _MERCH_STOP = {
        "service", "services", "station", "stations", "limited", "ltd",
        "commercial", "repairs", "repair", "garage", "petrol", "fuel",
        "road", "street", "the", "and", "plc", "uk", "gb", "store",
        "stores", "supermarket", "shop", "filling", "motors",
    }

    def _norm_merch(s) -> str:
        return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()

    def _merch_match(a, b) -> bool:
        """True when two merchant/contact names plausibly refer to the same
        business. Lenient on purpose: receipt OCR names and the bank-feed
        contact name rarely match exactly (e.g. "Rupeyal Service Station" vs
        "Rupeyal Service Station & Rup", "E&M COMMERCIAL REPAIRS" vs "E&M
        Commercial Repairs", "MFG Gailey Service Station" vs "Mfg")."""
        na, nb = _norm_merch(a), _norm_merch(b)
        if not na or not nb:
            return False
        if na in nb or nb in na:
            return True
        ta = {t for t in na.split() if len(t) >= 4 and t not in _MERCH_STOP}
        tb = {t for t in nb.split() if len(t) >= 4 and t not in _MERCH_STOP}
        return bool(ta & tb)

    def _dump_card_feed_check(engineer, amount_inc, purchased_on,
                              card_account: str = "") -> str:
        """Check whether a card-paid receipt appears in the Xero card feed.

        Returns '' (n/a), 'skipped' (Xero paused), 'matched' or 'missing'.
        Real Xero call, fully gated by the global kill-switch.

        ``card_account`` is the human-readable name of the card account for
        this batch (e.g. "Charge Card - Dan").  When provided, only SPEND
        transactions on that account are considered, so receipts from other
        accounts can't produce a false match — and the log shows which
        account was searched.

        NOTE: this reads Xero's BankTransactions API, which only returns SPEND
        transactions that have been entered or reconciled. Card-feed statement
        lines that haven't been reconciled yet are NOT returned by this API, so
        a "missing" result can simply mean "not reconciled in Xero yet".
        Tolerances kept in step with the reconciliation preview (±£1, ±10 days).
        """
        if not amount_inc or not engineer or engineer.get("kind") != "company_card":
            return ""
        # Plaid (the card's REAL bank feed) is the primary, Xero-independent
        # source: it sees the card payments Xero won't expose until they are
        # reconciled. Only a CONFIDENT single match counts as "matched" — an
        # ambiguous "review" (several same-priced payments, or a weak name match)
        # is deliberately treated as not-on-feed so the receipt is surfaced for a
        # human to check rather than silently waved through. The richer, name-aware
        # ambiguity handling still happens in the reconciliation preview.
        if cardfeed.is_connected(config.admin_db_file):
            txs = cardfeed.get_cached_transactions(config.admin_db_file)
            status = plaid_match.match_summary(amount_inc, purchased_on, "", txs)
            if status == "matched":
                return "matched"
            return "missing" if status in ("review", "missing") else ""
        if xero_is_disabled():
            return "skipped"
        try:
            d = dt.date.fromisoformat((purchased_on or "").strip())
        except (ValueError, TypeError):
            return ""
        try:
            client = build_xero_client(config)
            payload = client.get_bank_transactions(
                start_date=d - dt.timedelta(days=10),
                end_date=d + dt.timedelta(days=10),
            )
            raw = (payload or {}).get("BankTransactions") or []
            n_spend = 0
            n_acct_filtered = 0
            _na = lambda s: re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()
            c_norm = _na(card_account) if card_account else ""
            for t in raw:
                if str(t.get("Type") or "").upper() != "SPEND":
                    continue
                n_spend += 1
                # When a specific card account was chosen for this batch, only
                # check transactions on that account.  This prevents amounts that
                # happen to match on a completely different account (e.g. the
                # current account) from producing a false "matched" result.
                if c_norm:
                    acct_name = (t.get("BankAccount") or {}).get("Name") or ""
                    a = _na(acct_name)
                    if a and c_norm not in a and a not in c_norm:
                        n_acct_filtered += 1
                        continue
                try:
                    if abs(float(t.get("Total") or 0) - float(amount_inc)) <= 1.00:
                        return "matched"
                except (TypeError, ValueError):
                    continue
            print(f"[card-feed-check] amount={amount_inc} date={d} "
                  f"card_account={card_account!r}: "
                  f"{len(raw)} tx in window, {n_spend} SPEND, "
                  f"{n_acct_filtered} filtered to other accounts, "
                  f"no match -> missing",
                  flush=True)
            return "missing"
        except Exception as e:
            print(f"[card-feed-check] error: {e}", flush=True)
            return ""

    def _xero_date_to_iso(value) -> str:
        """Parse Xero's ``/Date(1234567890000+0000)/`` into ``YYYY-MM-DD``.

        Falls back to the first 10 chars for plain ISO strings, or '' when it
        can't make sense of the value (so a bad date never crashes the preview).
        """
        s = str(value or "")
        m = re.search(r"/Date\((\d+)", s)
        if m:
            try:
                ms = int(m.group(1))
                return dt.datetime.fromtimestamp(
                    ms / 1000, dt.timezone.utc
                ).date().isoformat()
            except (ValueError, OverflowError, OSError):
                return ""
        return s[:10]

    def _dump_bank_feed_recon(items, batch=None):
        """Read-only preview of how ALL receipts in the batch (including
        duplicates and ignored ones) line up with the connected Xero card/bank
        feeds.

        Returns, in upload order, one row per receipt in THIS batch tagged as:
          - "already_xero": a confirmed duplicate already submitted to Xero
          - "matched": a SPEND transaction on the card lines up (±£1, ±10 days)
          - "no_match": nothing on the card feed lines up
        Plus "outstanding": card transactions in the window with no receipt in
        this batch that aren't already reconciled / attached in Xero.

        Important: Xero's BankTransactions API only returns SPEND transactions
        that have been entered or reconciled. Unreconciled card-feed statement
        lines are NOT returned, so "no_match" can mean "not reconciled in Xero
        yet" rather than "no such card payment".

        Never writes to Xero and is fully gated by the kill-switch. Returns None
        when there are no receipts to show.
        """
        chosen = ((batch or {}).get("card_account") or "").strip()
        receipts = [it for it in items if it.get("amount_inc") is not None]
        if not receipts:
            return None
        plaid_on = cardfeed.is_connected(config.admin_db_file)
        # Xero being paused no longer blocks the preview: Plaid is an independent
        # source of the real card feed, so we only bail out when neither is on.
        if xero_is_disabled() and not plaid_on:
            return {"paused": True, "rows": [], "outstanding": [], "chosen": chosen}

        def _norm_acct(s):
            # Normalise account names for lenient comparison (drop punctuation /
            # collapse whitespace) so e.g. "Charge Card - Dan" matches "Charge
            # Card – Dan".
            return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()

        def _dup_kind(it):
            # STATUS_DUPLICATE covers both receipts already in the system and
            # plain in-batch re-uploads; only the former counts as "already in
            # Xero" — an in-batch re-upload is just a "Duplicate upload".
            if "another uploaded receipt" in (it.get("dup_reason") or "").lower():
                return "dup_upload"
            return "already_xero"

        def _row_kind_no_tx(it):
            if it.get("status") == dump_store.STATUS_DUPLICATE:
                return _dup_kind(it)
            return "no_match"

        def _fallback_rows():
            return [{"item": it, "tx": None, "kind": _row_kind_no_tx(it)}
                    for it in receipts]

        # No card chosen for this batch → refuse to guess. Mixing every bank
        # account's feed lines into one list is worse than showing nothing, so
        # card-feed matching is skipped entirely. Company-card batches get a
        # "pick the card" prompt on the results page; subcontractor batches
        # pay from their own money, so card matching doesn't apply and the
        # panel is simply hidden for them.
        if not chosen:
            if ((batch or {}).get("subcontractor_account") or "").strip():
                return None
            return {"paused": False, "no_card": True, "rows": [],
                    "outstanding": [], "txs": [], "chosen": ""}

        dates = []
        for it in receipts:
            try:
                dates.append(dt.date.fromisoformat((it.get("purchased_on") or "")[:10]))
            except (ValueError, TypeError):
                pass
        if not dates:
            return {"paused": False, "rows": _fallback_rows(),
                    "outstanding": [], "chosen": chosen}

        # Fetch a wide window so far-off candidates are available to the
        # suggestion pass: receipt OCR dates can be months wrong (day/month
        # swaps move a date up to ~6 months, year misreads up to ~12), so a tight
        # window would hide the very transactions we want to suggest. Confident
        # matching is still bounded by the 90d/45d caps in the qualify logic
        # below; only the exact-amount-plus-name suggestions use the extra slack.
        start = min(dates) - dt.timedelta(days=365)
        end = max(dates) + dt.timedelta(days=365)

        txs = []
        acct_names_seen: set = set()

        # Source A — Xero BankTransactions (entered / reconciled spends). These
        # carry the "already reconciled / receipt attached in Xero" signal but
        # never include unreconciled card-feed lines.
        if not xero_is_disabled():
            client = None
            try:
                client = build_xero_client(config)
            except Exception as e:
                print(f"[recon] xero client error: {e}", flush=True)
            if client is None and not plaid_on:
                return {"paused": True, "rows": _fallback_rows(),
                        "outstanding": [], "chosen": chosen}
            if client is not None:
                try:
                    payload = client.get_bank_transactions(
                        start_date=start, end_date=end)
                except Exception as e:
                    print(f"[recon] bank tx fetch failed: {e}", flush=True)
                    payload = None
                    if not plaid_on:
                        return {"paused": False, "error": True,
                                "rows": _fallback_rows(),
                                "outstanding": [], "chosen": chosen}
                raw = (payload or {}).get("BankTransactions") or []
                n_spend = 0
                for t in raw:
                    if str(t.get("Type") or "").upper() != "SPEND":
                        continue
                    n_spend += 1
                    try:
                        total = float(t.get("Total") or 0)
                    except (TypeError, ValueError):
                        continue
                    acct_name = (t.get("BankAccount") or {}).get("Name") or "Bank account"
                    acct_names_seen.add(acct_name.strip())
                    # Lenient account filter: only drop a transaction when a card
                    # was chosen AND it's clearly on a different account (substring
                    # either way), so small naming differences don't wipe matches.
                    if chosen:
                        a = _norm_acct(acct_name)
                        c = _norm_acct(chosen)
                        if c and a and c not in a and a not in c:
                            continue
                    txs.append({
                        "account": acct_name,
                        "total": total,
                        "date": _xero_date_to_iso(t.get("Date")),
                        "contact": (t.get("Contact") or {}).get("Name") or "",
                        "reconciled": bool(t.get("IsReconciled")),
                        "has_attachment": bool(t.get("HasAttachments")),
                        "used": False,
                    })

        # Source B — Plaid (the card's REAL bank feed). This is the source that
        # surfaces card payments Xero can't expose until they're reconciled. We
        # skip any line already present in the Xero set (same amount + date) so a
        # single payment is never counted twice.
        if plaid_on:
            seen_xero = {(round(t["total"], 2), t["date"]) for t in txs}
            # Filter feed lines to the CHOSEN card only.  CSV transactions carry
            # the file's account id (card ending / bank account number); the
            # admin labels each id with its Xero bank account on the Bank
            # Statement page, so we can map id -> Xero name and compare it to
            # the batch's chosen card.  Without this filter every account in
            # the CSV store (e.g. the main Pow Wash current account) leaks into
            # the charge card's outstanding list.
            try:
                _acct_labels = cardfeed.get_account_labels(config.admin_db_file)
            except Exception:
                _acct_labels = {}
            c_norm = _norm_acct(chosen)
            n_feed_seen = n_feed_kept = 0
            for pt in cardfeed.get_cached_transactions(config.admin_db_file):
                n_feed_seen += 1
                acc_id = str(pt.get("account_id") or "").strip()
                label_name = str(
                    (_acct_labels.get(acc_id) or {}).get("xero_account_name") or ""
                ).strip()
                if c_norm and _acct_labels:
                    # Only filter when the admin has labelled at least one
                    # account — with no labels at all we can't tell accounts
                    # apart, so fall back to the old include-everything
                    # behaviour rather than silently showing nothing.
                    a = _norm_acct(label_name)
                    if not (a and (c_norm in a or a in c_norm)):
                        continue
                try:
                    total = round(float(pt.get("amount") or 0), 2)
                except (TypeError, ValueError):
                    continue
                pdate = (pt.get("date") or "")[:10]
                if (total, pdate) in seen_xero:
                    continue
                n_feed_kept += 1
                txs.append({
                    "account": label_name or "Card (bank feed)",
                    "total": total,
                    "date": pdate,
                    "contact": pt.get("name") or "",
                    "reconciled": False,
                    "has_attachment": False,
                    "used": False,
                })
            print(f"[recon] card feed: {n_feed_kept}/{n_feed_seen} lines kept "
                  f"for chosen card {chosen!r}", flush=True)

        print(f"[recon] window {start}..{end}: {len(txs)} candidate tx "
              f"(plaid={'on' if plaid_on else 'off'}, chosen={chosen!r}); "
              f"accounts seen={sorted(acct_names_seen)}", flush=True)

        # Matching runs in two passes:
        #
        #   Pass 1 — CONFIDENT matches.  Receipt OCR dates are unreliable —
        #   day/month get swapped (a UK 05/11 read as 11/05, pushing the date
        #   months out) and there are settlement delays — so a strict date window
        #   wrongly rejects real matches.  We treat the merchant name as the
        #   strong signal:
        #     * amount close AND date within 10 days       -> match (classic), or
        #     * date with day/month swapped within 10 days -> match (OCR swap), or
        #     * merchant matches AND amount exact          -> match within 90d
        #       (settlement delay), or
        #     * merchant matches AND amount close          -> match within 45d.
        #   The 90-day cap on the exact-amount rule stops recurring same-amount
        #   merchants (fuel, supermarkets) being auto-matched months apart by
        #   fluke.  A confident match CLAIMS its transaction (used=True).
        #
        #   Pass 2 — SUGGESTED matches.  For receipts left over, if a still-
        #   unclaimed transaction has the EXACT same amount AND a matching shop
        #   name, surface it as a "possible match" for the admin to confirm — no
        #   matter how far apart the dates are.  These are only suggestions, so
        #   they do NOT claim the transaction (it still counts as outstanding)
        #   and, when more than one transaction qualifies, the receipt is flagged
        #   ambiguous instead of guessing.
        def _amt(it):
            try:
                return float(it.get("amount_inc") or 0)
            except (TypeError, ValueError):
                return 0.0

        def _rdate(it):
            try:
                return dt.date.fromisoformat((it.get("purchased_on") or "")[:10])
            except (ValueError, TypeError):
                return None

        rows = []
        pending = []  # (item, amt, mname) receipts with no confident match yet
        for it in receipts:
            if it.get("status") == dump_store.STATUS_DUPLICATE:
                rows.append({"item": it, "tx": None, "kind": _dup_kind(it)})
                continue
            amt = _amt(it)
            rdate = _rdate(it)
            mname = it.get("merchant")
            # Day/month-swapped form of the receipt date, when valid (day <= 12),
            # so an OCR swap that lands near a transaction still counts as on-date.
            sdate = None
            if rdate:
                try:
                    sdate = rdate.replace(month=rdate.day, day=rdate.month)
                except ValueError:
                    sdate = None
            candidates = []
            for t in txs:
                if t["used"] or amt <= 0:
                    continue
                adiff = abs(t["total"] - amt)
                if adiff > 1.00:
                    continue
                ddiff = None
                if rdate and t["date"]:
                    try:
                        ddiff = abs((dt.date.fromisoformat(t["date"]) - rdate).days)
                    except ValueError:
                        ddiff = None
                mok = _merch_match(mname, t.get("contact"))
                # Treat a day/month-swapped receipt date that lands near the
                # transaction as on-date too (common OCR failure).
                date_close = ddiff is not None and ddiff <= 10
                if not date_close and sdate and t["date"]:
                    try:
                        sdiff = abs(
                            (dt.date.fromisoformat(t["date"]) - sdate).days)
                        date_close = sdiff <= 10
                    except ValueError:
                        pass
                if date_close:
                    qualifies = True
                elif mok and adiff <= 0.05 and (ddiff is None or ddiff <= 90):
                    qualifies = True
                elif mok and (ddiff is None or ddiff <= 45):
                    qualifies = True
                else:
                    qualifies = False
                if qualifies:
                    # Sort so the most convincing candidate wins: merchant
                    # matches first, then closest amount, then closest date.
                    candidates.append(
                        (0 if mok else 1, adiff,
                         ddiff if ddiff is not None else 9999, t)
                    )
            best = min(candidates, key=lambda c: c[:3])[3] if candidates else None
            if best:
                best["used"] = True
                best["matched_item"] = it
                rows.append({"item": it, "tx": best, "kind": "matched"})
            else:
                pending.append((it, amt, mname))

        # Pass 2: suggest exact-amount + matching-name transactions for the
        # receipts that didn't confidently match, drawing from transactions not
        # already claimed by a confident match. Suggestions never claim a
        # transaction (used stays False), so the same card payment can be
        # surfaced on more than one competing receipt for the admin to judge —
        # this keeps the result order-independent and leaves every suggested
        # transaction in the outstanding list until it's confirmed.
        for it, amt, mname in pending:
            sugg = []
            amt_only = []  # exact-amount, name didn't match (weaker price hint)
            if amt > 0:
                for t in txs:
                    if t["used"]:
                        continue
                    if abs(t["total"] - amt) > 0.05:
                        continue
                    if _merch_match(mname, t.get("contact")):
                        sugg.append(t)
                    else:
                        amt_only.append(t)
            # Price-only fallback: OCR sometimes mangles the shop name (e.g. an
            # Esso fuel receipt read as "PUMP"), so a name match never fires even
            # though the exact amount is sat right there in the feed.  When we
            # have no name+amount suggestion, surface exact-amount-only matches
            # as a weaker "check by price" hint — exactly the cross-reference the
            # admin asked for ("there's a similar receipt for £X in the feed").
            if not sugg and amt_only:
                if len(amt_only) >= 2:
                    def _dkey_amt(t):
                        if not (rdate := _rdate(it)) or not t["date"]:
                            return 9999
                        try:
                            return abs(
                                (dt.date.fromisoformat(t["date"]) - rdate).days)
                        except ValueError:
                            return 9999
                    example = min(amt_only, key=_dkey_amt)
                    rows.append({"item": it, "tx": example, "kind": "price_only",
                                 "ambiguous": True, "n_sugg": len(amt_only)})
                else:
                    rows.append({"item": it, "tx": amt_only[0],
                                 "kind": "price_only"})
                continue
            if not sugg:
                rows.append({"item": it, "tx": None, "kind": "no_match"})
            elif len(sugg) >= 2:
                # Several card payments share this amount and shop name — don't
                # guess; flag for the admin to check. Show the nearest by date.
                def _dkey(t):
                    if not (rdate := _rdate(it)) or not t["date"]:
                        return 9999
                    try:
                        return abs((dt.date.fromisoformat(t["date"]) - rdate).days)
                    except ValueError:
                        return 9999
                example = min(sugg, key=_dkey)
                rows.append({"item": it, "tx": example, "kind": "suggested",
                             "ambiguous": True, "n_sugg": len(sugg)})
            else:
                rows.append({"item": it, "tx": sugg[0], "kind": "suggested"})

        outstanding = [t for t in txs
                       if not t["used"] and not t["reconciled"]
                       and not t["has_attachment"]]
        return {"paused": False, "rows": rows, "outstanding": outstanding,
                "txs": txs, "chosen": chosen}

    def _dump_outstanding_panel(recon, batch_id: str = "",
                                set_card_action: str = "") -> str:
        """Collapsible (closed by default) list of card transactions still
        unreconciled — i.e. card payments in this batch's date range that have
        no matching receipt here and aren't yet reconciled / attached in Xero.

        Shown at the bottom of the results page. Read-only; never writes to Xero.
        The per-receipt match status itself lives on the item cards (pills).
        """
        if not recon:
            return ""
        sym = "\u00a3"
        chosen = (recon.get("chosen") or "").strip()
        title = "Still unreconciled on this card"
        if chosen:
            title += " \u2014 " + chosen

        def _wrap(inner: str, count_badge: str = "") -> str:
            return (
                "<details class='mt-8 rounded-xl border border-gray-200 bg-gray-50 "
                "p-4'><summary class='cursor-pointer select-none text-sm "
                "font-semibold text-gray-800 flex items-center gap-2'>"
                + count_badge + escape(title) + "</summary>"
                "<div class='mt-3'>" + inner + "</div></details>"
            )

        if recon.get("no_card"):
            # No card picked at upload time — never mix accounts; ask instead.
            opts: set = set()
            try:
                for v in (cardfeed.get_account_labels(config.admin_db_file) or {}).values():
                    n = str((v or {}).get("xero_account_name") or "").strip()
                    if n:
                        opts.add(n)
            except Exception:
                pass
            try:
                _at, _tid, _ = _load_xero_at_tid(config)
                if _at and _tid:
                    _r, _banks, _t, _bw = _get_tenant_acct_themes(_at, _tid)
                    for a in _banks or []:
                        n = str(a.get("Name") or "").strip()
                        if n:
                            opts.add(n)
            except Exception:
                pass
            if opts:
                picker = (
                    "<select name='card_account' class='rounded-lg "
                    "border-gray-300 text-sm'>"
                    + "".join(
                        "<option value='" + escape(o) + "'>" + escape(o)
                        + "</option>" for o in sorted(opts)
                    )
                    + "</select>"
                )
            else:
                picker = (
                    "<input type='text' name='card_account' placeholder="
                    "'e.g. Charge Card - Dan' class='rounded-lg "
                    "border-gray-300 text-sm'>"
                )
            inner = (
                "<p class='text-xs text-gray-600 mb-2'>No card was chosen for "
                "this batch, so we can't tell which account's payments to "
                "check these receipts against (and we won't mix accounts). "
                "Pick the card these receipts were paid from:</p>"
                "<form method='post' action='"
                + escape(set_card_action
                         or ("/receipts/expenses/dump/" + (batch_id or "")
                             + "/set-card")) + "' "
                "class='flex items-center gap-2'>"
                + picker +
                "<button type='submit' class='text-xs font-semibold px-3 py-2 "
                "rounded-lg bg-indigo-600 text-white'>Check this card</button>"
                "</form>"
            )
            return (
                "<details open class='mt-8 rounded-xl border border-amber-200 "
                "bg-amber-50 p-4'><summary class='cursor-pointer select-none "
                "text-sm font-semibold text-amber-900 flex items-center gap-2'>"
                "Which card were these receipts paid from?</summary>"
                "<div class='mt-3'>" + inner + "</div></details>"
            )

        if recon.get("paused"):
            return _wrap(
                "<p class='text-xs text-gray-500'>Xero is paused, so we can't "
                "check the card feed right now. Switch Xero back on to see what's "
                "still unreconciled.</p>"
            )
        if recon.get("error"):
            return _wrap(
                "<p class='text-xs text-gray-500'>Couldn't reach Xero to load "
                "your card feed just now — try again in a moment.</p>"
            )

        # Build the card-feed timeline. Every transaction on the chosen card is
        # classified:
        #   - already reconciled / attachment in Xero AND no receipt in this
        #     batch  -> fully left out (nothing to do)
        #   - matched to a receipt in this batch, but the transaction ALREADY
        #     had a receipt / was reconciled in Xero -> "Previously submitted"
        #   - matched to a receipt in this batch -> full (green) row
        #   - unreconciled with no receipt -> thin row (needs a receipt)
        timeline = []
        for t in recon.get("txs") or []:
            matched = t.get("matched_item")
            try:
                _amt = float(t.get("total") or 0)
            except (TypeError, ValueError):
                _amt = 0.0
            if _amt <= 0 and not matched:
                # Money IN — card being paid off / a refund, not an expense.
                continue
            already = bool(t.get("reconciled")) or bool(t.get("has_attachment"))
            if already and not matched:
                continue          # settled in Xero, nothing uploaded here — hide
            if matched and already:
                kind = "prev"
            elif matched:
                kind = "matched"
            else:
                kind = "thin"
            timeline.append((t, kind))
        # Newest first; undated lines sink to the bottom.
        timeline.sort(key=lambda p: p[0].get("date") or "0000-00-00",
                      reverse=True)

        n_thin = sum(1 for _, k in timeline if k == "thin")
        if not timeline:
            return _wrap(
                "<p class='text-xs text-gray-500'>Nothing left over — every card "
                "transaction in this date range either matched a receipt here or "
                "is already reconciled in Xero.</p>"
            )

        def _tx_amt(t) -> str:
            try:
                return sym + format(float(t.get("total") or 0), ",.2f")
            except (TypeError, ValueError):
                return "\u2014"

        def _render_row(t, kind) -> str:
            contact = escape(t.get("contact") or "Unknown merchant")
            td = escape(_exp_uk_date(t.get("date") or ""))
            amt_s = _tx_amt(t)
            if kind == "thin":
                # Unreconciled, no receipt — deliberately compact.
                return (
                    "<div class='flex items-center justify-between px-4 py-1 "
                    "border-b border-gray-100 text-xs text-gray-600'>"
                    "<div class='min-w-0 pr-3 truncate'>" + contact
                    + " <span class='text-gray-400'>\u00b7 " + td + "</span></div>"
                    "<div class='font-medium text-gray-700 shrink-0'>"
                    + amt_s + "</div></div>"
                )
            it = t.get("matched_item") or {}
            r_merchant = escape(str(it.get("merchant") or "receipt"))
            if kind == "prev":
                badge_html = (
                    "<span class='text-[11px] font-semibold px-2 py-0.5 "
                    "rounded-full bg-amber-100 text-amber-800 shrink-0'>"
                    "Previously submitted</span>"
                )
                note = ("This card payment already had a receipt / was "
                        "reconciled in Xero before this batch.")
            else:
                badge_html = (
                    "<span class='text-[11px] font-semibold px-2 py-0.5 "
                    "rounded-full bg-emerald-100 text-emerald-700 shrink-0'>"
                    "Matched \u2014 receipt in this batch</span>"
                )
                note = "Matched to the uploaded receipt: " + r_merchant
            bg = "bg-amber-50/40" if kind == "prev" else "bg-emerald-50/40"
            return (
                "<div class='px-4 py-2 border-b border-gray-100 text-sm " + bg + "'>"
                "<div class='flex items-center justify-between gap-2'>"
                "<div class='min-w-0 pr-1'>"
                "<div class='font-medium text-gray-800 truncate'>" + contact + "</div>"
                "<div class='text-xs text-gray-500'>" + td + "</div></div>"
                "<div class='flex items-center gap-2 shrink-0'>" + badge_html
                + "<span class='font-semibold text-gray-900'>" + amt_s
                + "</span></div></div>"
                "<div class='text-[11px] text-gray-500 mt-0.5'>"
                + escape(note) + "</div></div>"
            )

        # Group into calendar months, newest month first. Only the most recent
        # month is shown initially; each click on "Show another month" reveals
        # the next one down.
        months: list[tuple[str, list]] = []
        for t, kind in timeline:
            mk = (t.get("date") or "")[:7] or "undated"
            if not months or months[-1][0] != mk:
                months.append((mk, []))
            months[-1][1].append((t, kind))

        def _month_label(mk: str) -> str:
            try:
                d = dt.date.fromisoformat(mk + "-01")
                return d.strftime("%B %Y")
            except ValueError:
                return "Undated"

        sections = ""
        for i, (mk, pairs) in enumerate(months):
            rows_html = "".join(_render_row(t, k) for t, k in pairs)
            thin_ct = sum(1 for _, k in pairs if k == "thin")
            sub = f"{len(pairs)} transaction{'s' if len(pairs) != 1 else ''}"
            if thin_ct:
                sub += f" \u00b7 {thin_ct} missing a receipt"
            hidden = "" if i == 0 else " hidden"
            sections += (
                "<div class='dump-feed-month" + hidden + "'>"
                "<div class='flex items-center justify-between px-4 py-2 "
                "bg-gray-100 border-b border-gray-200'>"
                "<span class='text-xs font-semibold text-gray-700 uppercase "
                "tracking-wide'>" + escape(_month_label(mk)) + "</span>"
                "<span class='text-[11px] text-gray-500'>" + escape(sub)
                + "</span></div>" + rows_html + "</div>"
            )

        more_btn = ""
        if len(months) > 1:
            more_btn = (
                "<div class='text-center py-2'>"
                "<button type='button' id='dump-feed-more' "
                "class='text-xs font-semibold text-indigo-600 hover:underline' "
                "onclick=\"(function(){var m=document.querySelectorAll("
                "'.dump-feed-month.hidden');if(m.length){m[0].classList.remove("
                "'hidden');}if(m.length<=1){document.getElementById("
                "'dump-feed-more').style.display='none';}})()\">"
                "Show another month \u2193</button></div>"
            )

        badge = (
            "<span class='text-xs font-semibold px-2 py-0.5 rounded-full "
            "bg-gray-200 text-gray-700'>" + str(n_thin) + "</span>"
        )

        # "Non-submission report": every card payment (all months in the
        # window) that still has no receipt, with dates, a grand total and the
        # VAT locked inside those totals (20% rate → total ÷ 6).
        nonsub_html = ""
        nonsub = [t for t, k in timeline if k == "thin"]
        if nonsub:
            _tot = 0.0
            body = ""
            for t in nonsub:
                try:
                    _a = float(t.get("total") or 0)
                except (TypeError, ValueError):
                    _a = 0.0
                _tot += _a
                body += (
                    "<tr class='border-b border-gray-100'>"
                    "<td class='py-1 pr-3 text-gray-500 whitespace-nowrap'>"
                    + escape(_exp_uk_date(t.get("date") or "")) + "</td>"
                    "<td class='py-1 pr-3'>"
                    + escape(t.get("contact") or "Unknown merchant") + "</td>"
                    "<td class='py-1 text-right font-medium whitespace-nowrap'>"
                    + sym + format(_a, ",.2f") + "</td></tr>"
                )
            _vat = _tot / 6.0
            nonsub_html = (
                "<div class='text-center py-3'>"
                "<button type='button' class='text-xs font-semibold px-3 py-2 "
                "rounded-lg border border-gray-300 text-gray-700 "
                "hover:bg-gray-50' onclick=\"document.getElementById("
                "'dump-nonsub').classList.toggle('hidden')\">"
                "Non-submission report</button></div>"
                "<div id='dump-nonsub' class='hidden bg-white rounded-xl "
                "border border-gray-200 p-4 text-sm'>"
                "<div class='font-semibold text-gray-800 mb-2'>"
                "Non-submission report \u2014 card payments with no receipt "
                "handed in</div>"
                "<table class='w-full text-xs'><thead>"
                "<tr class='text-left text-gray-400'>"
                "<th class='py-1 pr-3 font-medium'>Date</th>"
                "<th class='py-1 pr-3 font-medium'>Merchant</th>"
                "<th class='py-1 text-right font-medium'>Amount</th>"
                "</tr></thead><tbody>" + body + "</tbody></table>"
                "<div class='mt-3 pt-2 border-t border-gray-200 flex "
                "justify-between text-xs'>"
                "<span class='text-gray-500'>" + str(len(nonsub))
                + " unsubmitted expense" + ("s" if len(nonsub) != 1 else "")
                + "</span><span class='font-semibold text-gray-900'>Total "
                + sym + format(_tot, ",.2f") + "</span></div>"
                "<div class='flex justify-between text-xs mt-1'>"
                "<span class='text-gray-500'>Potential VAT inside these "
                "totals (at 20%, i.e. total \u00f7 6)</span>"
                "<span class='font-semibold text-gray-900'>"
                + sym + format(_vat, ",.2f") + "</span></div></div>"
            )

        inner = (
            "<p class='text-xs text-gray-500 mb-2'>Your card feed, newest first. "
            "Compact lines are card payments with no receipt yet; highlighted "
            "lines matched a receipt in this batch. Payments already settled in "
            "Xero with no receipt here are hidden.</p>"
            "<div class='bg-white rounded-xl border border-gray-200 "
            "overflow-hidden'>" + sections + more_btn + "</div>"
            + nonsub_html
        )
        return _wrap(inner, badge)

    def _dump_fetch_xero_receipt_image(match) -> str:
        """For a matched existing receipt whose local image file is gone, try to
        pull the previously-submitted image back from Xero so it can be compared.

        Returns a local temp file path, or '' if unavailable (Xero paused, no
        Xero linkage, no image attachment, or any error). Fully gated.
        """
        if xero_is_disabled():
            return ""
        xtype = str((match or {}).get("xero_type") or "").strip().lower()
        xid = str((match or {}).get("xero_id") or "").strip()
        if not xid:
            return ""
        if "invoice" in xtype or "accpay" in xtype or "accrec" in xtype or "bill" in xtype:
            endpoint = "Invoices"
        else:
            endpoint = "BankTransactions"
        try:
            client = build_xero_client(config)
            attachments = client.get_attachments(endpoint, xid)
            chosen = None
            for a in attachments or []:
                mt = str(a.get("MimeType") or "").lower()
                if mt.startswith("image/"):
                    chosen = a
                    break
            if not chosen and attachments:
                chosen = attachments[0]
            if not chosen:
                return ""
            name = str(chosen.get("FileName") or "").strip()
            if not name:
                return ""
            content, _mime = client.get_attachment_content(endpoint, xid, name)
            if not content:
                return ""
            import tempfile as _tempfile
            suffix = os.path.splitext(name)[1] or ".jpg"
            fd, tmp_path = _tempfile.mkstemp(prefix="xero_rcpt_", suffix=suffix)
            with os.fdopen(fd, "wb") as fh:
                fh.write(content)
            return tmp_path
        except Exception:
            return ""

    def _dump_process(batch, files):
        """Process an uploaded batch: OCR + AI-code + dedupe + classify each file,
        persisting a dump item per receipt. ``files`` is a list of
        (bytes, filename, content_type)."""
        db = config.admin_db_file
        svc = ReceiptService(config)
        settings = get_expense_settings(db)
        vat_rate = settings["vat_rate"]
        engineer_id = batch.get("engineer_id")
        engineer = exp_store.get_engineer(db, int(engineer_id)) if engineer_id else None

        exp_accounts: list = []
        try:
            _at, _tid, _ = _load_xero_at_tid(config)
            exp_accounts, _ = _get_xero_expense_accounts(_at, _tid, db)
        except Exception:
            exp_accounts = []

        existing = exp_store.list_all_receipts(db, limit=1_000_000)
        eng_map = {e["id"]: e["name"] for e in exp_store.list_engineers(db)}
        existing_by_digest: dict = {}
        for r in existing:
            dg = _dump_stored_digest(r.get("stored_file"))
            if dg:
                existing_by_digest.setdefault(dg, r)
        prior_hashes = dump_store.hashes_in_other_batches(db, batch["id"])
        if batch.get("is_test"):
            # Test mode is a dry-run — it must never be blocked by images that
            # appeared in previous batches (real or test), otherwise re-uploading
            # the same photos to run the pipeline again would permanently flag
            # everything as a duplicate.  In-batch dedup (seen_in_batch) still
            # applies so we don't process the exact same file twice within a
            # single test run.
            existing_by_digest = {}
            prior_hashes = set()
        seen_in_batch: set = set()

        # Record the total up-front so the results page can show a real progress
        # bar (done / total) while the background thread works through the files.
        try:
            dump_store.update_batch(db, batch["id"], total_count=len(files))
        except Exception:
            pass

        counts: dict = {}
        seq = 0
        for file_bytes, filename, content_type in files:
            seq += 1
            # Compute hashes from the ORIGINAL bytes so dedup stays consistent
            # with prior uploads of the same photo, then shrink the image for
            # faster OCR and smaller on-disk storage.
            full_hash, digest16 = _dump_digests(file_bytes)
            file_bytes, filename, content_type = _exp_resize_for_ocr(
                file_bytes, filename, content_type)
            try:
                result = svc.analyze_upload(
                    file_bytes=file_bytes, filename=filename, mime_type=content_type
                )
            except Exception as exc:
                result = {
                    "stored_file": "", "merchant": "", "total": None, "net": None,
                    "tax": None, "date": "", "currency": "GBP", "raw_text": "",
                    "ocr_error": str(exc).splitlines()[0][:200],
                }

            # One photo can contain SEVERAL physical receipts (e.g. two pump
            # receipts side by side). Ask the AI (conservatively); when it
            # confidently finds 2+, the first is processed here as normal and
            # the rest become their own dump items further down, sharing the
            # same photo.
            splits: list = []
            if result.get("raw_text") and not result.get("ocr_error"):
                try:
                    splits = _ai_split_multi_receipts(db, result.get("raw_text", ""))
                except Exception:
                    splits = []
            if len(splits) >= 2:
                s0 = splits[0]
                result = dict(result)
                if s0.get("merchant"):
                    result["merchant"] = s0["merchant"]
                if s0.get("date"):
                    result["date"] = s0["date"]
                result["total"] = s0.get("total")
                result["net"] = s0.get("net")
                result["tax"] = s0.get("tax")
                if s0.get("text"):
                    result["raw_text"] = s0["text"]

            ocr_net, ocr_tax = result.get("net"), result.get("tax")
            ocr_had_breakdown = ocr_net is not None or ocr_tax is not None
            total, net, tax, zero_rated = _exp_reconcile_amounts(
                result.get("total"), ocr_net, ocr_tax, vat_rate
            )

            segments = []
            cat_code = cat_name = ""
            ai_uncertain = False
            try:
                segments = _ai_analyze_receipt(
                    db, result.get("merchant", ""), result.get("raw_text", ""),
                    total, exp_accounts, vat_rate,
                )
                if segments:
                    primary = max(segments, key=lambda s: s.get("gross") or 0)
                    cat_code, cat_name = primary["account_code"], primary["account_name"]
                    if len(segments) > 1 and not ocr_had_breakdown:
                        net = round(sum(s["net"] for s in segments), 2)
                        tax = round(sum(s["vat"] for s in segments), 2)
                        total = round(sum(s["gross"] for s in segments), 2)
                else:
                    cat_code, cat_name = _ai_categorize_receipt(
                        db, result.get("merchant", ""), result.get("raw_text", ""),
                        total, exp_accounts,
                    )
                    if not cat_code:
                        ai_uncertain = True
                        fallback = (
                            ((engineer.get("expense_account_code") if engineer else "") or "").strip()
                            or (settings.get("default_expense_account") or "").strip()
                        )
                        if fallback:
                            cat_code = fallback
                            for _a in exp_accounts:
                                if str(_a.get("Code") or "").strip() == fallback:
                                    cat_name = str(_a.get("Name") or "").strip()
                                    break
            except Exception:
                pass
            # If the AI never produced an account (categorise failed or raised),
            # treat it as uncertain so the receipt is surfaced for a manual pick
            # rather than silently importing with no/guessed account.
            if not cat_code:
                ai_uncertain = True
            # Deterministic £40 fuel rule: under £40 → machinery fuel,
            # £40+ → van fuel; diesel is ALWAYS van fuel (machines take
            # unleaded). Done in code, never left to the LLM.
            segments, cat_code, cat_name = _apply_fuel_threshold(
                segments, cat_code, cat_name, total, exp_accounts,
                result.get("raw_text", ""),
            )

            today = dt.datetime.now(dt.timezone.utc).date().isoformat()
            purchased_on = result.get("date") or today

            status = dump_store.STATUS_NEW
            dup_reason = ""
            match_id = ""
            match_eid = None
            image_check: dict = {}

            if digest16 and digest16 in existing_by_digest:
                m = existing_by_digest[digest16]
                status = dump_store.STATUS_DUPLICATE
                dup_reason = "Identical image already submitted in Field Expenses."
                match_id = m.get("id", "")
                match_eid = m.get("engineer_id")
            elif full_hash in seen_in_batch or full_hash in prior_hashes:
                status = dump_store.STATUS_DUPLICATE
                dup_reason = "Duplicate of another uploaded receipt (identical image)."
            else:
                this_key = _dump_norm_merchant(result.get("merchant", ""))
                match = None
                if this_key and total:
                    for r in existing:
                        try:
                            r_amt = float(r.get("amount_inc") or 0)
                        except (TypeError, ValueError):
                            continue
                        if (
                            abs(r_amt - float(total)) <= 0.02
                            and (r.get("purchased_on") or "") == purchased_on
                            and _dump_norm_merchant(r.get("merchant")) == this_key
                        ):
                            match = r
                            break
                if match:
                    match_id = match.get("id", "")
                    match_eid = match.get("engineer_id")
                    same_person = (
                        engineer_id and match_eid
                        and int(match_eid) == int(engineer_id)
                    )
                    if same_person:
                        status = dump_store.STATUS_POSSIBLE_DUP
                        dup_reason = "Same merchant, date and amount as an existing claim by this person."
                    else:
                        other = eng_map.get(match_eid, "another person")
                        other_path = match.get("stored_file", "") or ""
                        tmp_fetched = ""
                        if not (other_path and os.path.exists(other_path)):
                            tmp_fetched = _dump_fetch_xero_receipt_image(match)
                            if tmp_fetched:
                                other_path = tmp_fetched
                        try:
                            image_check = _dump_compare_receipt_images(
                                db, result.get("stored_file", ""), other_path
                            )
                        finally:
                            if tmp_fetched:
                                try:
                                    os.remove(tmp_fetched)
                                except OSError:
                                    pass
                        if image_check.get("available") and image_check.get("same"):
                            status = dump_store.STATUS_DUPLICATE
                            dup_reason = (
                                "Confirmed same physical receipt as " + str(other)
                                + "'s claim (image match) — discounted."
                            )
                        elif image_check.get("available"):
                            status = dump_store.STATUS_SUSPICIOUS
                            dup_reason = (
                                "Resembles " + str(other) + "'s claim, but the image "
                                "check says they differ — please review."
                            )
                        else:
                            status = dump_store.STATUS_SUSPICIOUS
                            dup_reason = (
                                "Looks like " + str(other) + "'s claim; image check "
                                "unavailable. " + (image_check.get("reason") or "")
                            ).strip()

            batch_card_acct = (batch.get("card_account") or "").strip()
            card_status = ""
            if status == dump_store.STATUS_NEW:
                card_status = _dump_card_feed_check(
                    engineer, total, purchased_on, card_account=batch_card_acct
                )
                if card_status == "missing":
                    if batch_card_acct:
                        # The admin explicitly designated this batch for a named
                        # card account, so "missing" most likely means the
                        # statement line hasn't been reconciled in Xero yet
                        # (BankTransactions API limitation: only reconciled / hand-
                        # entered transactions appear). Keep the receipt as "new"
                        # — it will show the "not yet in Xero" pill on the results
                        # page so the admin can see it and reconcile later.
                        dup_reason = (
                            "Not yet found in the Xero card feed — may not have "
                            "been reconciled yet. Treated as a company card expense."
                        )
                    else:
                        # No card account specified: it's unclear whether this was
                        # paid on the company card or a personal card — ask.
                        status = dump_store.STATUS_NEEDS_ACCOUNT
                        dup_reason = (
                            "Not found in the company card feed — was this paid on a "
                            "personal card? Pick the account to use."
                        )

            # When the AI couldn't confidently pick an account, surface the
            # receipt for a manual choice before importing instead of silently
            # using the fallback (or no account at all).
            if status == dump_store.STATUS_NEW and ai_uncertain:
                status = dump_store.STATUS_NEEDS_ACCOUNT
                if cat_code:
                    dup_reason = (
                        "The AI couldn't confidently match this to an account, so "
                        "it fell back to " + (cat_name or cat_code) + ". Please "
                        "confirm or pick the right account before importing."
                    )
                else:
                    dup_reason = (
                        "The AI couldn't work out which account this belongs to. "
                        "Please pick the right account before importing."
                    )

            if len(splits) >= 2:
                _note = ("This photo contains " + str(len(splits))
                         + " receipts — split into separate entries (this is "
                         "1 of " + str(len(splits)) + ").")
                dup_reason = (_note + " " + dup_reason).strip()

            seen_in_batch.add(full_hash)
            counts[status] = counts.get(status, 0) + 1
            dump_store.create_item(
                db,
                batch_id=batch["id"],
                seq=seq,
                content_sha256=full_hash,
                merchant=result.get("merchant", ""),
                purchased_on=purchased_on,
                amount_inc=total, amount_ex=net, vat_amount=tax,
                currency=result.get("currency", "GBP"),
                is_split=bool(segments and len(segments) > 1),
                segments=segments,
                category_account_code=cat_code,
                category_account_name=cat_name,
                status=status,
                dup_reason=dup_reason,
                match_receipt_id=match_id or "",
                match_engineer_id=match_eid,
                image_check=image_check,
                card_feed_status=card_status,
                stored_file=result.get("stored_file", ""),
                filename=filename,
                mime_type=content_type,
                ocr_raw=(result.get("raw_text", "") or "")[:4000],
                ocr_error=result.get("ocr_error", ""),
            )

            # Remaining receipts found in the same photo become their own dump
            # items (own merchant/date/amount/account), sharing the image.
            # Skipped when the photo itself is a duplicate upload.
            if len(splits) >= 2 and status != dump_store.STATUS_DUPLICATE:
                for _si, _sp in enumerate(splits[1:], start=2):
                    seq += 1
                    s_text = _sp.get("text") or result.get("raw_text", "")
                    s_total, s_net, s_tax, _zr = _exp_reconcile_amounts(
                        _sp.get("total"), _sp.get("net"), _sp.get("tax"),
                        vat_rate)
                    s_merchant = _sp.get("merchant") or result.get("merchant", "")
                    s_date = (_sp.get("date") or purchased_on or today)
                    s_segments: list = []
                    s_code = s_name = ""
                    try:
                        s_segments = _ai_analyze_receipt(
                            db, s_merchant, s_text, s_total, exp_accounts,
                            vat_rate)
                        if s_segments:
                            _prim = max(s_segments,
                                        key=lambda s: s.get("gross") or 0)
                            s_code = _prim["account_code"]
                            s_name = _prim["account_name"]
                        else:
                            s_code, s_name = _ai_categorize_receipt(
                                db, s_merchant, s_text, s_total, exp_accounts)
                    except Exception:
                        pass
                    s_segments, s_code, s_name = _apply_fuel_threshold(
                        s_segments, s_code, s_name, s_total, exp_accounts,
                        s_text)
                    # Mirror the main path's gating so split receipts are
                    # never quietly more importable than normal ones.
                    s_status = dump_store.STATUS_NEW
                    s_reason = ""
                    s_card = ""
                    if s_status == dump_store.STATUS_NEW:
                        s_card = _dump_card_feed_check(
                            engineer, s_total, s_date,
                            card_account=batch_card_acct)
                        if s_card == "missing":
                            if batch_card_acct:
                                s_reason = (
                                    "Not yet found in the Xero card feed — may "
                                    "not have been reconciled yet. Treated as "
                                    "a company card expense.")
                            else:
                                s_status = dump_store.STATUS_NEEDS_ACCOUNT
                                s_reason = (
                                    "Not found in the company card feed — was "
                                    "this paid on a personal card? Pick the "
                                    "account to use.")
                    if not s_code:
                        s_status = dump_store.STATUS_NEEDS_ACCOUNT
                        s_reason = (
                            "The AI couldn't work out which account this "
                            "belongs to. Please pick the right account before "
                            "importing.")
                    counts[s_status] = counts.get(s_status, 0) + 1
                    dump_store.create_item(
                        db,
                        batch_id=batch["id"],
                        seq=seq,
                        content_sha256=full_hash + "#" + str(_si),
                        merchant=s_merchant,
                        purchased_on=s_date,
                        amount_inc=s_total, amount_ex=s_net, vat_amount=s_tax,
                        currency=result.get("currency", "GBP"),
                        is_split=bool(s_segments and len(s_segments) > 1),
                        segments=s_segments,
                        category_account_code=s_code,
                        category_account_name=s_name,
                        status=s_status,
                        dup_reason=(("This photo contains " + str(len(splits))
                                     + " receipts — split into separate "
                                     "entries (this is " + str(_si) + " of "
                                     + str(len(splits)) + "). "
                                     + s_reason).strip()),
                        match_receipt_id="",
                        match_engineer_id=None,
                        image_check={},
                        card_feed_status=s_card,
                        stored_file=result.get("stored_file", ""),
                        filename=filename,
                        mime_type=content_type,
                        ocr_raw=(s_text or "")[:4000],
                        ocr_error="",
                    )

        dump_store.update_batch(
            db, batch["id"], status="ready", total_count=seq, summary=counts
        )

    def _cardfeed_redirect_uri():
        configured = (os.getenv("ENABLE_BANKING_REDIRECT_URI") or "").strip()
        if configured:
            return configured
        return request.url_root.rstrip("/") + "/cardfeed/callback"

    def _cardfeed_notice_html():
        notice = session.pop("save_notice", "")
        if not notice:
            return ""
        ok = notice.startswith("success:")
        txt = notice.split(":", 1)[1] if ":" in notice else notice
        cls = ("bg-emerald-50 border-emerald-200 text-emerald-800" if ok
               else "bg-red-50 border-red-200 text-red-800")
        return ("<div class='rounded-lg border px-4 py-3 text-sm mb-4 "
                + cls + "'>" + escape(txt) + "</div>")

    def _cardfeed_header():
        return (
            "<div class='flex items-center justify-between mb-2'>"
            "<h1 class='text-2xl font-bold text-gray-900'>Card feed</h1>"
            "<a href='/receipts/expenses/dump' class='text-sm text-indigo-600 hover:underline'>&larr; Receipt Dump</a>"
            "</div>"
            "<p class='text-sm text-gray-500 mb-6'>Give the app the company card's "
            "real bank transactions so receipts can be matched against them &mdash; "
            "the payments Xero can't share until they're reconciled. Upload the "
            "bank's CSV export below (works with any bank), or connect a bank "
            "automatically where supported.</p>"
        )

    def _cardfeed_csv_section_html(db):
        try:
            cs = cardfeed.csv_status(db)
        except Exception:
            cs = {"has_data": False}
        months = max(1, round((cs.get("retention_days") or 366) / 30.5))

        # Account labels (Xero bank account names saved at upload time).
        try:
            labels = cardfeed.get_account_labels(db)
        except Exception:
            labels = {}

        # Live Xero bank accounts for the upload-form dropdown.
        xero_bank_accts = _get_xero_bank_accts_for_cardfeed()

        # Build a map of account_id → engineer name(s) so we can label each feed.
        try:
            from .receipts import expense_store as exp_store
            all_engs = exp_store.list_engineers(db)
        except Exception:
            all_engs = []
        eng_by_account: dict[str, list[str]] = {}
        card_engs = [e for e in all_engs
                     if e.get("kind") == "company_card" and e.get("active")]
        for e in card_engs:
            acc = (e.get("plaid_account_id") or "").strip()
            if acc:
                eng_by_account.setdefault(acc, []).append(e.get("name") or f"Engineer {e['id']}")

        # ── Per-account status table ────────────────────────────────────────
        accts_on_file = cs.get("accounts") or []
        acct_ids_on_file = {a.get("account_id") for a in accts_on_file if a.get("account_id")}

        # ── Month coverage bar helper ────────────────────────────────────────────
        def _month_bar_html(months_dict: dict) -> str:
            """12 month chips coloured by whether that month has tx data."""
            from datetime import date, timedelta
            today = date.today()
            # Generate last 12 calendar months including current month.
            month_keys = []
            for i in range(11, -1, -1):
                # Subtract i months from the first day of today's month.
                first = date(today.year, today.month, 1)
                # Roll back i months.
                m = first.month - i
                y = first.year
                while m < 1:
                    m += 12
                    y -= 1
                month_keys.append((f"{y:04d}-{m:02d}", date(y, m, 1).strftime("%b")))
            chips = ""
            for ym, label in month_keys:
                count = months_dict.get(ym, 0)
                is_current = ym == f"{today.year:04d}-{today.month:02d}"
                if count:
                    title = f"{count} transaction{'s' if count != 1 else ''}"
                    chips += (
                        f"<div title='{label} — {title}' "
                        f"class='flex flex-col items-center rounded px-1 py-0.5 "
                        f"bg-emerald-100 border border-emerald-300 min-w-[2.4rem]'>"
                        f"<span class='text-xs font-medium text-emerald-800 leading-none'>{label}</span>"
                        f"<span class='text-[10px] text-emerald-600 leading-none mt-0.5'>{count}</span>"
                        f"</div>"
                    )
                else:
                    dim = "opacity-40" if not is_current else "opacity-60 border-dashed"
                    chips += (
                        f"<div title='{label} — no data uploaded' "
                        f"class='flex flex-col items-center rounded px-1 py-0.5 "
                        f"bg-gray-100 border border-gray-200 min-w-[2.4rem] {dim}'>"
                        f"<span class='text-xs font-medium text-gray-400 leading-none'>{label}</span>"
                        f"<span class='text-[10px] text-gray-300 leading-none mt-0.5'>—</span>"
                        f"</div>"
                    )
            return (
                "<div class='flex flex-wrap gap-1 items-end pt-1 pb-2'>"
                + chips + "</div>"
            )

        if accts_on_file:
            rows = ""
            for a in accts_on_file:
                acc_id = a.get("account_id") or ""
                lbl = labels.get(acc_id) or {}
                xero_name = lbl.get("xero_account_name") or ""
                xero_id_saved = lbl.get("xero_account_id") or ""
                # Account column: Xero name if labelled, else raw mask + label prompt
                if xero_name:
                    acct_html = (
                        f"<span class='font-medium text-gray-900'>{escape(xero_name)}</span>"
                        f"<br><span class='text-xs text-gray-400 font-mono'>••••{escape(acc_id[-4:])}</span>"
                    )
                else:
                    # Build inline relabel form with Xero dropdown
                    if xero_bank_accts:
                        _opt_parts = []
                        for _xb in xero_bank_accts:
                            _xid = escape(_xb.get('AccountID') or '')
                            _xnm = escape(_xb.get('Name') or '')
                            _xbn = escape(_xb.get('BankAccountNumber') or '')
                            _opt_parts.append(
                                f"<option value='{_xid}|{_xnm}|{_xbn}'>{_xnm}</option>"
                            )
                        opts = "<option value=''>— label this account —</option>" + "".join(_opt_parts)
                        acct_html = (
                            f"<span class='text-xs font-mono text-gray-500'>••••{escape(acc_id[-4:])}</span>"
                            f"<form method='post' action='/cardfeed/csv-label' class='mt-1 flex gap-1 items-center'>"
                            f"<input type='hidden' name='account_id' value='{escape(acc_id)}'>"
                            f"<select name='xero_acct' class='text-xs border border-gray-300 rounded px-1 py-0.5'>{opts}</select>"
                            f"<button type='submit' class='text-xs px-2 py-0.5 bg-indigo-600 text-white rounded'>Save</button>"
                            f"</form>"
                        )
                    else:
                        acct_html = f"<span class='text-xs font-mono text-gray-500'>••••{escape(acc_id[-4:])}</span>"
                linked = eng_by_account.get(acc_id)
                if linked:
                    who = ", ".join(linked)
                    who_html = f"<span class='text-gray-800 text-sm'>{escape(who)}</span>"
                else:
                    who_html = (
                        "<a href='/receipts/expenses' "
                        "class='inline-flex items-center gap-1 text-xs font-medium "
                        "text-indigo-600 hover:text-indigo-800'>"
                        "Link in Field Expenses &rarr;</a>"
                    )
                month_bar = _month_bar_html(a.get("months") or {})
                rows += (
                    "<tr class='border-t border-gray-100'>"
                    f"<td class='py-2 pr-4'>{acct_html}</td>"
                    f"<td class='py-2 pr-4 text-sm text-gray-600'>"
                    f"{escape(str(a.get('date_from') or ''))} &rarr; {escape(str(a.get('date_to') or ''))}</td>"
                    f"<td class='py-2 pr-4 text-sm text-gray-600'>{a.get('transaction_count', 0):,} txns</td>"
                    f"<td class='py-2'>{who_html}</td>"
                    "</tr>"
                    "<tr>"
                    f"<td colspan='4' class='pb-3 pt-0'>"
                    f"<div class='text-[10px] text-gray-400 mb-0.5 uppercase tracking-wide'>Coverage — last 12 months</div>"
                    f"{month_bar}"
                    f"</td>"
                    "</tr>"
                )
            accts_table = (
                "<table class='w-full text-left mt-1'>"
                "<thead><tr>"
                "<th class='pb-1 text-xs font-medium text-gray-400 pr-4'>Xero account / CSV</th>"
                "<th class='pb-1 text-xs font-medium text-gray-400 pr-4'>Date range</th>"
                "<th class='pb-1 text-xs font-medium text-gray-400 pr-4'>Transactions</th>"
                "<th class='pb-1 text-xs font-medium text-gray-400'>Linked engineer</th>"
                "</tr></thead>"
                f"<tbody>{rows}</tbody>"
                "</table>"
                "<p class='text-xs text-gray-400 mt-1'>Last upload: "
                + escape(str(cs.get("last_upload_at") or "unknown")) + "</p>"
            )

            # Engineers whose card is not in any uploaded file yet.
            missing_engs = [e for e in card_engs
                            if not (e.get("plaid_account_id") or "").strip()
                            or (e.get("plaid_account_id") or "").strip() not in acct_ids_on_file]
            if missing_engs:
                missing_items = "".join(
                    "<li class='text-sm text-amber-800 flex items-center justify-between gap-2'>"
                    "<span>" + escape(e.get("name") or f"Engineer {e['id']}")
                    + ("" if (e.get("plaid_account_id") or "").strip()
                       else " <span class='text-xs text-amber-600'>"
                            "— no Linked card set on profile</span>")
                    + "</span>"
                    "<a href='/receipts/expenses' class='text-xs text-indigo-600 "
                    "hover:text-indigo-800 font-medium whitespace-nowrap'>"
                    "Set Linked card &rarr;</a>"
                    "</li>"
                    for e in missing_engs
                )
                missing_html = (
                    "<div class='rounded-lg bg-amber-50 border border-amber-200 px-4 py-3 space-y-2'>"
                    "<p class='text-xs font-semibold text-amber-800'>Statement not yet uploaded for:</p>"
                    "<ul class='space-y-1.5'>" + missing_items + "</ul>"
                    "<p class='text-xs text-amber-700 border-t border-amber-200 pt-2 mt-1'>"
                    "Upload each account's CSV below, then in Field Expenses open the engineer, "
                    "expand their card and set <strong>Linked card account</strong> to match.</p>"
                    "</div>"
                )
            else:
                missing_html = ""

            status_html = accts_table + missing_html
            clear_html = (
                "<form method='post' action='/cardfeed/csv-clear' "
                "onsubmit=\"return confirm('Delete ALL uploaded CSV transactions? "
                "Receipt matching against the card feed will stop until you upload "
                "a new file.');\">"
                "<button type='submit' class='px-3 py-1.5 border border-gray-300 "
                "text-gray-700 rounded-lg text-sm hover:bg-gray-50'>Clear all CSV data</button>"
                "</form>"
            )
        else:
            status_html = (
                "<p class='text-sm text-gray-600'>No statement data uploaded yet. "
                "Download each card account's transactions as a CSV from internet banking "
                "(Lloyds: Statements &rarr; Export), then upload each file here.</p>"
            )
            # Show which engineers need a statement even before any upload.
            if card_engs:
                items = "".join(
                    "<li class='text-sm text-amber-700'>" + escape(e.get("name") or "") + "</li>"
                    for e in card_engs
                )
                status_html += (
                    "<div class='rounded-lg bg-amber-50 border border-amber-200 px-4 py-3 mt-2'>"
                    "<p class='text-xs font-semibold text-amber-800 mb-1'>Statements needed for:</p>"
                    "<ul class='list-disc ml-4 space-y-0.5'>" + items + "</ul>"
                    "</div>"
                )
            clear_html = ""

        # Upload form — include Xero bank account selector if available.
        if xero_bank_accts:
            _upload_opts = []
            for _xb in xero_bank_accts:
                _xid = escape(_xb.get('AccountID') or '')
                _xnm = escape(_xb.get('Name') or '')
                _xbn = escape(_xb.get('BankAccountNumber') or '')
                _upload_opts.append(f"<option value='{_xid}|{_xnm}|{_xbn}'>{_xnm}</option>")
            xero_opts = "<option value=''>— which Xero account is this? —</option>" + "".join(_upload_opts)
            acct_select = (
                "<div>"
                "<label class='block text-xs text-gray-500 mb-1'>Xero bank account "
                "<span class='text-gray-400'>(optional — names the account; used to detect mismatches)</span></label>"
                f"<select name='xero_acct' class='w-full sm:w-auto text-sm border "
                f"border-gray-300 rounded-lg px-3 py-2'>{xero_opts}</select>"
                "</div>"
            )
        else:
            acct_select = ""

        return (
            "<div class='rounded-xl border border-gray-200 bg-white p-6 shadow-sm space-y-4'>"
            "<h2 class='text-base font-semibold text-gray-900'>Statement upload (CSV)</h2>"
            + status_html +
            "<div class='border-t border-gray-100 pt-4 space-y-3'>"
            "<p class='text-xs text-gray-500'>Upload one or more monthly CSV exports. "
            "Select multiple files at once (hold Shift or Cmd/Ctrl) to import several months together. "
            "Duplicates across overlapping files are skipped automatically.</p>"
            "<form method='post' action='/cardfeed/csv-upload' "
            "enctype='multipart/form-data' class='space-y-2'>"
            + acct_select +
            "<div class='flex flex-wrap items-center gap-3'>"
            "<label class='block text-xs text-gray-500'>CSV file(s)</label>"
            "<input type='file' name='csv_file' accept='.csv,text/csv' required multiple "
            "class='text-sm text-gray-600 file:mr-3 file:px-3 file:py-1.5 "
            "file:rounded-lg file:border-0 file:bg-indigo-50 file:text-indigo-700 "
            "file:text-sm file:font-medium hover:file:bg-indigo-100'/>"
            "<button type='submit' class='px-4 py-2 bg-indigo-600 text-white "
            "rounded-lg text-sm font-medium hover:bg-indigo-700'>Upload</button>"
            + clear_html +
            "</div></form>"
            "<p class='text-xs text-gray-400'>Transactions are kept for up to "
            + str(months) + " months. Re-uploading overlapping exports is safe &mdash; "
            "duplicates are skipped automatically.</p>"
            "</div>"
            "</div>"
        )

    def _cardfeed_eb_section_html(db):
        client = cardfeed.EnableBankingClient()
        redirect_uri = escape(_cardfeed_redirect_uri())
        title = ("<h2 class='text-base font-semibold text-gray-900'>"
                 "Automatic bank connection</h2>")
        caveat = (
            "<p class='text-xs text-gray-500'>Uses Enable Banking (free for your "
            "own accounts). UK production coverage is limited &mdash; currently "
            "Barclays, HSBC, NatWest/RBS, Coutts and ABN AMRO only. If the card's "
            "bank isn't supported, use the CSV upload above instead.</p>"
        )
        if not client.configured:
            inner = (
                title + caveat +
                "<p class='text-sm text-gray-600'>Not set up. To use it, create an "
                "account at <a class='underline text-indigo-600' "
                "href='https://enablebanking.com/sign-in/' target='_blank' rel='noopener'>"
                "enablebanking.com</a>, register an application, then add "
                "<code>ENABLE_BANKING_APP_ID</code> and "
                "<code>ENABLE_BANKING_PRIVATE_KEY</code> to the Secrets panel. "
                "Whitelist this redirect URL: "
                "<span class='font-mono text-xs break-all'>" + redirect_uri + "</span></p>"
            )
            return ("<div class='rounded-xl border border-gray-200 bg-gray-50 p-6 "
                    "space-y-3 mt-4'>" + inner + "</div>")

        try:
            st = cardfeed.eb_connection_status(db)
        except Exception:
            st = {"connected": False}

        if not st.get("connected"):
            bank_err = ""
            try:
                banks = client.list_aspsps()
            except Exception as e:
                banks = []
                bank_err = str(e)[:200]
            opts = "".join(
                "<option value='" + escape(str(b.get("name") or "")) + "'>"
                + escape(str(b.get("name") or "")) + "</option>"
                for b in banks if b.get("name")
            )
            if opts:
                picker = (
                    "<form method='post' action='/cardfeed/connect' class='space-y-3'>"
                    "<label class='block text-sm text-gray-600'>Choose the card's bank</label>"
                    "<select name='aspsp' class='w-full border border-gray-300 rounded-lg px-3 py-2 text-sm'>"
                    + opts + "</select>"
                    "<button type='submit' "
                    "class='px-4 py-2 bg-indigo-600 text-white rounded-lg text-sm font-medium hover:bg-indigo-700'>"
                    "Connect bank</button></form>"
                )
            else:
                picker = (
                    "<p class='text-sm text-red-700'>Couldn't load the bank list"
                    + ((": " + escape(bank_err)) if bank_err else ".")
                    + " Check your Enable Banking credentials and try again.</p>"
                )
            inner = (
                title + caveat + picker +
                "<p class='text-xs text-gray-400'>Redirect URL (must be whitelisted in "
                "your Enable Banking app): <span class='font-mono break-all'>"
                + redirect_uri + "</span></p>"
            )
            return ("<div class='rounded-xl border border-gray-200 bg-white p-6 "
                    "shadow-sm space-y-4 mt-4'>" + inner + "</div>")

        accts = ""
        for a in (st.get("accounts") or []):
            mask = (" &bull;&bull;&bull;&bull;" + escape(str(a.get("mask")))) if a.get("mask") else ""
            sub = escape(str(a.get("subtype") or a.get("type") or ""))
            accts += ("<li class='text-sm text-gray-700'>"
                      + escape(str(a.get("name") or "Account")) + mask
                      + " <span class='text-xs text-gray-400'>(" + sub + ")</span></li>")
        last_sync = escape(str(st.get("last_sync_at") or "never"))
        valid_until = escape(str(st.get("valid_until") or ""))
        valid_html = (
            "<p class='text-xs text-gray-500'>Access valid until: <strong>"
            + valid_until + "</strong></p>"
        ) if valid_until else ""
        inner = (
            title +
            "<div class='flex items-center gap-2'>"
            "<span class='px-2 py-1 rounded-full bg-emerald-50 text-emerald-700 border border-emerald-200 text-xs font-semibold'>Connected</span>"
            "<span class='text-sm font-semibold text-gray-900'>"
            + escape(str(st.get("institution_name") or "Connected bank"))
            + "</span></div>"
            "<ul class='list-disc ml-5 space-y-0.5'>"
            + (accts or "<li class='text-sm text-gray-400'>No accounts listed</li>")
            + "</ul>"
            "<p class='text-xs text-gray-500'>Transactions on file: <strong>"
            + str(st.get("transaction_count") or 0) + "</strong> &middot; last synced: "
            + last_sync + "</p>"
            + valid_html +
            "<div class='flex items-center gap-2 pt-2'>"
            "<form method='post' action='/cardfeed/sync'><button type='submit' "
            "class='px-3 py-1.5 bg-indigo-600 text-white rounded-lg text-sm font-medium hover:bg-indigo-700'>Sync now</button></form>"
            "<form method='post' action='/cardfeed/disconnect' "
            "onsubmit=\"return confirm('Disconnect this bank? Receipt matching against the card feed will stop until you reconnect.');\">"
            "<button type='submit' class='px-3 py-1.5 border border-gray-300 text-gray-700 rounded-lg text-sm hover:bg-gray-50'>Disconnect</button></form>"
            "</div>"
            "<p class='text-xs text-blue-800'>Open-banking rules mean the bank link "
            "needs re-approving periodically (up to ~180 days, often 90). If matching "
            "goes quiet, come back here and reconnect &mdash; it takes about 30 seconds.</p>"
        )
        return ("<div class='rounded-xl border border-gray-200 bg-white p-6 "
                "shadow-sm space-y-4 mt-4'>" + inner + "</div>")

    def _get_xero_bank_accts_for_cardfeed():
        """Return list of active Xero BANK accounts for the upload-form dropdown."""
        try:
            client = build_xero_client(config)
            if not client:
                return []
            r = client._request(
                "GET", "https://api.xero.com/api.xro/2.0/Accounts",
                params={"where": 'Type=="BANK"', "summaryOnly": "true"},
            )
            if not r.ok:
                return []
            return [
                a for a in (r.json().get("Accounts") or [])
                if a.get("Status") == "ACTIVE"
            ]
        except Exception:
            return []

    def _cardfeed_page_body():
        db = config.admin_db_file
        return _page(
            "<div class='max-w-3xl mx-auto px-4 py-8'>"
            + _cardfeed_header() + _cardfeed_notice_html()
            + _cardfeed_csv_section_html(db)
            + _cardfeed_eb_section_html(db)
            + "</div>"
        )

    @app.get("/cardfeed")
    @require_login
    def cardfeed_page():
        return _cardfeed_page_body()

    @app.post("/cardfeed/connect")
    @require_login
    def cardfeed_connect():
        client = cardfeed.EnableBankingClient()
        if not client.configured:
            session["save_notice"] = "error:Enable Banking is not configured."
            return redirect("/cardfeed")
        aspsp = (request.form.get("aspsp") or "").strip()
        if not aspsp:
            session["save_notice"] = "error:Choose a bank first."
            return redirect("/cardfeed")
        import secrets as _secrets
        state = _secrets.token_urlsafe(24)
        session["cardfeed_state"] = state
        session["cardfeed_aspsp"] = aspsp
        try:
            res = client.start_auth(aspsp, _cardfeed_redirect_uri(), state)
            session["cardfeed_valid_until"] = res.get("valid_until") or ""
            url = res.get("url")
            if not url:
                raise RuntimeError("Enable Banking did not return an authorization URL.")
            return redirect(url)
        except Exception as e:
            print(f"[cardfeed] start_auth error: {e}", flush=True)
            session["save_notice"] = f"error:Could not start bank connection: {str(e)[:200]}"
            return redirect("/cardfeed")

    @app.get("/cardfeed/callback")
    @require_login
    def cardfeed_callback():
        err = request.args.get("error")
        if err:
            desc = request.args.get("error_description") or err
            session["save_notice"] = f"error:Bank connection cancelled: {str(desc)[:200]}"
            return redirect("/cardfeed")
        code = (request.args.get("code") or "").strip()
        state = (request.args.get("state") or "").strip()
        expected = session.pop("cardfeed_state", "")
        if not code or not state or state != expected:
            session["save_notice"] = "error:Bank connection could not be verified. Please try again."
            return redirect("/cardfeed")
        client = cardfeed.EnableBankingClient()
        aspsp = session.pop("cardfeed_aspsp", "") or "Connected bank"
        valid_until = session.pop("cardfeed_valid_until", "")
        try:
            sess = client.create_session(code)
            cardfeed.store_session(config.admin_db_file, client, sess, aspsp, valid_until)
            try:
                cardfeed.sync_transactions(config.admin_db_file, client)
            except Exception as e:
                print(f"[cardfeed] initial sync failed: {e}", flush=True)
            session["save_notice"] = "success:Bank connected."
        except Exception as e:
            print(f"[cardfeed] callback error: {e}", flush=True)
            session["save_notice"] = f"error:Could not finish connecting: {str(e)[:200]}"
        return redirect("/cardfeed")

    @app.post("/cardfeed/sync")
    @require_login
    def cardfeed_sync():
        client = cardfeed.EnableBankingClient()
        try:
            res = cardfeed.sync_transactions(config.admin_db_file, client)
            session["save_notice"] = (
                f"success:Synced card transactions ({res.get('total', 0)} on file).")
        except Exception as e:
            print(f"[cardfeed] sync error: {e}", flush=True)
            session["save_notice"] = f"error:Card sync failed: {str(e)[:200]}"
        return redirect("/cardfeed")

    @app.post("/cardfeed/csv-upload")
    @require_login
    def cardfeed_csv_upload():
        from .csv_card_feed import parse_csv as _parse_csv
        files = request.files.getlist("csv_file")
        files = [f for f in files if f and (f.filename or "").strip()]
        if not files:
            session["save_notice"] = "error:Choose at least one CSV file to upload."
            return redirect("/cardfeed")

        # Parse the Xero account selection (AccountID|Name|BankAccountNumber).
        xero_acct_raw = (request.form.get("xero_acct") or "").strip()
        xero_acct_id = xero_acct_name = xero_bank_number = ""
        if xero_acct_raw:
            parts = xero_acct_raw.split("|")
            xero_acct_id = parts[0] if len(parts) > 0 else ""
            xero_acct_name = parts[1] if len(parts) > 1 else ""
            xero_bank_number = parts[2] if len(parts) > 2 else ""

        def _is_card_ending(aid: str) -> bool:
            """Credit card CSVs use the 4-digit card ending as account_id.
            Xero's BankAccountNumber is a different number, so mismatch
            checking doesn't apply to credit card files."""
            return bool(aid) and len(aid) <= 4 and aid.isdigit()

        def _accounts_match(csv_aid: str, xero_bn: str) -> bool:
            """True if the CSV account number is consistent with the Xero BankAccountNumber."""
            if not csv_aid or not xero_bn:
                return True  # can't check, assume ok
            if _is_card_ending(csv_aid):
                return True  # credit card file — card ending ≠ Xero acct number, skip check
            bn = xero_bn.replace("-", "").replace(" ", "")
            ca = csv_aid.replace("-", "").replace(" ", "")
            return bn.endswith(ca) or ca.endswith(bn)

        total_added = total_dup = total_old = 0
        mismatches: list[str] = []
        errors: list[str] = []
        labelled_accounts: set[str] = set()

        for f in files:
            fname = f.filename or "file"
            try:
                data = f.read()
                if len(data) > 10 * 1024 * 1024:
                    errors.append(f"{fname}: too large (max 10 MB)")
                    continue
                res = cardfeed.ingest_csv(config.admin_db_file, data)
                total_added += res.get("added", 0)
                total_dup += res.get("skipped_duplicates", 0)
                total_old += res.get("too_old", 0)

                # Mismatch detection: does the CSV account match the chosen Xero account?
                if xero_acct_id and xero_bank_number:
                    parsed_txs = _parse_csv(data)
                    csv_aids = {t.get("account_id") or "" for t in parsed_txs if t.get("account_id")}
                    for aid in csv_aids:
                        if not _accounts_match(aid, xero_bank_number):
                            mismatches.append(
                                f"{fname}: CSV account ••••{aid[-4:]} doesn't match "
                                f"{xero_acct_name} (Xero ••••{xero_bank_number[-4:]})"
                            )
                        elif xero_acct_id and xero_acct_name and aid not in labelled_accounts:
                            cardfeed.set_account_label(
                                config.admin_db_file, aid,
                                xero_account_id=xero_acct_id,
                                xero_account_name=xero_acct_name,
                            )
                            labelled_accounts.add(aid)
                elif xero_acct_id and xero_acct_name:
                    # No bank number to check — just label.
                    parsed_txs = _parse_csv(data)
                    for t in parsed_txs:
                        aid = t.get("account_id") or ""
                        if aid and aid not in labelled_accounts:
                            cardfeed.set_account_label(
                                config.admin_db_file, aid,
                                xero_account_id=xero_acct_id,
                                xero_account_name=xero_acct_name,
                            )
                            labelled_accounts.add(aid)
                            break

            except ValueError as e:
                errors.append(f"{fname}: {str(e)[:200]}")
            except Exception as e:
                print(f"[cardfeed] csv upload error ({fname}): {e}", flush=True)
                errors.append(f"{fname}: couldn't parse — check it's a Lloyds CSV export")

        # Build result message.
        file_word = "file" if len(files) == 1 else "files"
        bits = [f"Processed {len(files)} {file_word}"]
        if total_added:
            bits.append(f"{total_added} new transaction(s) imported")
        if total_dup:
            bits.append(f"{total_dup} already on file")
        if total_old:
            bits.append(f"{total_old} older than the 12-month window")
        if labelled_accounts:
            bits.append(f"labelled as '{xero_acct_name}'")

        if mismatches:
            mismatch_detail = "; ".join(mismatches)
            session["save_notice"] = (
                f"error:Account mismatch detected — {mismatch_detail}. "
                "Upload cancelled for those files. Check you selected the right Xero account."
            )
        elif errors:
            err_detail = "; ".join(errors)
            session["save_notice"] = f"error:Some files failed: {err_detail}"
        else:
            session["save_notice"] = "success:" + ", ".join(bits) + "."
        return redirect("/cardfeed")

    @app.post("/cardfeed/csv-label")
    @require_login
    def cardfeed_csv_label():
        """Save (or update) the Xero account label for an existing uploaded account."""
        account_id = (request.form.get("account_id") or "").strip()
        xero_acct_raw = (request.form.get("xero_acct") or "").strip()
        if account_id and xero_acct_raw and "|" in xero_acct_raw:
            # Value format: AccountID|Name|BankAccountNumber
            parts = xero_acct_raw.split("|")
            xero_acct_id = parts[0] if len(parts) > 0 else ""
            xero_acct_name = parts[1] if len(parts) > 1 else ""
            xero_bank_number = parts[2] if len(parts) > 2 else ""
            if xero_acct_id and xero_acct_name:
                # Mismatch check: warn if the CSV account doesn't match the Xero BankAccountNumber.
                mismatch = False
                if xero_bank_number:
                    bn = xero_bank_number.replace("-", "").replace(" ", "")
                    ca = account_id.replace("-", "").replace(" ", "")
                    if not (bn.endswith(ca) or ca.endswith(bn)):
                        mismatch = True
                if mismatch:
                    session["save_notice"] = (
                        f"error:Mismatch — CSV account ••••{account_id[-4:]} doesn't match "
                        f"{xero_acct_name} (Xero ••••{xero_bank_number[-4:]}). "
                        "Label not saved."
                    )
                else:
                    cardfeed.set_account_label(
                        config.admin_db_file, account_id,
                        xero_account_id=xero_acct_id,
                        xero_account_name=xero_acct_name,
                    )
                    session["save_notice"] = f"success:Account ••••{account_id[-4:]} labelled as '{xero_acct_name}'."
            else:
                session["save_notice"] = "error:Couldn't save label — select a Xero account first."
        else:
            session["save_notice"] = "error:Couldn't save label — select a Xero account first."
        return redirect("/cardfeed")

    @app.post("/cardfeed/csv-clear")
    @require_login
    def cardfeed_csv_clear():
        try:
            cardfeed.csv_clear(config.admin_db_file)
            session["save_notice"] = "success:Uploaded CSV data cleared."
        except Exception as e:
            print(f"[cardfeed] csv clear error: {e}", flush=True)
            session["save_notice"] = "error:Couldn't clear the CSV data."
        return redirect("/cardfeed")

    @app.post("/cardfeed/disconnect")
    @require_login
    def cardfeed_disconnect():
        client = cardfeed.EnableBankingClient()
        try:
            cardfeed.disconnect(config.admin_db_file, client)
            session["save_notice"] = "success:Bank disconnected."
        except Exception as e:
            print(f"[cardfeed] disconnect error: {e}", flush=True)
            session["save_notice"] = f"error:{str(e)[:200]}"
        return redirect("/cardfeed")

    @app.get("/receipts/expenses/dump")
    @require_login
    def expense_dump_home():
        db = config.admin_db_file
        engineers = exp_store.list_engineers(db, include_inactive=False)
        batches = dump_store.list_batches(db, limit=15)
        paused = xero_is_disabled()

        eng_opts = "".join(
            "<option value='" + str(e["id"]) + "'>" + escape(e["name"])
            + (" (subcontractor)" if e.get("kind") == "subcontractor" else " (card)")
            + "</option>"
            for e in engineers
        )
        if not eng_opts:
            eng_opts = "<option value=''>No people yet — add one in Field Expenses</option>"

        # Card/bank accounts for the dropdown.  Use the same cached, scope-aware
        # path as the Settings page (_get_tenant_acct_themes) rather than a bare
        # live call, so the list is reliable and still populates (from cache)
        # while Xero is paused.  Accounts are raw Xero dicts; card_account stores
        # the account *name* because the bank-feed recon matches on
        # BankAccount.Name.
        bank_accounts = []
        try:
            _at, _tid, _ = _load_xero_at_tid(config)
            if _at and _tid:
                _rev, bank_accounts, _themes, _bw = _get_tenant_acct_themes(_at, _tid)
        except Exception:
            bank_accounts = []
        if bank_accounts:
            # Preselect the card used on the previous batch — most uploads are
            # for the same company card, and a forgotten selection means the
            # reconciliation can't run at all.
            try:
                _last_card = str(get_json_setting(
                    config.admin_db_file, "dump_last_card", "") or "").strip()
            except Exception:
                _last_card = ""
            _copts = (
                "<option value=''>— choose card / bank account —</option>"
                + "".join(
                    "<option value='" + escape(str(a.get("Name") or "")) + "'"
                    + (" selected"
                       if _last_card
                       and str(a.get("Name") or "").strip() == _last_card
                       else "")
                    + ">"
                    + escape(str(a.get("Name") or ""))
                    + ((" (" + escape(str(a.get("Code"))) + ")") if a.get("Code") else "")
                    + "</option>"
                    for a in bank_accounts
                )
            )
            card_input = (
                "<select name='card_account' class='block w-full rounded-lg "
                "border-gray-300 text-sm'>" + _copts + "</select>"
            )
        else:
            card_input = (
                "<input type='text' name='card_account' placeholder='e.g. Pow Wash "
                "/ Charge Card - Dan' class='block w-full rounded-lg border-gray-300 "
                "text-sm'>"
            )

        paused_banner = ""
        if paused:
            paused_banner = (
                "<div class='rounded-lg border border-amber-200 bg-amber-50 p-3 "
                "text-xs text-amber-800 mb-4'>Xero is currently paused, so live "
                "card-feed checks and subcontractor payment balancing are skipped. "
                "Everything else (OCR, AI coding, duplicate detection, cross-person "
                "image checks) works normally, and the Xero checks resume "
                "automatically when Xero is switched back on.</div>"
            )

        hints_saved_banner = ""
        if (request.args.get("saved") or "").strip() == "hints":
            hints_saved_banner = (
                "<div class='rounded-lg border border-emerald-200 bg-emerald-50 p-3 "
                "text-xs text-emerald-800 mb-4'>AI hints saved — they'll be used "
                "the next time receipts are coded.</div>"
            )

        cur_hints = get_ai_receipt_hints(db)
        ai_hints_panel = (
            "<details class='rounded-xl border border-gray-200 bg-white p-4 mb-6 "
            "shadow-sm'" + (" open" if cur_hints else "") + ">"
            "<summary class='cursor-pointer select-none text-sm font-semibold "
            "text-gray-800'>AI coding hints "
            "<span class='font-normal text-gray-400'>(optional)</span></summary>"
            "<form method='post' action='/receipts/expenses/dump/hints' "
            "class='mt-3 space-y-2'>"
            "<p class='text-xs text-gray-500'>Free-text guidance for how receipts "
            "are coded to Xero accounts. One rule per line, e.g. "
            "<span class='text-gray-600'>“petrol receipts below £30 can be "
            "machinery fuel”</span>. Applied to every receipt the AI codes.</p>"
            "<textarea name='hints' rows='4' class='block w-full rounded-lg "
            "border-gray-300 text-sm' placeholder='e.g. petrol receipts below £30 "
            "can be considered machinery fuel'>" + escape(cur_hints) + "</textarea>"
            "<button class='bg-indigo-600 hover:bg-indigo-700 text-white text-sm "
            "font-semibold px-4 py-2 rounded-lg'>Save hints</button>"
            "</form></details>"
        )

        upload_error_banner = ""
        _ue = (request.args.get("error") or "").strip()
        if _ue == "upload":
            upload_error_banner = (
                "<div class='rounded-lg border border-rose-200 bg-rose-50 p-3 "
                "text-xs text-rose-800 mb-4'>That upload didn't go through — the "
                "files couldn't be read (the upload may have been interrupted, or a "
                "file type wasn't a normal photo/PDF). Try again with fewer files at "
                "a time, and use JPG, PNG or PDF receipts.</div>"
            )

        _del = (request.args.get("deleted") or "").strip()
        if _del == "tests":
            _n = (request.args.get("n") or "0").strip()
            upload_error_banner += (
                "<div class='rounded-lg border border-emerald-200 bg-emerald-50 p-3 "
                "text-xs text-emerald-800 mb-4'>Deleted " + escape(_n) + " test dump"
                + ("s" if _n != "1" else "") + " and freed up their stored images.</div>"
            )
        elif _del:
            upload_error_banner += (
                "<div class='rounded-lg border border-emerald-200 bg-emerald-50 p-3 "
                "text-xs text-emerald-800 mb-4'>Dump deleted — its stored receipt "
                "images have been removed to free up space.</div>"
            )
        _busy = (request.args.get("busy") or "").strip()
        if _busy and _busy != "0":
            upload_error_banner += (
                "<div class='rounded-lg border border-amber-200 bg-amber-50 p-3 "
                "text-xs text-amber-800 mb-4'>" + escape(_busy) + " dump(s) were still "
                "being read and couldn't be deleted yet — try again once they finish."
                "</div>"
            )

        rows = ""
        n_test = 0
        for b in batches:
            s = b.get("summary") or {}
            try:
                import json as _json
                s = _json.loads(b.get("summary_json") or "{}")
            except Exception:
                s = {}
            chips = " ".join(
                k.replace("_", " ") + ": " + str(v) for k, v in (s.items() if isinstance(s, dict) else [])
            )
            is_test_b = bool(b.get("is_test"))
            if is_test_b:
                n_test += 1
            ready_n = int(s.get("new", 0) or 0) if isinstance(s, dict) else 0
            if is_test_b:
                confirm_msg = ("Delete this test dump and its stored receipt images? "
                               "This frees up space and cannot be undone.")
            elif ready_n > 0:
                confirm_msg = ("This dump still has " + str(ready_n) + " receipt(s) "
                               "ready to import that have NOT been imported yet. "
                               "Deleting removes those receipts and their images for "
                               "good. Continue?")
            else:
                confirm_msg = ("Delete this dump and its stored receipt images? "
                               "This cannot be undone.")
            test_tag = (
                "<span class='ml-2 text-[10px] font-semibold px-1.5 py-0.5 rounded "
                "bg-amber-100 text-amber-700 align-middle'>TEST</span>"
                if is_test_b else ""
            )
            rows += (
                "<div class='flex items-stretch border-b border-gray-100 "
                "hover:bg-gray-50'>"
                "<a href='/receipts/expenses/dump/" + b["id"] + "' class='flex-1 "
                "min-w-0 px-4 py-3'>"
                "<div class='flex items-center justify-between'>"
                "<span class='text-sm font-medium text-gray-800'>"
                + escape(b.get("label") or b["id"]) + test_tag + "</span>"
                "<span class='text-xs text-gray-400'>" + escape(str(b.get("created_at", ""))[:16]) + "</span>"
                "</div><div class='text-xs text-gray-500 mt-0.5'>"
                + str(b.get("total_count", 0)) + " receipts · " + escape(chips or "—")
                + "</div></a>"
                "<form method='post' action='/receipts/expenses/dump/" + b["id"]
                + "/delete' class='flex items-center pr-3' "
                "onsubmit=\"return confirm('" + confirm_msg + "')\">"
                "<button type='submit' title='Delete dump' class='text-xs "
                "font-medium text-rose-600 hover:text-rose-800 hover:bg-rose-50 "
                "px-2.5 py-1 rounded-lg'>Delete</button></form>"
                "</div>"
            )
        if not rows:
            rows = "<div class='px-4 py-6 text-center text-sm text-gray-400'>No dumps yet.</div>"

        delete_tests_bar = ""
        if n_test:
            delete_tests_bar = (
                "<form method='post' action='/receipts/expenses/dump/delete-tests' "
                "class='mt-3 text-right' onsubmit=\"return confirm('Delete all "
                + str(n_test) + " test dump(s) and their stored images? This cannot "
                "be undone.')\">"
                "<button type='submit' class='text-xs font-medium text-rose-600 "
                "hover:text-rose-800 hover:bg-rose-50 px-3 py-1.5 rounded-lg "
                "border border-rose-200'>Delete all test dumps ("
                + str(n_test) + ")</button></form>"
            )

        # Show a prominent "return to batch" banner when the most recent batch
        # is still active (ready or stuck-finalizing) so the user can get back
        # to it after an unexpected page change (e.g. an app restart).
        resume_banner = ""
        recent = batches[0] if batches else None
        if recent and recent.get("status") in ("ready", "finalizing"):
            b_label = escape(recent.get("label") or recent["id"])
            b_url = "/receipts/expenses/dump/" + recent["id"]
            b_items = recent.get("total_count") or 0
            banner_color = "indigo" if recent.get("status") == "ready" else "amber"
            banner_msg = (
                f"{b_items} receipt{'s' if b_items != 1 else ''} ready to review"
                if recent.get("status") == "ready"
                else "Upload in progress (or interrupted) — click to check"
            )
            resume_banner = (
                "<div class='rounded-xl border border-" + banner_color + "-200 "
                "bg-" + banner_color + "-50 p-4 mb-6 flex items-center "
                "justify-between gap-4'>"
                "<div>"
                "<div class='text-sm font-semibold text-" + banner_color + "-800'>"
                + b_label + "</div>"
                "<div class='text-xs text-" + banner_color + "-600 mt-0.5'>"
                + banner_msg + "</div>"
                "</div>"
                "<a href='" + b_url + "' class='flex-shrink-0 inline-block "
                "bg-" + banner_color + "-600 hover:bg-" + banner_color + "-700 "
                "text-white text-sm font-semibold px-4 py-2 rounded-lg'>"
                "Return to batch →</a>"
                "</div>"
            )

        body = (
            "<div class='max-w-3xl mx-auto px-4 py-8'>"
            "<div class='flex items-center justify-between mb-2'>"
            "<h1 class='text-2xl font-bold text-gray-900'>Receipt Dump</h1>"
            "<div class='flex items-center gap-3'>"
            "<a href='/cardfeed' class='text-sm text-indigo-600 hover:underline'>Card feed</a>"
            "<a href='/receipts/expenses' class='text-sm text-indigo-600 hover:underline'>← Field Expenses</a>"
            "</div>"
            "</div>"
            "<p class='text-sm text-gray-500 mb-6'>Bulk-upload a pile of past, "
            "unsubmitted receipts. They're read, coded to Xero accounts, and "
            "de-duplicated before you review and import them.</p>"
            + resume_banner
            + _dump_help_panel()
            + paused_banner
            + hints_saved_banner
            + ai_hints_panel
            + upload_error_banner
            + "<form method='post' action='/receipts/expenses/dump' "
            "enctype='multipart/form-data' class='bg-white rounded-xl border "
            "border-gray-200 p-6 shadow-sm space-y-4'>"
            "<div>"
            "<label class='block text-sm font-medium text-gray-700 mb-1'>"
            "Receipt photos / PDFs"
            + _dump_tooltip("Drag and drop files here, or click to browse. "
                            "Identical images and already-submitted receipts "
                            "are skipped automatically. Up to 300 files, 500 MB total.")
            + "</label>"
            "<input type='file' id='dump-file-input' name='receipts' multiple "
            "accept='image/*,application/pdf' class='hidden'>"
            "<div id='dump-drop-zone' "
            "class='relative flex flex-col items-center justify-center gap-2 "
            "w-full rounded-xl border-2 border-dashed border-gray-300 bg-gray-50 "
            "px-6 py-10 text-center cursor-pointer transition-colors "
            "hover:border-indigo-400 hover:bg-indigo-50'>"
            "<svg class='w-10 h-10 text-gray-300' fill='none' stroke='currentColor' "
            "stroke-width='1.5' viewBox='0 0 24 24'>"
            "<path stroke-linecap='round' stroke-linejoin='round' "
            "d='M3 16.5v2.25A2.25 2.25 0 005.25 21h13.5A2.25 2.25 0 0021 "
            "18.75V16.5m-13.5-9L12 3m0 0l4.5 4.5M12 3v13.5'/></svg>"
            "<p class='text-sm font-medium text-gray-600'>Drag &amp; drop files here</p>"
            "<p class='text-xs text-gray-400'>or click to browse &mdash; "
            "images &amp; PDFs, up to 300 files</p>"
            "<p id='dump-file-count' class='hidden text-sm font-semibold text-indigo-700'></p>"
            "</div>"
            "<script>"
            r"""
(function(){
  function dumpInit(){
  var zone=document.getElementById('dump-drop-zone');
  if(!zone){return;}
  var submitting=false;
  var inp=document.getElementById('dump-file-input');
  var cnt=document.getElementById('dump-file-count');
  var form=zone.closest('form');
  var btn=document.getElementById('dump-submit');
  var statusEl=document.getElementById('dump-status');
  var MAXFILES=300;
  function showCount(files){
    if(files&&files.length){
      cnt.textContent=files.length+' file'+(files.length===1?'':'s')+' selected';
      cnt.classList.remove('hidden');
      zone.classList.add('border-indigo-500','bg-indigo-50');
      zone.classList.remove('border-gray-300','bg-gray-50');
    }else{
      cnt.classList.add('hidden');
      zone.classList.remove('border-indigo-500','bg-indigo-50');
      zone.classList.add('border-gray-300','bg-gray-50');
    }
  }
  function setStatus(msg){
    if(!statusEl){return;}
    if(msg){statusEl.textContent=msg;statusEl.classList.remove('hidden');}
    else{statusEl.classList.add('hidden');}
  }
  zone.addEventListener('click',function(){inp.click();});
  inp.addEventListener('change',function(){showCount(inp.files);});
  zone.addEventListener('dragover',function(e){e.preventDefault();zone.classList.add('border-indigo-500','bg-indigo-50');zone.classList.remove('border-gray-300','bg-gray-50');});
  zone.addEventListener('dragleave',function(e){if(!zone.contains(e.relatedTarget)){zone.classList.remove('border-indigo-500','bg-indigo-50');zone.classList.add('border-gray-300','bg-gray-50');}});
  zone.addEventListener('drop',function(e){e.preventDefault();var dt=e.dataTransfer;if(!dt||!dt.files.length){return;}var transfer=new DataTransfer();for(var i=0;i<dt.files.length&&i<MAXFILES;i++){transfer.items.add(dt.files[i]);}inp.files=transfer.files;showCount(inp.files);});
  form.addEventListener('submit',function(e){
    e.preventDefault();
    if(submitting){return;}
    if(!inp.files||!inp.files.length){
      zone.classList.add('border-red-400');
      var msg=zone.querySelector('p.text-sm');
      if(msg){msg.textContent='Please select at least one file';}
      return;
    }
    submitting=true;
    if(btn){btn.disabled=true;btn.textContent='Working...';}
    var files=Array.prototype.slice.call(inp.files,0,MAXFILES);
    var total=files.length;
    var meta=new FormData();
    var fields=form.querySelectorAll('input,select,textarea');
    for(var i=0;i<fields.length;i++){
      var el=fields[i];
      if(el===inp){continue;}
      if(!el.name){continue;}
      if((el.type==='checkbox'||el.type==='radio')&&!el.checked){continue;}
      meta.append(el.name,el.value);
    }
    function reset(m){
      submitting=false;
      setStatus(m||'Upload failed - please check your connection and try again.');
      if(btn){btn.disabled=false;btn.textContent='Upload & process';}
    }
    function postForm(url,body){
      return new Promise(function(resolve,reject){
        var x=new XMLHttpRequest();
        x.open('POST',url,true);
        x.withCredentials=true;
        x.onload=function(){if(x.status>=200&&x.status<300){resolve(x);}else{reject(x);}};
        x.onerror=function(){reject(x);};
        x.send(body);
      });
    }
    var failed=0;
    function uploadOne(addUrl,idx,attempt){
      var f=files[idx];
      var fd=new FormData();
      fd.append('receipt',f,f.name);
      setStatus('Uploading receipt '+(idx+1)+' of '+total+' - '+(total-idx-1)+' to go... please keep this page open.');
      if(btn){btn.textContent='Uploading '+(idx+1)+'/'+total;}
      return postForm(addUrl,fd).catch(function(x){
        if((attempt||0)<2){
          return new Promise(function(r){setTimeout(r,800);}).then(function(){
            return uploadOne(addUrl,idx,(attempt||0)+1);
          });
        }
        failed++;
        return null;
      });
    }
    setStatus('Preparing upload of '+total+' file'+(total===1?'':'s')+'...');
    postForm('/receipts/expenses/dump/start',meta).then(function(x){
      var data={};try{data=JSON.parse(x.responseText);}catch(err){}
      if(!data.batch_id){throw x;}
      var addUrl='/receipts/expenses/dump/'+data.batch_id+'/add';
      var finishUrl='/receipts/expenses/dump/'+data.batch_id+'/finish';
      var chain=Promise.resolve();
      for(var k=0;k<total;k++){
        (function(idx){chain=chain.then(function(){return uploadOne(addUrl,idx,0);});})(k);
      }
      return chain.then(function(){
        if(total-failed<=0){
          reset('None of the files could be uploaded - please check your connection and try again.');
          return;
        }
        var okn=total-failed;
        setStatus('All uploaded ('+okn+' of '+total+(failed?(', '+failed+' could not be sent'):'')+') - processing now... this can take a little while, please keep this page open.');
        if(btn){btn.textContent='Processing...';}
        return postForm(finishUrl,new FormData()).then(function(x2){
          var d2={};try{d2=JSON.parse(x2.responseText);}catch(err){}
          window.location=d2.url||('/receipts/expenses/dump/'+data.batch_id);
        });
      });
    }).catch(function(){
      reset('Upload failed - please check your connection and try again.');
    });
  });
  }
  if(document.readyState==='loading'){document.addEventListener('DOMContentLoaded',dumpInit);}else{dumpInit();}
})();
"""
            "</script>"
            "</div>"
            "<div><label class='block text-sm font-medium text-gray-700 mb-1'>"
            "Person / card these belong to"
            + _dump_tooltip("Used to spot cross-person duplicates and to pick a "
                            "fallback expense account.")
            + "</label><select name='engineer_id' class='block w-full rounded-lg "
            "border-gray-300 text-sm'>" + eng_opts + "</select></div>"
            "<div><label class='block text-sm font-medium text-gray-700 mb-1'>"
            "Card / bank account these receipts are on (optional)"
            + _dump_tooltip("Pick the card or bank account in Xero these receipts "
                            "were paid from. After processing we'll show which "
                            "receipts match a transaction on this feed and which "
                            "are left over.")
            + "</label>" + card_input + "</div>"
            "<div><label class='block text-sm font-medium text-gray-700 mb-1'>"
            "Subcontractor account number (optional)"
            + _dump_tooltip("If this batch is a subcontractor you pay in batches, "
                            "enter their account. We'll balance the batch against "
                            "payments up to today once Xero is back on.")
            + "</label><input type='text' name='subcontractor_account' "
            "placeholder='e.g. 200 / SUB-ACME' class='block w-full rounded-lg "
            "border-gray-300 text-sm'></div>"
            "<div><label class='block text-sm font-medium text-gray-700 mb-1'>"
            "Batch label (optional)</label><input type='text' name='label' "
            "placeholder='e.g. Jan–Mar fuel receipts' class='block w-full "
            "rounded-lg border-gray-300 text-sm'></div>"
            "<label class='flex items-start gap-2 rounded-lg border border-gray-200 "
            "bg-gray-50 p-3 cursor-pointer'>"
            "<input type='checkbox' name='test_mode' value='1' "
            "class='mt-0.5 rounded border-gray-300 text-indigo-600'>"
            "<span class='text-sm text-gray-700'>Test mode"
            + _dump_tooltip("Runs the full read + AI coding + duplicate checks and "
                            "shows you exactly what the AI decided for each receipt "
                            "(account, VAT, splits) — but does NOT import anything "
                            "into Field Expenses. Use it to sanity-check before a "
                            "real run.")
            + "<span class='block text-xs text-gray-500'>Process and show the AI's "
            "decisions only — nothing is imported.</span></span></label>"
            "<button type='submit' id='dump-submit' class='w-full bg-indigo-600 "
            "hover:bg-indigo-700 text-white text-sm font-semibold py-2.5 "
            "rounded-lg disabled:opacity-60'>Upload &amp; process</button>"
            "<p id='dump-status' class='hidden text-xs font-medium "
            "text-indigo-600'></p>"
            "<p class='text-xs text-gray-400'>Photos are uploaded at full quality "
            "so faded or creased receipts read correctly. Processing reads each "
            "receipt with OCR + AI and may take a few seconds per file &mdash; "
            "the button will stay grey until it is done.</p>"
            "</form>"
            "<div class='mt-8'><h2 class='text-sm font-semibold text-gray-700 mb-2'>"
            "Recent dumps</h2><div class='bg-white rounded-xl border border-gray-200 "
            "overflow-hidden'>" + rows + "</div>" + delete_tests_bar + "</div>"
            "</div>"
        )
        return _page(body)

    @app.post("/receipts/expenses/dump/hints")
    @require_login
    def expense_dump_save_hints():
        db = config.admin_db_file
        set_ai_receipt_hints(db, request.form.get("hints") or "")
        return redirect("/receipts/expenses/dump?saved=hints")

    @app.post("/receipts/expenses/dump")
    @require_login
    def expense_dump_upload():
        db = config.admin_db_file
        try:
            # Accessing request.files triggers multipart parsing.  A malformed or
            # truncated upload (e.g. the connection dropped mid-send, or a part
            # had no Content-Disposition) makes Werkzeug raise BadRequest here —
            # which would otherwise surface to the user as a bare HTTP 400.
            files = request.files.getlist("receipts")
        except RequestEntityTooLarge:
            raise  # handled by the 413 error handler (nicer "too large" page)
        except Exception as exc:  # noqa: BLE001 — Werkzeug BadRequest etc.
            app.logger.warning(
                "Receipt dump upload could not be parsed: %r", exc
            )
            return redirect("/receipts/expenses/dump?error=upload")
        files = [f for f in files if f and (f.filename or "").strip()]
        if not files:
            return redirect("/receipts/expenses/dump")
        if len(files) > 300:
            files = files[:300]
        engineer_id = (request.form.get("engineer_id") or "").strip()
        eid = int(engineer_id) if engineer_id.isdigit() else None
        is_test = bool(request.form.get("test_mode"))
        batch = dump_store.create_batch(
            db,
            label=(request.form.get("label") or "").strip(),
            engineer_id=eid,
            subcontractor_account=(request.form.get("subcontractor_account") or "").strip(),
            card_account=(request.form.get("card_account") or "").strip(),
            is_test=is_test,
        )
        payloads = []
        for f in files:
            data = f.read()
            if not data:
                continue
            data, fname, fmime = _exp_heic_to_jpeg(data, f.filename, f.mimetype or "")
            mime = _exp_sniff_mime(data[:16]) or fmime or "application/octet-stream"
            payloads.append((data, fname, mime))
        _dump_process(batch, payloads)
        return redirect("/receipts/expenses/dump/" + batch["id"])

    def _dump_pending_dir(batch_id: str) -> Path:
        """Where files are staged between the per-file uploads and processing."""
        return Path(config.receipts_upload_dir) / "_dump_pending" / batch_id

    def _dump_delete_with_files(db: str, batch_id: str) -> str:
        """Delete a dump batch, its items, and its now-orphaned image files.

        An image file is only removed once no *remaining* dump item and no
        imported Field Expenses receipt still references it — so deleting a dump
        never breaks a receipt that already reached Xero/Field Expenses.

        Returns ``"deleted"``, ``"busy"`` (still being read — refuse so the
        background worker can't recreate orphaned items/files), or ``"missing"``.
        """
        batch = dump_store.get_batch(db, batch_id)
        if not batch:
            return "missing"
        # A batch that's genuinely stuck mid-processing is flipped to "ready" by
        # the recovery helper; one that's actively being read stays busy and must
        # not be deleted out from under the background thread.
        batch = _dump_stuck_recover(db, batch)
        if batch.get("status") in ("processing", "finalizing"):
            return "busy"
        files = dump_store.stored_files_for_batch(db, batch_id)
        existed = dump_store.delete_batch(db, batch_id)
        if not existed:
            return "missing"
        for f in files:
            if not f:
                continue
            if dump_store.stored_file_in_use(db, f) or exp_store.stored_file_in_use(db, f):
                continue
            try:
                os.remove(os.path.abspath(f))
            except OSError:
                pass  # already gone / unreadable — nothing to reclaim
        # Remove any leftover staging dir for this batch.
        try:
            import shutil as _shutil
            pdir = _dump_pending_dir(batch_id)
            if pdir.exists():
                _shutil.rmtree(pdir, ignore_errors=True)
        except Exception:
            pass
        return "deleted"

    @app.post("/receipts/expenses/dump/start")
    @require_login
    def expense_dump_start():
        """Create an empty batch so the browser can then upload files one at a
        time (far more reliable than one giant multipart POST on a slow link)."""
        db = config.admin_db_file
        engineer_id = (request.form.get("engineer_id") or "").strip()
        eid = int(engineer_id) if engineer_id.isdigit() else None
        is_test = bool(request.form.get("test_mode"))
        batch = dump_store.create_batch(
            db,
            label=(request.form.get("label") or "").strip(),
            engineer_id=eid,
            subcontractor_account=(request.form.get("subcontractor_account") or "").strip(),
            card_account=(request.form.get("card_account") or "").strip(),
            is_test=is_test,
        )
        # Remember the last card used so the upload form preselects it next
        # time — forgetting to pick the card is the #1 cause of a useless
        # "mixed accounts" reconciliation.
        _card = (request.form.get("card_account") or "").strip()
        if _card:
            try:
                set_json_setting(db, "dump_last_card", _card)
            except Exception:
                pass
        return jsonify({"batch_id": batch["id"]})

    @app.post("/receipts/expenses/dump/<batch_id>/set-card")
    @require_login
    def expense_dump_set_card(batch_id):
        """Set (or change) which card/bank account a batch's receipts were paid
        from, then bounce back to the results page so the reconciliation panel
        recomputes against that card only."""
        db = config.admin_db_file
        if not dump_store.get_batch(db, batch_id):
            return redirect("/receipts/expenses/dump")
        card = (request.form.get("card_account") or "").strip()
        dump_store.update_batch(db, batch_id, card_account=card)
        if card:
            try:
                set_json_setting(db, "dump_last_card", card)
            except Exception:
                pass
        return redirect("/receipts/expenses/dump/" + batch_id)

    @app.post("/receipts/expenses/dump/<batch_id>/add")
    @require_login
    def expense_dump_add(batch_id):
        """Stage a single uploaded receipt for a batch. Idempotent enough that a
        client retry of the same file is harmless (dedupe catches identical bytes
        at processing time)."""
        db = config.admin_db_file
        if not dump_store.get_batch(db, batch_id):
            return jsonify({"ok": False, "error": "no_batch"}), 404
        try:
            f = request.files.get("receipt")
        except RequestEntityTooLarge:
            raise
        except Exception as exc:  # noqa: BLE001 — Werkzeug BadRequest etc.
            app.logger.warning("Receipt dump add could not be parsed: %r", exc)
            return jsonify({"ok": False, "error": "parse"}), 400
        if not f or not (f.filename or "").strip():
            return jsonify({"ok": False, "error": "empty"}), 400
        data = f.read()
        if not data:
            return jsonify({"ok": False, "error": "empty"}), 400
        data, fname, _fmime = _exp_heic_to_jpeg(data, f.filename, f.mimetype or "")
        pdir = _dump_pending_dir(batch_id)
        pdir.mkdir(parents=True, exist_ok=True)
        seq = sum(1 for p in pdir.iterdir() if p.is_file())
        safe = Path(fname or "receipt").name
        (pdir / (f"{seq:04d}__" + safe)).write_bytes(data)
        return jsonify({"ok": True})

    @app.post("/receipts/expenses/dump/<batch_id>/finish")
    @require_login
    def expense_dump_finish(batch_id):
        """Kick off background processing for the batch and return the results
        URL immediately so the browser can redirect without waiting for OCR/AI.
        The results page polls (auto-refresh) until the batch status is ready."""
        db = config.admin_db_file
        batch = dump_store.get_batch(db, batch_id)
        if not batch:
            return jsonify({"ok": False, "error": "no_batch"}), 404
        result_url = "/receipts/expenses/dump/" + batch_id
        # Idempotency: already finalizing (bg thread running) or done → return URL.
        if batch.get("status") != "processing":
            return jsonify({"url": result_url})
        # Mark as finalizing so a retried /finish doesn't double-launch.
        dump_store.update_batch(db, batch_id, status="finalizing")
        # Load staged files into memory now, then clean up the staging dir
        # immediately (before the thread starts) so there's no race on cleanup.
        pdir = _dump_pending_dir(batch_id)
        payloads = []
        if pdir.exists():
            for p in sorted(pdir.iterdir()):
                if not p.is_file():
                    continue
                data = p.read_bytes()
                if not data:
                    continue
                name = p.name.split("__", 1)[1] if "__" in p.name else p.name
                mime = _exp_sniff_mime(data[:16]) or "application/octet-stream"
                payloads.append((data, name, mime))
        try:
            if pdir.exists():
                for p in pdir.iterdir():
                    try:
                        p.unlink()
                    except OSError:
                        pass
                pdir.rmdir()
        except OSError:
            pass
        # Run OCR + AI in a background thread so this request returns instantly.
        # The results page detects "finalizing" status and auto-refreshes until done.
        def _bg(b=batch, pl=payloads):
            try:
                if pl:
                    _dump_process(b, pl)
                else:
                    dump_store.update_batch(db, b["id"], status="ready", total_count=0)
            except Exception:
                try:
                    dump_store.update_batch(db, b["id"], status="ready", total_count=0)
                except Exception:
                    pass
        threading.Thread(target=_bg, daemon=True).start()
        return jsonify({"url": result_url})

    def _dump_last_activity(batch, items):
        """Most recent activity timestamp for a batch — the latest of the batch's
        own updated_at and any of its items' updated_at. Used as a heartbeat to
        tell a genuinely-working batch apart from one whose background thread was
        killed (e.g. an app restart) and is stuck in 'finalizing'."""
        latest = (batch or {}).get("updated_at") or ""
        for it in (items or []):
            u = it.get("updated_at") or ""
            if u > latest:
                latest = u
        return latest

    def _dump_stuck_recover(db, batch):
        """If a batch is stuck in 'finalizing' with no activity for >10 minutes,
        flip it to 'ready' so the user sees whatever was processed. Returns the
        (possibly refreshed) batch."""
        if not batch or batch.get("status") != "finalizing":
            return batch
        items = dump_store.list_items(db, batch["id"])
        last = _dump_last_activity(batch, items)
        try:
            updated = dt.datetime.fromisoformat((last or "").replace("Z", "+00:00"))
            age_mins = (dt.datetime.now(dt.timezone.utc) - updated).total_seconds() / 60
        except Exception:
            age_mins = 0
        # While items are being written, updated_at keeps advancing, so a stale
        # heartbeat means the worker died. But before the FIRST item is written a
        # slow/hung OCR can legitimately take a while, so give a longer grace when
        # nothing has been processed yet to avoid recovering a still-working batch.
        threshold = 10 if items else 25
        if age_mins > threshold:
            dump_store.update_batch(db, batch["id"], status="ready")
            return dump_store.get_batch(db, batch["id"]) or batch
        return batch

    @app.get("/receipts/expenses/dump/<batch_id>/status")
    @require_login
    def expense_dump_status(batch_id):
        """Lightweight JSON status for the results page to poll while a batch is
        being read — drives the progress bar without reloading the whole page."""
        db = config.admin_db_file
        batch = dump_store.get_batch(db, batch_id)
        if not batch:
            return jsonify({"ok": False}), 404
        batch = _dump_stuck_recover(db, batch)
        done = len(dump_store.list_items(db, batch_id))
        total = batch.get("total_count") or 0
        return jsonify({
            "ok": True,
            "status": batch.get("status"),
            "done": done,
            "total": total,
        })

    _CARD_PILL = {
        "matched": ("Matched to card", "bg-emerald-100 text-emerald-700"),
        "suggested": ("Possible match — confirm", "bg-amber-100 text-amber-800"),
        "price_only": ("Possible match by price — check", "bg-amber-100 text-amber-800"),
        "no_match": ("No card match", "bg-slate-100 text-slate-600"),
        "already_xero": ("Already in Xero", "bg-sky-100 text-sky-700"),
        "dup_upload": ("Duplicate upload", "bg-gray-100 text-gray-500"),
    }

    def _dump_item_card(item, exp_accounts, eng_map, batch_id, detailed=False,
                        recon_map=None):
        sym = "\u00a3"
        amt = item.get("amount_inc")
        amt_s = (sym + format(float(amt), ",.2f")) if amt is not None else "—"

        # Card-feed match pill (from the read-only bank reconciliation), shown on
        # the card itself so the status lives with the receipt instead of in a
        # separate panel.
        recon_row = (recon_map or {}).get(item.get("id")) or {}
        recon_kind = recon_row.get("kind")
        recon_tx = recon_row.get("tx")
        recon_ambiguous = bool(recon_row.get("ambiguous"))
        pill_html = ""
        recon_sub = ""
        if recon_kind in _CARD_PILL:
            lbl, cls = _CARD_PILL[recon_kind]
            if recon_kind in ("suggested", "price_only") and recon_ambiguous:
                lbl = "Possible match — needs a check"
                cls = "bg-orange-100 text-orange-700"
            pill_html = (
                "<span class='inline-block text-xs font-medium px-2 py-0.5 "
                "rounded-full " + cls + "'>" + lbl + "</span>"
            )
            if recon_kind == "suggested":
                tline = escape(recon_tx.get("contact") or "card transaction") \
                    if recon_tx else "card transaction"
                td = escape(_exp_uk_date(recon_tx.get("date") or "")) \
                    if recon_tx else ""
                if recon_ambiguous:
                    n = recon_row.get("n_sugg") or 2
                    recon_sub = (
                        "<div class='text-xs text-amber-700 mt-1'>"
                        + str(n) + " card payments match this amount &amp; shop "
                        "name \u2014 check which one is right in Xero.</div>"
                    )
                else:
                    recon_sub = (
                        "<div class='text-xs text-amber-700 mt-1'>Possible card "
                        "payment: " + tline + (" \u00b7 " + td if td else "")
                        + " \u2014 amount &amp; shop match (date differs, please "
                        "confirm).</div>"
                    )
            elif recon_kind == "price_only":
                # Same amount in the feed but the shop name didn't match (often
                # because the OCR mangled it) — flag it as a price-based hint.
                tline = escape(recon_tx.get("contact") or "card transaction") \
                    if recon_tx else "card transaction"
                td = escape(_exp_uk_date(recon_tx.get("date") or "")) \
                    if recon_tx else ""
                if recon_ambiguous:
                    n = recon_row.get("n_sugg") or 2
                    recon_sub = (
                        "<div class='text-xs text-amber-700 mt-1'>"
                        + str(n) + " card payments have this exact amount (shop "
                        "name didn't match) \u2014 check if one of these is it.</div>"
                    )
                else:
                    recon_sub = (
                        "<div class='text-xs text-amber-700 mt-1'>Same amount in "
                        "the feed: " + tline + (" \u00b7 " + td if td else "")
                        + " \u2014 shop name didn't match, may this be it?</div>"
                    )
            elif recon_tx:
                tline = escape(recon_tx.get("contact") or "card transaction")
                td = escape(_exp_uk_date(recon_tx.get("date") or ""))
                note = ""
                if recon_tx.get("reconciled"):
                    note = " \u00b7 already reconciled in Xero"
                elif recon_tx.get("has_attachment"):
                    note = " \u00b7 receipt already attached in Xero"
                recon_sub = (
                    "<div class='text-xs text-gray-500 mt-1'>Card: " + tline
                    + " \u00b7 " + td + note + "</div>"
                )

        # What the AI decided — account chosen (code + name) and the VAT split.
        ai_code = (item.get("category_account_code") or "").strip()
        ai_name = (item.get("category_account_name") or "").strip()
        acct_label = (
            (ai_name + (" (" + ai_code + ")" if ai_code else ""))
            if (ai_name or ai_code) else "no account chosen"
        )
        ai_html = ""
        if detailed:
            ex = item.get("amount_ex")
            vat = item.get("vat_amount")
            ex_s = (sym + format(float(ex), ",.2f")) if ex is not None else "—"
            vat_s = (sym + format(float(vat), ",.2f")) if vat is not None else "—"
            err = (item.get("ocr_error") or "").strip()
            ai_html = (
                "<div class='mt-2 rounded-lg bg-indigo-50 border border-indigo-100 "
                "p-2 text-xs text-indigo-900'>"
                "<div class='font-semibold'>AI decided</div>"
                "<div>Account: " + escape(acct_label) + "</div>"
                "<div>Net " + ex_s + " · VAT " + vat_s + " · Total " + amt_s + "</div>"
                + ("<div class='text-rose-600 mt-1'>Read issue: " + escape(err) + "</div>"
                   if err else "")
                + "</div>"
            )

        segs = item.get("segments") or []
        seg_html = ""
        if len(segs) > 1:
            if detailed:
                rows_ = "".join(
                    "<div class='flex justify-between'><span>"
                    + escape(s.get("label", "") or "part") + " → "
                    + escape(s.get("account_name", "") or "—")
                    + ((" (" + escape(s.get("account_code", "")) + ")")
                       if s.get("account_code") else "")
                    + "</span><span>" + sym
                    + format(float(s.get("gross") or 0), ",.2f") + "</span></div>"
                    for s in segs
                )
                seg_html = (
                    "<div class='mt-1 text-xs text-gray-600 border-l-2 border-gray-200 "
                    "pl-2'><div class='font-medium text-gray-500'>Split into "
                    + str(len(segs)) + " accounts</div>" + rows_ + "</div>"
                )
            else:
                seg_html = (
                    "<div class='text-xs text-gray-500 mt-1'>Split: "
                    + ", ".join(
                        escape(s.get("label", "")) + " → " + escape(s.get("account_name", ""))
                        for s in segs
                    ) + "</div>"
                )
        reason = item.get("dup_reason") or ""
        # Don't show the stored "not found in the company card feed" note when the
        # live reconciliation has since matched this receipt to a card payment —
        # the pill is the source of truth and the two would otherwise contradict.
        if recon_kind in ("matched", "suggested", "price_only") \
                and "card feed" in reason.lower():
            reason = ""
        reason_html = (
            "<div class='text-xs text-gray-600 mt-1'>" + escape(reason) + "</div>"
            if reason else ""
        )
        ic = item.get("image_check") or {}
        ic_html = ""
        if ic.get("available") is not None and (ic.get("reason") or ic.get("same") is not None):
            verdict = "same receipt" if ic.get("same") else "different"
            conf = ic.get("confidence")
            ic_html = (
                "<div class='text-xs text-purple-700 mt-1'>Image check: " + verdict
                + ((" (" + str(conf) + ")") if conf else "")
                + (" — " + escape(ic.get("reason")) if ic.get("reason") else "") + "</div>"
            )
        view_html = ""
        if item.get("stored_file"):
            view_html = (
                "<a href='/receipts/expenses/dump/" + batch_id + "/item/"
                + item["id"] + "/image' target='_blank' rel='noopener' "
                "class='inline-flex items-center gap-1 mt-2 px-2.5 py-1 text-xs "
                "font-medium bg-sky-100 text-sky-800 rounded-lg hover:bg-sky-200'>"
                "<svg class='w-3.5 h-3.5' fill='none' stroke='currentColor' "
                "stroke-width='2' viewBox='0 0 24 24'><path stroke-linecap='round' "
                "stroke-linejoin='round' d='M2.036 12.322a1.012 1.012 0 010-.639C3.423 "
                "7.51 7.36 4.5 12 4.5c4.638 0 8.573 3.007 9.963 7.178.07.207.07.431 0 "
                ".639C20.577 16.49 16.64 19.5 12 19.5c-4.638 0-8.573-3.007-9.963-7.178z'/>"
                "<path stroke-linecap='round' stroke-linejoin='round' d='M15 12a3 3 0 "
                "11-6 0 3 3 0 016 0z'/></svg>View receipt</a>"
            )
        controls = ""
        is_active = item["status"] in dump_store.ACTIVE_STATUSES
        if is_active:
            opts = _exp_acct_options(
                exp_accounts, item.get("category_account_code") or "",
                default_label="— choose account —",
            )
            controls = (
                "<form method='post' action='/receipts/expenses/dump/" + batch_id
                + "/item/" + item["id"] + "' class='flex flex-wrap items-center "
                "gap-2 mt-2'>"
                "<select name='account_code' class='rounded-lg border-gray-300 "
                "text-xs py-1'>" + opts + "</select>"
                "<button name='action' value='keep' class='px-2.5 py-1 text-xs "
                "font-medium bg-emerald-600 text-white rounded-lg'>Keep</button>"
                "<button name='action' value='ignore' class='px-2.5 py-1 text-xs "
                "font-medium bg-gray-200 text-gray-700 rounded-lg'>Ignore</button>"
                "</form>"
            )
        # For inactive items (duplicates, ignored, imported) wrap the duplicate
        # reason and image-check inside a collapsible. The "View receipt" button
        # stays always-visible so it's never buried.
        if not is_active and (reason_html or ic_html):
            collapse = (
                "<details class='mt-1'>"
                "<summary style='list-style:none;display:flex;align-items:center;"
                "gap:3px' class='text-xs text-gray-400 cursor-pointer select-none "
                "hover:text-gray-500'>"
                "<svg style='width:9px;height:9px;flex-shrink:0' fill='none' "
                "stroke='currentColor' stroke-width='2.5' viewBox='0 0 24 24'>"
                "<path stroke-linecap='round' stroke-linejoin='round' "
                "d='M19 9l-7 7-7-7'/></svg>details</summary>"
                + reason_html + ic_html
                + "</details>"
            )
            extras = ai_html + seg_html + recon_sub + collapse + view_html
        else:
            extras = ai_html + seg_html + recon_sub + reason_html + ic_html + view_html
        return (
            "<div class='px-4 py-3 border-b border-gray-100'>"
            "<div class='flex items-center justify-between gap-3'>"
            "<div class='min-w-0'><div class='text-sm font-medium text-gray-800 "
            "truncate'>" + escape(item.get("merchant") or item.get("filename") or "Receipt")
            + "</div><div class='text-xs text-gray-500'>"
            + escape(_exp_uk_date(item.get("purchased_on") or "")) + " · "
            + escape(item.get("category_account_name") or "no account")
            + "</div></div><div class='shrink-0 text-right'>"
            "<div class='text-sm font-semibold text-gray-900'>" + amt_s + "</div>"
            + ("<div class='mt-1'>" + pill_html + "</div>" if pill_html else "")
            + "</div></div>"
            + extras + controls + "</div>"
        )

    def _dump_sub_summary(batch, items):
        """Compute the subcontractor reconciliation figures for a batch.

        Returns None when the batch isn't for a subcontractor, else a dict with
        owed/batch_total/account/paid/net plus a human note. Xero payment lookup
        is gated by the kill-switch and degrades gracefully.
        """
        db = config.admin_db_file
        eng = (
            exp_store.get_engineer(db, int(batch["engineer_id"]))
            if batch.get("engineer_id") else None
        )
        if not (eng and eng.get("kind") == "subcontractor"):
            return None
        owed = exp_store.amount_owed_to_engineer(db, eng["id"])
        batch_total = sum(
            float(it.get("amount_inc") or 0) for it in items
            if it["status"] in (dump_store.STATUS_NEW, dump_store.STATUS_IMPORTED)
        )
        acct = batch.get("subcontractor_account") or ""
        out = {
            "engineer": eng, "owed": owed, "batch_total": batch_total,
            "account": acct, "paid_total": None, "net_balance": None,
            "paused": xero_is_disabled(),
            "note": "Xero payment balancing resumes when Xero is switched back on.",
        }
        if not xero_is_disabled() and acct:
            try:
                today = dt.datetime.now(dt.timezone.utc).date()
                client = build_xero_client(config)
                paid = client.get_payments_to_account(acct, end_date=today)
                out["paid_total"] = float(paid.get("total") or 0.0)
                out["net_balance"] = round(out["paid_total"] - owed, 2)
                out["note"] = "Balanced against Xero payments to this account up to today."
            except Exception as exc:
                out["note"] = (
                    "Could not read Xero payments for this account ("
                    + str(exc).splitlines()[0][:120] + ")."
                )
        elif not xero_is_disabled() and not acct:
            out["note"] = (
                "Enter a subcontractor account number to balance against Xero payments."
            )
        return out

    def _dump_balance_panel(summary):
        if not summary:
            return ""
        sym = "\u00a3"
        paid_line = ""
        if summary.get("paid_total") is not None:
            net = summary.get("net_balance") or 0.0
            bal_word = "in credit" if net >= 0 else "still owed"
            paid_line = (
                "<div class='text-sm text-gray-600'>Paid to this account "
                "(Xero, to date): " + sym + format(summary["paid_total"], ",.2f") + "</div>"
                "<div class='text-sm text-gray-800 font-semibold'>Net balance: "
                + sym + format(abs(net), ",.2f") + " " + bal_word + "</div>"
            )
        return (
            "<div class='bg-white rounded-xl border border-gray-200 p-5 mb-6'>"
            "<h2 class='text-sm font-semibold text-gray-700 mb-2'>"
            "Subcontractor balance" + _dump_tooltip(
                "Owed = outstanding receipts for this subcontractor. We balance "
                "this against payments made to their account up to today.")
            + "</h2>"
            "<div class='text-sm text-gray-600'>Account: <span "
            "class='font-mono'>" + escape(summary.get("account") or "—") + "</span></div>"
            "<div class='text-sm text-gray-600'>This batch: " + sym
            + format(summary["batch_total"], ",.2f") + "</div>"
            "<div class='text-sm text-gray-800 font-semibold'>Currently "
            "outstanding (owed): " + sym + format(summary["owed"], ",.2f") + "</div>"
            + paid_line
            + "<div class='text-xs text-gray-400 mt-1'>" + escape(summary["note"]) + "</div>"
            "</div>"
        )

    @app.get("/receipts/expenses/dump/<batch_id>/item/<item_id>/image")
    @require_login
    def expense_dump_item_image(batch_id, item_id):
        db = config.admin_db_file
        item = dump_store.get_item(db, item_id)
        if not item or item.get("batch_id") != batch_id:
            return ("Not found", 404)
        path = os.path.abspath(item.get("stored_file") or "")
        if not path or not os.path.exists(path):
            return ("Receipt image not available", 404)
        with open(path, "rb") as fh:
            head = fh.read(16)
        safe_mime = _exp_sniff_mime(head)
        if safe_mime and safe_mime.startswith("image/"):
            return send_file(path, mimetype=safe_mime)
        if safe_mime == "application/pdf":
            return send_file(
                path,
                mimetype="application/pdf",
                as_attachment=True,
                download_name="receipt.pdf",
            )
        return send_file(path, as_attachment=True, download_name="receipt")

    @app.get("/receipts/expenses/dump/<batch_id>")
    @require_login
    def expense_dump_results(batch_id):
        db = config.admin_db_file
        batch = dump_store.get_batch(db, batch_id)
        if not batch:
            return redirect("/receipts/expenses/dump")
        # While OCR/AI processing runs in the background, show a live progress bar.
        # If the batch is stuck in "finalizing" with no activity for >10 minutes
        # (e.g. the background thread was killed by an app restart), auto-recover.
        batch = _dump_stuck_recover(db, batch)
        if batch.get("status") in ("processing", "finalizing"):
            total0 = batch.get("total_count") or 0
            done0 = len(dump_store.list_items(db, batch_id))
            pct0 = min(99, round(done0 / total0 * 100)) if total0 else 3
            count_line = (
                ("Read " + str(done0) + " of " + str(total0) + " receipts")
                if total0 else "Getting your receipts ready…"
            )
            # No meta-refresh (that caused the whole page to flash). Instead we
            # poll a tiny JSON endpoint and update the bar in place, then reload
            # ONCE when processing is done.
            body = (
                "<div class='max-w-3xl mx-auto px-4 py-16 text-center'>"
                "<div class='inline-flex items-center justify-center w-16 h-16 "
                "bg-indigo-100 rounded-2xl mb-6'>"
                "<svg class='w-8 h-8 text-indigo-600 animate-spin' fill='none' "
                "viewBox='0 0 24 24'>"
                "<circle class='opacity-25' cx='12' cy='12' r='10' "
                "stroke='currentColor' stroke-width='4'></circle>"
                "<path class='opacity-75' fill='currentColor' d='M4 12a8 8 0 "
                "018-8V0C5.373 0 0 5.373 0 12h4z'></path></svg>"
                "</div>"
                "<h1 class='text-xl font-bold text-gray-900 mb-2'>Reading your receipts…</h1>"
                "<p class='text-sm text-gray-500 mb-6'>Running OCR and AI "
                "categorisation. You don't need to do anything — keep this page open.</p>"
                "<div class='max-w-md mx-auto'>"
                "<div class='w-full bg-gray-200 rounded-full h-3 overflow-hidden'>"
                "<div id='dump-progress-bar' class='bg-indigo-600 h-3 rounded-full "
                "transition-all duration-500 ease-out' style='width:" + str(pct0) + "%'>"
                "</div></div>"
                "<p id='dump-progress-text' class='text-sm font-medium text-gray-600 "
                "mt-3'>" + count_line + "</p>"
                "</div>"
                "</div>"
                "<script>(function(){"
                "var bid=" + json.dumps(batch_id) + ";"
                "var bar=document.getElementById('dump-progress-bar');"
                "var txt=document.getElementById('dump-progress-text');"
                "function poll(){"
                "fetch('/receipts/expenses/dump/'+bid+'/status',{headers:{'Accept':'application/json'}})"
                ".then(function(r){return r.json();}).then(function(d){"
                "if(!d||!d.ok){setTimeout(poll,2500);return;}"
                "if(d.status!=='processing'&&d.status!=='finalizing'){window.location.reload();return;}"
                "var total=d.total||0,done=d.done||0;"
                "var pct=total?Math.min(99,Math.round(done/total*100)):3;"
                "if(bar)bar.style.width=pct+'%';"
                "if(txt)txt.textContent=total?('Read '+done+' of '+total+' receipts'):'Reading receipts…';"
                "setTimeout(poll,1500);"
                "}).catch(function(){setTimeout(poll,2500);});"
                "}setTimeout(poll,1200);})();</script>"
            )
            return _page(body)
        items = dump_store.list_items(db, batch_id)
        is_test = bool(batch.get("is_test"))
        exp_accounts: list = []
        try:
            _at, _tid, _ = _load_xero_at_tid(config)
            exp_accounts, _ = _get_xero_expense_accounts(_at, _tid, db)
        except Exception:
            exp_accounts = []
        eng_map = {e["id"]: e["name"] for e in exp_store.list_engineers(db)}

        groups = [
            (dump_store.STATUS_NEEDS_ACCOUNT, "Needs an account — please pick one", "amber"),
            (dump_store.STATUS_SUSPICIOUS, "Suspicious — possible cross-person duplicate", "red"),
            (dump_store.STATUS_POSSIBLE_DUP, "Possible duplicates", "amber"),
            (dump_store.STATUS_NEW, "Ready to import", "emerald"),
            (dump_store.STATUS_DUPLICATE, "Ignored — duplicates / already reconciled", "gray"),
            (dump_store.STATUS_IMPORTED, "Imported", "sky"),
            (dump_store.STATUS_IGNORED, "Ignored by you", "gray"),
        ]
        # Read-only card-feed reconciliation, computed once. The per-receipt
        # match status is shown as a pill on each item card (in the grouped
        # sections), and the leftover card transactions go in a collapsible at
        # the bottom of the page.
        recon = _dump_bank_feed_recon(items, batch)
        recon_map = {
            r["item"]["id"]: r
            for r in ((recon or {}).get("rows") or [])
            if r.get("item")
        }

        sections = ""
        for status, title, colour in groups:
            sel = [it for it in items if it["status"] == status]
            if not sel:
                continue
            cards = "".join(
                _dump_item_card(it, exp_accounts, eng_map, batch_id,
                                detailed=is_test, recon_map=recon_map)
                for it in sel
            )
            sections += (
                "<div class='mb-6'><div class='flex items-center gap-2 mb-2'>"
                "<span class='text-xs font-semibold px-2 py-0.5 rounded-full bg-"
                + colour + "-100 text-" + colour + "-700'>" + str(len(sel))
                + "</span><h2 class='text-sm font-semibold text-gray-700'>"
                + escape(title) + "</h2></div>"
                "<div class='bg-white rounded-xl border border-gray-200 "
                "overflow-hidden'>" + cards + "</div></div>"
            )

        sub_summary = _dump_sub_summary(batch, items)
        is_sub = sub_summary is not None

        ready = [it for it in items if it["status"] == dump_store.STATUS_NEW]
        import_bar = ""
        if ready and not is_test:
            total_ready = sum(float(it.get("amount_inc") or 0) for it in ready)
            count_line = (
                "<div class='text-sm text-gray-700'><span class='font-semibold'>"
                + str(len(ready)) + "</span> receipts ready · \u00a3"
                + format(total_ready, ",.2f") + "</div>"
            )
            if is_sub:
                # Subcontractors get a reconciliation preview before importing.
                import_bar = (
                    "<div class='sticky bottom-4 bg-white rounded-xl border "
                    "border-emerald-200 shadow-lg p-4 flex items-center "
                    "justify-between'>" + count_line
                    + "<a href='/receipts/expenses/dump/" + batch_id
                    + "/confirm' class='bg-emerald-600 hover:bg-emerald-700 text-white "
                    "text-sm font-semibold px-4 py-2 rounded-lg'>Review &amp; "
                    "import…</a></div>"
                )
            else:
                import_bar = (
                    "<form method='post' action='/receipts/expenses/dump/" + batch_id
                    + "/import' class='sticky bottom-4 bg-white rounded-xl border "
                    "border-emerald-200 shadow-lg p-4 flex items-center "
                    "justify-between'>" + count_line
                    + "<button class='bg-emerald-600 hover:bg-emerald-700 text-white "
                    "text-sm font-semibold px-4 py-2 rounded-lg'>Import into Field "
                    "Expenses</button></form>"
                )

        test_banner = ""
        if is_test:
            n_show = len([it for it in items if it["status"] in (
                dump_store.STATUS_NEW, dump_store.STATUS_NEEDS_ACCOUNT,
                dump_store.STATUS_POSSIBLE_DUP, dump_store.STATUS_SUSPICIOUS)])
            test_banner = (
                "<div class='bg-amber-50 border border-amber-200 rounded-xl p-4 "
                "mb-6'><div class='text-sm font-semibold text-amber-800'>Test mode "
                "— nothing has been or will be imported</div>"
                "<div class='text-xs text-amber-700 mt-1'>This run shows you exactly "
                "what the AI decided for each receipt (account, VAT and any splits). "
                + str(n_show) + " receipt(s) would be put forward. To actually import, "
                "upload a new batch with Test mode switched off.</div></div>"
            )

        balance_html = _dump_balance_panel(sub_summary)
        outstanding_html = _dump_outstanding_panel(recon, batch_id)

        if is_test:
            _del_msg = ("Delete this test dump and its stored receipt images? "
                        "This frees up space and cannot be undone.")
        elif ready:
            _del_msg = ("This dump still has " + str(len(ready)) + " receipt(s) "
                        "ready to import that have NOT been imported yet. Deleting "
                        "removes those receipts and their images for good. Continue?")
        else:
            _del_msg = ("Delete this dump and its stored receipt images? "
                        "This cannot be undone.")
        delete_header = (
            "<div class='flex items-center gap-3'>"
            "<a href='/receipts/expenses/dump' class='text-sm text-indigo-600 "
            "hover:underline'>← All dumps</a>"
            "<form method='post' action='/receipts/expenses/dump/" + batch_id
            + "/delete' onsubmit=\"return confirm('" + _del_msg + "')\">"
            "<button type='submit' class='text-sm font-medium text-rose-600 "
            "hover:text-rose-800 hover:bg-rose-50 px-2.5 py-1 rounded-lg'>"
            "Delete dump</button></form></div>"
        )
        body = (
            "<div class='max-w-3xl mx-auto px-4 py-8'>"
            "<div class='flex items-center justify-between mb-4'>"
            "<h1 class='text-xl font-bold text-gray-900'>"
            + escape(batch.get("label") or "Receipt dump")
            + ("<span class='ml-2 align-middle text-xs font-semibold px-2 py-0.5 "
               "rounded-full bg-amber-100 text-amber-700'>TEST</span>" if is_test else "")
            + "</h1>"
            + delete_header + "</div>"
            + test_banner + balance_html
            + (sections or "<p class='text-sm text-gray-500'>No "
            "receipts in this batch.</p>")
            + outstanding_html + import_bar
            + "</div>"
        )
        return _page(body)

    @app.post("/receipts/expenses/dump/delete-tests")
    @require_login
    def expense_dump_delete_tests():
        """Delete every test (dry-run) dump in one go, freeing their images."""
        db = config.admin_db_file
        ids = dump_store.test_batch_ids(db)
        n = 0
        busy = 0
        for bid in ids:
            res = _dump_delete_with_files(db, bid)
            if res == "deleted":
                n += 1
            elif res == "busy":
                busy += 1
        url = "/receipts/expenses/dump?deleted=tests&n=" + str(n)
        if busy:
            url += "&busy=" + str(busy)
        return redirect(url)

    @app.post("/receipts/expenses/dump/<batch_id>/delete")
    @require_login
    def expense_dump_delete(batch_id):
        """Delete a single dump (DB rows + its image files)."""
        db = config.admin_db_file
        res = _dump_delete_with_files(db, batch_id)
        if res == "busy":
            return redirect("/receipts/expenses/dump/" + batch_id + "?error=busy")
        return redirect("/receipts/expenses/dump?deleted=1")

    @app.get("/receipts/expenses/dump/<batch_id>/confirm")
    @require_login
    def expense_dump_confirm(batch_id):
        db = config.admin_db_file
        batch = dump_store.get_batch(db, batch_id)
        if not batch:
            return redirect("/receipts/expenses/dump")
        items = dump_store.list_items(db, batch_id)
        if batch.get("is_test"):
            return redirect("/receipts/expenses/dump/" + batch_id)
        summary = _dump_sub_summary(batch, items)
        if summary is None:
            # Not a subcontractor — confirmation preview only applies to those.
            return redirect("/receipts/expenses/dump/" + batch_id)

        ready = [it for it in items if it["status"] == dump_store.STATUS_NEW]
        if not ready:
            return redirect("/receipts/expenses/dump/" + batch_id)
        sym = "\u00a3"
        rows = "".join(
            "<div class='flex items-center justify-between px-4 py-2 border-b "
            "border-gray-100 text-sm'><div class='min-w-0'><div class='font-medium "
            "text-gray-800 truncate'>" + escape(it.get("merchant") or it.get("filename")
            or "Receipt") + "</div><div class='text-xs text-gray-500'>"
            + escape(_exp_uk_date(it.get("purchased_on") or "")) + " · "
            + escape(it.get("category_account_name") or "no account")
            + "</div></div><div class='font-semibold text-gray-900'>" + sym
            + format(float(it.get("amount_inc") or 0), ",.2f") + "</div></div>"
            for it in ready
        )
        total_ready = sum(float(it.get("amount_inc") or 0) for it in ready)
        owed_after = summary["owed"] + total_ready
        after_lines = (
            "<div class='text-sm text-gray-600'>Outstanding now: " + sym
            + format(summary["owed"], ",.2f") + "</div>"
            "<div class='text-sm text-gray-600'>This batch adds: " + sym
            + format(total_ready, ",.2f") + "</div>"
            "<div class='text-sm text-gray-800 font-semibold'>Outstanding after "
            "import: " + sym + format(owed_after, ",.2f") + "</div>"
        )
        body = (
            "<div class='max-w-3xl mx-auto px-4 py-8'>"
            "<div class='flex items-center justify-between mb-4'>"
            "<h1 class='text-xl font-bold text-gray-900'>Confirm subcontractor "
            "import</h1><a href='/receipts/expenses/dump/" + batch_id
            + "' class='text-sm text-indigo-600 hover:underline'>← Back</a></div>"
            "<p class='text-sm text-gray-600 mb-4'>Review the reconciliation for "
            "<span class='font-semibold'>" + escape(summary["engineer"]["name"])
            + "</span> before importing. Nothing has been imported yet.</p>"
            + _dump_balance_panel(summary)
            + "<div class='bg-white rounded-xl border border-gray-200 p-5 mb-6'>"
            "<h2 class='text-sm font-semibold text-gray-700 mb-2'>After this "
            "import</h2>" + after_lines + "</div>"
            "<div class='bg-white rounded-xl border border-gray-200 overflow-hidden "
            "mb-6'><div class='px-4 py-2 bg-gray-50 text-xs font-semibold "
            "text-gray-600'>" + str(len(ready)) + " receipt(s) to import · " + sym
            + format(total_ready, ",.2f") + "</div>" + rows + "</div>"
            "<form method='post' action='/receipts/expenses/dump/" + batch_id
            + "/import' class='sticky bottom-4 bg-white rounded-xl border "
            "border-emerald-200 shadow-lg p-4 flex items-center justify-between'>"
            "<a href='/receipts/expenses/dump/" + batch_id + "' class='text-sm "
            "text-gray-600 hover:underline'>Cancel</a>"
            "<button class='bg-emerald-600 hover:bg-emerald-700 text-white text-sm "
            "font-semibold px-4 py-2 rounded-lg'>Confirm &amp; import "
            + str(len(ready)) + " receipts</button></form>"
            "</div>"
        )
        return _page(body)

    @app.post("/receipts/expenses/dump/<batch_id>/item/<item_id>")
    @require_login
    def expense_dump_item_update(batch_id, item_id):
        db = config.admin_db_file
        item = dump_store.get_item(db, item_id)
        if not item or item.get("batch_id") != batch_id:
            return redirect("/receipts/expenses/dump/" + batch_id)
        action = (request.form.get("action") or "keep").strip()
        code = (request.form.get("account_code") or "").strip()
        fields: dict = {}
        if code and code != (item.get("category_account_code") or ""):
            name = ""
            try:
                _at, _tid, _ = _load_xero_at_tid(config)
                accts, _ = _get_xero_expense_accounts(_at, _tid, db)
                for a in accts:
                    if str(a.get("Code") or "").strip() == code:
                        name = str(a.get("Name") or "").strip()
                        break
            except Exception:
                name = ""
            fields["category_account_code"] = code
            fields["category_account_name"] = name
        if action == "ignore":
            fields["status"] = dump_store.STATUS_IGNORED
        else:
            fields["status"] = dump_store.STATUS_NEW
        dump_store.update_item(db, item_id, **fields)
        return redirect("/receipts/expenses/dump/" + batch_id)

    @app.post("/receipts/expenses/dump/<batch_id>/import")
    @require_login
    def expense_dump_import(batch_id):
        db = config.admin_db_file
        batch = dump_store.get_batch(db, batch_id)
        if not batch:
            return redirect("/receipts/expenses/dump")
        if batch.get("is_test"):
            # Test-mode batches are dry-run only — never import.
            return redirect("/receipts/expenses/dump/" + batch_id)
        items = dump_store.list_items(db, batch_id)
        default_eid = batch.get("engineer_id")
        imported = 0
        for it in items:
            if it["status"] != dump_store.STATUS_NEW:
                continue
            eid = it.get("assigned_engineer_id") or default_eid
            if not eid:
                continue
            exp_store.create_receipt(
                db,
                engineer_id=int(eid),
                merchant=it.get("merchant", ""),
                purchased_on=it.get("purchased_on", ""),
                amount_inc=it.get("amount_inc"),
                amount_ex=it.get("amount_ex"),
                vat_amount=it.get("vat_amount"),
                currency=it.get("currency", "GBP"),
                ocr_merchant=it.get("merchant", ""),
                ocr_amount=it.get("amount_inc"),
                ocr_date=it.get("purchased_on", ""),
                ocr_raw=it.get("ocr_raw", ""),
                ocr_error=it.get("ocr_error", ""),
                stored_file=it.get("stored_file", ""),
                filename=it.get("filename", ""),
                mime_type=it.get("mime_type", ""),
                category_account_code=it.get("category_account_code", ""),
                category_account_name=it.get("category_account_name", ""),
                status="pending_review",
            )
            dump_store.update_item(db, it["id"], status=dump_store.STATUS_IMPORTED)
            imported += 1
        return redirect("/receipts/expenses/dump/" + batch_id + "?imported=" + str(imported))

    # ═══════════════════════════════════════════════════════════════════════════
    # Email Invoice Importer
    # ═══════════════════════════════════════════════════════════════════════════

    _EMAIL_SCAN_SETTINGS_KEY = "email_scan_settings"
    _EMAIL_SCAN_DEFAULT_OWN_NAMES = [
        "Power Wash", "Power Wash Ltd", "Pow Wash", "Powwash",
        "Pow Services", "Pow Services Ltd", "Pow Services Limited",
        "POW Services", "POW Services Ltd", "POW Services Limited",
    ]
    _EMAIL_SCAN_DEFAULT_OWN_DOMAINS = ["powwash.co.uk"]
    _EMAIL_SCAN_SETTINGS_DEFAULT: dict = {
        "own_company_names":      _EMAIL_SCAN_DEFAULT_OWN_NAMES,
        "own_company_domains":    _EMAIL_SCAN_DEFAULT_OWN_DOMAINS,
        "scan_time":              "07:00",
        "auto_scan_enabled":      False,
        "auto_scan_lookback_days": 1,
        "default_engineer_id":    None,
    }

    def _escan_settings() -> dict:
        raw = get_json_setting(
            config.admin_db_file, _EMAIL_SCAN_SETTINGS_KEY, None
        )
        if not raw:
            return dict(_EMAIL_SCAN_SETTINGS_DEFAULT)
        merged = dict(_EMAIL_SCAN_SETTINGS_DEFAULT)
        merged.update(raw)
        # Union saved own_company_names with the current hard-coded defaults so
        # any new company-name variants added to the defaults are picked up
        # automatically for existing installations, without losing user additions.
        saved_names = list(raw.get("own_company_names") or [])
        for _n in _EMAIL_SCAN_DEFAULT_OWN_NAMES:
            if _n not in saved_names:
                saved_names.append(_n)
        merged["own_company_names"] = saved_names
        return merged

    def _escan_settings_save(**kw) -> None:
        current = _escan_settings()
        current.update(kw)
        set_json_setting(config.admin_db_file, _EMAIL_SCAN_SETTINGS_KEY, current)

    def _gmail_connected() -> tuple[bool, str]:
        """(ok, message)  — checks admin creds have gmail.readonly scope."""
        creds = load_admin_credentials(config)
        if creds is None:
            return False, "Google not connected — reconnect via Settings."
        scope_ok = _gmail_mod.GMAIL_READONLY_SCOPE in (creds.scopes or set())
        if not scope_ok:
            return False, (
                "Gmail access not yet granted. "
                "Please reconnect Google (Settings → Reconnect Google) "
                "to approve the new Gmail read-only scope."
            )
        return True, ""

    def _connection_rows() -> str:
        """Build the connection-status panel rows (Google, Gmail, Xero, OpenAI)."""
        def _row(name: str, ok: bool, detail: str, paused: bool = False, href: str = "") -> str:
            if paused:
                dot   = '<span class="inline-block w-2.5 h-2.5 rounded-full bg-amber-400"></span>'
                label = '<span class="text-xs font-medium text-amber-700">Paused</span>'
            elif ok:
                dot   = '<span class="inline-block w-2.5 h-2.5 rounded-full bg-emerald-500"></span>'
                label = '<span class="text-xs font-medium text-emerald-700">Connected</span>'
            else:
                dot   = '<span class="inline-block w-2.5 h-2.5 rounded-full bg-red-400"></span>'
                label = '<span class="text-xs font-medium text-red-600">Not connected</span>'
            setup = '<span class="text-xs text-indigo-600 font-medium">Set up &rarr;</span>' if href else ""
            inner = f"""
  <div class="flex items-center gap-2 min-w-0">
    {dot}
    <span class="text-sm font-medium text-gray-800">{name}</span>
    <span class="text-xs text-gray-400 truncate">{detail}</span>
  </div>
  <div class="flex items-center gap-2 shrink-0">{label}{setup}</div>"""
            cls = "flex items-center justify-between py-2 border-b border-gray-100 last:border-0"
            if href:
                return f'<a href="{escape(href)}" class="{cls} hover:bg-gray-50 -mx-2 px-2 rounded-lg">{inner}</a>'
            return f'<div class="{cls}">{inner}</div>'

        # Google account (Calendar / Sheets / Gmail share one OAuth)
        try:
            g_creds = load_admin_credentials(config)
        except Exception:
            g_creds = None
        google_ok = g_creds is not None

        # Gmail read-only scope
        gmail_ok2, gmail_detail = _gmail_connected()
        if gmail_ok2:
            gmail_detail = "read-only access granted"

        # Xero
        xero_paused = xero_is_disabled()
        xero_ok = False
        xero_detail = ""
        if xero_paused:
            xero_detail = "Xero integration is paused (XERO_DISABLED)"
        else:
            try:
                _tok = load_xero_token(config.xero_token_file)
                xero_ok = bool(_tok and _tok.get("access_token") and _tok.get("tenant_id"))
                xero_detail = "" if xero_ok else "Connect Xero in Settings"
            except Exception:
                xero_ok = False
                xero_detail = "Connect Xero in Settings"

        # OpenAI (AI categorisation)
        try:
            _oa = get_openai_settings(config.admin_db_file)
            oa_ok = bool((_oa.get("api_key") or "").strip() or (os.getenv("OPENAI_API_KEY") or "").strip())
        except Exception:
            oa_ok = bool((os.getenv("OPENAI_API_KEY") or "").strip())

        google_detail = "Calendar · Sheets · Gmail — one Google login" if google_ok else "Connect in Settings - Reconnect Google"
        if not gmail_ok2:
            gmail_detail = gmail_detail or "Same Google login — reconnect to approve read-only Gmail"
        oa_detail = "for AI categorisation" if oa_ok else "Add an OpenAI key in Settings (optional)"

        google_combined_ok = google_ok and gmail_ok2
        if not google_ok:
            google_combined_detail = "Connect in Settings — Reconnect Google"
        elif not gmail_ok2:
            google_combined_detail = "Connected — reconnect to approve read-only Gmail access"
        else:
            google_combined_detail = "Calendar · Sheets · Gmail — read-only access granted"

        return (
            _row(
                "Google (Gmail invoice scanning)", google_combined_ok,
                google_combined_detail, href="/settings#google",
            )
            + _row("Xero", xero_ok, xero_detail, paused=xero_paused, href="/settings#xero")
            + _row("OpenAI", oa_ok, oa_detail, href="/settings#openai")
        )

    def _escan_status_badge(status: str) -> str:
        MAP = {
            "new":         ("Ready to import",    "bg-emerald-100 text-emerald-800"),
            "no_account":  ("Needs account",       "bg-amber-100  text-amber-800"),
            "possible_dup":("Possible duplicate",  "bg-amber-100  text-amber-800"),
            "suspicious":  ("Suspicious",          "bg-orange-100 text-orange-800"),
            "duplicate":   ("Duplicate",           "bg-gray-100   text-gray-500"),
            "own_company": ("Skipped - Powwash sent", "bg-gray-100   text-gray-500"),
            "not_invoice": ("Not an invoice",      "bg-gray-100   text-gray-500"),
            "skipped_email": ("Skipped — check me", "bg-orange-100 text-orange-700"),
            "ignored":     ("Ignored",             "bg-gray-100   text-gray-500"),
            "imported":    ("Imported",            "bg-indigo-100 text-indigo-700"),
        }
        label, cls = MAP.get(status, (status, "bg-gray-100 text-gray-600"))
        return f'<span class="px-2 py-0.5 rounded-full text-xs font-medium {cls}">{label}</span>'

    def _escan_item_card(it: dict, exp_accounts: list, batch_id: str, is_test: bool,
                         recon_map: dict | None = None) -> str:
        sid   = it["id"]
        st    = it.get("status", "new")
        badge = _escan_status_badge(st)
        amt   = it.get("amount_inc")
        amt_s = f"£{amt:,.2f}" if amt is not None else "—"
        merchant  = it.get("merchant", "") or it.get("sender_name", "") or it.get("sender_from", "")
        purchased = it.get("purchased_on", "") or it.get("email_date", "")
        subject   = it.get("subject", "")[:80]
        from_addr = it.get("sender_from", "")
        acct_code = it.get("category_account_code", "")
        acct_name = it.get("category_account_name", "")
        dup_msg   = it.get("dup_reason", "")
        ocr_err   = it.get("ocr_error", "")

        # Account dropdown
        opts = '<option value="">— select account —</option>'
        for a in (exp_accounts or []):
            sel = 'selected' if str(a.get("Code", "")) == acct_code else ""
            opts += f'<option value="{a.get("Code","")}|{a.get("Name","")}" {sel}>{a.get("Code","")} — {a.get("Name","")}</option>'

        # Card-feed match pill — SAME look as the receipt dump: shown on the
        # card itself so the match status lives with the invoice.
        recon_row = (recon_map or {}).get(sid) or {}
        recon_kind = recon_row.get("kind")
        recon_tx = recon_row.get("tx")
        recon_ambiguous = bool(recon_row.get("ambiguous"))
        recon_pill = ""
        recon_sub = ""
        if recon_kind in _CARD_PILL:
            lbl, cls = _CARD_PILL[recon_kind]
            if recon_kind in ("suggested", "price_only") and recon_ambiguous:
                lbl = "Possible match — needs a check"
                cls = "bg-orange-100 text-orange-700"
            recon_pill = (
                "<span class='inline-block text-xs font-medium px-2 py-0.5 "
                "rounded-full " + cls + "'>" + lbl + "</span>"
            )
            if recon_kind in ("suggested", "price_only"):
                tline = escape(recon_tx.get("contact") or "card transaction") \
                    if recon_tx else "card transaction"
                td = escape(_exp_uk_date(recon_tx.get("date") or "")) \
                    if recon_tx else ""
                if recon_ambiguous:
                    n = recon_row.get("n_sugg") or 2
                    recon_sub = (
                        "<div class='text-xs text-amber-700 mt-1'>"
                        + str(n) + " card payments look like this one — check "
                        "which is right in Xero.</div>"
                    )
                else:
                    recon_sub = (
                        "<div class='text-xs text-amber-700 mt-1'>Possible card "
                        "payment: " + tline + (" \u00b7 " + td if td else "")
                        + " — please confirm.</div>"
                    )
            elif recon_tx:
                tline = escape(recon_tx.get("contact") or "card transaction")
                td = escape(_exp_uk_date(recon_tx.get("date") or ""))
                note = ""
                if recon_tx.get("reconciled"):
                    note = " \u00b7 already reconciled in Xero"
                elif recon_tx.get("has_attachment"):
                    note = " \u00b7 receipt already attached in Xero"
                recon_sub = (
                    "<div class='text-xs text-gray-500 mt-1'>Card: " + tline
                    + " \u00b7 " + td + note + "</div>"
                )

        action_row = ""
        if st in ("skipped_email", "own_company") and not is_test:
            action_row = f"""
<div class="mt-3 flex flex-wrap gap-2 items-center border-t border-gray-100 pt-3">
  <form method="post" action="/receipts/emails/{batch_id}/item/{sid}/rescan">
    <button class="px-3 py-1.5 text-xs bg-indigo-600 text-white rounded hover:bg-indigo-500">
      Scan anyway — this IS an invoice
    </button>
  </form>
  <form method="post" action="/receipts/emails/{batch_id}/item/{sid}">
    <input type="hidden" name="status" value="ignored">
    <button class="px-3 py-1.5 text-xs border border-gray-200 text-gray-500 rounded hover:bg-gray-50">
      Remove — not needed
    </button>
  </form>
  <span class="text-xs text-gray-400">Scanning downloads and reads this email's attachments in full.</span>
</div>"""
        elif is_test and st not in ("imported", "own_company", "duplicate"):
            amount_ex = it.get("amount_ex")
            vat_amount = it.get("vat_amount")
            ex_s = f"£{float(amount_ex):,.2f}" if amount_ex is not None else "—"
            vat_s = f"£{float(vat_amount):,.2f}" if vat_amount is not None else "—"
            raw = (it.get("ocr_raw") or "").strip().replace("\n", " ")
            raw = raw[:220] + ("..." if len(raw) > 220 else "")
            raw_html = f'<p class="text-xs text-gray-500 mt-2">OCR preview: {escape(raw)}</p>' if raw else ""
            if st == "ignored":
                test_cross_btn = f"""
  <form method="post" action="/receipts/emails/{batch_id}/item/{sid}" class="ml-auto" data-ajax-crossoff="{sid}">
    <input type="hidden" name="status" value="new">
    <button class="px-3 py-1.5 text-xs border border-emerald-300 text-emerald-700 rounded hover:bg-emerald-50">
      &#8634; Restore
    </button>
  </form>"""
            else:
                test_cross_btn = f"""
  <form method="post" action="/receipts/emails/{batch_id}/item/{sid}" class="ml-auto" data-ajax-crossoff="{sid}">
    <input type="hidden" name="status" value="ignored">
    <button class="px-3 py-1.5 text-xs border border-red-200 text-red-600 rounded hover:bg-red-50"
            title="Cross this off — hide from this preview">
      &#10005; Cross off
    </button>
  </form>"""
            action_row = f"""
<div class="mt-3 border-t border-amber-100 pt-3 bg-amber-50/50 rounded-lg p-3">
  <div class="grid grid-cols-1 sm:grid-cols-3 gap-2 text-xs text-gray-700 mb-2">
    <div><span class="text-gray-400">Attachment</span><br><span class="font-medium">{escape(it.get("attachment_name","") or "invoice")}</span></div>
    <div><span class="text-gray-400">VAT</span><br><span class="font-medium">inc {amt_s} · ex {ex_s} · VAT {vat_s}</span></div>
    <div><span class="text-gray-400">Import action</span><br><span class="font-medium text-amber-700">Test only — nothing imported</span></div>
  </div>
  {raw_html}
  <div class="flex justify-end mt-2">{test_cross_btn}</div>
</div>"""
        elif st not in ("imported", "own_company", "duplicate"):
            status_opts = ""
            for s, lbl in [("new","Ready to import"),("ignored","Ignore"),("possible_dup","Possible dup")]:
                sel = "selected" if s == st else ""
                status_opts += f'<option value="{s}" {sel}>{lbl}</option>'
            if st == "ignored":
                cross_btn = f"""
  <form method="post" action="/receipts/emails/{batch_id}/item/{sid}" class="ml-auto" data-ajax-crossoff="{sid}">
    <input type="hidden" name="status" value="new">
    <button class="px-3 py-1.5 text-xs border border-emerald-300 text-emerald-700 rounded hover:bg-emerald-50"
            title="Put this invoice back so it IS imported">
      &#8634; Restore — import after all
    </button>
  </form>"""
            else:
                cross_btn = f"""
  <form method="post" action="/receipts/emails/{batch_id}/item/{sid}" class="ml-auto" data-ajax-crossoff="{sid}">
    <input type="hidden" name="status" value="ignored">
    <button class="px-3 py-1.5 text-xs border border-red-200 text-red-600 rounded hover:bg-red-50"
            title="Cross this invoice off — it will NOT be imported or sent to Xero">
      &#10005; Cross off — don't send to Xero
    </button>
  </form>"""
            action_row = f"""
<div class="mt-3 flex flex-wrap gap-2 items-end border-t border-gray-100 pt-3">
<form method="post" action="/receipts/emails/{batch_id}/item/{sid}" class="flex flex-wrap gap-2 items-end flex-1">
  <div class="flex-1 min-w-48">
    <label class="block text-xs text-gray-500 mb-1">Account</label>
    <select name="account" class="w-full text-xs border border-gray-200 rounded px-2 py-1">
      {opts}
    </select>
  </div>
  <div>
    <label class="block text-xs text-gray-500 mb-1">Status</label>
    <select name="status" class="text-xs border border-gray-200 rounded px-2 py-1">
      {status_opts}
    </select>
  </div>
  <button class="px-3 py-1.5 text-xs bg-indigo-600 text-white rounded hover:bg-indigo-500">Save</button>
</form>
{cross_btn}
</div>"""

        dup_html = f'<p class="text-xs text-amber-600 mt-1">⚠ {dup_msg}</p>' if dup_msg else ""
        err_html = f'<p class="text-xs text-red-500 mt-1">OCR error: {ocr_err[:120]}</p>' if ocr_err else ""
        view_html = ""
        if it.get("stored_file"):
            att_name = (it.get("attachment_name") or "").lower()
            att_mime = (it.get("attachment_mime") or "").lower()
            img_url = f"/receipts/emails/{batch_id}/item/{sid}/image"
            is_docx = att_name.endswith(".docx") or "wordprocessing" in att_mime
            if is_docx:
                view_html = (f'<a href="{img_url}" download '
                             f'class="inline-block text-xs text-indigo-600 hover:underline mt-1">'
                             f'&#128462; Download Word document</a>')
            else:
                view_html = (f'<a href="#" onclick="escanView({json.dumps(img_url)});return false;" '
                             f'class="inline-block text-xs text-indigo-600 hover:underline mt-1">'
                             f'View attachment &rarr;</a>')
        acct_html = ""
        if acct_code:
            acct_html = (f'<span class="text-xs font-medium text-indigo-700 bg-indigo-50 px-2 py-0.5 rounded" '
                         f'title="Xero account chosen by AI — change it in the Account dropdown below">'
                         f'AI &rarr; {acct_code} — {acct_name}</span>')
        elif st in ("new", "no_account", "possible_dup", "suspicious"):
            acct_html = ('<span class="text-xs font-medium text-amber-700 bg-amber-50 px-2 py-0.5 rounded" '
                         'title="The AI could not pick a Xero account — choose one below">'
                         'No account yet — pick one below</span>')

        return f"""
<div class="bg-white border border-gray-200 rounded-xl p-4 shadow-sm" id="escan-card-{sid}">
  <div class="flex flex-wrap items-start gap-2 justify-between">
    <div class="flex-1 min-w-0">
      <div class="flex items-center gap-2 flex-wrap">
        {badge}
        {recon_pill}
        {acct_html}
        <span class="text-sm font-semibold text-gray-800 truncate">{merchant}</span>
      </div>
      <p class="text-xs text-gray-500 mt-0.5 truncate">{from_addr} · {subject}</p>
      {dup_html}{recon_sub}{err_html}{view_html}
    </div>
    <div class="text-right shrink-0">
      <p class="text-lg font-bold text-gray-900">{amt_s}</p>
      <p class="text-xs text-gray-400">{purchased}</p>
    </div>
  </div>
  {action_row}
</div>"""

    def _escan_purge_batch_files(batch_id: str) -> int:
        """Remove this batch's locally-stored attachment files (best-effort).
        _exp_safe_remove_file refuses to delete a file still referenced by an
        imported expense receipt or dump item, so Xero-bound images stay safe."""
        n = 0
        try:
            for it in em_store.list_items(config.admin_db_file, batch_id):
                sf = it.get("stored_file") or ""
                if sf and _exp_safe_remove_file(config.admin_db_file, sf):
                    n += 1
        except Exception:
            pass
        return n

    def _escan_purge_done_files(batch_id: str) -> int:
        """After a live import, drop local attachment copies we no longer need.
        Imported items keep their file (it rides into Xero via the expense
        receipt, and _exp_safe_remove_file guards it); duplicate / own-company /
        ignored items are cleaned up here so nothing lingers on disk."""
        n = 0
        drop = {"duplicate", "own_company", "ignored", "imported", "not_invoice"}
        try:
            for it in em_store.list_items(config.admin_db_file, batch_id):
                if (it.get("status") or "") not in drop:
                    continue
                sf = it.get("stored_file") or ""
                if sf and _exp_safe_remove_file(config.admin_db_file, sf):
                    n += 1
        except Exception:
            pass
        return n

    # ── GET /receipts/emails ─────────────────────────────────────────────────

    @app.get("/receipts/emails")
    @require_login
    def email_scan_list():
        settings  = _escan_settings()
        batches   = em_store.list_batches(config.admin_db_file)
        gmail_ok, gmail_msg = _gmail_connected()
        engineers = exp_store.list_engineers(config.admin_db_file)

        # build engineer options
        eng_opts = '<option value="">— select —</option>'
        def_eid  = settings.get("default_engineer_id")
        for e in engineers:
            sel = "selected" if str(e["id"]) == str(def_eid or "") else ""
            eng_opts += f'<option value="{e["id"]}" {sel}>{e.get("name","?")} ({e.get("kind","?")})</option>'

        own_names_str   = "\n".join(settings.get("own_company_names", []))
        own_domains_str = "\n".join(settings.get("own_company_domains", []))
        auto_checked    = "checked" if settings.get("auto_scan_enabled") else ""
        scan_time       = settings.get("scan_time", "07:00")
        lookback        = settings.get("auto_scan_lookback_days", 1)

        gmail_banner = ""
        if not gmail_ok:
            gmail_banner = f"""
<div class="bg-amber-50 border border-amber-200 rounded-xl p-4 mb-4 flex items-start gap-3">
  <span class="text-amber-500 text-xl">⚠</span>
  <div>
    <p class="text-sm font-medium text-amber-800">Gmail not connected</p>
    <p class="text-xs text-amber-600 mt-0.5">{gmail_msg}</p>
  </div>
</div>"""

        # Most recent scan error (e.g. Gmail API not enabled) — surface it clearly
        scan_err_banner = ""
        for b in batches:
            if b.get("status") != "error":
                continue
            raw = b.get("summary") or b.get("summary_json") or {}
            if isinstance(raw, str):
                try:
                    raw = __import__("json").loads(raw)
                except Exception:
                    raw = {}
            emsg = raw.get("error") if isinstance(raw, dict) else ""
            if emsg:
                scan_err_banner = f"""
<div class="bg-red-50 border border-red-200 rounded-xl p-4 mb-4 flex items-start gap-3">
  <span class="text-red-500 text-xl">⚠</span>
  <div>
    <p class="text-sm font-medium text-red-800">Last scan couldn't run</p>
    <p class="text-xs text-red-600 mt-0.5">{escape(str(emsg))}</p>
  </div>
</div>"""
            break

        # Batch history rows
        batch_rows = ""
        for b in batches:
            raw_summary = b.get("summary") or b.get("summary_json") or {}
            if isinstance(raw_summary, str):
                try:
                    raw_summary = __import__("json").loads(raw_summary)
                except Exception:
                    raw_summary = {}
            found    = raw_summary.get("found", b.get("total_found", 0))
            new_c    = raw_summary.get("new", 0)
            dup_c    = raw_summary.get("duplicate", 0)
            err_c    = raw_summary.get("error", "")
            test_tag = '<span class="ml-1 px-1.5 py-0.5 text-xs bg-amber-100 text-amber-700 rounded">test</span>' if b.get("is_test") else ""
            st_col   = {"processing":"text-blue-600","ready":"text-emerald-600","error":"text-red-600","done":"text-gray-400"}.get(b.get("status",""),"text-gray-600")
            batch_rows += f"""
<tr class="hover:bg-gray-50">
  <td class="px-4 py-2 text-xs text-gray-500">{b.get("created_at","")[:16]}</td>
  <td class="px-4 py-2 text-xs font-medium">{b.get("label") or b.get("id","")}{test_tag}</td>
  <td class="px-4 py-2 text-xs text-gray-600">{b.get("date_from","")} → {b.get("date_to","")}</td>
  <td class="px-4 py-2 text-xs {st_col} font-medium">{b.get("status","")}</td>
  <td class="px-4 py-2 text-xs">{found} found · {new_c} new · {dup_c} dup{(" · error: "+str(err_c)) if err_c else ""}</td>
  <td class="px-4 py-2 text-xs">
    <div class="flex items-center gap-3">
      <a href="/receipts/emails/{b['id']}" class="text-indigo-600 hover:underline">View →</a>
      <form method="post" action="/receipts/emails/{b['id']}/delete" onsubmit="return confirm('Delete this scan and its extracted data?')">
        <button class="text-red-500 hover:text-red-700 hover:underline">Delete</button>
      </form>
    </div>
  </td>
</tr>"""

        has_test_batches = any(b.get("is_test") for b in batches)

        if not batches:
            batch_rows = """<tr><td colspan="6" class="px-4 py-8 text-center">
  <p class="text-sm font-medium text-gray-500">No scans yet</p>
  <p class="text-xs text-gray-400 mt-1">Use the form above to run your first scan. Try <strong>Test mode</strong> first to see what would be imported before committing.</p>
</td></tr>"""

        from datetime import date, timedelta
        today = date.today()
        d_from_def = (today - timedelta(days=90)).isoformat()
        d_to_def   = today.isoformat()

        # Card / bank account picker — same options as the receipt dump
        # (labelled feed accounts + Xero bank accounts), preselecting the
        # last card used anywhere, so the reconciliation panel can check the
        # scan's invoices against the right card straight away.
        try:
            _ec_last = str(get_json_setting(
                config.admin_db_file, "dump_last_card", "") or "").strip()
        except Exception:
            _ec_last = ""
        _ec_names: set = set()
        try:
            for v in (cardfeed.get_account_labels(config.admin_db_file) or {}).values():
                n = str((v or {}).get("xero_account_name") or "").strip()
                if n:
                    _ec_names.add(n)
        except Exception:
            pass
        try:
            _at, _tid, _ = _load_xero_at_tid(config)
            if _at and _tid:
                _r, _banks, _t, _bw = _get_tenant_acct_themes(_at, _tid)
                for a in _banks or []:
                    n = str(a.get("Name") or "").strip()
                    if n:
                        _ec_names.add(n)
        except Exception:
            pass
        if _ec_names:
            card_select_html = (
                "<div><label class='block text-xs text-gray-500 mb-1'>"
                "Card these were paid from</label>"
                "<select name='card_account' class='text-sm border "
                "border-gray-200 rounded px-3 py-1.5'>"
                "<option value=''>— choose card / bank account —</option>"
                + "".join(
                    "<option value='" + escape(o) + "'"
                    + (" selected" if _ec_last and o == _ec_last else "")
                    + ">" + escape(o) + "</option>"
                    for o in sorted(_ec_names)
                )
                + "</select></div>"
            )
        else:
            card_select_html = (
                "<div><label class='block text-xs text-gray-500 mb-1'>"
                "Card these were paid from</label>"
                "<input type='text' name='card_account' "
                "placeholder='e.g. Charge Card - Dan' value='"
                + escape(_ec_last) + "' class='text-sm border border-gray-200 "
                "rounded px-3 py-1.5 w-48'></div>"
            )

        body = f"""
<div class="max-w-5xl mx-auto px-4 py-6 space-y-6">

  <div class="flex items-center justify-between">
    <h1 class="text-2xl font-bold text-gray-900">Email Invoice Importer</h1>
    <a href="/receipts/expenses" class="text-sm text-indigo-600 hover:underline">← Field Expenses</a>
  </div>

  <!-- Connection status -->
  <details class="group bg-white border border-gray-200 rounded-xl shadow-sm overflow-hidden">
    <summary class="flex items-center justify-between px-5 py-3 cursor-pointer list-none select-none hover:bg-gray-50">
      <h2 class="text-base font-semibold text-gray-800">Connections set up</h2>
      <svg class="w-4 h-4 text-gray-400 transition-transform duration-200 group-open:rotate-180 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"/></svg>
    </summary>
    <div class="px-5 pb-5 pt-1 border-t border-gray-100">
      <div class="flex items-center justify-between mb-1">
        <p class="text-xs text-gray-400">What this app is currently connected to.</p>
        <a href="/settings#google" class="text-xs text-indigo-600 hover:underline">Manage Google/Gmail setup &rarr;</a>
      </div>
      {_connection_rows()}
    </div>
  </details>

  {gmail_banner}
  {scan_err_banner}

  <!-- New scan form -->
  <div class="bg-white border border-gray-200 rounded-xl p-5 shadow-sm">
    <h2 class="text-base font-semibold text-gray-800 mb-1">Scan emails for invoices</h2>
    <p class="text-xs text-gray-400 mb-4">Looks through Gmail for invoices from suppliers — PDFs, scanned images and Word (.docx) attachments. Reads emails only — never marks them as read or sends anything. Invoices <strong>sent by Powwash</strong> are automatically skipped.</p>
    <form method="post" action="/receipts/emails/scan" id="escan-form" class="space-y-4">
      <div class="flex flex-wrap gap-4 items-end">
        <div>
          <label class="block text-xs text-gray-500 mb-1">From date</label>
          <input type="date" name="date_from" value="{d_from_def}" class="text-sm border border-gray-200 rounded px-3 py-1.5" required>
        </div>
        <div>
          <label class="block text-xs text-gray-500 mb-1">To date</label>
          <input type="date" name="date_to" value="{d_to_def}" class="text-sm border border-gray-200 rounded px-3 py-1.5" required>
        </div>
        <div>
          <label class="block text-xs text-gray-500 mb-1">Label (optional)</label>
          <input type="text" name="label" placeholder="e.g. Jan 2025" class="text-sm border border-gray-200 rounded px-3 py-1.5 w-40">
        </div>
        {card_select_html}
        <button type="submit" {"disabled" if not gmail_ok else ""} class="px-4 py-1.5 text-sm bg-indigo-600 text-white rounded hover:bg-indigo-500 disabled:opacity-40 disabled:cursor-not-allowed self-end">
          Start scan →
        </button>
      </div>
      <!-- Test mode toggle -->
      <div id="escan-test-box" class="flex items-start gap-3 rounded-lg border border-gray-200 bg-gray-50 px-4 py-3 cursor-pointer select-none transition-colors"
           onclick="var cb=document.getElementById('escan-test-cb');cb.checked=!cb.checked;_escanTestToggle()">
        <input type="checkbox" name="is_test" id="escan-test-cb" value="1"
               class="mt-0.5 rounded border-gray-300 accent-amber-500"
               onclick="event.stopPropagation();_escanTestToggle()">
        <div>
          <span class="text-sm font-medium text-gray-800">Test mode</span>
          <p class="text-xs text-gray-500 mt-0.5">Scans and extracts invoices, shows what <em>would</em> be imported — nothing is actually saved. Run without test mode when you're ready to import for real.</p>
        </div>
      </div>
    </form>
    <script>
    function _escanTestToggle() {{
      var cb  = document.getElementById('escan-test-cb');
      var box = document.getElementById('escan-test-box');
      if (cb.checked) {{
        box.className = box.className.replace('border-gray-200 bg-gray-50','border-amber-300 bg-amber-50');
      }} else {{
        box.className = box.className.replace('border-amber-300 bg-amber-50','border-gray-200 bg-gray-50');
      }}
    }}
    </script>
  </div>

  <!-- Scan history -->
  <div class="bg-white border border-gray-200 rounded-xl shadow-sm overflow-hidden">
    <div class="px-5 py-3 border-b border-gray-100 flex items-center justify-between gap-2">
      <h2 class="text-sm font-semibold text-gray-700">Scan history</h2>
      {"" if not has_test_batches else """<form method="post" action="/receipts/emails/delete-tests"
            onsubmit="return confirm('Delete all test scans and their extracted data?')">
        <button class="px-3 py-1 text-xs rounded border border-amber-200 text-amber-700 bg-amber-50 hover:bg-amber-100">
          Delete test scans
        </button>
      </form>"""}
    </div>
    <div class="overflow-x-auto">
      <table class="w-full text-left">
        <thead class="bg-gray-50 text-xs text-gray-500 uppercase">
          <tr>
            <th class="px-4 py-2">Started</th>
            <th class="px-4 py-2">Label / ID</th>
            <th class="px-4 py-2">Date range</th>
            <th class="px-4 py-2">Status</th>
            <th class="px-4 py-2">Summary</th>
            <th class="px-4 py-2"></th>
          </tr>
        </thead>
        <tbody class="divide-y divide-gray-100">
          {batch_rows}
        </tbody>
      </table>
    </div>
  </div>

  <!-- Settings panel -->
  <details id="email-invoice-importer-settings" class="bg-white border border-gray-200 rounded-xl shadow-sm">
    <summary class="px-5 py-3 cursor-pointer text-sm font-semibold text-gray-700 select-none">Settings</summary>
    <form method="post" action="/receipts/emails/settings" class="px-5 pb-5 pt-3 space-y-4 border-t border-gray-100">
      <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
        <div>
          <label class="block text-xs font-medium text-gray-600 mb-1">
            Own company names (one per line)
            <span class="text-gray-400 font-normal ml-1">— only emails <strong>sent by</strong> these (sender name/address) are skipped. Invoices addressed <strong>to</strong> you are still imported.</span>
          </label>
          <textarea name="own_company_names" rows="3" class="w-full text-xs border border-gray-200 rounded px-2 py-1.5 font-mono">{own_names_str}</textarea>
          <p class="text-xs text-gray-400 mt-1">Default includes Power Wash, Power Wash Ltd, Pow Wash and Powwash.</p>
        </div>
        <div>
          <label class="block text-xs font-medium text-gray-600 mb-1">
            Own company email domains (one per line)
          </label>
          <textarea name="own_company_domains" rows="3" class="w-full text-xs border border-gray-200 rounded px-2 py-1.5 font-mono">{own_domains_str}</textarea>
        </div>
        <div>
          <label class="block text-xs font-medium text-gray-600 mb-1">Import receipts as engineer</label>
          <select name="default_engineer_id" class="w-full text-sm border border-gray-200 rounded px-2 py-1.5">
            {eng_opts}
          </select>
        </div>
        <div>
          <label class="block text-xs font-medium text-gray-600 mb-1">Daily auto-scan</label>
          <div class="flex items-center gap-3 mt-1">
            <label class="flex items-center gap-1.5 text-sm text-gray-700">
              <input type="checkbox" name="auto_scan_enabled" {auto_checked} class="rounded border-gray-300">
              Enabled
            </label>
            <div class="flex items-center gap-1.5">
              <span class="text-xs text-gray-500">Time</span>
              <input type="time" name="scan_time" value="{scan_time}" class="text-sm border border-gray-200 rounded px-2 py-1">
            </div>
            <div class="flex items-center gap-1.5">
              <span class="text-xs text-gray-500">Look back</span>
              <input type="number" name="auto_scan_lookback_days" value="{lookback}" min="1" max="30" class="w-16 text-sm border border-gray-200 rounded px-2 py-1">
              <span class="text-xs text-gray-500">day(s)</span>
            </div>
          </div>
        </div>
      </div>
      <button type="submit" class="px-4 py-1.5 text-sm bg-gray-800 text-white rounded hover:bg-gray-700">Save settings</button>
    </form>
  </details>

</div>"""
        return _page(body)

    # ── POST /receipts/emails/settings ──────────────────────────────────────

    @app.post("/receipts/emails/settings")
    @require_login
    def email_scan_settings_save():
        from flask import request as _req
        names   = [x.strip() for x in (_req.form.get("own_company_names", "") or "").splitlines() if x.strip()]
        domains = [x.strip() for x in (_req.form.get("own_company_domains", "") or "").splitlines() if x.strip()]
        eid_raw = (_req.form.get("default_engineer_id") or "").strip()
        _escan_settings_save(
            own_company_names=names or list(_EMAIL_SCAN_DEFAULT_OWN_NAMES),
            own_company_domains=domains or list(_EMAIL_SCAN_DEFAULT_OWN_DOMAINS),
            scan_time=(_req.form.get("scan_time") or "07:00").strip(),
            auto_scan_enabled=bool(_req.form.get("auto_scan_enabled")),
            auto_scan_lookback_days=int(_req.form.get("auto_scan_lookback_days") or 1),
            default_engineer_id=int(eid_raw) if eid_raw else None,
        )
        return redirect("/receipts/emails")

    # ── POST /receipts/emails/scan ───────────────────────────────────────────

    @app.post("/receipts/emails/scan")
    @require_login
    def email_scan_start():
        import threading as _thr
        from flask import request as _req

        gmail_ok, gmail_msg = _gmail_connected()
        if not gmail_ok:
            return redirect("/receipts/emails")

        date_from = (_req.form.get("date_from") or "").strip()
        date_to   = (_req.form.get("date_to") or "").strip()
        label     = (_req.form.get("label") or "").strip()
        is_test   = bool(_req.form.get("is_test"))
        card_acct = (_req.form.get("card_account") or "").strip()

        if not date_from or not date_to:
            return redirect("/receipts/emails")

        batch = em_store.create_batch(
            config.admin_db_file,
            label=label or f"{date_from} → {date_to}",
            date_from=date_from,
            date_to=date_to,
            is_test=is_test,
            card_account=card_acct,
        )
        if card_acct:
            try:
                set_json_setting(config.admin_db_file, "dump_last_card",
                                 card_acct)
            except Exception:
                pass

        settings    = _escan_settings()
        gmail_creds = load_admin_credentials(config)
        svc         = ReceiptService(config)

        # Load Xero expense accounts if available
        exp_accts: list = []
        try:
            _at, _tid, _ = _load_xero_at_tid(config)
            exp_accts, _ = _get_xero_expense_accounts(_at, _tid, config.admin_db_file)
        except Exception:
            pass

        _oa_cfg    = get_openai_settings(config.admin_db_file)
        openai_key = (_oa_cfg.get("api_key") or "").strip() or (os.getenv("OPENAI_API_KEY") or "").strip()

        def _run():
            em_pipe.scan_email_batch(
                batch["id"],
                config.admin_db_file,
                gmail_creds=gmail_creds,
                own_names=settings.get("own_company_names", []),
                own_domains=settings.get("own_company_domains", []),
                exp_accounts=exp_accts,
                vat_rate=20.0,
                openai_key=openai_key or "",
                receipt_service=svc,
            )

        _thr.Thread(target=_run, daemon=True, name="email-scan").start()
        return redirect(f"/receipts/emails/{batch['id']}")

    # ── GET /receipts/emails/<batch_id> ──────────────────────────────────────

    @app.get("/receipts/emails/<batch_id>")
    @require_login
    def email_scan_results(batch_id: str):
        batch = em_store.get_batch(config.admin_db_file, batch_id)
        if not batch:
            return redirect("/receipts/emails")

        items    = em_store.list_items(config.admin_db_file, batch_id)
        is_test  = bool(batch.get("is_test"))
        status   = batch.get("status", "")
        settings = _escan_settings()
        def_eid  = settings.get("default_engineer_id")

        exp_accts: list = []
        try:
            _at, _tid, _ = _load_xero_at_tid(config)
            exp_accts, _ = _get_xero_expense_accounts(_at, _tid, config.admin_db_file)
        except Exception:
            pass

        # Group items
        groups = {
            "new":         [],
            "no_account":  [],
            "possible_dup":[],
            "suspicious":  [],
            "duplicate":   [],
            "own_company": [],
            "not_invoice": [],
            "skipped_email": [],
            "ignored":     [],
            "imported":    [],
        }
        for it in items:
            g = groups.get(it.get("status", "new"))
            if g is not None:
                g.append(it)
            else:
                groups["new"].append(it)

        recon_map: dict = {}

        def _group_section(title: str, color: str, items_list: list, collapsed: bool = False) -> str:
            if not items_list:
                return ""
            cards = "".join(
                _escan_item_card(i, exp_accts, batch_id, is_test,
                                 recon_map=recon_map)
                for i in items_list
            )
            arrow = "▶" if collapsed else "▼"
            det_open = "" if collapsed else "open"
            return f"""
<details {det_open} class="space-y-1">
  <summary class="cursor-pointer flex items-center gap-2 py-1 select-none">
    <span class="text-xs text-gray-400">{arrow}</span>
    <h3 class="text-sm font-semibold {color}">{title}</h3>
    <span class="text-xs text-gray-400">({len(items_list)})</span>
  </summary>
  <div class="space-y-3 mt-2">{cards}</div>
</details>"""

        raw_summary = batch.get("summary") or {}
        if isinstance(raw_summary, str):
            try:
                raw_summary = __import__("json").loads(raw_summary)
            except Exception:
                raw_summary = {}
        found    = raw_summary.get("found", batch.get("total_found", 0))
        new_c    = raw_summary.get("new", 0)
        dup_c    = raw_summary.get("duplicate", 0)
        own_c    = raw_summary.get("own_company", 0)
        no_inv_c = raw_summary.get("no_invoice", 0)
        err_c    = raw_summary.get("error", 0)
        no_acc_c = raw_summary.get("no_account", 0)
        not_inv_c   = raw_summary.get("not_invoice", 0)
        skip_em_c   = raw_summary.get("skipped_email", 0)

        err_notice = raw_summary.get("error", "")
        if isinstance(err_notice, str) and err_notice:
            err_banner = f'<div class="bg-red-50 border border-red-200 rounded-xl p-4 text-sm text-red-700">Error during scan: {err_notice}</div>'
        else:
            err_banner = ""

        processing_banner = ""
        if status == "processing":
            _sc0  = raw_summary.get("scanned", 0)
            _tot0 = raw_summary.get("total", 0)
            _pct0 = min(99, round(_sc0 / _tot0 * 100)) if _tot0 else 5
            _lbl0 = (f"Scanned {_sc0} of {_tot0} emails…" if _tot0
                     else "Connecting to Gmail…")
            processing_banner = f"""
<div id="escan-proc-banner" class="bg-blue-50 border border-blue-200 rounded-xl p-5">
  <div class="flex items-center gap-3 mb-3">
    <svg class="animate-spin h-5 w-5 text-blue-500 shrink-0" viewBox="0 0 24 24" fill="none">
      <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"/>
      <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z"/>
    </svg>
    <span class="text-sm font-medium text-blue-800">Scanning emails for invoices…</span>
  </div>
  <div class="w-full bg-blue-200 rounded-full h-2.5 overflow-hidden">
    <div id="escan-pbar" class="bg-blue-600 h-2.5 rounded-full transition-all duration-700 ease-out"
         style="width:{_pct0}%"></div>
  </div>
  <p id="escan-plbl" class="text-xs text-blue-600 mt-2">{_lbl0}</p>
</div>
<script>(function(){{
  var bid={json.dumps(batch_id)};
  var bar=document.getElementById('escan-pbar');
  var lbl=document.getElementById('escan-plbl');
  function poll(){{
    fetch('/receipts/emails/'+bid+'/status',{{redirect:'manual',headers:{{'Accept':'application/json'}}}})
    .then(function(r){{
      if(!r.ok||r.type==='opaqueredirect'){{setTimeout(poll,3000);return null;}}
      return r.json().catch(function(){{return null;}});
    }})
    .then(function(d){{
      if(!d){{return;}}
      if(!d.ok){{setTimeout(poll,3000);return;}}
      if(d.status!=='processing'){{window.location.reload();return;}}
      var sc=d.scanned||0,tot=d.total||0,found=d.found||0;
      var pct=tot?Math.min(99,Math.round(sc/tot*100)):5;
      if(bar)bar.style.width=pct+'%';
      if(lbl)lbl.textContent=tot?('Scanned '+sc+' of '+tot+' emails — '+found+' invoice(s) found so far'):'Connecting to Gmail…';
      setTimeout(poll,1500);
    }}).catch(function(){{setTimeout(poll,3000);}});
  }}
  setTimeout(poll,1200);
}})();</script>"""

        test_banner = ""
        if is_test:
            n_show = len([i for i in items if i.get("status") in ("new", "no_account", "possible_dup", "suspicious")])
            d_from_enc = batch.get("date_from", "")
            d_to_enc   = batch.get("date_to", "")
            lbl_enc    = (batch.get("label") or "").replace('"', "&quot;")
            test_banner = f"""
<div class="bg-amber-50 border border-amber-200 rounded-xl p-4 flex flex-col sm:flex-row sm:items-center gap-3">
  <div class="flex-1">
    <p class="text-sm font-semibold text-amber-800">Test mode — nothing imported</p>
    <p class="text-xs text-amber-700 mt-0.5">{n_show} invoice(s) would be put forward for import. Review the results below, then run a live scan when ready.</p>
  </div>
  <form method="post" action="/receipts/emails/scan" class="shrink-0">
    <input type="hidden" name="date_from" value="{d_from_enc}">
    <input type="hidden" name="date_to"   value="{d_to_enc}">
    <input type="hidden" name="label"     value="{lbl_enc}">
    <button class="px-3 py-1.5 text-xs bg-amber-700 text-white rounded hover:bg-amber-600 whitespace-nowrap">
      Run live scan for same dates →
    </button>
  </form>
</div>"""

        import_bar = ""
        n_importable = len([i for i in items if i.get("status") == "new"])
        if is_test and n_importable > 0 and status in ("ready", "done"):
            import_bar = f"""
<div class="bg-amber-50 border border-amber-200 rounded-xl p-4 flex flex-wrap items-center gap-4">
  <p class="text-sm font-medium text-amber-800 flex-1">{n_importable} invoice(s) would be imported into Field Expenses in live mode.</p>
  <span class="px-4 py-1.5 text-sm bg-gray-100 text-gray-500 rounded">Import disabled in test mode</span>
</div>"""
        elif not is_test and n_importable > 0 and status in ("ready", "done"):
            eid_str = str(def_eid) if def_eid else ""
            engineers = exp_store.list_engineers(config.admin_db_file)
            eng_opts2 = '<option value="">— pick engineer / owner —</option>'
            for e in engineers:
                sel = "selected" if str(e["id"]) == eid_str else ""
                eng_opts2 += f'<option value="{e["id"]}" {sel}>{e.get("name","?")} ({e.get("kind","?")})</option>'
            import_bar = f"""
<div class="bg-emerald-50 border border-emerald-200 rounded-xl p-4 flex flex-wrap items-center gap-4">
  <p class="text-sm font-medium text-emerald-800 flex-1">{n_importable} invoice(s) ready to import into Field Expenses</p>
  <form method="post" action="/receipts/emails/{batch_id}/import" class="flex items-center gap-2">
    <select name="engineer_id" class="text-sm border border-gray-200 rounded px-2 py-1.5">
      {eng_opts2}
    </select>
    <button class="px-4 py-1.5 text-sm bg-emerald-600 text-white rounded hover:bg-emerald-500">
      Import {n_importable} invoice(s) →
    </button>
  </form>
</div>"""

        status_pill_cls = ("bg-blue-100 text-blue-700" if status == "processing"
                           else "bg-emerald-100 text-emerald-700" if status in ("ready", "done")
                           else "bg-red-100 text-red-700")

        def _stat(val, label, color="text-gray-800"):
            return (f'<div class="text-center px-4 py-2 bg-gray-50 rounded-lg border border-gray-100">'
                    f'<p class="text-lg font-bold {color}">{val}</p>'
                    f'<p class="text-xs text-gray-400 mt-0.5">{label}</p></div>')

        stats_html = (
            _stat(found, "found")
            + _stat(new_c, "new", "text-emerald-700")
            + (_stat(no_acc_c, "needs account", "text-amber-600") if no_acc_c else "")
            + (_stat(dup_c, "duplicate", "text-gray-400") if dup_c else "")
            + (_stat(own_c, "own-company", "text-gray-400") if own_c else "")
            + (_stat(not_inv_c, "AI: not invoice", "text-gray-400") if not_inv_c else "")
            + (_stat(skip_em_c, "AI: emails skipped", "text-gray-400") if skip_em_c else "")
            + (_stat(no_inv_c, "non-invoice", "text-gray-400") if no_inv_c else "")
        )

        # Card-feed reconciliation panel — the SAME engine and display as the
        # receipt dump (matched rows, previously-submitted rows, thin
        # "needs a receipt" rows, non-submission report button). Only real
        # candidate invoices are matched: own-company / AI-filtered / ignored
        # items never count as receipts against the card.
        outstanding_html = ""
        if status in ("ready", "done"):
            _recon_items = [
                i for i in items
                if i.get("status") not in ("own_company", "not_invoice",
                                           "ignored")
            ]
            try:
                recon = _dump_bank_feed_recon(_recon_items, batch)
            except Exception as e:
                print(f"[escan] recon failed: {e}", flush=True)
                recon = None
            recon_map.update({
                r["item"]["id"]: r
                for r in ((recon or {}).get("rows") or [])
                if r.get("item")
            })
            outstanding_html = _dump_outstanding_panel(
                recon, batch_id,
                set_card_action="/receipts/emails/" + batch_id + "/set-card")

        body = f"""
<div class="max-w-5xl mx-auto px-4 py-6 space-y-4">
  <div class="flex items-center gap-3 flex-wrap">
    <a href="/receipts/emails" class="text-sm text-indigo-600 hover:underline shrink-0">← All scans</a>
    <h1 class="text-xl font-bold text-gray-900 flex-1">
      {batch.get("label") or batch_id}
      {"<span class='ml-2 text-xs bg-amber-100 text-amber-700 px-2 py-0.5 rounded-full font-medium'>test mode</span>" if is_test else ""}
    </h1>
    <span class="px-2 py-0.5 rounded-full text-xs font-medium {status_pill_cls}">{status}</span>
    <form method="post" action="/receipts/emails/{batch_id}/delete" class="shrink-0"
          onsubmit="return confirm('Delete this scan and remove its locally-stored attachment files? Invoices already imported into Field Expenses (and their Xero attachments) are not affected.');">
      <button class="px-2.5 py-1 text-xs border border-red-200 text-red-600 rounded hover:bg-red-50">Delete scan</button>
    </form>
  </div>
  <p class="text-xs text-gray-400">{batch.get("date_from","")} → {batch.get("date_to","")}</p>

  <!-- Summary stat pills -->
  <div class="flex flex-wrap gap-2">{stats_html}</div>

  {processing_banner}
  {test_banner}
  {err_banner}
  {import_bar}

  <!-- Items grouped by status -->
  <div class="space-y-4">
    {_group_section("Ready to import", "text-emerald-700", groups["new"])}
    {_group_section("Needs an account code", "text-amber-700", groups["no_account"])}
    {_group_section("Possible duplicates — review", "text-amber-700", groups["possible_dup"])}
    {_group_section("Suspicious — review", "text-orange-700", groups["suspicious"])}
    {_group_section("Skipped emails — worth a quick check", "text-orange-700", groups["skipped_email"], collapsed=True)}
    {_group_section("Duplicates (skipped)", "text-gray-500", groups["duplicate"], collapsed=True)}
    {_group_section("Not an invoice — filtered by AI (open to override)", "text-gray-500", groups["not_invoice"], collapsed=True)}
    {_group_section("Own company (filtered)", "text-gray-500", groups["own_company"], collapsed=True)}
    {_group_section("Ignored", "text-gray-400", groups["ignored"], collapsed=True)}
    {_group_section("Already imported", "text-indigo-500", groups["imported"], collapsed=True)}
  </div>

  {("<div class='text-center py-10'><p class='text-sm font-medium text-gray-500'>No invoice attachments found</p><p class='text-xs text-gray-400 mt-1'>No emails with PDF invoices in this date range, or all were filtered (own-company / non-invoice).</p></div>" if not items and status in ("ready","done") else "")}

  {outstanding_html}
</div>

<!-- ── Attachment lightbox ──────────────────────────────────────────────── -->
<div id="escan-lb-backdrop"
     onclick="escanClose()"
     style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.72);z-index:9000;
            display:none;align-items:center;justify-content:center;">
  <div onclick="event.stopPropagation()"
       style="position:relative;width:92vw;max-width:960px;height:88vh;
              background:#fff;border-radius:12px;overflow:hidden;
              display:flex;flex-direction:column;box-shadow:0 25px 60px rgba(0,0,0,.5);">
    <div style="display:flex;align-items:center;justify-content:space-between;
                padding:10px 14px;border-bottom:1px solid #e5e7eb;background:#f9fafb;
                flex-shrink:0;">
      <span id="escan-lb-title" style="font-size:13px;font-weight:600;color:#374151;
            max-width:calc(100% - 36px);overflow:hidden;text-overflow:ellipsis;
            white-space:nowrap;"></span>
      <button onclick="escanClose()"
              style="width:28px;height:28px;border:none;background:#e5e7eb;border-radius:50%;
                     cursor:pointer;font-size:16px;line-height:1;color:#6b7280;">&#215;</button>
    </div>
    <iframe id="escan-lb-frame"
            src=""
            style="flex:1;width:100%;border:none;"
            allow="fullscreen"></iframe>
  </div>
</div>
<script>
function escanView(url) {{
  var bd = document.getElementById('escan-lb-backdrop');
  var fr = document.getElementById('escan-lb-frame');
  var tl = document.getElementById('escan-lb-title');
  if (!bd || !fr) return;
  fr.src = url;
  tl.textContent = url.split('/').pop() || 'Attachment';
  bd.style.display = 'flex';
  document.body.style.overflow = 'hidden';
}}
function escanClose() {{
  var bd = document.getElementById('escan-lb-backdrop');
  var fr = document.getElementById('escan-lb-frame');
  if (!bd) return;
  bd.style.display = 'none';
  document.body.style.overflow = '';
  if (fr) fr.src = '';
}}
document.addEventListener('keydown', function(e) {{
  if (e.key === 'Escape') escanClose();
}});

// AJAX cross-off / restore — no page reload
document.addEventListener('submit', function(e) {{
  var form = e.target;
  var sid = form.getAttribute('data-ajax-crossoff');
  if (!sid) return;
  e.preventDefault();
  var btn = form.querySelector('button');
  if (btn) btn.disabled = true;
  var body = new URLSearchParams(new FormData(form));
  fetch(form.action, {{
    method: 'POST',
    redirect: 'manual',
    headers: {{'X-Requested-With': 'XMLHttpRequest',
               'Content-Type': 'application/x-www-form-urlencoded'}},
    body: body.toString()
  }}).then(function(r) {{
    if (!r.ok || r.type === 'opaqueredirect') {{
      // session expired — do a normal page load so the login redirect works
      window.location.reload(); return Promise.reject();
    }}
    return r.json();
  }}).then(function(data) {{
    if (!data || !data.ok) {{ if (btn) btn.disabled = false; return; }}
    var card = document.getElementById('escan-card-' + sid);
    if (!card) return;
    if (data.status === 'ignored') {{
      card.style.transition = 'opacity 0.25s';
      card.style.opacity = '0';
      setTimeout(function() {{ card.remove(); }}, 260);
    }} else {{
      window.location.reload();
    }}
  }}).catch(function() {{ if (btn) btn.disabled = false; }});
}});
</script>"""
        return _page(body)

    # ── POST /receipts/emails/<batch_id>/set-card ────────────────────────────

    @app.post("/receipts/emails/<batch_id>/set-card")
    @require_login
    def email_scan_set_card(batch_id: str):
        """Set (or change) which card/bank account this email scan's invoices
        should be checked against, then bounce back so the reconciliation
        panel recomputes against that card only (same as the receipt dump)."""
        db = config.admin_db_file
        if not em_store.get_batch(db, batch_id):
            return redirect("/receipts/emails")
        card = (request.form.get("card_account") or "").strip()
        em_store.update_batch(db, batch_id, card_account=card)
        if card:
            try:
                set_json_setting(db, "dump_last_card", card)
            except Exception:
                pass
        return redirect("/receipts/emails/" + batch_id)

    # ── POST /receipts/emails/<batch_id>/item/<item_id> ──────────────────────

    @app.post("/receipts/emails/<batch_id>/item/<item_id>")
    @require_login
    def email_scan_item_update(batch_id: str, item_id: str):
        from flask import request as _req
        batch = em_store.get_batch(config.admin_db_file, batch_id)
        if not batch:
            if _req.headers.get("X-Requested-With") == "XMLHttpRequest":
                return {"ok": False, "error": "not found"}, 404
            return redirect(f"/receipts/emails/{batch_id}")

        is_test_batch = bool(batch.get("is_test"))
        new_status   = (_req.form.get("status") or "").strip()
        account_raw  = (_req.form.get("account") or "").strip()

        updates: dict = {}
        if new_status in ("new", "ignored", "possible_dup"):
            updates["status"] = new_status
        # account changes only apply to live batches
        if not is_test_batch and account_raw and "|" in account_raw:
            code, name = account_raw.split("|", 1)
            updates["category_account_code"] = code.strip()
            updates["category_account_name"] = name.strip()
            if updates.get("status") != "ignored":
                updates["status"] = "new"
        if updates:
            em_store.update_item(config.admin_db_file, item_id, **updates)

        if _req.headers.get("X-Requested-With") == "XMLHttpRequest":
            return {"ok": True, "status": updates.get("status", new_status)}
        return redirect(f"/receipts/emails/{batch_id}")

    # ── POST /receipts/emails/<batch_id>/item/<item_id>/rescan ───────────────
    # "Scan anyway" on a skipped email — force-processes that one message,
    # bypassing the AI gates and the photo/logo attachment filter.

    @app.post("/receipts/emails/<batch_id>/item/<item_id>/rescan")
    @require_login
    def email_scan_item_rescan(batch_id: str, item_id: str):
        batch = em_store.get_batch(config.admin_db_file, batch_id)
        if not batch or batch.get("is_test"):
            return redirect(f"/receipts/emails/{batch_id}")

        gmail_ok, _msg = _gmail_connected()
        if not gmail_ok:
            return redirect(f"/receipts/emails/{batch_id}")

        gmail_creds = load_admin_credentials(config)
        svc         = ReceiptService(config)

        exp_accts: list = []
        try:
            _at, _tid, _ = _load_xero_at_tid(config)
            exp_accts, _ = _get_xero_expense_accounts(_at, _tid, config.admin_db_file)
        except Exception:
            pass

        _oa_cfg    = get_openai_settings(config.admin_db_file)
        openai_key = (_oa_cfg.get("api_key") or "").strip() or (os.getenv("OPENAI_API_KEY") or "").strip()

        try:
            n, err = em_pipe.rescan_message(
                batch_id, item_id, config.admin_db_file,
                gmail_creds=gmail_creds,
                exp_accounts=exp_accts,
                vat_rate=20.0,
                openai_key=openai_key or "",
                receipt_service=svc,
                own_names=_escan_settings().get("own_company_names", []),
            )
            if err:
                print(f"[escan] rescan {item_id}: {err}", flush=True)
        except Exception as e:
            print(f"[escan] rescan {item_id} failed: {e}", flush=True)
        return redirect(f"/receipts/emails/{batch_id}")

    # ── POST /receipts/emails/<batch_id>/import ──────────────────────────────

    @app.post("/receipts/emails/<batch_id>/import")
    @require_login
    def email_scan_import(batch_id: str):
        from flask import request as _req
        batch = em_store.get_batch(config.admin_db_file, batch_id)
        if not batch:
            return redirect("/receipts/emails")
        if batch.get("is_test"):
            return redirect(f"/receipts/emails/{batch_id}")

        eid_raw = (_req.form.get("engineer_id") or "").strip()
        settings = _escan_settings()
        eid      = int(eid_raw) if eid_raw else settings.get("default_engineer_id")
        if not eid:
            return redirect(f"/receipts/emails/{batch_id}")

        imported = em_pipe.import_batch_items(
            batch_id, config.admin_db_file, default_engineer_id=int(eid)
        )
        # Imported invoices now ride into Xero via the expense receipt; drop the
        # local copies we no longer need so images aren't retained on disk.
        _escan_purge_done_files(batch_id)
        return redirect(f"/receipts/emails/{batch_id}?imported={imported}")

    # ── POST /receipts/emails/delete-tests ──────────────────────────────────
    @app.post("/receipts/emails/delete-tests")
    @require_login
    def email_scan_delete_tests():
        batches = em_store.list_batches(config.admin_db_file)
        for b in batches:
            if b.get("is_test"):
                try:
                    _escan_purge_batch_files(b["id"])
                    em_store.delete_batch(config.admin_db_file, b["id"])
                except Exception:
                    pass
        return redirect("/receipts/emails")

    # ── GET /receipts/emails/<batch_id>/status (JSON polling) ────────────────
    @app.get("/receipts/emails/<batch_id>/status")
    @require_login
    def email_scan_batch_status(batch_id: str):
        batch = em_store.get_batch(config.admin_db_file, batch_id)
        if not batch:
            return {"ok": False}
        raw_sum = batch.get("summary") or {}
        if isinstance(raw_sum, str):
            try:
                raw_sum = json.loads(raw_sum)
            except Exception:
                raw_sum = {}
        return {
            "ok":      True,
            "status":  batch.get("status", "processing"),
            "scanned": raw_sum.get("scanned", 0),
            "total":   raw_sum.get("total", 0),
            "found":   raw_sum.get("found", 0),
        }

    # ── GET /receipts/emails/<batch_id>/item/<item_id>/image ─────────────────
    @app.get("/receipts/emails/<batch_id>/item/<item_id>/image")
    @require_login
    def email_scan_item_image(batch_id: str, item_id: str):
        item = em_store.get_item(config.admin_db_file, item_id)
        if not item or item.get("batch_id") != batch_id:
            return ("Not found", 404)
        path = os.path.abspath(item.get("stored_file") or "")
        if not path or not os.path.exists(path):
            return ("Attachment no longer stored locally — it was cleaned up after "
                    "import (the original is attached to the receipt in Xero).", 404)
        with open(path, "rb") as fh:
            head = fh.read(16)
        safe_mime = _exp_sniff_mime(head)
        if safe_mime and safe_mime.startswith("image/"):
            return send_file(path, mimetype=safe_mime)
        if safe_mime == "application/pdf":
            return send_file(path, mimetype="application/pdf")
        # Word docs and anything else → offer as download
        fname_dl = os.path.basename(path) or "invoice"
        return send_file(path, as_attachment=True, download_name=fname_dl)

    # ── POST /receipts/emails/<batch_id>/delete ──────────────────────────────
    @app.post("/receipts/emails/<batch_id>/delete")
    @require_login
    def email_scan_delete(batch_id: str):
        _escan_purge_batch_files(batch_id)
        try:
            em_store.delete_batch(config.admin_db_file, batch_id)
        except Exception:
            pass
        return redirect("/receipts/emails")

    # ═══════════════════════════════════════════════════════════════════════════
    # End Email Invoice Importer
    # ═══════════════════════════════════════════════════════════════════════════

    return app


def run_web() -> None:
    config = load_config()
    app = create_app()
    app.run(host=config.web_host, port=config.web_port)


if __name__ == "__main__":
    run_web()
