from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import time
from zoneinfo import ZoneInfo

from .admin_store import (
    DEFAULT_SALES_STATS_FIELDS,
    DEFAULT_STATS_FIELDS,
    add_seen_submitter,
    get_cash_backlog,
    get_cash_submitter_sheets,
    get_calendar_cash_sheets,
    get_calendar_sales_sheets,
    get_active_calendars,
    get_cash_sheet_target,
    get_enabled,
    get_google_watches,
    get_json_setting,
    get_seen_submitters,
    get_sheet_target,
    get_sheet_backlog,
    get_sales_backlog,
    get_sales_sheet_target,
    get_sales_stats_fields,
    get_sales_submitter_sheets,
    get_stats_fields,
    get_submitter_aliases,
    init_admin_store,
    set_cash_backlog,
    set_sheet_backlog,
    set_sales_backlog,
    set_google_watch,
    delete_google_watch,
)
from .config import load_config
from .event_processor import (
    apply_validation_hints,
    compute_invoice_totals,
    done_choice_is_yes,
    ensure_notes_template,
    normalize_user_sections,
    send_choice_is_yes,
    extract_event_details,
    extract_invoice_lines,
    extract_sales_lines,
    invoice_has_cash_marker,
    parse_customer_fields,
    parse_invoice_contact_overrides,
    collapse_invoice_override_section,
    parse_event_address,
    payment_choice,
    upsert_invoice_profile_missing_hint,
    upsert_send_failure,
    upsert_invoice_summary,
    upsert_cash_confirmation,
    upsert_send_confirmation,
    get_title_progress_dots,
    set_title_status_emoji,
    set_title_mail_emoji,
    validate_customer_fields,
)
from .google_calendar import (
    build_calendar_service,
    list_recent_events,
    update_event_description,
    register_calendar_watch,
    stop_calendar_watch,
    RateLimitError,
)
from .trigger import (
    wait_for_poll,
    consume_watch_check,
    consume_calendar_targets,
    consume_event_targets,
)
from .google_sheets import append_stats_row, ensure_header, update_invoice_paid_in_sheet
from .google_admin import load_admin_credentials
from .state import (
    get_cash_log_marker,
    get_contact_for_event,
    get_contact_update_marker,
    get_contact_fingerprint,
    get_draft_sync_attempted_at,
    get_draft_sync_fingerprint,
    get_invoice_for_event,
    get_invoice_update_marker,
    get_last_sync,
    get_processed_update_marker,
    get_cash_global_log_marker,
    get_sheet_log_marker,
    get_sales_log_marker,
    is_prefilled,
    is_invoice_sent,
    is_invoice_paid,
    is_processed,
    load_state,
    mark_prefilled,
    mark_invoice_sent,
    mark_invoice_paid,
    mark_processed,
    prune_state,
    save_state,
    set_contact_update_marker,
    set_contact_for_event,
    set_contact_fingerprint,
    set_draft_sync_attempted_at,
    set_draft_sync_fingerprint,
    set_invoice_for_event,
    set_invoice_update_marker,
    set_last_sync,
    set_cash_log_marker,
    set_cash_global_log_marker,
    set_processed_update_marker,
    set_sheet_log_marker,
    set_sales_log_marker,
)
from .xero_client import XeroClient, build_xero_client, get_xero_rate_limit_until_ts
from .log_feed import feed as _feed

LONDON_TZ = ZoneInfo("Europe/London")

# Engineering note:
# If you change polling/retry/title-state behavior in this module, update
# docs/ENGINEERING_LOGIC_GUARDRAILS.md in the same commit.


def run() -> None:
    config = load_config()
    init_admin_store(config.admin_db_file)
    # Baseline: do not touch any events created before this run starts.
    run_started_at = dt.datetime.now(dt.timezone.utc)
    state = load_state(config.state_file)
    state["run_started_at"] = run_started_at.isoformat()
    if not state.get("xero_retry_queue_reset_at"):
        _retry_after_map = state.get("event_xero_retry_after") or {}
        _retry_backoff_map = state.get("event_xero_retry_backoff") or {}
        if _retry_after_map or _retry_backoff_map:
            state["event_xero_retry_after"] = {}
            state["event_xero_retry_backoff"] = {}
            _feed.push("Xero retry queue reset on startup", "system")
        state["xero_retry_queue_reset_at"] = run_started_at.isoformat()
    save_state(config.state_file, state)

    has_saved_sync = "last_sync" in state
    if has_saved_sync:
        last_sync = get_last_sync(state)
    else:
        # First run: only look back a few minutes to avoid replaying old DONE events.
        last_sync = run_started_at - dt.timedelta(minutes=5)
    if last_sync > run_started_at:
        last_sync = run_started_at
    # Allow up to 4 hours of lookback on restart so events missed during crashes
    # or brief downtime are caught on the next startup. The is_processed check
    # in the event loop prevents double-processing of already-handled events.
    lookback_floor = run_started_at - dt.timedelta(hours=4)
    if last_sync < lookback_floor:
        last_sync = lookback_floor

    def _build_xero_client_safe() -> XeroClient | None:
        try:
            return build_xero_client(config)
        except Exception as exc:
            print(f"Xero client init failed: {exc}", flush=True)
            _feed.push(
                f"Xero auth failed: {str(exc).splitlines()[0][:120]}",
                "error",
            )
            return None

    xero_client = _build_xero_client_safe()
    _xero_built_at: float = time.time()
    _xero_retry_after: float = float(state.get("xero_lockout_until_ts") or 0.0)
    _xero_lock_notice_ts: float = 0.0
    _XERO_REBUILD_INTERVAL = 3300  # rebuild token ~55 min (Xero tokens last 30 min)
    _XERO_429_COOLDOWN_SECONDS = 180
    _XERO_EVENT_429_COOLDOWN_SECONDS = 900
    _XERO_EVENT_429_MAX_COOLDOWN_SECONDS = max(
        int(os.getenv("XERO_EVENT_429_MAX_COOLDOWN_SECONDS", "90000") or "90000"),
        _XERO_EVENT_429_COOLDOWN_SECONDS,
    )
    _XERO_LOCK_PROBE_SECONDS = max(
        int(os.getenv("XERO_LOCK_PROBE_SECONDS", "1800") or "1800"), 300
    )
    _XERO_HEALTH_CHECK_SECONDS = max(
        int(os.getenv("XERO_HEALTH_CHECK_SECONDS", "1800") or "1800"), 300
    )
    _last_xero_health_check_ts: float = 0.0
    _XERO_EVENTS_PER_CYCLE = max(int(os.getenv("XERO_EVENTS_PER_CYCLE", "4") or "4"), 1)
    _last_xero_429_notice_at: float = 0.0

    _headers_initialized: set[str] = set()  # sheet keys that have had ensure_header run

    _last_watch_check: float = 0.0
    _WATCH_CHECK_INTERVAL = 3600  # re-check watches at most once per hour

    # Scan strategy:
    # - Webhook-targeted cycles scan only touched calendars/events.
    # - Every poll cycle does a lightweight "recent saves" Google-only safety scan.
    # - Hourly reconcile scans past entries for paid status, in controlled Xero batches.
    _last_hourly_reconcile_ts: float = 0.0
    _HOURLY_RECONCILE_SECONDS = max(
        int(os.getenv("HOURLY_RECONCILE_SECONDS", "3600") or "3600"), 300
    )
    _HOURLY_RECONCILE_PAST_DAYS = max(
        int(os.getenv("HOURLY_RECONCILE_PAST_DAYS", "45") or "45"), 1
    )
    _DRAFT_CLEANUP_PER_HOURLY = max(
        int(os.getenv("DRAFT_CLEANUP_PER_HOURLY", "1") or "1"), 1
    )
    _DRAFT_CLEANUP_MIN_BACKOFF = max(
        int(os.getenv("DRAFT_CLEANUP_MIN_BACKOFF", "3600") or "3600"), 300
    )
    _DRAFT_CLEANUP_MAX_BACKOFF = max(
        int(os.getenv("DRAFT_CLEANUP_MAX_BACKOFF", "86400") or "86400"),
        _DRAFT_CLEANUP_MIN_BACKOFF,
    )
    _DRAFT_SYNC_COOLDOWN_SECONDS = max(
        int(os.getenv("DRAFT_SYNC_COOLDOWN_SECONDS", "120") or "120"), 10
    )

    backoff_seconds = max(config.poll_seconds, 5)
    max_backoff = max(backoff_seconds, 60)

    def safe_update(
        event_id: str,
        description: str,
        label: str | None = None,
        summary_status: str | None = None,
        current_summary: str | None = None,
        calendar_id: str | None = None,
        draft_progress_increment: bool = False,
    ):
        import re

        summary = None
        if summary_status:
            # Never downgrade a paid/green title back to yellow/orange.
            # This avoids regressions when delayed poll paths run after a paid webhook update.
            if (
                summary_status in {"yellow", "orange"}
                and current_summary
                and re.match(r"^\s*🟢\b", current_summary)
            ):
                summary_status = "green"
            desc_lower = (description or "").lower()
            is_draft_orange = (
                summary_status == "orange"
                and ("invoice sent ✅".lower() not in desc_lower)
                and ("invoice send failed ❌".lower() not in desc_lower)
                and ("entry complete ✅".lower() not in desc_lower)
            )
            draft_dots = 0
            if summary_status == "orange":
                if is_draft_orange:
                    existing_dots = get_title_progress_dots(current_summary)
                    # First draft save from non-orange => plain orange.
                    # Subsequent draft edits while already orange => +1 dot each save.
                    if draft_progress_increment and (current_summary or "").strip().startswith("🟠"):
                        # Keep title readable: cap progress dots.
                        draft_dots = min(existing_dots + 1, 3)
                    else:
                        draft_dots = min(existing_dots, 3)
                else:
                    # Non-draft states keep orange without progress dots.
                    draft_dots = 0
            summary = set_title_status_emoji(
                current_summary,
                summary_status,
                draft_dots=(draft_dots if summary_status == "orange" else None),
            )
            mail_failed = "invoice send failed" in (description or "").lower()
            summary = set_title_mail_emoji(summary, mail_failed)
        try:
            return update_event_description(
                config=config,
                event_id=event_id,
                description=description,
                summary=summary,
                calendar_id=calendar_id,
            )
        except RateLimitError:
            prefix = f"{label}: " if label else ""
            print(f"{prefix}Google Calendar rate limit hit. Skipping update for {event_id}.")
            _feed.push(f"{prefix or 'Update: '}rate-limited for {event_id}", "warn")
            return None
        except Exception as exc:
            prefix = f"{label}: " if label else ""
            print(f"{prefix}Google Calendar update failed for {event_id}: {exc}", flush=True)
            _feed.push(
                f"{prefix or 'Update: '}failed for {event_id}: {str(exc).splitlines()[0][:100]}",
                "error",
            )
            return None

    def _extract_totals(invoice_resp: dict) -> tuple[float | None, float | None]:
        invoices = invoice_resp.get("Invoices") or []
        if not invoices:
            return None, None
        inv = invoices[0]
        subtotal = inv.get("SubTotal")
        total = inv.get("Total")
        if subtotal is None or total is None:
            return None, None
        return float(subtotal), float(total)

    def _invoice_brief(invoice_resp: dict) -> str:
        invoices = invoice_resp.get("Invoices") or []
        if not invoices:
            return "no invoice payload"
        inv = invoices[0]
        number = inv.get("InvoiceNumber") or inv.get("InvoiceID") or "unknown"
        total = inv.get("Total")
        status = inv.get("Status") or "unknown"
        if total is None:
            return f"{number} ({status})"
        return f"{number} ({status}) total £{float(total):.2f}"

    def _is_invoice_mutable(invoice_id: str) -> tuple[bool, str]:
        """
        Invoices can be mutated from calendar edits when:
        - DRAFT
        - SUBMITTED
        - AUTHORISED with no payments applied yet
        """
        if not xero_client:
            return False, "NO_CLIENT"
        try:
            invoice_data = xero_client.get_invoice(invoice_id)
        except Exception as exc:
            return False, f"LOOKUP_FAILED: {exc}"
        status = (invoice_data.get("Status") or "").upper()
        if status in {"DRAFT", "SUBMITTED"}:
            return True, status
        if status == "AUTHORISED":
            try:
                amount_paid = float(invoice_data.get("AmountPaid") or 0.0)
            except Exception:
                amount_paid = 0.0
            return amount_paid <= 0.0001, status
        return False, status or "UNKNOWN"

    def _format_display_datetime(raw: str | None = None) -> str:
        if raw:
            try:
                ts = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=dt.timezone.utc)
                return ts.astimezone(LONDON_TZ).strftime("%d/%m/%Y %H:%M")
            except ValueError:
                pass
        return dt.datetime.now(dt.timezone.utc).astimezone(LONDON_TZ).strftime("%d/%m/%Y %H:%M")

    _calendar_name_cache: dict[str, str] = {}
    _calendar_name_cache_ts: float = 0.0

    def _refresh_calendar_name_cache(active_calendar_ids: list[str]) -> None:
        nonlocal _calendar_name_cache, _calendar_name_cache_ts
        now_ts = time.time()
        # Refresh at most every 10 minutes.
        if _calendar_name_cache and (now_ts - _calendar_name_cache_ts) < 600:
            return
        try:
            service = build_calendar_service(config)
        except Exception:
            return
        mapping: dict[str, str] = {}
        page_token = None
        try:
            while True:
                resp = (
                    service.calendarList()
                    .list(pageToken=page_token, maxResults=250)
                    .execute()
                )
                for item in resp.get("items", []) or []:
                    cid = str(item.get("id") or "").strip()
                    if not cid:
                        continue
                    label = str(
                        item.get("summaryOverride")
                        or item.get("summary")
                        or item.get("id")
                        or ""
                    ).strip()
                    if label:
                        mapping[cid] = label
                page_token = resp.get("nextPageToken")
                if not page_token:
                    break
        except Exception:
            # Keep old cache on transient API failures.
            if _calendar_name_cache:
                return
        for cid in active_calendar_ids:
            mapping.setdefault(cid, cid)
        _calendar_name_cache = mapping
        _calendar_name_cache_ts = now_ts

    def _diary_entry_name(event: dict) -> str:
        import re

        cal_id = str(event.get("_calendar_id") or "").strip()
        if cal_id and cal_id in _calendar_name_cache:
            return _calendar_name_cache[cal_id]
        organizer = (event.get("organizer") or {})
        calendar_name = str(organizer.get("displayName") or "").strip()
        if calendar_name:
            return calendar_name
        text = (event.get("summary") or "").strip()
        text = re.sub(r"^\s*[🔵🟠🟡🟢🔴]\s*", "", text)
        text = re.sub(r"^\s*✉️?\s*", "", text)
        return text.strip()

    def _extract_title_status(summary: str | None) -> str | None:
        text = (summary or "").strip()
        for em in ("🔵", "🟠", "🟡", "🟢", "🔴"):
            if text.startswith(em):
                return em
        return None

    def _has_managed_sections(description: str | None) -> bool:
        text = (description or "").lower()
        return "[contact]" in text and "[invoice]" in text

    def _expected_title_status(
        description: str | None,
        *,
        has_done: bool,
        has_send: bool,
        sent_state: bool,
        paid_state: bool,
        current_summary: str | None,
        event_key: str,
    ) -> str:
        # If sending is requested but required integrations are down, show red immediately.
        if integration_issues and (has_done or has_send):
            return "red"
        # Once paid is confirmed (webhook or poll), always keep green regardless of mode.
        if paid_state:
            return "green"
        # Only show "yellow/pending" after send was actually confirmed.
        # If staff sets SEND=Y but Xero send/authorise fails, keep orange.
        if sent_state:
            pay_mode = payment_choice(description) or ""
            # Card/cash are immediate payment flows once send is confirmed.
            if sent_state and pay_mode in {"card", "cash"}:
                return "green"
            # For INVOICE mode, stay green once paid was observed.
            if pay_mode == "invoice":
                if paid_state:
                    return "green"
                if (current_summary or "").strip().startswith("🟢"):
                    return "green"
            return "yellow"
        if has_send and not sent_state:
            return "orange"
        if has_done:
            return "orange"
        return "blue"

    def _invoice_address_from_overrides(overrides: dict, fallback: dict | None) -> dict:
        invoice_profile = str(overrides.get("invoice_profile", "") or "").strip()
        invoice_name = str(overrides.get("invoice_name", "") or "").strip()
        addr1 = str(overrides.get("invoice_address_line_1", "") or "").strip()
        addr2 = str(overrides.get("invoice_address_line_2", "") or "").strip()
        city = str(overrides.get("invoice_city", "") or "").strip()
        postcode = str(overrides.get("invoice_postcode", "") or "").strip()
        country = str(overrides.get("invoice_country", "") or "").strip()
        has_any_invoice_override = any(
            [invoice_profile, invoice_name, addr1, addr2, city, postcode, country]
        )
        has_address_override = any([addr1, addr2, city, postcode, country])

        # No invoice override lines at all: use parsed Google/location fallback as before.
        if not has_any_invoice_override:
            return fallback or {}
        # Any invoice override line present: never backfill missing address fields
        # from Google/location data.
        if not has_address_override:
            return {}

        out = {"AddressType": "POBOX"}
        if addr1:
            out["AddressLine1"] = addr1
        if addr2:
            out["AddressLine2"] = addr2
        if city:
            out["City"] = city
        if postcode:
            out["PostalCode"] = postcode
        if country:
            out["Country"] = country
        return out

    def _resolve_invoice_contact(
        *,
        event: dict,
        customer: dict,
        location: str | None,
    ) -> tuple[dict | None, dict, str, str | None]:
        """
        Returns: (contact, address_payload, contact_name_for_logs, error_code)
        error_code:
          - 'PROFILE_NOT_FOUND' when Invoice profile is set but no match exists.
          - None on success / non-blocking no-op.
        """
        overrides = parse_invoice_contact_overrides(event.get("description"))
        fallback_address = parse_event_address(location)
        address_payload = _invoice_address_from_overrides(overrides, fallback_address)
        invoice_name = str(overrides.get("invoice_name", "") or "").strip() or str(customer.get("name", "") or "").strip()
        invoice_profile = str(overrides.get("invoice_profile", "") or "").strip()

        if not xero_client:
            return None, address_payload, invoice_name or invoice_profile or "", None

        if invoice_profile:
            matched = xero_client.find_contact_by_name(invoice_profile)
            if not matched or not matched.get("ContactID"):
                return None, address_payload, invoice_name or invoice_profile, "PROFILE_NOT_FOUND"
            # Invoice profile mode must not mutate/replace profile contact fields.
            return matched, {}, str(matched.get("Name") or invoice_profile), None

        if not invoice_name:
            return None, address_payload, "", None

        contact_result = xero_client.ensure_contact(
            name=invoice_name,
            email=customer.get("email", ""),
            phone=customer.get("phone", ""),
            address=address_payload if address_payload else None,
        )
        contact = contact_result.get("contact")
        return contact, address_payload, invoice_name, None

    def _append_sheet_stats_if_enabled(
        *,
        event: dict,
        event_key: str,
        invoice_id: str,
        payment_method: str,
        paid_override: bool | None = None,
        submitter_display: str,
        admin_creds,
        sheet_target: dict[str, str],
        stats_fields: list[str],
        state: dict,
    ) -> dict:
        if not stats_fields:
            return state

        spreadsheet_id = sheet_target.get("spreadsheet_id", "").strip()
        sheet_name = sheet_target.get("sheet_name", "Sheet1").strip() or "Sheet1"

        def _fmt_british(iso_str: str) -> str:
            if not iso_str:
                return ""
            try:
                if "T" in iso_str:
                    obj = dt.datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
                    if obj.tzinfo is None:
                        obj = obj.replace(tzinfo=dt.timezone.utc)
                    return obj.astimezone(LONDON_TZ).strftime("%d/%m/%Y %H:%M")
                else:
                    obj = dt.date.fromisoformat(iso_str)
                    return obj.strftime("%d/%m/%Y")
            except Exception:
                return iso_str

        def _fmt_money(value) -> str:
            if value in (None, ""):
                return ""
            try:
                return f"{float(value):.2f}"
            except Exception:
                return str(value)

        invoice = {}
        if xero_client and invoice_id:
            try:
                invoice = xero_client.get_invoice(invoice_id)
            except Exception as exc:
                # Keep going with calendar-derived totals so universal sheet still logs.
                print(f"Sheets: failed to read invoice {invoice_id}, using fallback totals: {exc}")

        subtotal = invoice.get("SubTotal")
        total = invoice.get("Total")
        if subtotal in (None, "") or total in (None, ""):
            fallback_lines = extract_invoice_lines(event.get("description"))
            if fallback_lines:
                fb_subtotal, fb_total = compute_invoice_totals(fallback_lines)
                if subtotal in (None, ""):
                    subtotal = fb_subtotal
                if total in (None, ""):
                    total = fb_total

        start = (event.get("start", {}) or {}).get("dateTime") or (event.get("start", {}) or {}).get("date") or ""
        end = (event.get("end", {}) or {}).get("dateTime") or (event.get("end", {}) or {}).get("date") or ""
        start_fmt = _fmt_british(start)
        end_fmt = _fmt_british(end)
        slot_text = f"{start_fmt} – {end_fmt}".strip(" –") if start_fmt != end_fmt else start_fmt
        customer_fields = parse_customer_fields(event.get("description"))
        paid_immediately = paid_override if paid_override is not None else (payment_method.lower() in {"card", "cash"})
        payload_payment_method = payment_method.upper() if payment_method else "N/A"
        payload = {
            "diary_entry_name": _diary_entry_name(event),
            "submitter": submitter_display
            or (event.get("creator", {}) or {}).get("email")
            or (event.get("organizer", {}) or {}).get("email")
            or "",
            "customer": customer_fields.get("name") or "",
            "invoice_number": invoice.get("InvoiceNumber") or "",
            "receipt_details": "",
            "slot_datetime": slot_text,
            "payment_datetime": dt.datetime.now(dt.timezone.utc).astimezone(LONDON_TZ).strftime("%d/%m/%Y %H:%M")
            if paid_immediately
            else "🔴 N/A",
            "payment_method": payload_payment_method,
            "paid_status": "Paid" if paid_immediately else "Pending",
            "job_cost_ex_vat": _fmt_money(subtotal),
            "job_cost_inc_vat": _fmt_money(total),
        }
        event_id_raw = event.get("id") or ""
        date_part = start.split("T", 1)[0].replace("-", "")
        suffix = event_id_raw[-4:] if event_id_raw else "0000"
        event_id_display = f"GC-{date_part}-{suffix}" if date_part else (event_id_raw or event_key)

        marker = (
            f"{invoice_id}:{payload_payment_method}:{spreadsheet_id}:{sheet_name}:"
            f"{payload.get('job_cost_ex_vat','')}:{payload.get('job_cost_inc_vat','')}"
        ).upper()
        if get_sheet_log_marker(state, event_key) == marker:
            return state

        def _queue_backlog() -> None:
            backlog = get_sheet_backlog(config.admin_db_file)
            row = {
                "event_key": event_key,
                "invoice_id": invoice_id,
                "payment_method": payload_payment_method,
                "stats_fields": stats_fields,
                "payload": payload,
                "event_id_display": event_id_display,
                "marker": marker,
            }
            replaced = False
            for idx, existing in enumerate(backlog):
                if existing.get("event_key") == event_key:
                    backlog[idx] = row
                    replaced = True
                    break
            if not replaced:
                backlog.append(row)
            set_sheet_backlog(config.admin_db_file, backlog)

        if not admin_creds:
            _queue_backlog()
            print(f"Sheets row queued for {event_key}: Google credentials unavailable")
            return state
        if not spreadsheet_id:
            _queue_backlog()
            print(f"Sheets row queued for {event_key}: universal sheet target not set")
            return state
        try:
            ensure_header(
                admin_creds,
                spreadsheet_id=spreadsheet_id,
                sheet_name=sheet_name,
                stats_fields=stats_fields,
            )
            append_stats_row(
                admin_creds,
                spreadsheet_id=spreadsheet_id,
                sheet_name=sheet_name,
                event_key=event_key,
                stats_fields=stats_fields,
                payload=payload,
                event_id_display=event_id_display,
            )
            print(f"Sheets row appended for {event_key}")
            _feed.push(f"Sheet row logged: {event_key}", "success")
            backlog = get_sheet_backlog(config.admin_db_file)
            backlog = [r for r in backlog if r.get("event_key") != event_key]
            set_sheet_backlog(config.admin_db_file, backlog)
            return set_sheet_log_marker(state, event_key, marker)
        except Exception as exc:
            print(f"Sheets append failed for {event_key}: {exc}")
            _queue_backlog()
            return state

    def _flush_sheet_backlog(admin_creds, sheet_target: dict[str, str], state: dict) -> dict:
        if not admin_creds:
            return state
        backlog = get_sheet_backlog(config.admin_db_file)
        if not backlog:
            return state
        spreadsheet_id = (sheet_target.get("spreadsheet_id") or "").strip()
        sheet_name = (sheet_target.get("sheet_name") or "Sheet1").strip() or "Sheet1"
        if not spreadsheet_id:
            return state

        remaining: list[dict] = []
        for row in backlog:
            event_key = str(row.get("event_key") or "").strip()
            payload = row.get("payload") or {}
            event_id_display = str(row.get("event_id_display") or "").strip()
            marker = str(row.get("marker") or "").strip()
            stats = row.get("stats_fields") or []
            if not event_key or not isinstance(payload, dict):
                continue
            try:
                ensure_header(
                    admin_creds,
                    spreadsheet_id=spreadsheet_id,
                    sheet_name=sheet_name,
                    stats_fields=[str(s) for s in stats] or DEFAULT_STATS_FIELDS,
                )
                append_stats_row(
                    admin_creds,
                    spreadsheet_id=spreadsheet_id,
                    sheet_name=sheet_name,
                    event_key=event_key,
                    stats_fields=[str(s) for s in stats] or DEFAULT_STATS_FIELDS,
                    payload=payload,
                    event_id_display=event_id_display,
                )
                if marker:
                    state = set_sheet_log_marker(state, event_key, marker)
                print(f"Universal sheet backlog flushed for {event_key}", flush=True)
            except Exception as exc:
                print(f"Universal sheet backlog flush failed for {event_key}: {exc}", flush=True)
                remaining.append(row)
        if len(remaining) != len(backlog):
            set_sheet_backlog(config.admin_db_file, remaining)
        return state

    def _append_sales_rows_if_enabled(
        *,
        event: dict,
        event_key: str,
        invoice_id: str,
        payment_method: str,
        submitter_email: str,
        submitter_display: str,
        admin_creds,
        sales_sheet_target: dict[str, str],
        sales_stats_fields: list[str],
        state: dict,
    ) -> dict:
        if not admin_creds:
            print(
                f"Sales row skipped for {event_key}: admin_creds={bool(admin_creds)}",
                flush=True,
            )
            return state
        effective_sales_stats_fields = [
            str(s) for s in (sales_stats_fields or DEFAULT_SALES_STATS_FIELDS)
        ]

        sales_lines = extract_sales_lines(event.get("description"))
        if not sales_lines:
            print(f"Sales row skipped for {event_key}: no sales lines parsed from description", flush=True)
            return state

        # Exclude "Materials" lines from the sales sheet (Xero invoice is unaffected).
        sales_lines = [
            li for li in sales_lines
            if "material" not in (li.get("Description") or "").lower()
        ]
        if not sales_lines:
            print(f"Sales row skipped for {event_key}: only materials lines, nothing to write", flush=True)
            return state

        calendar_id = (event.get("_calendar_id") or config.google_calendar_id or "").strip()
        cal_mapping = get_calendar_sales_sheets(config.admin_db_file)
        route = cal_mapping.get(calendar_id) or {}
        spreadsheet_id = str(route.get("spreadsheet_id", "")).strip()
        sheet_name = str(route.get("sheet_name", "Sales")).strip() or "Sales"
        if not spreadsheet_id:
            spreadsheet_id = sales_sheet_target.get("spreadsheet_id", "").strip()
            sheet_name = sales_sheet_target.get("sheet_name", "Sales").strip() or "Sales"

        sales_total_ex = round(
            sum(float(li.get("UnitAmount") or 0.0) * float(li.get("Quantity") or 1.0) for li in sales_lines),
            2,
        )
        sales_total_inc = round(
            sum(
                (float(li.get("UnitAmount") or 0.0) * float(li.get("Quantity") or 1.0))
                * (1.2 if (li.get("TaxType") or "").upper() == "OUTPUT2" else 1.0)
                for li in sales_lines
            ),
            2,
        )

        invoice_number_display = ""
        if xero_client and invoice_id:
            try:
                inv = xero_client.get_invoice(invoice_id)
                invoice_number_display = inv.get("InvoiceNumber") or ""
            except Exception as _exc:
                print(f"Sales rows: failed to fetch invoice number for {invoice_id}: {_exc}")

        def _fmt_british(iso_str: str) -> str:
            if not iso_str:
                return ""
            try:
                if "T" in iso_str:
                    obj = dt.datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
                    if obj.tzinfo is None:
                        obj = obj.replace(tzinfo=dt.timezone.utc)
                    return obj.astimezone(LONDON_TZ).strftime("%d/%m/%Y %H:%M")
                obj = dt.date.fromisoformat(iso_str)
                return obj.strftime("%d/%m/%Y")
            except Exception:
                return iso_str

        start = (event.get("start", {}) or {}).get("dateTime") or (event.get("start", {}) or {}).get("date") or ""
        end = (event.get("end", {}) or {}).get("dateTime") or (event.get("end", {}) or {}).get("date") or ""
        start_fmt = _fmt_british(start)
        end_fmt = _fmt_british(end)
        slot_text = f"{start_fmt} – {end_fmt}".strip(" –") if start_fmt != end_fmt else start_fmt
        customer_fields = parse_customer_fields(event.get("description"))
        event_id_raw = event.get("id") or ""
        date_part = start.split("T", 1)[0].replace("-", "")
        suffix = event_id_raw[-4:] if event_id_raw else "0000"
        event_id_display = f"GC-{date_part}-{suffix}" if date_part else (event_id_raw or event_key)

        sales_lines_text: list[str] = []
        for line in sales_lines:
            ex_vat = round(
                float(line.get("UnitAmount") or 0.0) * float(line.get("Quantity") or 1.0),
                2,
            )
            sales_lines_text.append(f"{line.get('Description') or ''} = £{ex_vat:.2f}")

        payload_rows: list[dict] = [
            {
                "event_key": f"{event_key}:sales",
                "event_id_display": f"{event_id_display}-S",
                "payload": {
                    "submitter": submitter_display
                    or (event.get("creator", {}) or {}).get("email")
                    or (event.get("organizer", {}) or {}).get("email")
                    or "",
                    "customer": customer_fields.get("name") or "",
                    "invoice_number": invoice_number_display,
                    "slot_datetime": slot_text,
                    "payment_method": payment_method.upper() if payment_method else "",
                    "sales_item_desc": "\n".join(sales_lines_text),
                    "sales_total_ex_vat": f"{sales_total_ex:.2f}",
                },
            }
        ]

        backlog_row = {
            "event_key": event_key,
            "calendar_id": calendar_id,
            "submitter_email": submitter_email.lower(),
            "stats_fields": effective_sales_stats_fields,
            "rows": payload_rows,
            "event_id_display": event_id_display,
            "invoice_id": invoice_id,
            "payment_method": payment_method,
            "sales_total_ex": sales_total_ex,
            "sales_total_inc": sales_total_inc,
        }

        def _upsert_sales_backlog_row() -> None:
            backlog = get_sales_backlog(config.admin_db_file)
            replaced = False
            for idx, existing in enumerate(backlog):
                if existing.get("event_key") == event_key:
                    backlog[idx] = backlog_row
                    replaced = True
                    break
            if not replaced:
                backlog.append(backlog_row)
            set_sales_backlog(config.admin_db_file, backlog)

        if not spreadsheet_id:
            print(f"Sales row skipped/queued for {event_key}: no sales sheet mapped for calendar '{calendar_id}' (cal_mapping keys={list(cal_mapping.keys())})", flush=True)
            _upsert_sales_backlog_row()
            print(
                f"Sales row queued for {event_key}: no sales sheet mapped for calendar {calendar_id}",
                flush=True,
            )
            return state
        print(f"Sales row writing for {event_key}: spreadsheet={spreadsheet_id} sheet={sheet_name} lines={len(sales_lines)}", flush=True)

        marker = (
            f"{invoice_id}:{payment_method}:sales:{spreadsheet_id}:{sheet_name}:{len(sales_lines)}:{sales_total_ex:.2f}:{sales_total_inc:.2f}"
        ).upper()
        if get_sales_log_marker(state, event_key) == marker:
            return state

        # Persist backlog *before* remote write so unexpected restarts/OOM
        # still replay this sales payload on next cycle.
        _upsert_sales_backlog_row()

        try:
            ensure_header(
                admin_creds,
                spreadsheet_id=spreadsheet_id,
                sheet_name=sheet_name,
                stats_fields=effective_sales_stats_fields,
            )
            for row in payload_rows:
                append_stats_row(
                    admin_creds,
                    spreadsheet_id=spreadsheet_id,
                    sheet_name=sheet_name,
                    event_key=row["event_key"],
                    stats_fields=effective_sales_stats_fields,
                    payload=row["payload"],
                    event_id_display=str(row.get("event_id_display") or event_id_display),
                    dedupe_signature={
                        "Event ID": str(row.get("event_id_display") or event_id_display),
                    },
                )
            print(f"Sales row appended for {event_key}: {len(sales_lines)} item(s)")
            _feed.push(
                f"Sales logged ({len(sales_lines)} item(s)) for \"{event.get('summary', event_key)}\"",
                "success",
            )
            backlog = get_sales_backlog(config.admin_db_file)
            backlog = [r for r in backlog if r.get("event_key") != event_key]
            set_sales_backlog(config.admin_db_file, backlog)
            return set_sales_log_marker(state, event_key, marker)
        except Exception as exc:
            print(f"Sales sheet append failed for {event_key}: {exc}")
            _upsert_sales_backlog_row()
            return state

    def _append_cash_row_or_backlog(
        *,
        event: dict,
        event_key: str,
        invoice_id: str,
        submitter_email: str,
        submitter_display: str,
        admin_creds,
        stats_fields: list[str],
        cash_sheet_target: dict[str, str],
        state: dict,
    ) -> dict:
        """
        Route CASH payments to calendar-specific sheets.
        If the calendar has no sheet mapping yet, store in backlog for replay.
        """
        if not admin_creds:
            print(f"Cash row deferred for {event_key}: Google credentials unavailable")
            return state

        def _normalize_cash_stats_fields(raw_fields: list[str]) -> list[str]:
            normalized: list[str] = []
            for field in raw_fields:
                key = str(field).strip()
                if not key or key in {"paid_status", "job_cost_ex_vat", "job_cost_inc_vat"}:
                    continue
                if key not in normalized:
                    normalized.append(key)
            if "job_cost" not in normalized:
                normalized.append("job_cost")
            return normalized

        cash_stats_fields = _normalize_cash_stats_fields(stats_fields or DEFAULT_STATS_FIELDS)

        calendar_id = (event.get("_calendar_id") or config.google_calendar_id or "").strip()
        cal_mapping = get_calendar_cash_sheets(config.admin_db_file)
        route = cal_mapping.get(calendar_id) or {}
        spreadsheet_id = str(route.get("spreadsheet_id", "")).strip()
        sheet_name = str(route.get("sheet_name", "Sheet1")).strip() or "Sheet1"

        invoice = {}
        if xero_client and invoice_id:
            try:
                invoice = xero_client.get_invoice(invoice_id)
            except Exception as exc:
                print(f"Cash sheet: failed to read invoice {invoice_id}: {exc}")
        subtotal = invoice.get("SubTotal")
        total = invoice.get("Total")
        if subtotal in (None, "") or total in (None, ""):
            fallback_lines = extract_invoice_lines(event.get("description"))
            if fallback_lines:
                fb_subtotal, fb_total = compute_invoice_totals(fallback_lines)
                if subtotal in (None, ""):
                    subtotal = fb_subtotal
                if total in (None, ""):
                    total = fb_total
        cash_marker_mode = invoice_has_cash_marker(event.get("description"))

        def _as_float(value):
            try:
                return float(value)
            except Exception:
                return None

        subtotal_f = _as_float(subtotal)
        total_f = _as_float(total)
        if cash_marker_mode:
            # Explicit *cash* marker means treat invoice amounts as no-VAT.
            cash_amount = subtotal_f if subtotal_f is not None else total_f
        else:
            # Normal cash flow: if VAT exists, use the higher (inc VAT) value.
            cash_amount = total_f if total_f is not None else subtotal_f

        start = (event.get("start", {}) or {}).get("dateTime") or (event.get("start", {}) or {}).get("date") or ""
        end = (event.get("end", {}) or {}).get("dateTime") or (event.get("end", {}) or {}).get("date") or ""
        def _fmt_british(iso_str: str) -> str:
            if not iso_str:
                return ""
            try:
                if "T" in iso_str:
                    obj = dt.datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
                    if obj.tzinfo is None:
                        obj = obj.replace(tzinfo=dt.timezone.utc)
                    return obj.astimezone(LONDON_TZ).strftime("%d/%m/%Y %H:%M")
                obj = dt.date.fromisoformat(iso_str)
                return obj.strftime("%d/%m/%Y")
            except Exception:
                return iso_str

        start_fmt = _fmt_british(start)
        end_fmt = _fmt_british(end)
        slot_text = f"{start_fmt} – {end_fmt}".strip(" –") if start_fmt != end_fmt else start_fmt
        customer_fields = parse_customer_fields(event.get("description"))
        payload = {
            "diary_entry_name": _diary_entry_name(event),
            "submitter": submitter_display or submitter_email,
            "customer": customer_fields.get("name") or "",
            "invoice_number": invoice.get("InvoiceNumber") or "",
            "receipt_details": "",
            "slot_datetime": slot_text,
            "payment_datetime": dt.datetime.now(dt.timezone.utc).astimezone(LONDON_TZ).strftime("%d/%m/%Y %H:%M"),
            "payment_method": "CASH",
            "job_cost": f"{cash_amount:.2f}" if cash_amount is not None else "",
        }
        event_id_raw = event.get("id") or ""
        date_part = start.split("T", 1)[0].replace("-", "") if start else ""
        suffix = event_id_raw[-4:] if event_id_raw else "0000"
        event_id_display = f"GC-{date_part}-{suffix}" if date_part else (event_id_raw or event_key)

        if not spreadsheet_id:
            backlog = get_cash_backlog(config.admin_db_file)
            row = {
                "event_key": event_key,
                "calendar_id": calendar_id,
                "submitter_email": submitter_email.lower(),
                "stats_fields": cash_stats_fields,
                "payload": payload,
                "event_id_display": event_id_display,
                "invoice_id": invoice_id,
            }
            replaced = False
            for idx, existing in enumerate(backlog):
                if existing.get("event_key") == event_key:
                    backlog[idx] = row
                    replaced = True
                    break
            if not replaced:
                backlog.append(row)
            set_cash_backlog(config.admin_db_file, backlog)
            print(
                f"Cash row queued for {event_key}: no cash sheet mapped for calendar {calendar_id}",
                flush=True,
            )
            return state

        marker = f"{invoice_id}:cash:{spreadsheet_id}:{sheet_name}".upper()
        if get_cash_log_marker(state, event_key) == marker:
            # still attempt global cash sheet routing below
            pass

        if get_cash_log_marker(state, event_key) != marker:
            try:
                ensure_header(
                    admin_creds,
                    spreadsheet_id=spreadsheet_id,
                    sheet_name=sheet_name,
                    stats_fields=cash_stats_fields,
                )
                append_stats_row(
                    admin_creds,
                    spreadsheet_id=spreadsheet_id,
                    sheet_name=sheet_name,
                    event_key=event_key,
                    stats_fields=cash_stats_fields,
                    payload=payload,
                    event_id_display=event_id_display,
                )
                print(f"Cash sheet row appended for {event_key} -> calendar {calendar_id}", flush=True)
                _feed.push(f"Cash row logged for {submitter_display or submitter_email}", "success")
                state = set_cash_log_marker(state, event_key, marker)
            except Exception as exc:
                print(f"Cash sheet append failed for {event_key}: {exc}", flush=True)
                backlog = get_cash_backlog(config.admin_db_file)
                row = {
                    "event_key": event_key,
                    "calendar_id": calendar_id,
                    "submitter_email": submitter_email.lower(),
                    "stats_fields": cash_stats_fields,
                    "payload": payload,
                    "event_id_display": event_id_display,
                    "invoice_id": invoice_id,
                }
                replaced = False
                for idx, existing in enumerate(backlog):
                    if existing.get("event_key") == event_key:
                        backlog[idx] = row
                        replaced = True
                        break
                if not replaced:
                    backlog.append(row)
                set_cash_backlog(config.admin_db_file, backlog)
                return state

        # Optional global cash sheet (all cash payments irrespective of calendar/person)
        global_spreadsheet_id = (cash_sheet_target.get("spreadsheet_id") or "").strip()
        global_sheet_name = (cash_sheet_target.get("sheet_name") or "Cash").strip() or "Cash"
        if global_spreadsheet_id:
            global_marker = f"{invoice_id}:cash_global:{global_spreadsheet_id}:{global_sheet_name}".upper()
            if get_cash_global_log_marker(state, event_key) != global_marker:
                try:
                    ensure_header(
                        admin_creds,
                        spreadsheet_id=global_spreadsheet_id,
                        sheet_name=global_sheet_name,
                        stats_fields=cash_stats_fields,
                    )
                    append_stats_row(
                        admin_creds,
                        spreadsheet_id=global_spreadsheet_id,
                        sheet_name=global_sheet_name,
                        event_key=f"{event_key}:cash-global",
                        stats_fields=cash_stats_fields,
                        payload=payload,
                        event_id_display=event_id_display,
                    )
                    state = set_cash_global_log_marker(state, event_key, global_marker)
                except Exception as exc:
                    print(f"Global cash sheet append failed for {event_key}: {exc}", flush=True)

        return state

    def _flush_cash_backlog(admin_creds) -> None:
        if not admin_creds:
            return
        backlog = get_cash_backlog(config.admin_db_file)
        if not backlog:
            return
        cal_mapping = get_calendar_cash_sheets(config.admin_db_file)
        remaining: list[dict] = []
        for row in backlog:
            calendar_id = str(row.get("calendar_id", "")).strip()
            route = cal_mapping.get(calendar_id) or {}
            spreadsheet_id = str(route.get("spreadsheet_id", "")).strip()
            sheet_name = str(route.get("sheet_name", "Sheet1")).strip() or "Sheet1"
            if not spreadsheet_id:
                remaining.append(row)
                continue
            stats = row.get("stats_fields") or []
            normalized_stats: list[str] = []
            for field in stats:
                key = str(field).strip()
                if not key or key in {"paid_status", "job_cost_ex_vat", "job_cost_inc_vat"}:
                    continue
                if key not in normalized_stats:
                    normalized_stats.append(key)
            if "job_cost" not in normalized_stats:
                normalized_stats.append("job_cost")
            payload = row.get("payload") or {}
            if isinstance(payload, dict):
                if "job_cost" not in payload:
                    old = payload.get("job_cost_inc_vat")
                    if old in (None, ""):
                        old = payload.get("job_cost_ex_vat", "")
                    payload["job_cost"] = old
            event_key = str(row.get("event_key", "")).strip()
            event_id_display = str(row.get("event_id_display", "")).strip()
            if not event_key or not isinstance(payload, dict):
                continue
            try:
                ensure_header(
                    admin_creds,
                    spreadsheet_id=spreadsheet_id,
                    sheet_name=sheet_name,
                    stats_fields=normalized_stats,
                )
                append_stats_row(
                    admin_creds,
                    spreadsheet_id=spreadsheet_id,
                    sheet_name=sheet_name,
                    event_key=event_key,
                    stats_fields=normalized_stats,
                    payload=payload,
                    event_id_display=event_id_display,
                )
                print(
                    f"Cash backlog flushed for calendar {calendar_id}: {event_key}",
                    flush=True,
                )
            except Exception as exc:
                print(
                    f"Cash backlog flush failed for calendar {calendar_id}: {exc}",
                    flush=True,
                )
                remaining.append(row)
        if len(remaining) != len(backlog):
            set_cash_backlog(config.admin_db_file, remaining)

    def _flush_sales_backlog(admin_creds) -> None:
        if not admin_creds:
            return
        backlog = get_sales_backlog(config.admin_db_file)
        if not backlog:
            return
        cal_mapping = get_calendar_sales_sheets(config.admin_db_file)
        sales_target = get_sales_sheet_target(config.admin_db_file)
        remaining: list[dict] = []
        for row in backlog:
            calendar_id = str(row.get("calendar_id", "")).strip()
            route = cal_mapping.get(calendar_id) or {}
            spreadsheet_id = str(route.get("spreadsheet_id", "")).strip()
            sheet_name = str(route.get("sheet_name", "Sales")).strip() or "Sales"
            if not spreadsheet_id:
                spreadsheet_id = sales_target.get("spreadsheet_id", "").strip()
                sheet_name = sales_target.get("sheet_name", "Sales").strip() or "Sales"
            if not spreadsheet_id:
                remaining.append(row)
                continue

            stats = row.get("stats_fields") or []
            rows = row.get("rows") or []
            event_id_display = str(row.get("event_id_display", "")).strip()
            if not isinstance(rows, list) or not rows:
                continue
            try:
                ensure_header(
                    admin_creds,
                    spreadsheet_id=spreadsheet_id,
                    sheet_name=sheet_name,
                    stats_fields=[str(s) for s in stats] or DEFAULT_SALES_STATS_FIELDS,
                )
                for item in rows:
                    if not isinstance(item, dict):
                        continue
                    event_key_row = str(item.get("event_key", "")).strip()
                    payload = item.get("payload") or {}
                    row_event_id_display = str(item.get("event_id_display") or event_id_display)
                    if not event_key_row or not isinstance(payload, dict):
                        continue
                    append_stats_row(
                        admin_creds,
                        spreadsheet_id=spreadsheet_id,
                        sheet_name=sheet_name,
                        event_key=event_key_row,
                        stats_fields=[str(s) for s in stats] or DEFAULT_SALES_STATS_FIELDS,
                        payload=payload,
                        event_id_display=row_event_id_display,
                        dedupe_signature={"Event ID": row_event_id_display},
                    )
                print(
                    f"Sales backlog flushed for calendar {calendar_id}: {row.get('event_key', '')}",
                    flush=True,
                )
            except Exception as exc:
                print(
                    f"Sales backlog flush failed for calendar {calendar_id}: {exc}",
                    flush=True,
                )
                remaining.append(row)

        if len(remaining) != len(backlog):
            set_sales_backlog(config.admin_db_file, remaining)

    _was_enabled = True

    def _webhook_base_url() -> str:
        """
        Derive the public base URL used for webhook registration.
        Prefer OAuth redirect URIs so this works in Fly without request context.
        """
        explicit = os.getenv("APP_BASE_URL", "").strip().rstrip("/")
        if explicit:
            return explicit

        stored = str(get_json_setting(config.admin_db_file, "public_base_url", "") or "").strip().rstrip("/")
        if stored and stored.startswith("http"):
            return stored

        fly_app = os.getenv("FLY_APP_NAME", "").strip() or os.getenv("FLY_APP", "").strip()
        if fly_app:
            return f"https://{fly_app}.fly.dev"

        candidates = [
            (config.google_oauth_redirect_uri or "").strip(),
            (config.xero_redirect_uri or "").strip(),
        ]
        for uri in candidates:
            if not uri:
                continue
            u = uri.strip().rstrip("/")
            if u.startswith("http://localhost") or u.startswith("http://127.0.0.1"):
                continue
            if u.endswith("/oauth/callback"):
                return u[: -len("/oauth/callback")]
            if u.endswith("/xero/callback"):
                return u[: -len("/xero/callback")]
        return ""

    # Safety overlap to avoid missing edits that land between poll cycles
    # (or between a fetch and last_sync update). State markers de-duplicate.
    _poll_overlap_seconds = int(os.getenv("POLL_OVERLAP_SECONDS", "1200") or "1200")
    _poll_overlap = dt.timedelta(seconds=max(_poll_overlap_seconds, 0))

    def _is_xero_429(exc: Exception) -> bool:
        text = str(exc or "")
        return "429" in text or "Too Many Requests" in text

    def _xero_retry_after_hint_seconds(exc: Exception) -> int | None:
        import re

        text = str(exc or "")
        m = re.search(r"Retry-After=(\d+)", text, flags=re.I)
        if not m:
            return None
        try:
            return max(60, int(m.group(1)))
        except Exception:
            return None

    def _event_end_utc(event: dict) -> dt.datetime | None:
        end_obj = (event.get("end") or {}) if isinstance(event, dict) else {}
        raw_dt = str(end_obj.get("dateTime") or "").strip()
        if raw_dt:
            try:
                out = dt.datetime.fromisoformat(raw_dt.replace("Z", "+00:00"))
                if out.tzinfo is None:
                    out = out.replace(tzinfo=dt.timezone.utc)
                return out.astimezone(dt.timezone.utc)
            except Exception:
                return None
        raw_date = str(end_obj.get("date") or "").strip()
        if raw_date:
            try:
                d = dt.date.fromisoformat(raw_date)
                # all-day end date is exclusive in Google Calendar;
                # treat it as end-of-previous-day local time for "past" checks.
                local = dt.datetime.combine(
                    d, dt.time.min, tzinfo=LONDON_TZ
                ) - dt.timedelta(seconds=1)
                return local.astimezone(dt.timezone.utc)
            except Exception:
                return None
        return None

    def _clear_draft_cleanup_warning(description: str) -> str:
        lines = (description or "").splitlines()
        kept = [
            ln
            for ln in lines
            if "draft cleanup pending" not in ln.lower()
        ]
        return "\n".join(kept)

    def _draft_sync_fingerprint(
        *,
        invoice_lines: list[dict],
        contact_id: str,
        payment_mode: str,
        force_no_vat: bool,
    ) -> str:
        payload = {
            "contact_id": str(contact_id or ""),
            "payment_mode": str(payment_mode or "").lower(),
            "force_no_vat": bool(force_no_vat),
            "invoice_lines": [
                {
                    "Description": str((line or {}).get("Description") or "").strip(),
                    "Quantity": float((line or {}).get("Quantity") or 0),
                    "UnitAmount": float((line or {}).get("UnitAmount") or 0),
                    "TaxType": str((line or {}).get("TaxType") or "").strip().upper(),
                }
                for line in (invoice_lines or [])
            ],
        }
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    while True:
        now = dt.datetime.now(dt.timezone.utc)
        _now_ts_for_xero = time.time()
        _persisted_lock_until = float(state.get("xero_lockout_until_ts") or 0.0)
        _global_lock_until = float(get_xero_rate_limit_until_ts() or 0.0)
        _effective_lock_until = max(_xero_retry_after, _persisted_lock_until, _global_lock_until)
        if _effective_lock_until > _now_ts_for_xero:
            _xero_retry_after = _effective_lock_until
            state["xero_lockout_until_ts"] = _effective_lock_until
            state["xero_lockout_reason"] = "Xero API day/minute rate limit (429)"
            state["xero_lockout_updated_at_ts"] = _now_ts_for_xero
            if xero_client is not None:
                xero_client = None
            _last_probe = float(state.get("xero_lockout_last_probe_ts") or 0.0)
            if (_now_ts_for_xero - _last_probe) >= _XERO_LOCK_PROBE_SECONDS:
                state["xero_lockout_last_probe_ts"] = _now_ts_for_xero
                try:
                    _probe_client = _build_xero_client_safe()
                    if _probe_client:
                        _probe_client.get_organisation()
                        state["xero_lockout_until_ts"] = 0.0
                        state["xero_lockout_reason"] = ""
                        state["xero_lockout_updated_at_ts"] = _now_ts_for_xero
                        _xero_retry_after = 0.0
                        xero_client = _probe_client
                        _xero_built_at = _now_ts_for_xero
                        _feed.push("Xero lockout cleared — processing resumed", "success")
                except Exception:
                    pass
            if (_now_ts_for_xero - _xero_lock_notice_ts) >= 300:
                _mins = int(max(1, (_effective_lock_until - _now_ts_for_xero) // 60))
                _feed.push(
                    f"Xero lockout active — queued mode ({_mins}m remaining)",
                    "warn",
                )
                _xero_lock_notice_ts = _now_ts_for_xero
            save_state(config.state_file, state)
        elif _persisted_lock_until:
            state["xero_lockout_until_ts"] = 0.0
            state["xero_lockout_reason"] = ""
            state["xero_lockout_updated_at_ts"] = _now_ts_for_xero

        # Rebuild Xero client only when the cached one is stale or missing.
        if (xero_client is None and _now_ts_for_xero >= _xero_retry_after) or (
            xero_client is not None and (_now_ts_for_xero - _xero_built_at) > _XERO_REBUILD_INTERVAL
        ):
            xero_client = _build_xero_client_safe()
            _xero_built_at = _now_ts_for_xero
            if xero_client is None:
                # Avoid hammering Xero token endpoint every poll cycle.
                _xero_retry_after = _now_ts_for_xero + 120
            else:
                _xero_retry_after = 0.0
        # Lightweight health check to detect lockouts even before staff submit anything.
        if (
            xero_client is not None
            and _xero_retry_after <= _now_ts_for_xero
            and (_now_ts_for_xero - _last_xero_health_check_ts) >= _XERO_HEALTH_CHECK_SECONDS
        ):
            _last_xero_health_check_ts = _now_ts_for_xero
            try:
                xero_client.get_organisation()
            except Exception as exc:
                if _is_xero_429(exc):
                    _retry_hint_seconds = _xero_retry_after_hint_seconds(exc)
                    _global_lock_until = float(get_xero_rate_limit_until_ts() or 0.0)
                    _hint_lock_until = (
                        (time.time() + _retry_hint_seconds)
                        if _retry_hint_seconds
                        else (time.time() + _XERO_429_COOLDOWN_SECONDS)
                    )
                    _xero_retry_after = max(_xero_retry_after, _global_lock_until, _hint_lock_until)
                    state["xero_lockout_until_ts"] = _xero_retry_after
                    state["xero_lockout_reason"] = "Xero API rate limit (429)"
                    state["xero_lockout_updated_at_ts"] = time.time()
                    xero_client = None
                    _mins = int(max(1, (_xero_retry_after - time.time()) // 60))
                    _feed.push(
                        f"Xero lockout detected by health check — cooling down for ~{_mins}m.",
                        "warn",
                    )

        # Global on/off toggle
        _enabled = get_enabled(config.admin_db_file)
        if not _enabled:
            if _was_enabled:
                _feed.push("System paused — no events will be processed", "system")
            _was_enabled = False
            wait_for_poll(backoff_seconds)
            continue
        if not _was_enabled:
            _feed.push("System resumed — watching for events", "system")
        _was_enabled = True
        active_calendars = get_active_calendars(
            config.admin_db_file, config.google_calendar_id
        )
        _refresh_calendar_name_cache(active_calendars)

        _queued_calendar_targets = {
            c for c in consume_calendar_targets() if c in set(active_calendars)
        }
        _queued_event_targets = [e for e in consume_event_targets() if ":" in e]
        _target_event_ids_by_calendar: dict[str, set[str]] = {}
        for _key in _queued_event_targets:
            _cal_id, _ev_id = _key.split(":", 1)
            if _cal_id not in active_calendars or not _ev_id:
                continue
            _target_event_ids_by_calendar.setdefault(_cal_id, set()).add(_ev_id)
        _target_calendar_ids = set(_queued_calendar_targets) | set(
            _target_event_ids_by_calendar.keys()
        )
        _target_event_keys = {
            k
            for k in _queued_event_targets
            if (k.split(":", 1)[0] in set(active_calendars))
        }
        _now_ts = time.time()
        _is_targeted_cycle = bool(_target_calendar_ids)
        _is_hourly_reconcile_cycle = (_now_ts - _last_hourly_reconcile_ts) >= _HOURLY_RECONCILE_SECONDS
        if _is_hourly_reconcile_cycle:
            _last_hourly_reconcile_ts = _now_ts

        # Auto-manage Google Calendar watches — run at most once per hour,
        # or immediately when calendar settings change (triggered by the settings page).
        if consume_watch_check() or (_now_ts - _last_watch_check) >= _WATCH_CHECK_INTERVAL:
            _last_watch_check = _now_ts
            _run_watch_check = True
        else:
            _run_watch_check = False

        if _run_watch_check:
            try:
                watches = get_google_watches(config.admin_db_file)
                now_ms = int(now.timestamp() * 1000)
                renew_before_ms = 24 * 3600 * 1000
                base_url = _webhook_base_url().rstrip("/")
                webhook_url = f"{base_url}/webhooks/google-calendar" if base_url else ""

                # Remove watches for de-selected calendars.
                for cal_id, winfo in list(watches.items()):
                    if cal_id in active_calendars:
                        continue
                    try:
                        if winfo.get("channel_id") and winfo.get("resource_id"):
                            stop_calendar_watch(
                                config, winfo["channel_id"], winfo["resource_id"]
                            )
                    except Exception:
                        pass
                    delete_google_watch(config.admin_db_file, cal_id)
                    print(f"[watch] Removed watch for inactive calendar {cal_id}", flush=True)

                # Ensure active calendars always have a valid watch.
                for cal_id in active_calendars:
                    winfo = watches.get(cal_id) or {}
                    exp_ms = int(winfo.get("expiration_ms") or 0)
                    url_mismatch = bool(
                        webhook_url
                        and winfo.get("webhook_url")
                        and winfo.get("webhook_url") != webhook_url
                    )
                    needs_register = (
                        not winfo
                        or not winfo.get("channel_id")
                        or not winfo.get("resource_id")
                        or not exp_ms
                        or (exp_ms - now_ms) < renew_before_ms
                        or url_mismatch
                    )
                    if not needs_register:
                        continue
                    if not webhook_url:
                        print(
                            f"[watch] Skipping watch for {cal_id}: no public base URL configured",
                            flush=True,
                        )
                        continue

                    try:
                        if winfo.get("channel_id") and winfo.get("resource_id"):
                            stop_calendar_watch(
                                config, winfo["channel_id"], winfo["resource_id"]
                            )
                    except Exception:
                        pass
                    try:
                        resp = register_calendar_watch(config, cal_id, webhook_url)
                        set_google_watch(
                            config.admin_db_file,
                            cal_id,
                            resp["id"],
                            resp["resourceId"],
                            int(resp.get("expiration") or 0),
                            webhook_url=webhook_url,
                        )
                        print(
                            f"[watch] Registered/renewed Google Calendar watch for {cal_id}",
                            flush=True,
                        )
                    except Exception as exc:
                        print(
                            f"[watch] Failed to register/renew watch for {cal_id}: {exc}",
                            flush=True,
                        )
                        _feed.push(
                            f"Google webhook renewal failed for {cal_id}: {str(exc).splitlines()[0][:100]}",
                            "error",
                        )
            except Exception as exc:
                print(f"[watch] Auto-watch manager failed: {exc}", flush=True)
                _feed.push(f"Google webhook manager error: {str(exc).splitlines()[0][:100]}", "error")

        events: list[dict] = []
        seen_event_keys: set[str] = set()

        def _push_unique_event(calendar_id: str, event: dict) -> None:
            event_id = str((event or {}).get("id") or "").strip()
            if not event_id:
                return
            dedupe_key = f"{calendar_id}:{event_id}"
            if dedupe_key in seen_event_keys:
                return
            seen_event_keys.add(dedupe_key)
            event["_calendar_id"] = calendar_id
            events.append(event)

        calendar_fetch_failed = False
        # Intentionally overlap the updated_min window for reliability.
        # This prevents races where a calendar change happens right after a fetch
        # but before last_sync is advanced.
        query_updated_min = last_sync - _poll_overlap
        _service = None
        if _target_event_ids_by_calendar:
            try:
                _service = build_calendar_service(config)
            except Exception as exc:
                print(f"[poll] Failed to build calendar service for targeted events: {exc}", flush=True)
                _feed.push(
                    f"Targeted event read failed: {str(exc).splitlines()[0][:100]}",
                    "error",
                )

        # Directly read webhook-targeted event ids first, avoiding full-cycle scans.
        if _service:
            for _cal_id, _event_ids in _target_event_ids_by_calendar.items():
                for _event_id in sorted(_event_ids):
                    try:
                        _ev = _service.events().get(calendarId=_cal_id, eventId=_event_id).execute()
                        _push_unique_event(_cal_id, _ev)
                    except Exception as exc:
                        print(f"[poll] Targeted event read failed for {_cal_id}:{_event_id}: {exc}", flush=True)

        # Build scan windows:
        # - Targeted cycles: focused scan around touched calendars/events.
        # - Every cycle: lightweight recent-saves scan only (Google safety net).
        # - Hourly: add a past-only sweep for payment reconciliation candidates.
        _scan_windows: list[tuple[dt.datetime, dt.datetime]] = []
        if _is_targeted_cycle:
            _scan_windows.append(
                (
                    now - dt.timedelta(days=2),
                    now + dt.timedelta(days=30),
                )
            )
            calendars_to_scan = [c for c in active_calendars if c in _target_calendar_ids]
        else:
            calendars_to_scan = list(active_calendars)
            _start_of_today = now.astimezone(LONDON_TZ).replace(
                hour=0, minute=0, second=0, microsecond=0
            ).astimezone(dt.timezone.utc)
            _scan_windows.append(
                (
                    _start_of_today - dt.timedelta(hours=1),
                    _start_of_today + dt.timedelta(days=1, hours=1),
                )
            )
            if _is_hourly_reconcile_cycle:
                _scan_windows.append(
                    (
                        now - dt.timedelta(days=_HOURLY_RECONCILE_PAST_DAYS),
                        now + dt.timedelta(hours=1),
                    )
                )

        for calendar_id in calendars_to_scan:
            try:
                cal_events: list[dict] = []
                for _time_min, _time_max in _scan_windows:
                    cal_events.extend(
                        list_recent_events(
                            config=config,
                            updated_min=query_updated_min,
                            time_min=_time_min,
                            time_max=_time_max,
                            calendar_id=calendar_id,
                        )
                    )
            except Exception as exc:
                calendar_fetch_failed = True
                print(
                    f"[poll] Failed to read calendar {calendar_id}: {exc}",
                    flush=True,
                )
                _feed.push(
                    f"Calendar read failed ({calendar_id}): {str(exc).splitlines()[0][:100]}",
                    "error",
                )
                continue
            for e in cal_events:
                _push_unique_event(calendar_id, e)
        # Process newest changes first so current operations are not starved by backlog.
        events.sort(key=lambda e: str(e.get("updated") or ""), reverse=True)
        had_changes = bool(events)
        events_by_key = {
            f"{str(e.get('_calendar_id') or '')}:{str(e.get('id') or '')}": e
            for e in events
            if e.get("id") and e.get("_calendar_id")
        }

        # Retry deferred Xero draft cleanup for cash-completed entries.
        # Keep this low-frequency and low-volume (hourly, capped) to avoid extra load.
        if _is_hourly_reconcile_cycle and xero_client:
            _cleanup_queue = dict(state.get("draft_cleanup_queue", {}) or {})
            if _cleanup_queue:
                _cleanup_now = time.time()
                _cleanup_done = 0
                _cleanup_service = None
                for _ev_key, _row in sorted(
                    _cleanup_queue.items(),
                    key=lambda kv: float(((kv[1] or {}).get("next_retry_at") or 0.0)),
                ):
                    if _cleanup_done >= _DRAFT_CLEANUP_PER_HOURLY:
                        break
                    _row = dict(_row or {})
                    _inv_id = str(_row.get("invoice_id") or "").strip()
                    if not _inv_id:
                        _cleanup_queue.pop(_ev_key, None)
                        continue
                    _next_retry_at = float(_row.get("next_retry_at") or 0.0)
                    if _next_retry_at and _next_retry_at > _cleanup_now:
                        continue
                    try:
                        xero_client.delete_draft_invoice(_inv_id)
                        _cleanup_queue.pop(_ev_key, None)
                        if ":" in _ev_key:
                            _cal_id, _eid = _ev_key.split(":", 1)
                            _ev = events_by_key.get(_ev_key)
                            if _ev is None:
                                try:
                                    if _cleanup_service is None:
                                        _cleanup_service = build_calendar_service(config)
                                    _ev = (
                                        _cleanup_service.events()
                                        .get(calendarId=_cal_id, eventId=_eid)
                                        .execute()
                                    )
                                except Exception:
                                    _ev = None
                            if _ev:
                                _desc = str(_ev.get("description") or "")
                                _new_desc = _clear_draft_cleanup_warning(_desc)
                                if _new_desc != _desc:
                                    _upd = safe_update(
                                        event_id=_eid,
                                        description=_new_desc,
                                        label="Draft cleanup complete",
                                        summary_status="green",
                                        current_summary=_ev.get("summary"),
                                        calendar_id=_cal_id,
                                    )
                                    if _upd:
                                        _ev["description"] = _new_desc
                                        _ev["updated"] = _upd.get("updated", _ev.get("updated"))
                        _feed.push(
                            f"Draft cleanup completed for {_inv_id[:8]}…",
                            "success",
                        )
                    except Exception as _exc:
                        _prev_backoff = int(_row.get("backoff_seconds") or 0)
                        _next_backoff = max(
                            _DRAFT_CLEANUP_MIN_BACKOFF,
                            (_prev_backoff * 2) if _prev_backoff else _DRAFT_CLEANUP_MIN_BACKOFF,
                        )
                        _next_backoff = min(_next_backoff, _DRAFT_CLEANUP_MAX_BACKOFF)
                        _cleanup_queue[_ev_key] = {
                            "invoice_id": _inv_id,
                            "next_retry_at": _cleanup_now + _next_backoff,
                            "backoff_seconds": _next_backoff,
                            "last_error": str(_exc).splitlines()[0][:220],
                        }
                    _cleanup_done += 1
                state["draft_cleanup_queue"] = _cleanup_queue

        admin_creds = load_admin_credentials(config)
        sheet_target = get_sheet_target(config.admin_db_file)
        stats_fields = get_stats_fields(config.admin_db_file)
        sales_sheet_target = get_sales_sheet_target(config.admin_db_file)
        cash_sheet_target = get_cash_sheet_target(config.admin_db_file)
        sales_stats_fields = get_sales_stats_fields(config.admin_db_file)
        submitter_aliases = get_submitter_aliases(config.admin_db_file)
        sheet_enabled = bool(
            admin_creds
            and sheet_target.get("spreadsheet_id")
            and sheet_target.get("sheet_name")
            and stats_fields
        )
        sales_sheet_enabled = bool(
            admin_creds
            and sales_sheet_target.get("spreadsheet_id")
            and sales_sheet_target.get("sheet_name")
            and sales_stats_fields
        )
        # ensure_header is cached: only call it when the sheet target changes.
        _sheet_key = f"{sheet_target.get('spreadsheet_id')}:{sheet_target.get('sheet_name')}"
        if sheet_enabled and admin_creds and _sheet_key not in _headers_initialized:
            try:
                ensure_header(
                    admin_creds,
                    spreadsheet_id=sheet_target["spreadsheet_id"],
                    sheet_name=sheet_target["sheet_name"],
                    stats_fields=stats_fields,
                )
                _headers_initialized.add(_sheet_key)
            except Exception as exc:
                print(f"Sheets header setup failed: {exc}")
                sheet_enabled = False

        _sales_sheet_key = f"{sales_sheet_target.get('spreadsheet_id')}:{sales_sheet_target.get('sheet_name')}"
        if sales_sheet_enabled and admin_creds and _sales_sheet_key not in _headers_initialized:
            try:
                ensure_header(
                    admin_creds,
                    spreadsheet_id=sales_sheet_target["spreadsheet_id"],
                    sheet_name=sales_sheet_target["sheet_name"],
                    stats_fields=sales_stats_fields,
                )
                _headers_initialized.add(_sales_sheet_key)
            except Exception as exc:
                print(f"Sales sheet header setup failed: {exc}")
                sales_sheet_enabled = False

        # Integration health used to mark titles red when processing is blocked.
        integration_issues: list[str] = []
        if not xero_client:
            integration_issues.append("xero_disconnected")
        if not admin_creds:
            integration_issues.append("google_disconnected")
        if calendar_fetch_failed:
            integration_issues.append("calendar_read_failed")
        if sheet_target.get("spreadsheet_id", "").strip() and not sheet_enabled:
            integration_issues.append("master_sheet_disconnected")
        if sales_sheet_target.get("spreadsheet_id", "").strip() and not sales_sheet_enabled:
            integration_issues.append("sales_sheet_disconnected")
        if active_calendars:
            watches = get_google_watches(config.admin_db_file)
            now_ms = int(now.timestamp() * 1000)
            base_url = _webhook_base_url().rstrip("/")
            expected_webhook_url = f"{base_url}/webhooks/google-calendar" if base_url else ""
            if not expected_webhook_url:
                integration_issues.append("webhook_base_url_missing")
            else:
                bad_watch = False
                for cal_id in active_calendars:
                    winfo = watches.get(cal_id) or {}
                    exp_ms = int(winfo.get("expiration_ms") or 0)
                    if (
                        not winfo.get("channel_id")
                        or not winfo.get("resource_id")
                        or exp_ms <= now_ms
                    ):
                        bad_watch = True
                        break
                    saved_url = str(winfo.get("webhook_url") or "").strip()
                    if saved_url and saved_url != expected_webhook_url:
                        bad_watch = True
                        break
                if bad_watch:
                    integration_issues.append("google_webhook_not_attached")

        state = _flush_sheet_backlog(admin_creds, sheet_target, state)
        _flush_cash_backlog(admin_creds)
        _flush_sales_backlog(admin_creds)

        _xero_events_used = 0
        for event in events:
            try:
                event_id = event.get("id") or ""
                calendar_id = event.get("_calendar_id") or config.google_calendar_id
                event_key = f"{calendar_id}:{event_id}"
                _event_retry_after = float((state.get("event_xero_retry_after", {}) or {}).get(event_key) or 0.0)
                if _event_retry_after and time.time() < _event_retry_after:
                    _desc = event.get("description") or ""
                    if done_choice_is_yes(_desc) or send_choice_is_yes(_desc):
                        continue
                # If Xero is in cooldown after a 429 burst, avoid touching actionable
                # invoice events until the cooldown expires.
                if xero_client is None and time.time() < _xero_retry_after:
                    _desc = event.get("description") or ""
                    if done_choice_is_yes(_desc) or send_choice_is_yes(_desc):
                        continue
                submitter_email = (
                    (event.get("creator", {}) or {}).get("email")
                    or (event.get("organizer", {}) or {}).get("email")
                    or ""
                ).strip()
                if submitter_email:
                    _existing_submitters = set(get_seen_submitters(config.admin_db_file))
                    _is_new_contact = submitter_email.lower() not in _existing_submitters
                    add_seen_submitter(config.admin_db_file, submitter_email)
                    if _is_new_contact:
                        _display_preview = submitter_aliases.get(submitter_email.lower(), submitter_email)
                        _feed.push(f"New contact: {_display_preview}", "event")
                submitter_display = submitter_aliases.get(
                    submitter_email.lower(), submitter_email
                )
                submitted_at_display = _format_display_datetime(event.get("updated"))
                if (event.get("status") or "").lower() == "cancelled":
                    continue
                created_raw = event.get("created")
                updated_raw = event.get("updated")
                created_at = None
                updated_at = None
                if created_raw:
                    try:
                        created_at = dt.datetime.fromisoformat(
                            created_raw.replace("Z", "+00:00")
                        )
                    except ValueError:
                        created_at = None
                if updated_raw:
                    try:
                        updated_at = dt.datetime.fromisoformat(
                            updated_raw.replace("Z", "+00:00")
                        )
                    except ValueError:
                        updated_at = None

                # Only touch events created after this run started.
                # If created time is missing, fall back to updated time.
                should_prefill = False
                if created_at and created_at >= run_started_at:
                    should_prefill = True
                elif not created_at and updated_at and updated_at >= run_started_at:
                    should_prefill = True

                if should_prefill:
                    if not event.get("id") or is_prefilled(state, event_key):
                        pass
                    else:
                        new_description = ensure_notes_template(event.get("description"))
                        if new_description != (event.get("description") or ""):
                            updated = safe_update(
                                event_id=event.get("id"),
                                description=new_description,
                                label="Prefill",
                                summary_status="blue",
                                current_summary=event.get("summary"),
                                calendar_id=calendar_id,
                            )
                            if updated:
                                print(f"Prefilled notes for new event {updated.get('id')}")
                                event["description"] = updated.get("description", new_description)
                                event["updated"] = updated.get("updated", event.get("updated"))
                        # Remember the prefill update marker so we can detect first
                        # user edit and flip blue -> orange.
                        state = set_processed_update_marker(
                            state,
                            event_key,
                            event.get("updated") or "",
                        )
                        state = mark_prefilled(state, event_key)

                # Process any event with DONE/SEND, regardless of when it was created.
                has_done = done_choice_is_yes(event.get("description"))
                # Only send when user explicitly answers Y/YES.
                has_send = send_choice_is_yes(event.get("description"))
                sent_state = is_invoice_sent(state, event_key)
                paid_state = is_invoice_paid(state, event_key)
                pay_mode_hint = payment_choice(event.get("description")) or ""
                _event_end = _event_end_utc(event)
                _event_is_past = bool(_event_end and _event_end <= now)
                _event_targeted = (
                    (event_key in _target_event_keys)
                    or (calendar_id in _target_calendar_ids)
                )
                _allow_paid_reconcile = bool(
                    _event_is_past and (_is_hourly_reconcile_cycle or _event_targeted)
                )
                _needs_xero_event_work = bool(
                    (
                        (has_done or has_send)
                        and not sent_state
                    )
                    or (
                        sent_state
                        and not paid_state
                        and pay_mode_hint in {"invoice", "card"}
                        and _allow_paid_reconcile
                    )
                )
                if _needs_xero_event_work:
                    if _xero_events_used >= _XERO_EVENTS_PER_CYCLE:
                        continue
                    _xero_events_used += 1
                # Keep title light resilient: if a move/edit flow overwrites summary
                # and removes/changes the status prefix, restamp the expected one.
                current_summary = (event.get("summary") or "").strip()
                if event.get("id") and _has_managed_sections(event.get("description")):
                    expected_status = _expected_title_status(
                        event.get("description"),
                        has_done=has_done,
                        has_send=has_send,
                        sent_state=sent_state,
                        paid_state=paid_state,
                        current_summary=current_summary,
                        event_key=event_key,
                    )
                    expected_emoji = {
                        "blue": "🔵",
                        "orange": "🟠",
                        "yellow": "🟡",
                        "green": "🟢",
                        "red": "🔴",
                    }.get(expected_status)
                    current_emoji = _extract_title_status(current_summary)
                    if expected_emoji and current_emoji != expected_emoji:
                        updated = safe_update(
                            event_id=event.get("id"),
                            description=event.get("description") or "",
                            label="Title light restamp",
                            summary_status=expected_status,
                            current_summary=event.get("summary"),
                            calendar_id=calendar_id,
                        )
                        if updated:
                            event["summary"] = updated.get("summary", event.get("summary"))
                            event["updated"] = updated.get("updated", event.get("updated"))
                            current_summary = (event.get("summary") or "").strip()
                # Finalised entries (green title) are normally immutable.
                # Exception: if staff explicitly requests SEND NOW on a green+mail-failed
                # entry, allow a safe email-only retry without changing invoice/payment data.
                if current_summary.startswith("🟢"):
                    _desc_now = event.get("description") or ""
                    _mail_retry_requested = bool(
                        has_send and ("invoice send failed" in _desc_now.lower())
                    )
                    _mail_retry_invoice_id = get_invoice_for_event(state, event_key) or ""
                    if _mail_retry_requested and _mail_retry_invoice_id and xero_client:
                        try:
                            _emailed = xero_client.email_invoice(_mail_retry_invoice_id)
                            if _emailed:
                                _invoice_url = xero_client.get_online_invoice_url(
                                    _mail_retry_invoice_id
                                )
                                _updated_desc = upsert_send_confirmation(
                                    _desc_now,
                                    invoice_url=_invoice_url,
                                    submitter=submitter_display,
                                    submitted_at=submitted_at_display,
                                )
                                _updated_event = safe_update(
                                    event_id=event.get("id"),
                                    description=_updated_desc,
                                    label="Invoice email retry",
                                    summary_status="green",
                                    current_summary=event.get("summary"),
                                    calendar_id=calendar_id,
                                )
                                if _updated_event:
                                    event["description"] = _updated_desc
                                    event["updated"] = _updated_event.get(
                                        "updated", event.get("updated")
                                    )
                                state = mark_invoice_sent(state, event_key)
                                state = mark_invoice_paid(state, event_key)
                                _feed.push(
                                    f"Invoice email sent on retry for \"{event.get('summary', event_id)}\"",
                                    "success",
                                )
                            else:
                                _feed.push(
                                    f"Invoice email retry failed for \"{event.get('summary', event_id)}\"",
                                    "warn",
                                )
                        except Exception as _exc:
                            print(
                                f"Event {event_id}: email-only retry failed: {_exc}",
                                flush=True,
                            )
                            _feed.push(
                                f"Invoice email retry failed for \"{event.get('summary', event_id)}\": {str(_exc).splitlines()[0][:100]}",
                                "error",
                            )
                    state = set_processed_update_marker(
                        state,
                        event_key,
                        event.get("updated") or "",
                    )
                    continue
                # If integrations are down, mark actionable entries red so staff can
                # immediately see they need intervention before retrying.
                if integration_issues and (has_done or has_send):
                    if not current_summary.startswith("🔴"):
                        updated = safe_update(
                            event_id=event.get("id"),
                            description=event.get("description") or "",
                            label="Integration issue",
                            summary_status="red",
                            current_summary=event.get("summary"),
                            calendar_id=calendar_id,
                        )
                        if updated:
                            event["updated"] = updated.get("updated", event.get("updated"))
                    print(
                        f"Event {event_id}: blocked due to integration issues: {', '.join(integration_issues)}",
                        flush=True,
                    )
                    _feed.push(
                        f"Blocked \"{event.get('summary', event_id)}\": {', '.join(integration_issues)}",
                        "error",
                    )
                    continue
                # Keep blue until staff explicitly confirms PROCESS DRAFT = Y.
                if (has_done or has_send) and event.get("id"):
                    event_updated = event.get("updated") or ""
                    normalized_description = normalize_user_sections(
                        event.get("description") or ""
                    )
                    if normalized_description != (event.get("description") or ""):
                        updated = safe_update(
                            event_id=event.get("id"),
                            description=normalized_description,
                            label="Normalize notes",
                            calendar_id=calendar_id,
                        )
                        if updated:
                            event["description"] = normalized_description
                            event_updated = updated.get("updated") or event_updated
                        else:
                            event["description"] = normalized_description
                    invoice_lines = extract_invoice_lines(event.get("description"))
                    existing_invoice_id = get_invoice_for_event(state, event_key)
                    last_processed_update = get_processed_update_marker(state, event_key)
                    # If DONE + invoice lines exist but we still have no invoice_id,
                    # keep retrying until draft creation succeeds.
                    pending_draft = bool(has_done and invoice_lines and not existing_invoice_id)
                    # Skip reprocessing only when unchanged and nothing is pending.
                    # Normally skip unchanged events. Exception: sent INVOICE-mode
                    # events must still be rechecked so paid-state can be synced
                    # (master sheet Paid columns + invoice sales rows + green dot).
                    skip_unchanged = bool(
                        last_processed_update
                        and event_updated == last_processed_update
                        and not pending_draft
                    )
                    if skip_unchanged:
                        _sync_invoice_id = get_invoice_for_event(state, event_key)
                        _desc_now = event.get("description") or ""
                        _stale_payment_alert = (
                            "!!! payment type empty !!!!" in _desc_now.lower()
                        )
                        _has_sent_marker = "invoice sent ✅" in _desc_now.lower()
                        needs_invoice_paid_sync = bool(
                            has_done
                            and _allow_paid_reconcile
                            and xero_client
                            and _sync_invoice_id
                            and pay_mode_hint in {"invoice", "card"}
                            and not (event.get("summary") or "").strip().startswith("🟢")
                        )
                        # Cleanup stale payment-type alert when invoice was already sent
                        # (or paid) and no resend action is pending.
                        if _stale_payment_alert and (sent_state or paid_state or _has_sent_marker):
                            _invoice_url = ""
                            for _ln in _desc_now.splitlines():
                                if _ln.strip().lower().startswith("invoice link:"):
                                    _invoice_url = _ln.split(":", 1)[1].strip()
                                    break
                            if xero_client and _sync_invoice_id:
                                try:
                                    _live_url = xero_client.get_online_invoice_url(_sync_invoice_id)
                                    if _live_url:
                                        _invoice_url = _live_url
                                except Exception:
                                    pass
                            _cleaned = upsert_send_confirmation(
                                _desc_now,
                                invoice_url=_invoice_url or None,
                                submitter=submitter_display,
                                submitted_at=submitted_at_display,
                            )
                            _summary_target = "green" if paid_state else "yellow"
                            _upd = safe_update(
                                event_id=event.get("id"),
                                description=_cleaned,
                                label="Clear stale payment alert",
                                summary_status=_summary_target,
                                current_summary=event.get("summary"),
                                calendar_id=calendar_id,
                            )
                            if _upd:
                                event["description"] = _cleaned
                                event_updated = _upd.get("updated") or event_updated
                                state = set_processed_update_marker(state, event_key, event_updated)
                        if not needs_invoice_paid_sync:
                            continue
                        # Poll Xero for payment status on unchanged INVOICE-mode events.
                        _sync_inv_id = _sync_invoice_id
                        try:
                            _sync_inv = xero_client.get_invoice(_sync_inv_id)
                            if (_sync_inv.get("Status") or "").upper() == "PAID":
                                safe_update(
                                    event_id=event.get("id"),
                                    description=event.get("description") or "",
                                    label="Invoice paid",
                                    summary_status="green",
                                    current_summary=event.get("summary"),
                                    calendar_id=calendar_id,
                                )
                                _feed.push(
                                    f"Invoice paid — marked green: \"{event.get('summary', event_id)}\"",
                                    "success",
                                )
                                state = mark_invoice_sent(state, event_key)
                                state = mark_invoice_paid(state, event_key)
                        except Exception as _exc:
                            print(f"Event {event_id}: paid-sync check failed: {_exc}")
                        continue
                    if has_done and not invoice_lines:
                        print(f"Event {event_id}: no invoice lines found, skipping invoice")
                        _feed.push(f"No job details in \"{event.get('summary', event_id)}\" — awaiting line items", "warn")
                    if is_processed(state, event_key):
                        # If we have a stored contact, update it only when the event changed.
                        existing_contact_id = get_contact_for_event(state, event_key)
                        last_contact_update = get_contact_update_marker(
                            state, event_key
                        )
                        # If the event was processed before but has no contact recorded, retry contact creation.
                        if not existing_contact_id and xero_client and has_done:
                            customer = parse_customer_fields(event.get("description"))
                            overrides = parse_invoice_contact_overrides(event.get("description"))
                            profile_mode = bool((overrides.get("invoice_profile") or "").strip())
                            address = _invoice_address_from_overrides(
                                overrides,
                                parse_event_address(event.get("location")),
                            )
                            errors = validate_customer_fields(customer)
                            blocking_errors = {
                                k: v for k, v in errors.items() if k != "phone"
                            }
                            if profile_mode:
                                blocking_errors = {}
                            invoice_name_for_fp = (overrides.get("invoice_name") or customer.get("name") or "").strip()
                            if not blocking_errors and (customer.get("name") or profile_mode or invoice_name_for_fp):
                                contact, _resolved_address, resolved_name, resolve_err = _resolve_invoice_contact(
                                    event=event,
                                    customer=customer,
                                    location=event.get("location"),
                                )
                                if resolve_err == "PROFILE_NOT_FOUND":
                                    hinted_desc = upsert_invoice_profile_missing_hint(
                                        event.get("description") or "",
                                        missing=True,
                                    )
                                    safe_update(
                                        event_id=event.get("id"),
                                        description=hinted_desc,
                                        label="Invoice profile missing",
                                        summary_status="blue",
                                        current_summary=event.get("summary"),
                                        calendar_id=calendar_id,
                                    )
                                    _feed.push(
                                        f"Invoice profile not found for \"{event.get('summary', event_id)}\"",
                                        "warn",
                                    )
                                    state = set_processed_update_marker(state, event_key, event_updated)
                                    continue
                                if contact and contact.get("ContactID"):
                                    cleaned_desc = upsert_invoice_profile_missing_hint(
                                        event.get("description") or "",
                                        missing=False,
                                    )
                                    if cleaned_desc != (event.get("description") or ""):
                                        updated = safe_update(
                                            event_id=event.get("id"),
                                            description=cleaned_desc,
                                            label="Invoice profile warning cleared",
                                            calendar_id=calendar_id,
                                        )
                                        if updated:
                                            event["description"] = cleaned_desc
                                    existing_contact_id = contact.get("ContactID")
                                    state = set_contact_for_event(
                                        state, event_key, existing_contact_id
                                    )
                                    state = set_contact_fingerprint(
                                        state,
                                        event_key,
                                        f"{resolved_name}|{customer.get('email','')}|{customer.get('phone','')}|{address or {}}",
                                    )
                                    print(
                                        f"Contact processed for event {event['id']}: {existing_contact_id}"
                                    )
                                    _feed.push(
                                        f"Customer saved to Xero: {resolved_name or customer.get('name', '')}",
                                        "success",
                                    )
                                else:
                                    print(f"Event {event_id}: contact not saved (no ContactID)")
                            elif blocking_errors:
                                print(f"Event {event_id}: contact validation failed {blocking_errors}")
                        if existing_contact_id and xero_client:
                            if event_updated and event_updated != last_contact_update:
                                customer = parse_customer_fields(event.get("description"))
                                overrides = parse_invoice_contact_overrides(event.get("description"))
                                profile_mode = bool((overrides.get("invoice_profile") or "").strip())
                                address = _invoice_address_from_overrides(
                                    overrides,
                                    parse_event_address(event.get("location")),
                                )
                                errors = validate_customer_fields(customer)
                                blocking_errors = {
                                    k: v for k, v in errors.items() if k != "phone"
                                }
                                if profile_mode:
                                    blocking_errors = {}
                                if not blocking_errors and not profile_mode:
                                    fingerprint = (
                                        f"{(overrides.get('invoice_name') or customer.get('name') or '').strip()}"
                                        f"|{customer.get('email','')}"
                                        f"|{customer.get('phone','')}"
                                        f"|{address or {}}"
                                    )
                                    last_fp = get_contact_fingerprint(state, event_key)
                                    if fingerprint != last_fp:
                                        xero_client.update_contact(
                                            contact_id=existing_contact_id,
                                            email=customer.get("email", ""),
                                            phone=customer.get("phone", ""),
                                            address=address if address else None,
                                        )
                                        state = set_contact_fingerprint(
                                            state, event_key, fingerprint
                                        )
                                        print(
                                            f"Contact updated for event {event['id']}: {existing_contact_id}"
                                        )
                                        _feed.push(
                                            f"Customer details updated in Xero: {customer.get('name', '')}",
                                            "info",
                                        )
                                    # Always advance marker to avoid repeated checks for same update.
                                    state = set_contact_update_marker(
                                        state, event_key, event_updated
                                    )
                            # Keep draft invoice in sync while DONE is present (until sent).
                            if (
                                invoice_lines
                                and xero_client
                                and existing_contact_id
                                and not is_invoice_sent(state, event_key)
                                and not has_send
                            ):
                                invoice_id = get_invoice_for_event(state, event_key)
                                last_invoice_update = get_invoice_update_marker(
                                    state, event_key
                                )
                                contact_ref = (
                                    {"ContactID": existing_contact_id}
                                    if existing_contact_id
                                    else None
                                )
                                draft_fp = _draft_sync_fingerprint(
                                    invoice_lines=invoice_lines,
                                    contact_id=existing_contact_id,
                                    payment_mode=payment_choice(event.get("description")) or "",
                                    force_no_vat=invoice_has_cash_marker(event.get("description")),
                                )
                                last_draft_fp = get_draft_sync_fingerprint(state, event_key)
                                last_draft_attempt = (
                                    get_draft_sync_attempted_at(state, event_key) or 0.0
                                )
                                now_ts = time.time()
                                in_cooldown = (
                                    (now_ts - last_draft_attempt) < _DRAFT_SYNC_COOLDOWN_SECONDS
                                )
                                if not invoice_id:
                                    if in_cooldown and draft_fp == last_draft_fp:
                                        if event_updated:
                                            state = set_invoice_update_marker(
                                                state, event_key, event_updated
                                            )
                                        continue
                                    print(f"Event {event_id}: creating draft invoice")
                                    _feed.push(f"Creating invoice in Xero for \"{event.get('summary', event_id)}\"…", "info")
                                    details = extract_event_details(event)
                                    state = set_draft_sync_attempted_at(state, event_key, now_ts)
                                    result = xero_client.create_invoice_from_event(
                                        details, contact=contact_ref, line_items=invoice_lines
                                    )
                                    print(f"Invoice draft created for event {details.get('id')}: {_invoice_brief(result)}")
                                    _feed.push(f"Invoice created in Xero — {_invoice_brief(result)}", "success")
                                    if result.get("Invoices"):
                                        invoice_id = result["Invoices"][0].get("InvoiceID")
                                        if invoice_id:
                                            state = set_invoice_for_event(
                                                state, event_key, invoice_id
                                            )
                                            state = set_draft_sync_fingerprint(
                                                state, event_key, draft_fp
                                            )
                                            state = set_invoice_update_marker(
                                                state, event_key, event_updated
                                            )
                                            save_state(config.state_file, state)
                                        else:
                                            print(f"Event {event_id}: draft create returned no InvoiceID")
                                    else:
                                        print(f"Event {event_id}: draft create returned no Invoices array")
                                    subtotal, total = _extract_totals(result)
                                    if subtotal is not None and total is not None:
                                        updated_description = upsert_invoice_summary(
                                            collapse_invoice_override_section(event.get("description") or ""),
                                            subtotal,
                                            total,
                                            sent=False,
                                            invoice_url=(
                                                xero_client.get_online_invoice_url(invoice_id)
                                                if xero_client and invoice_id
                                                else None
                                            ),
                                            include_prompt=True,
                                            submitter=submitter_display,
                                            submitted_at=submitted_at_display,
                                        )
                                        if updated_description != (event.get("description") or ""):
                                            updated = safe_update(
                                                event_id=event.get("id"),
                                                description=updated_description,
                                                label="Invoice summary",
                                                summary_status="orange",
                                                current_summary=event.get("summary"),
                                                calendar_id=calendar_id,
                                                draft_progress_increment=True,
                                            )
                                            if updated:
                                                event["description"] = updated_description
                                                event_updated = updated.get("updated") or event_updated
                                else:
                                    should_try_update = bool(
                                        (event_updated and event_updated != last_invoice_update)
                                        or (draft_fp != last_draft_fp)
                                    )
                                    if not should_try_update:
                                        pass
                                    elif in_cooldown and draft_fp == last_draft_fp:
                                        if event_updated:
                                            state = set_invoice_update_marker(
                                                state, event_key, event_updated
                                            )
                                    else:
                                        state = set_draft_sync_attempted_at(
                                            state, event_key, now_ts
                                        )
                                        mutable, status = _is_invoice_mutable(invoice_id)
                                        if not mutable:
                                            print(
                                                f"Event {event_id}: skip invoice update, status={status}"
                                            )
                                            status_upper = (status or "").upper()
                                            if status_upper == "PAID":
                                                if not (event.get("summary") or "").strip().startswith("🟢"):
                                                    updated = safe_update(
                                                        event_id=event.get("id"),
                                                        description=event.get("description") or "",
                                                        label="Invoice paid",
                                                        summary_status="green",
                                                        current_summary=event.get("summary"),
                                                        calendar_id=calendar_id,
                                                    )
                                                    if updated:
                                                        event_updated = updated.get("updated") or event_updated
                                                state = mark_invoice_sent(state, event_key)
                                                state = mark_invoice_paid(state, event_key)
                                            elif status_upper == "AUTHORISED":
                                                state = mark_invoice_sent(state, event_key)
                                                _pay_mode = payment_choice(event.get("description")) or ""
                                                _summary_target = "green" if _pay_mode in {"card", "cash"} else "yellow"
                                                _desc = event.get("description") or ""
                                                if "invoice sent ✅" not in _desc.lower():
                                                    _updated_description = upsert_send_confirmation(
                                                        _desc,
                                                        invoice_url=(
                                                            xero_client.get_online_invoice_url(invoice_id)
                                                            if xero_client and invoice_id
                                                            else None
                                                        ),
                                                        submitter=submitter_display,
                                                        submitted_at=submitted_at_display,
                                                    )
                                                    _upd = safe_update(
                                                        event_id=event.get("id"),
                                                        description=_updated_description,
                                                        label="Invoice authorised",
                                                        summary_status=_summary_target,
                                                        current_summary=event.get("summary"),
                                                        calendar_id=calendar_id,
                                                    )
                                                    if _upd:
                                                        event["description"] = _updated_description
                                                        event_updated = _upd.get("updated") or event_updated
                                                else:
                                                    _upd = safe_update(
                                                        event_id=event.get("id"),
                                                        description=_desc,
                                                        label="Invoice authorised",
                                                        summary_status=_summary_target,
                                                        current_summary=event.get("summary"),
                                                        calendar_id=calendar_id,
                                                    )
                                                    if _upd:
                                                        event_updated = _upd.get("updated") or event_updated
                                            state = set_invoice_update_marker(
                                                state, event_key, event_updated
                                            )
                                        else:
                                            result = xero_client.update_invoice(
                                                invoice_id=invoice_id,
                                                contact=contact_ref,
                                                line_items=invoice_lines,
                                            )
                                            print(f"Invoice draft updated for event {event['id']}: {_invoice_brief(result)}")
                                            _feed.push(
                                                f"Invoice draft updated: {_invoice_brief(result)}",
                                                "info",
                                            )
                                            subtotal, total = _extract_totals(result)
                                            if subtotal is not None and total is not None:
                                                updated_description = upsert_invoice_summary(
                                                    collapse_invoice_override_section(event.get("description") or ""),
                                                    subtotal,
                                                    total,
                                                    sent=False,
                                                    invoice_url=(
                                                        xero_client.get_online_invoice_url(invoice_id)
                                                        if xero_client and invoice_id
                                                        else None
                                                    ),
                                                    include_prompt=True,
                                                    submitter=submitter_display,
                                                    submitted_at=submitted_at_display,
                                                )
                                                if updated_description != (event.get("description") or ""):
                                                    updated = safe_update(
                                                        event_id=event.get("id"),
                                                        description=updated_description,
                                                        label="Invoice summary",
                                                        summary_status="orange",
                                                        current_summary=event.get("summary"),
                                                        calendar_id=calendar_id,
                                                        draft_progress_increment=True,
                                                    )
                                                    if updated:
                                                        event["description"] = updated_description
                                                        event_updated = updated.get("updated") or event_updated
                                            state = set_draft_sync_fingerprint(
                                                state, event_key, draft_fp
                                            )
                                            state = set_invoice_update_marker(
                                                state, event_key, event_updated
                                            )
                            elif has_done and not has_send and not existing_contact_id:
                                print(f"Event {event_id}: skipping invoice (no contact_id)")
                            elif has_done and not has_send and not xero_client:
                                print(f"Event {event_id}: skipping invoice (xero not configured)")

                            # If SEND keyword is present, mark as sent in notes.
                            if (
                                has_send
                                and invoice_lines
                                and not is_invoice_sent(state, event_key)
                                and (
                                    payment_choice(event.get("description")) == "cash"
                                    or (
                                        xero_client
                                        and get_invoice_for_event(state, event_key)
                                    )
                                )
                            ):
                                invoice_id = get_invoice_for_event(state, event_key) or ""
                                pay_mode = payment_choice(event.get("description"))
                                if pay_mode not in {"card", "invoice", "cash"}:
                                    failed_description = upsert_send_failure(
                                        event.get("description") or "",
                                        "Choose PAYMENT TYPE as CARD or INVOICE before SEND",
                                        invoice_url=(
                                            xero_client.get_online_invoice_url(invoice_id)
                                            if xero_client and invoice_id
                                            else None
                                        ),
                                        submitter=submitter_display,
                                        submitted_at=submitted_at_display,
                                    )
                                    updated = safe_update(
                                        event_id=event.get("id"),
                                        description=failed_description,
                                        label="Invoice send failed",
                                        summary_status="yellow",
                                        current_summary=event.get("summary"),
                                        calendar_id=calendar_id,
                                    )
                                    if updated:
                                        event["description"] = failed_description
                                        event_updated = updated.get("updated") or event_updated
                                    state = set_processed_update_marker(state, event_key, event_updated)
                                    continue
                                if pay_mode == "cash":
                                    cash_cleanup_warning = None
                                    _cleanup_queue = dict(state.get("draft_cleanup_queue", {}) or {})
                                    if invoice_id and xero_client:
                                        try:
                                            xero_client.delete_draft_invoice(invoice_id)
                                            _cleanup_queue.pop(event_key, None)
                                        except Exception as exc:
                                            print(f"Event {event_id}: failed to remove draft for cash flow: {exc}")
                                            cash_cleanup_warning = str(exc).splitlines()[0][:140]
                                            _cleanup_queue[event_key] = {
                                                "invoice_id": invoice_id,
                                                "next_retry_at": 0.0,
                                                "backoff_seconds": _DRAFT_CLEANUP_MIN_BACKOFF,
                                                "last_error": cash_cleanup_warning,
                                            }
                                            _feed.push(
                                                f"Cash marked complete but draft cleanup will retry: {cash_cleanup_warning}",
                                                "warn",
                                            )
                                    state["draft_cleanup_queue"] = _cleanup_queue

                                    # Log sales rows from the original invoice block before
                                    # we rewrite notes for cash completion.
                                    state = _append_sales_rows_if_enabled(
                                        event=event,
                                        event_key=event_key,
                                        invoice_id=invoice_id,
                                        payment_method=pay_mode,
                                        submitter_email=submitter_email,
                                        submitter_display=submitter_display,
                                        admin_creds=admin_creds,
                                        sales_sheet_target=sales_sheet_target,
                                        sales_stats_fields=sales_stats_fields,
                                        state=state,
                                    )

                                    updated_description = upsert_cash_confirmation(
                                        event.get("description") or "",
                                        submitter=submitter_display,
                                        submitted_at=submitted_at_display,
                                        cleanup_warning=cash_cleanup_warning,
                                    )
                                    if updated_description != (event.get("description") or ""):
                                        updated = safe_update(
                                            event_id=event.get("id"),
                                            description=updated_description,
                                            label="Cash payment recorded",
                                            summary_status="green",
                                            current_summary=event.get("summary"),
                                            calendar_id=calendar_id,
                                        )
                                        if updated:
                                            event["description"] = updated_description
                                            event_updated = updated.get("updated") or event_updated
                                    state = _append_cash_row_or_backlog(
                                        event=event,
                                        event_key=event_key,
                                        invoice_id=invoice_id,
                                        submitter_email=submitter_email,
                                        submitter_display=submitter_display,
                                        admin_creds=admin_creds,
                                        stats_fields=stats_fields,
                                        cash_sheet_target=cash_sheet_target,
                                        state=state,
                                    )
                                    state = mark_invoice_paid(state, event_key)
                                    state = set_invoice_for_event(state, event_key, "")
                                    state = set_invoice_update_marker(state, event_key, event_updated)
                                    print(f"Cash payment finalised for event {event_id}: draft {invoice_id} deleted")
                                    _feed.push(f"Cash payment logged for \"{event.get('summary', event_id)}\"", "success")
                                    state = set_processed_update_marker(state, event_key, event_updated)
                                    continue
                                invoice_url = None
                                if invoice_id:
                                    try:
                                        _issue_date = (
                                            dt.datetime.now(dt.timezone.utc)
                                            .astimezone(LONDON_TZ)
                                            .date()
                                            .isoformat()
                                        )
                                        xero_client.authorize_invoice(
                                            invoice_id, issue_date=_issue_date
                                        )
                                    except Exception as exc:
                                        print(f"Event {event_id}: failed to authorise invoice for send: {exc}")
                                        fail_reason = str(exc).splitlines()[0][:220]
                                        failed_description = upsert_send_failure(
                                            event.get("description") or "",
                                            fail_reason,
                                            invoice_url=xero_client.get_online_invoice_url(invoice_id),
                                            submitter=submitter_display,
                                            submitted_at=submitted_at_display,
                                        )
                                        updated = safe_update(
                                            event_id=event.get("id"),
                                            description=failed_description,
                                            label="Invoice send failed",
                                            summary_status="yellow",
                                            current_summary=event.get("summary"),
                                            calendar_id=calendar_id,
                                        )
                                        if updated:
                                            event["description"] = failed_description
                                            event_updated = updated.get("updated") or event_updated
                                        # Master sheet should still receive pending invoice rows.
                                        if pay_mode in {"invoice", "card"}:
                                            state = _append_sheet_stats_if_enabled(
                                                event=event,
                                                event_key=event_key,
                                                invoice_id=invoice_id,
                                                payment_method=pay_mode,
                                                paid_override=False,
                                                submitter_display=submitter_display,
                                                admin_creds=admin_creds,
                                                sheet_target=sheet_target,
                                                stats_fields=stats_fields,
                                                state=state,
                                            )
                                        state = set_processed_update_marker(state, event_key, event_updated)
                                        continue
                                    if pay_mode == "card":
                                        try:
                                            invoice_data = xero_client.get_invoice(invoice_id)
                                            amount_due = float(invoice_data.get("AmountDue") or 0.0)
                                            if amount_due > 0:
                                                xero_client.record_invoice_payment(
                                                    invoice_id=invoice_id,
                                                    amount=amount_due,
                                                )
                                        except Exception as exc:
                                            print(f"Event {event_id}: failed to mark invoice paid: {exc}")
                                            _feed.push(
                                                f"Card payment failed for \"{event.get('summary', event_id)}\": {str(exc).splitlines()[0][:180]}",
                                                "error",
                                            )
                                            fail_reason = str(exc).splitlines()[0][:220]
                                            failed_description = upsert_send_failure(
                                                event.get("description") or "",
                                                f"Card payment failed: {fail_reason}",
                                                invoice_url=xero_client.get_online_invoice_url(invoice_id),
                                                submitter=submitter_display,
                                                submitted_at=submitted_at_display,
                                            )
                                            updated = safe_update(
                                                event_id=event.get("id"),
                                                description=failed_description,
                                                label="Invoice send failed",
                                                summary_status="yellow",
                                                current_summary=event.get("summary"),
                                                calendar_id=calendar_id,
                                            )
                                            if updated:
                                                event["description"] = failed_description
                                                event_updated = updated.get("updated") or event_updated
                                            # Still log to sheets as outstanding so submissions are never dropped.
                                            state = _append_sheet_stats_if_enabled(
                                                event=event,
                                                event_key=event_key,
                                                invoice_id=invoice_id,
                                                payment_method=pay_mode,
                                                paid_override=False,
                                                submitter_display=submitter_display,
                                                admin_creds=admin_creds,
                                                sheet_target=sheet_target,
                                                stats_fields=stats_fields,
                                                state=state,
                                            )
                                            state = set_processed_update_marker(state, event_key, event_updated)
                                            continue
                                    emailed = xero_client.email_invoice(invoice_id)
                                    if not emailed:
                                        print(f"Event {event_id}: failed to email invoice {invoice_id}")
                                        _feed.push(
                                            f"Invoice email failed for \"{event.get('summary', event_id)}\" ({invoice_id[:8]}…)",
                                            "warn",
                                        )
                                        failed_description = upsert_send_failure(
                                            event.get("description") or "",
                                            None,
                                            invoice_url=xero_client.get_online_invoice_url(invoice_id),
                                            submitter=submitter_display,
                                            submitted_at=submitted_at_display,
                                        )
                                        updated = safe_update(
                                            event_id=event.get("id"),
                                            description=failed_description,
                                            label="Invoice send failed",
                                            summary_status=("green" if pay_mode == "card" else "yellow"),
                                            current_summary=event.get("summary"),
                                            calendar_id=calendar_id,
                                        )
                                        if updated:
                                            event["description"] = failed_description
                                            event_updated = updated.get("updated") or event_updated
                                        if pay_mode in {"invoice", "card"}:
                                            state = _append_sheet_stats_if_enabled(
                                                event=event,
                                                event_key=event_key,
                                                invoice_id=invoice_id,
                                                payment_method=pay_mode,
                                                paid_override=False if pay_mode == "invoice" else None,
                                                submitter_display=submitter_display,
                                                admin_creds=admin_creds,
                                                sheet_target=sheet_target,
                                                stats_fields=stats_fields,
                                                state=state,
                                            )
                                        if pay_mode == "card":
                                            state = _append_sales_rows_if_enabled(
                                                event=event,
                                                event_key=event_key,
                                                invoice_id=invoice_id,
                                                payment_method=pay_mode,
                                                submitter_email=submitter_email,
                                                submitter_display=submitter_display,
                                                admin_creds=admin_creds,
                                                sales_sheet_target=sales_sheet_target,
                                                sales_stats_fields=sales_stats_fields,
                                                state=state,
                                            )
                                            # Card payment is already captured even if e-mail send fails.
                                            state = mark_invoice_paid(state, event_key)
                                        state = set_invoice_update_marker(
                                            state, event_key, event_updated
                                        )
                                        state = set_processed_update_marker(state, event_key, event_updated)
                                        continue
                                    invoice_url = xero_client.get_online_invoice_url(invoice_id)
                                updated_description = upsert_send_confirmation(
                                    event.get("description") or "",
                                    invoice_url=invoice_url,
                                    submitter=submitter_display,
                                    submitted_at=submitted_at_display,
                                )
                                if updated_description != (event.get("description") or ""):
                                    updated = safe_update(
                                        event_id=event.get("id"),
                                        description=updated_description,
                                        label="Invoice sent",
                                        summary_status=("green" if pay_mode == "card" else "yellow"),
                                        current_summary=event.get("summary"),
                                        calendar_id=calendar_id,
                                    )
                                    if updated:
                                        event["description"] = updated_description
                                        event_updated = updated.get("updated") or event_updated
                                state = mark_invoice_sent(state, event_key)
                                if pay_mode == "card":
                                    state = mark_invoice_paid(state, event_key)
                                state = set_invoice_update_marker(
                                    state, event_key, event_updated
                                )
                                print(f"Invoice sent for event {event_id}: {invoice_id}")
                                _feed.push(f"Invoice authorised & emailed: {invoice_id[:8]}… for \"{event.get('summary', event_id)}\"", "success")
                                state = _append_sheet_stats_if_enabled(
                                    event=event,
                                    event_key=event_key,
                                    invoice_id=invoice_id,
                                    payment_method=pay_mode,
                                    submitter_display=submitter_display,
                                    admin_creds=admin_creds,
                                    sheet_target=sheet_target,
                                    stats_fields=stats_fields,
                                    state=state,
                                )
                                if pay_mode == "card":
                                    state = _append_sales_rows_if_enabled(
                                        event=event,
                                        event_key=event_key,
                                        invoice_id=invoice_id,
                                        payment_method=pay_mode,
                                        submitter_email=submitter_email,
                                        submitter_display=submitter_display,
                                        admin_creds=admin_creds,
                                        sales_sheet_target=sales_sheet_target,
                                        sales_stats_fields=sales_stats_fields,
                                        state=state,
                                    )
                            elif (
                                has_send
                                and invoice_lines
                                and is_invoice_sent(state, event_key)
                            ):
                                # Invoice already sent — retry any missing sheet writes without re-authorising
                                invoice_id_retry = get_invoice_for_event(state, event_key) or ""
                                pay_mode_retry = payment_choice(event.get("description")) or ""
                                print(f"Event {event_id}: invoice already sent, retrying sheet writes (pay={pay_mode_retry})", flush=True)
                                if pay_mode_retry == "cash":
                                    state = _append_cash_row_or_backlog(
                                        event=event,
                                        event_key=event_key,
                                        invoice_id=invoice_id_retry,
                                        submitter_email=submitter_email,
                                        submitter_display=submitter_display,
                                        admin_creds=admin_creds,
                                        stats_fields=stats_fields,
                                        cash_sheet_target=cash_sheet_target,
                                        state=state,
                                    )
                                    state = _append_sheet_stats_if_enabled(
                                        event=event,
                                        event_key=event_key,
                                        invoice_id=invoice_id_retry,
                                        payment_method=pay_mode_retry,
                                        submitter_display=submitter_display,
                                        admin_creds=admin_creds,
                                        sheet_target=sheet_target,
                                        stats_fields=stats_fields,
                                        state=state,
                                    )
                                    state = _append_sales_rows_if_enabled(
                                        event=event,
                                        event_key=event_key,
                                        invoice_id=invoice_id_retry,
                                        payment_method=pay_mode_retry,
                                        submitter_email=submitter_email,
                                        submitter_display=submitter_display,
                                        admin_creds=admin_creds,
                                        sales_sheet_target=sales_sheet_target,
                                        sales_stats_fields=sales_stats_fields,
                                        state=state,
                                    )
                                elif pay_mode_retry == "card":
                                    state = _append_sheet_stats_if_enabled(
                                        event=event,
                                        event_key=event_key,
                                        invoice_id=invoice_id_retry,
                                        payment_method=pay_mode_retry,
                                        submitter_display=submitter_display,
                                        admin_creds=admin_creds,
                                        sheet_target=sheet_target,
                                        stats_fields=stats_fields,
                                        state=state,
                                    )
                                    state = _append_sales_rows_if_enabled(
                                        event=event,
                                        event_key=event_key,
                                        invoice_id=invoice_id_retry,
                                        payment_method=pay_mode_retry,
                                        submitter_email=submitter_email,
                                        submitter_display=submitter_display,
                                        admin_creds=admin_creds,
                                        sales_sheet_target=sales_sheet_target,
                                        sales_stats_fields=sales_stats_fields,
                                        state=state,
                                    )
                                    # Card means immediate payment: keep retrying Xero payment-post
                                    # if prior attempts were rate-limited.
                                    if invoice_id_retry and xero_client:
                                        try:
                                            invoice_data = xero_client.get_invoice(invoice_id_retry)
                                            status = str(invoice_data.get("Status") or "").upper()
                                            amount_due = float(invoice_data.get("AmountDue") or 0.0)
                                            if status != "PAID" and amount_due > 0.0001:
                                                xero_client.record_invoice_payment(
                                                    invoice_id=invoice_id_retry,
                                                    amount=amount_due,
                                                )
                                                invoice_data = xero_client.get_invoice(invoice_id_retry)
                                                status = str(invoice_data.get("Status") or "").upper()
                                                amount_due = float(invoice_data.get("AmountDue") or 0.0)
                                            is_paid = status == "PAID" or amount_due <= 0.0001
                                        except Exception as exc:
                                            print(f"Event {event_id}: failed to verify/mark card payment on retry: {exc}", flush=True)
                                            is_paid = False
                                            invoice_data = {}
                                        if is_paid:
                                            inv_number = str(invoice_data.get("InvoiceNumber") or "").strip()
                                            if admin_creds and sheet_target.get("spreadsheet_id", "").strip() and inv_number:
                                                try:
                                                    sheet_update_status = update_invoice_paid_in_sheet(
                                                        admin_creds,
                                                        spreadsheet_id=sheet_target.get("spreadsheet_id", "").strip(),
                                                        sheet_name=sheet_target.get("sheet_name", "Sheet1").strip() or "Sheet1",
                                                        invoice_number=inv_number,
                                                    )
                                                    if sheet_update_status == "updated":
                                                        print(f"Master sheet marked Paid for {inv_number}", flush=True)
                                                    elif sheet_update_status == "already_paid":
                                                        print(f"Master sheet already paid for {inv_number}", flush=True)
                                                except Exception as exc:
                                                    print(f"Master sheet paid update failed for {inv_number}: {exc}", flush=True)
                                            if not (event.get("summary") or "").strip().startswith("🟢"):
                                                updated = safe_update(
                                                    event_id=event.get("id"),
                                                    description=event.get("description") or "",
                                                    label="Card payment settled",
                                                    summary_status="green",
                                                    current_summary=event.get("summary"),
                                                    calendar_id=calendar_id,
                                                )
                                                if updated:
                                                    event_updated = updated.get("updated") or event_updated
                                            state = mark_invoice_paid(state, event_key)
                                elif pay_mode_retry == "invoice" and invoice_id_retry and xero_client:
                                    try:
                                        invoice_data = xero_client.get_invoice(invoice_id_retry)
                                        status = str(invoice_data.get("Status") or "").upper()
                                        amount_due = float(invoice_data.get("AmountDue") or 0.0)
                                        is_paid = status == "PAID" or amount_due <= 0.0001
                                    except Exception as exc:
                                        print(f"Event {event_id}: failed to fetch invoice status for retry: {exc}", flush=True)
                                        is_paid = False
                                        invoice_data = {}
                                    if is_paid:
                                        inv_number = str(invoice_data.get("InvoiceNumber") or "").strip()
                                        # Update master sheet paid columns as a poll fallback (webhook is primary).
                                        if admin_creds and sheet_target.get("spreadsheet_id", "").strip() and inv_number:
                                            try:
                                                sheet_update_status = update_invoice_paid_in_sheet(
                                                    admin_creds,
                                                    spreadsheet_id=sheet_target.get("spreadsheet_id", "").strip(),
                                                    sheet_name=sheet_target.get("sheet_name", "Sheet1").strip() or "Sheet1",
                                                    invoice_number=inv_number,
                                                )
                                                if sheet_update_status == "updated":
                                                    print(f"Master sheet marked Paid for {inv_number}", flush=True)
                                                elif sheet_update_status == "already_paid":
                                                    print(f"Master sheet already paid for {inv_number}", flush=True)
                                            except Exception as exc:
                                                print(f"Master sheet paid update failed for {inv_number}: {exc}", flush=True)
                                        state = _append_sales_rows_if_enabled(
                                            event=event,
                                            event_key=event_key,
                                            invoice_id=invoice_id_retry,
                                            payment_method=pay_mode_retry,
                                            submitter_email=submitter_email,
                                            submitter_display=submitter_display,
                                            admin_creds=admin_creds,
                                            sales_sheet_target=sales_sheet_target,
                                            sales_stats_fields=sales_stats_fields,
                                            state=state,
                                        )
                                        if not (event.get("summary") or "").strip().startswith("🟢"):
                                            updated = safe_update(
                                                event_id=event.get("id"),
                                                description=event.get("description") or "",
                                                label="Invoice paid",
                                                summary_status="green",
                                                current_summary=event.get("summary"),
                                                calendar_id=calendar_id,
                                            )
                                            if updated:
                                                event_updated = updated.get("updated") or event_updated
                            if existing_contact_id:
                                state = set_contact_update_marker(
                                    state, event_key, event_updated
                                )
                            if get_invoice_for_event(state, event_key):
                                state = set_invoice_update_marker(
                                    state, event_key, event_updated
                                )
                            state = set_processed_update_marker(state, event_key, event_updated)
                            continue
                    else:
                        details = extract_event_details(event)
                        _feed.push(f"DONE event detected: \"{event.get('summary', event_id)}\"", "event")
                        customer = parse_customer_fields(event.get("description"))
                        overrides = parse_invoice_contact_overrides(event.get("description"))
                        profile_mode = bool((overrides.get("invoice_profile") or "").strip())
                        address = _invoice_address_from_overrides(
                            overrides,
                            parse_event_address(event.get("location")),
                        )
                        errors = validate_customer_fields(customer)
                        blocking_errors = {k: v for k, v in errors.items() if k != "phone"}
                        if profile_mode:
                            blocking_errors = {}
                        if errors:
                            hinted = apply_validation_hints(
                                event.get("description") or "", errors
                            )
                            if hinted != (event.get("description") or ""):
                                updated = safe_update(
                                    event_id=event.get("id"),
                                    description=hinted,
                                    label="Validation hints",
                                    calendar_id=calendar_id,
                                )
                                if updated:
                                    event["description"] = hinted
                            if blocking_errors and has_done:
                                print(
                                    f"Validation errors for event {details.get('id')}: {errors}"
                                )
                                missing = ", ".join(blocking_errors.keys())
                                _feed.push(f"Missing fields in \"{event.get('summary', event_id)}\": {missing}", "warn")
                                continue
                        if not xero_client:
                            print("Xero client not configured. Skipping send.")
                            print(details)
                        else:
                            contact = None
                            if customer.get("name") or profile_mode or (overrides.get("invoice_name") or "").strip():
                                contact, _resolved_address, resolved_name, resolve_err = _resolve_invoice_contact(
                                    event=event,
                                    customer=customer,
                                    location=event.get("location"),
                                )
                                if resolve_err == "PROFILE_NOT_FOUND":
                                    hinted_desc = upsert_invoice_profile_missing_hint(
                                        event.get("description") or "",
                                        missing=True,
                                    )
                                    safe_update(
                                        event_id=event.get("id"),
                                        description=hinted_desc,
                                        label="Invoice profile missing",
                                        summary_status="blue",
                                        current_summary=event.get("summary"),
                                        calendar_id=calendar_id,
                                    )
                                    _feed.push(
                                        f"Invoice profile not found for \"{event.get('summary', event_id)}\"",
                                        "warn",
                                    )
                                    state = set_processed_update_marker(state, event_key, event.get("updated") or "")
                                    continue
                                if contact and contact.get("ContactID"):
                                    cleaned_desc = upsert_invoice_profile_missing_hint(
                                        event.get("description") or "",
                                        missing=False,
                                    )
                                    if cleaned_desc != (event.get("description") or ""):
                                        updated = safe_update(
                                            event_id=event.get("id"),
                                            description=cleaned_desc,
                                            label="Invoice profile warning cleared",
                                            calendar_id=calendar_id,
                                        )
                                        if updated:
                                            event["description"] = cleaned_desc
                                    _cname = resolved_name or customer.get("name", "")
                                    _feed.push(
                                        f"Customer ready in Xero: {_cname}",
                                        "success",
                                    )
                                    state = set_contact_for_event(
                                        state, event_key, contact["ContactID"]
                                    )
                            # Address payload only used for Xero contact creation/update.
                            # Keep draft invoice in sync while DONE is present (until sent).
                            if (
                                invoice_lines
                                and xero_client
                                and contact
                                and contact.get("ContactID")
                                and not is_invoice_sent(state, event_key)
                                and not has_send
                            ):
                                event_updated = event.get("updated") or ""
                                invoice_id = get_invoice_for_event(state, event_key)
                                contact_ref = (
                                    {"ContactID": contact.get("ContactID")}
                                    if contact and contact.get("ContactID")
                                    else None
                                )
                                draft_fp = _draft_sync_fingerprint(
                                    invoice_lines=invoice_lines,
                                    contact_id=(contact.get("ContactID") if contact else ""),
                                    payment_mode=payment_choice(event.get("description")) or "",
                                    force_no_vat=invoice_has_cash_marker(event.get("description")),
                                )
                                last_draft_fp = get_draft_sync_fingerprint(state, event_key)
                                last_draft_attempt = (
                                    get_draft_sync_attempted_at(state, event_key) or 0.0
                                )
                                now_ts = time.time()
                                in_cooldown = (
                                    (now_ts - last_draft_attempt) < _DRAFT_SYNC_COOLDOWN_SECONDS
                                )
                                if not invoice_id:
                                    if in_cooldown and draft_fp == last_draft_fp:
                                        if event_updated:
                                            state = set_invoice_update_marker(
                                                state, event_key, event_updated
                                            )
                                        continue
                                    state = set_draft_sync_attempted_at(state, event_key, now_ts)
                                    result = xero_client.create_invoice_from_event(
                                        details, contact=contact_ref, line_items=invoice_lines
                                    )
                                    print(f"Invoice draft created for event {details.get('id')}: {_invoice_brief(result)}")
                                    _brief = _invoice_brief(result)
                                    _cname = customer.get("name") or event.get("summary", "")
                                    _feed.push(f"Invoice created in Xero — {_brief} for {_cname}", "success")
                                    if result.get("Invoices"):
                                        invoice_id = result["Invoices"][0].get("InvoiceID")
                                        if invoice_id:
                                            state = set_invoice_for_event(
                                                state, event_key, invoice_id
                                            )
                                            state = set_draft_sync_fingerprint(
                                                state, event_key, draft_fp
                                            )
                                            state = set_invoice_update_marker(
                                                state, event_key, event_updated
                                            )
                                            save_state(config.state_file, state)
                                    subtotal, total = _extract_totals(result)
                                    if subtotal is not None and total is not None:
                                        updated_description = upsert_invoice_summary(
                                            collapse_invoice_override_section(event.get("description") or ""),
                                            subtotal,
                                            total,
                                            sent=False,
                                            invoice_url=(
                                                xero_client.get_online_invoice_url(invoice_id)
                                                if xero_client and invoice_id
                                                else None
                                            ),
                                            include_prompt=True,
                                            submitter=submitter_display,
                                            submitted_at=submitted_at_display,
                                        )
                                        if updated_description != (event.get("description") or ""):
                                            updated = safe_update(
                                                event_id=event.get("id"),
                                                description=updated_description,
                                                label="Invoice summary",
                                                summary_status="orange",
                                                current_summary=event.get("summary"),
                                                calendar_id=calendar_id,
                                                draft_progress_increment=True,
                                            )
                                            if updated:
                                                event["description"] = updated_description
                                                event_updated = updated.get("updated") or event_updated
                                                state = set_invoice_update_marker(
                                                    state, event_key, event_updated
                                                )
                                else:
                                    last_invoice_update = get_invoice_update_marker(
                                        state, event_key
                                    )
                                    should_try_update = bool(
                                        (event_updated and event_updated != last_invoice_update)
                                        or (draft_fp != last_draft_fp)
                                    )
                                    if not should_try_update:
                                        pass
                                    elif in_cooldown and draft_fp == last_draft_fp:
                                        if event_updated:
                                            state = set_invoice_update_marker(
                                                state, event_key, event_updated
                                            )
                                    else:
                                        state = set_draft_sync_attempted_at(
                                            state, event_key, now_ts
                                        )
                                        mutable, status = _is_invoice_mutable(invoice_id)
                                        if not mutable:
                                            print(
                                                f"Event {event.get('id')}: skip invoice update, status={status}"
                                            )
                                            status_upper = (status or "").upper()
                                            if status_upper == "PAID":
                                                if not (event.get("summary") or "").strip().startswith("🟢"):
                                                    updated = safe_update(
                                                        event_id=event.get("id"),
                                                        description=event.get("description") or "",
                                                        label="Invoice paid",
                                                        summary_status="green",
                                                        current_summary=event.get("summary"),
                                                        calendar_id=calendar_id,
                                                    )
                                                    if updated:
                                                        event_updated = updated.get("updated") or event_updated
                                                state = mark_invoice_sent(state, event_key)
                                                state = mark_invoice_paid(state, event_key)
                                            elif status_upper == "AUTHORISED":
                                                state = mark_invoice_sent(state, event_key)
                                                _pay_mode = payment_choice(event.get("description")) or ""
                                                _summary_target = "green" if _pay_mode in {"card", "cash"} else "yellow"
                                                _desc = event.get("description") or ""
                                                if "invoice sent ✅" not in _desc.lower():
                                                    _updated_description = upsert_send_confirmation(
                                                        _desc,
                                                        invoice_url=(
                                                            xero_client.get_online_invoice_url(invoice_id)
                                                            if xero_client and invoice_id
                                                            else None
                                                        ),
                                                        submitter=submitter_display,
                                                        submitted_at=submitted_at_display,
                                                    )
                                                    _upd = safe_update(
                                                        event_id=event.get("id"),
                                                        description=_updated_description,
                                                        label="Invoice authorised",
                                                        summary_status=_summary_target,
                                                        current_summary=event.get("summary"),
                                                        calendar_id=calendar_id,
                                                    )
                                                    if _upd:
                                                        event["description"] = _updated_description
                                                        event_updated = _upd.get("updated") or event_updated
                                                else:
                                                    _upd = safe_update(
                                                        event_id=event.get("id"),
                                                        description=_desc,
                                                        label="Invoice authorised",
                                                        summary_status=_summary_target,
                                                        current_summary=event.get("summary"),
                                                        calendar_id=calendar_id,
                                                    )
                                                    if _upd:
                                                        event_updated = _upd.get("updated") or event_updated
                                            state = set_invoice_update_marker(
                                                state, event_key, event_updated
                                            )
                                        else:
                                            result = xero_client.update_invoice(
                                                invoice_id=invoice_id,
                                                contact=contact_ref,
                                                line_items=invoice_lines,
                                            )
                                            print(f"Invoice draft updated for event {details.get('id')}: {_invoice_brief(result)}")
                                            _feed.push(
                                                f"Invoice draft updated: {_invoice_brief(result)}",
                                                "info",
                                            )
                                            subtotal, total = _extract_totals(result)
                                            if subtotal is not None and total is not None:
                                                updated_description = upsert_invoice_summary(
                                                    collapse_invoice_override_section(event.get("description") or ""),
                                                    subtotal,
                                                    total,
                                                    sent=False,
                                                    invoice_url=(
                                                        xero_client.get_online_invoice_url(invoice_id)
                                                        if xero_client and invoice_id
                                                        else None
                                                    ),
                                                    include_prompt=True,
                                                    submitter=submitter_display,
                                                    submitted_at=submitted_at_display,
                                                )
                                                if updated_description != (event.get("description") or ""):
                                                    updated = safe_update(
                                                        event_id=event.get("id"),
                                                        description=updated_description,
                                                        label="Invoice summary",
                                                        summary_status="orange",
                                                        current_summary=event.get("summary"),
                                                        calendar_id=calendar_id,
                                                        draft_progress_increment=True,
                                                    )
                                                    if updated:
                                                        event["description"] = updated_description
                                                        event_updated = updated.get("updated") or event_updated
                                            state = set_draft_sync_fingerprint(
                                                state, event_key, draft_fp
                                            )
                                            state = set_invoice_update_marker(
                                                state, event_key, event_updated
                                            )

                            if (
                                has_send
                                and invoice_lines
                                and not is_invoice_sent(state, event_key)
                                and (
                                    payment_choice(event.get("description")) == "cash"
                                    or (
                                        xero_client
                                        and get_invoice_for_event(state, event_key)
                                    )
                                )
                            ):
                                invoice_id = get_invoice_for_event(state, event_key) or ""
                                pay_mode = payment_choice(event.get("description"))
                                if pay_mode not in {"card", "invoice", "cash"}:
                                    failed_description = upsert_send_failure(
                                        event.get("description") or "",
                                        "Choose PAYMENT TYPE as CARD or INVOICE before SEND",
                                        invoice_url=(
                                            xero_client.get_online_invoice_url(invoice_id)
                                            if xero_client and invoice_id
                                            else None
                                        ),
                                        submitter=submitter_display,
                                        submitted_at=submitted_at_display,
                                    )
                                    updated = safe_update(
                                        event_id=event.get("id"),
                                        description=failed_description,
                                        label="Invoice send failed",
                                        summary_status="yellow",
                                        current_summary=event.get("summary"),
                                        calendar_id=calendar_id,
                                    )
                                    if updated:
                                        event["description"] = failed_description
                                        event_updated = updated.get("updated") or event_updated
                                    state = set_processed_update_marker(state, event_key, event_updated)
                                    continue
                                if pay_mode == "cash":
                                    cash_cleanup_warning = None
                                    _cleanup_queue = dict(state.get("draft_cleanup_queue", {}) or {})
                                    if invoice_id and xero_client:
                                        try:
                                            xero_client.delete_draft_invoice(invoice_id)
                                            _cleanup_queue.pop(event_key, None)
                                        except Exception as exc:
                                            print(f"Event {event.get('id')}: failed to remove draft for cash flow: {exc}")
                                            cash_cleanup_warning = str(exc).splitlines()[0][:140]
                                            _cleanup_queue[event_key] = {
                                                "invoice_id": invoice_id,
                                                "next_retry_at": 0.0,
                                                "backoff_seconds": _DRAFT_CLEANUP_MIN_BACKOFF,
                                                "last_error": cash_cleanup_warning,
                                            }
                                            _feed.push(
                                                f"Cash marked complete but draft cleanup will retry: {cash_cleanup_warning}",
                                                "warn",
                                            )
                                    state["draft_cleanup_queue"] = _cleanup_queue

                                    # Log sales rows from the original invoice block before
                                    # we rewrite notes for cash completion.
                                    state = _append_sales_rows_if_enabled(
                                        event=event,
                                        event_key=event_key,
                                        invoice_id=invoice_id,
                                        payment_method=pay_mode,
                                        submitter_email=submitter_email,
                                        submitter_display=submitter_display,
                                        admin_creds=admin_creds,
                                        sales_sheet_target=sales_sheet_target,
                                        sales_stats_fields=sales_stats_fields,
                                        state=state,
                                    )

                                    updated_description = upsert_cash_confirmation(
                                        event.get("description") or "",
                                        submitter=submitter_display,
                                        submitted_at=submitted_at_display,
                                        cleanup_warning=cash_cleanup_warning,
                                    )
                                    if updated_description != (event.get("description") or ""):
                                        updated = safe_update(
                                            event_id=event.get("id"),
                                            description=updated_description,
                                            label="Cash payment recorded",
                                            summary_status="green",
                                            current_summary=event.get("summary"),
                                            calendar_id=calendar_id,
                                        )
                                        if updated:
                                            event["description"] = updated_description
                                            event_updated = updated.get("updated") or event_updated
                                    state = _append_cash_row_or_backlog(
                                        event=event,
                                        event_key=event_key,
                                        invoice_id=invoice_id,
                                        submitter_email=submitter_email,
                                        submitter_display=submitter_display,
                                        admin_creds=admin_creds,
                                        stats_fields=stats_fields,
                                        cash_sheet_target=cash_sheet_target,
                                        state=state,
                                    )
                                    state = mark_invoice_paid(state, event_key)
                                    state = set_invoice_for_event(state, event_key, "")
                                    state = set_invoice_update_marker(state, event_key, event_updated)
                                    print(f"Cash payment finalised for event {event.get('id')}: draft {invoice_id} deleted")
                                    _feed.push(f"Cash payment logged for \"{event.get('summary', event.get('id'))}\"", "success")
                                    state = set_processed_update_marker(state, event_key, event_updated)
                                    continue
                                invoice_url = None
                                if invoice_id:
                                    try:
                                        _issue_date = (
                                            dt.datetime.now(dt.timezone.utc)
                                            .astimezone(LONDON_TZ)
                                            .date()
                                            .isoformat()
                                        )
                                        xero_client.authorize_invoice(
                                            invoice_id, issue_date=_issue_date
                                        )
                                    except Exception as exc:
                                        print(f"Event {event.get('id')}: failed to authorise invoice for send: {exc}")
                                        fail_reason = str(exc).splitlines()[0][:220]
                                        failed_description = upsert_send_failure(
                                            event.get("description") or "",
                                            fail_reason,
                                            invoice_url=xero_client.get_online_invoice_url(invoice_id),
                                            submitter=submitter_display,
                                            submitted_at=submitted_at_display,
                                        )
                                        updated = safe_update(
                                            event_id=event.get("id"),
                                            description=failed_description,
                                            label="Invoice send failed",
                                            summary_status="yellow",
                                            current_summary=event.get("summary"),
                                            calendar_id=calendar_id,
                                        )
                                        if updated:
                                            event["description"] = failed_description
                                            event_updated = updated.get("updated") or event_updated
                                        # Master sheet should still receive pending invoice rows.
                                        if pay_mode in {"invoice", "card"}:
                                            state = _append_sheet_stats_if_enabled(
                                                event=event,
                                                event_key=event_key,
                                                invoice_id=invoice_id,
                                                payment_method=pay_mode,
                                                paid_override=False,
                                                submitter_display=submitter_display,
                                                admin_creds=admin_creds,
                                                sheet_target=sheet_target,
                                                stats_fields=stats_fields,
                                                state=state,
                                            )
                                        state = set_processed_update_marker(state, event_key, event_updated)
                                        continue
                                    if pay_mode == "card":
                                        try:
                                            invoice_data = xero_client.get_invoice(invoice_id)
                                            amount_due = float(invoice_data.get("AmountDue") or 0.0)
                                            if amount_due > 0:
                                                xero_client.record_invoice_payment(
                                                    invoice_id=invoice_id,
                                                    amount=amount_due,
                                                )
                                        except Exception as exc:
                                            print(f"Event {event.get('id')}: failed to mark invoice paid: {exc}")
                                            _feed.push(
                                                f"Card payment failed for \"{event.get('summary', event.get('id'))}\": {str(exc).splitlines()[0][:180]}",
                                                "error",
                                            )
                                            fail_reason = str(exc).splitlines()[0][:220]
                                            failed_description = upsert_send_failure(
                                                event.get("description") or "",
                                                f"Card payment failed: {fail_reason}",
                                                invoice_url=xero_client.get_online_invoice_url(invoice_id),
                                                submitter=submitter_display,
                                                submitted_at=submitted_at_display,
                                            )
                                            updated = safe_update(
                                                event_id=event.get("id"),
                                                description=failed_description,
                                                label="Invoice send failed",
                                                summary_status="yellow",
                                                current_summary=event.get("summary"),
                                                calendar_id=calendar_id,
                                            )
                                            if updated:
                                                event["description"] = failed_description
                                                event_updated = updated.get("updated") or event_updated
                                            # Still log to sheets as outstanding so submissions are never dropped.
                                            state = _append_sheet_stats_if_enabled(
                                                event=event,
                                                event_key=event_key,
                                                invoice_id=invoice_id,
                                                payment_method=pay_mode,
                                                paid_override=False,
                                                submitter_display=submitter_display,
                                                admin_creds=admin_creds,
                                                sheet_target=sheet_target,
                                                stats_fields=stats_fields,
                                                state=state,
                                            )
                                            state = set_processed_update_marker(state, event_key, event_updated)
                                            continue
                                    emailed = xero_client.email_invoice(invoice_id)
                                    if not emailed:
                                        print(f"Event {event.get('id')}: failed to email invoice {invoice_id}")
                                        _feed.push(
                                            f"Invoice email failed for \"{event.get('summary', event.get('id'))}\" ({invoice_id[:8]}…)",
                                            "warn",
                                        )
                                        failed_description = upsert_send_failure(
                                            event.get("description") or "",
                                            None,
                                            invoice_url=xero_client.get_online_invoice_url(invoice_id),
                                            submitter=submitter_display,
                                            submitted_at=submitted_at_display,
                                        )
                                        updated = safe_update(
                                            event_id=event.get("id"),
                                            description=failed_description,
                                            label="Invoice send failed",
                                            summary_status=("green" if pay_mode == "card" else "yellow"),
                                            current_summary=event.get("summary"),
                                            calendar_id=calendar_id,
                                        )
                                        if updated:
                                            event["description"] = failed_description
                                            event_updated = updated.get("updated") or event_updated
                                        if pay_mode in {"invoice", "card"}:
                                            state = _append_sheet_stats_if_enabled(
                                                event=event,
                                                event_key=event_key,
                                                invoice_id=invoice_id,
                                                payment_method=pay_mode,
                                                paid_override=False if pay_mode == "invoice" else None,
                                                submitter_display=submitter_display,
                                                admin_creds=admin_creds,
                                                sheet_target=sheet_target,
                                                stats_fields=stats_fields,
                                                state=state,
                                            )
                                        if pay_mode == "card":
                                            state = _append_sales_rows_if_enabled(
                                                event=event,
                                                event_key=event_key,
                                                invoice_id=invoice_id,
                                                payment_method=pay_mode,
                                                submitter_email=submitter_email,
                                                submitter_display=submitter_display,
                                                admin_creds=admin_creds,
                                                sales_sheet_target=sales_sheet_target,
                                                sales_stats_fields=sales_stats_fields,
                                                state=state,
                                            )
                                            # Card payment is already captured even if e-mail send fails.
                                            state = mark_invoice_paid(state, event_key)
                                        state = set_invoice_update_marker(
                                            state, event_key, event_updated
                                        )
                                        state = set_processed_update_marker(state, event_key, event_updated)
                                        continue
                                    invoice_url = xero_client.get_online_invoice_url(invoice_id)
                                updated_description = upsert_send_confirmation(
                                    event.get("description") or "",
                                    invoice_url=invoice_url,
                                    submitter=submitter_display,
                                    submitted_at=submitted_at_display,
                                )
                                if updated_description != (event.get("description") or ""):
                                    updated = safe_update(
                                        event_id=event.get("id"),
                                        description=updated_description,
                                        label="Invoice sent",
                                        summary_status=("green" if pay_mode == "card" else "yellow"),
                                        current_summary=event.get("summary"),
                                        calendar_id=calendar_id,
                                    )
                                    if updated:
                                        event["description"] = updated_description
                                        event_updated = updated.get("updated") or event_updated
                                state = mark_invoice_sent(state, event_key)
                                if pay_mode == "card":
                                    state = mark_invoice_paid(state, event_key)
                                state = set_invoice_update_marker(
                                    state, event_key, event_updated
                                )
                                print(f"Invoice sent for event {event.get('id')}: {invoice_id}")
                                _feed.push(f"Invoice authorised & emailed: {invoice_id[:8]}… for \"{event.get('summary', '')}\"", "success")
                                state = _append_sheet_stats_if_enabled(
                                    event=event,
                                    event_key=event_key,
                                    invoice_id=invoice_id,
                                    payment_method=pay_mode,
                                    submitter_display=submitter_display,
                                    admin_creds=admin_creds,
                                    sheet_target=sheet_target,
                                    stats_fields=stats_fields,
                                    state=state,
                                )
                                if pay_mode == "card":
                                    state = _append_sales_rows_if_enabled(
                                        event=event,
                                        event_key=event_key,
                                        invoice_id=invoice_id,
                                        payment_method=pay_mode,
                                        submitter_email=submitter_email,
                                        submitter_display=submitter_display,
                                        admin_creds=admin_creds,
                                        sales_sheet_target=sales_sheet_target,
                                        sales_stats_fields=sales_stats_fields,
                                        state=state,
                                    )
                            else:
                                if contact and contact.get("Name"):
                                    print(
                                        f"Contact processed for event {details.get('id')}: {contact.get('Name')}"
                                    )
                            if contact and contact.get("ContactID"):
                                state = set_contact_update_marker(
                                    state, event_key, event_updated
                                )
                            # Clear per-event retry cooldown once this event completes successfully.
                            _retry_map = dict(state.get("event_xero_retry_after", {}) or {})
                            if event_key in _retry_map:
                                _retry_map.pop(event_key, None)
                                state["event_xero_retry_after"] = _retry_map
                            _retry_backoff_map = dict(state.get("event_xero_retry_backoff", {}) or {})
                            if event_key in _retry_backoff_map:
                                _retry_backoff_map.pop(event_key, None)
                                state["event_xero_retry_backoff"] = _retry_backoff_map
                            if get_invoice_for_event(state, event_key):
                                state = set_invoice_update_marker(
                                    state, event_key, event_updated
                                )
                            state = mark_processed(state, event_key)
                            state = set_processed_update_marker(state, event_key, event_updated)
                            continue

            except Exception as exc:
                _ev_id = event.get("id") or "unknown"
                _ev_cal = event.get("_calendar_id") or config.google_calendar_id
                print(f"Event processing failed for {_ev_cal}:{_ev_id}: {exc}", flush=True)
                _feed.push(
                    f"Event processing error for \"{event.get('summary', _ev_id)}\": {str(exc).splitlines()[0][:120]}",
                    "error",
                )
                if _is_xero_429(exc):
                    _retry_hint_seconds = _xero_retry_after_hint_seconds(exc)
                    _global_lock_until = float(get_xero_rate_limit_until_ts() or 0.0)
                    _hint_lock_until = (
                        (time.time() + _retry_hint_seconds)
                        if _retry_hint_seconds
                        else 0.0
                    )
                    _effective_lock_until = max(_global_lock_until, _hint_lock_until)
                    # Enter a short global cooldown so one rate-limited event does not
                    # trigger a retry storm across all events/calendars.
                    xero_client = None
                    _xero_retry_after = max(
                        _xero_retry_after,
                        _effective_lock_until
                        if _effective_lock_until
                        else (
                            time.time() + _XERO_429_COOLDOWN_SECONDS
                        ),
                    )
                    state["xero_lockout_until_ts"] = _xero_retry_after
                    state["xero_lockout_reason"] = "Xero API rate limit (429)"
                    state["xero_lockout_updated_at_ts"] = time.time()
                    # Also pause retries for this specific event longer; this is the
                    # common case where one problematic event repeatedly re-triggers 429.
                    _retry_map = dict(state.get("event_xero_retry_after", {}) or {})
                    _retry_backoff_map = dict(state.get("event_xero_retry_backoff", {}) or {})
                    try:
                        _ev_key = event_key  # defined in the current event try block
                    except Exception:
                        _ev_key = ""
                    if _ev_key:
                        _prev = int(_retry_backoff_map.get(_ev_key) or 0)
                        _next_backoff = max(
                            _XERO_EVENT_429_COOLDOWN_SECONDS,
                            _prev * 2 if _prev else 0,
                        )
                        if _retry_hint_seconds:
                            _next_backoff = max(_next_backoff, _retry_hint_seconds)
                        _next_backoff = min(_next_backoff, _XERO_EVENT_429_MAX_COOLDOWN_SECONDS)
                        _retry_backoff_map[_ev_key] = _next_backoff
                        _retry_map[_ev_key] = time.time() + _next_backoff
                        state["event_xero_retry_after"] = _retry_map
                        state["event_xero_retry_backoff"] = _retry_backoff_map
                    _now_notice = time.time()
                    if (_now_notice - _last_xero_429_notice_at) >= 60:
                        _mins = int(
                            (
                                max(
                                    60,
                                    int(
                                        (
                                            _xero_retry_after - time.time()
                                        )
                                    ),
                                )
                            )
                            / 60
                        )
                        _feed.push(
                            f"Xero rate-limited (429). Cooling down for ~{max(_mins,1)} minutes.",
                            "warn",
                        )
                        _last_xero_429_notice_at = _now_notice
                    break
            # Persist state after each event so Ctrl+C doesn't lose progress and
            # cause duplicate retries/drafts on restart.
            save_state(config.state_file, state)

        if not calendar_fetch_failed:
            last_sync = now
            state = set_last_sync(state, now)
        else:
            print("[poll] Keeping last_sync unchanged because one or more calendars failed this cycle", flush=True)
        state = prune_state(state, keep_recent_events=2000)
        save_state(config.state_file, state)

        if config.run_once:
            break
        if had_changes:
            backoff_seconds = max(config.poll_seconds, 5)
        else:
            backoff_seconds = min(backoff_seconds * 2, max_backoff)
        wait_for_poll(backoff_seconds)


if __name__ == "__main__":
    run()
