from __future__ import annotations

import datetime as dt
import os
import time

from .admin_store import (
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
    parse_customer_fields,
    parse_event_address,
    parse_event_address_debug,
    payment_choice,
    upsert_send_failure,
    upsert_invoice_summary,
    upsert_cash_confirmation,
    upsert_send_confirmation,
    set_title_status_emoji,
    validate_customer_fields,
)
from .google_calendar import (
    list_recent_events,
    update_event_description,
    register_calendar_watch,
    stop_calendar_watch,
    RateLimitError,
)
from .trigger import wait_for_poll, consume_watch_check
from .google_sheets import append_stats_row, ensure_header
from .google_admin import load_admin_credentials
from .state import (
    get_cash_log_marker,
    get_contact_for_event,
    get_contact_update_marker,
    get_contact_fingerprint,
    get_invoice_for_event,
    get_invoice_update_marker,
    get_last_sync,
    get_processed_update_marker,
    get_cash_global_log_marker,
    get_sheet_log_marker,
    get_sales_log_marker,
    is_prefilled,
    is_invoice_sent,
    is_processed,
    load_state,
    mark_prefilled,
    mark_invoice_sent,
    mark_processed,
    prune_state,
    save_state,
    set_contact_update_marker,
    set_contact_for_event,
    set_contact_fingerprint,
    set_invoice_for_event,
    set_invoice_update_marker,
    set_last_sync,
    set_cash_log_marker,
    set_cash_global_log_marker,
    set_processed_update_marker,
    set_sheet_log_marker,
    set_sales_log_marker,
)
from .xero_client import XeroClient, build_xero_client
from .log_feed import feed as _feed


def run() -> None:
    config = load_config()
    init_admin_store(config.admin_db_file)
    # Baseline: do not touch any events created before this run starts.
    run_started_at = dt.datetime.now(dt.timezone.utc)
    state = load_state(config.state_file)
    state["run_started_at"] = run_started_at.isoformat()
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
    _xero_retry_after: float = 0.0
    _XERO_REBUILD_INTERVAL = 3300  # rebuild token ~55 min (Xero tokens last 30 min)

    _headers_initialized: set[str] = set()  # sheet keys that have had ensure_header run

    _last_watch_check: float = 0.0
    _WATCH_CHECK_INTERVAL = 3600  # re-check watches at most once per hour

    backoff_seconds = max(config.poll_seconds, 5)
    max_backoff = max(backoff_seconds, 60)

    def safe_update(
        event_id: str,
        description: str,
        label: str | None = None,
        summary_status: str | None = None,
        current_summary: str | None = None,
        calendar_id: str | None = None,
    ):
        summary = None
        if summary_status:
            summary = set_title_status_emoji(current_summary, summary_status)
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
        Only DRAFT invoices should be mutated from calendar edits.
        """
        if not xero_client:
            return False, "NO_CLIENT"
        try:
            invoice_data = xero_client.get_invoice(invoice_id)
        except Exception as exc:
            return False, f"LOOKUP_FAILED: {exc}"
        status = (invoice_data.get("Status") or "").upper()
        return status == "DRAFT", status or "UNKNOWN"

    def _format_display_datetime(raw: str | None = None) -> str:
        if raw:
            try:
                ts = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
                return ts.astimezone().strftime("%d/%m/%Y %H:%M")
            except ValueError:
                pass
        return dt.datetime.now().astimezone().strftime("%d/%m/%Y %H:%M")

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
                    obj = dt.datetime.fromisoformat(iso_str)
                    return obj.strftime("%d/%m/%Y %H:%M")
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
            "submitter": submitter_display
            or (event.get("creator", {}) or {}).get("email")
            or (event.get("organizer", {}) or {}).get("email")
            or "",
            "customer": customer_fields.get("name") or "",
            "invoice_number": invoice.get("InvoiceNumber") or "",
            "receipt_details": "",
            "slot_datetime": slot_text,
            "payment_datetime": _fmt_british(dt.datetime.now(dt.timezone.utc).isoformat())
            if paid_immediately
            else "N/A",
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
        if not admin_creds or not sales_stats_fields:
            print(f"Sales row skipped for {event_key}: admin_creds={bool(admin_creds)} sales_stats_fields={len(sales_stats_fields) if sales_stats_fields else 0}", flush=True)
            return state

        sales_lines = extract_sales_lines(event.get("description"))
        if not sales_lines:
            print(f"Sales row skipped for {event_key}: no sales lines parsed from description", flush=True)
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
                    obj = dt.datetime.fromisoformat(iso_str)
                    return obj.strftime("%d/%m/%Y %H:%M")
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

        payload_rows: list[dict] = []
        for idx, line in enumerate(sales_lines, start=1):
            ex_vat = round(
                float(line.get("UnitAmount") or 0.0) * float(line.get("Quantity") or 1.0),
                2,
            )
            inc_vat = round(
                ex_vat * (1.2 if (line.get("TaxType") or "").upper() == "OUTPUT2" else 1.0),
                2,
            )
            payload_rows.append(
                {
                    "event_key": f"{event_key}:sales:{idx}",
                    "payload": {
                        "submitter": submitter_display
                        or (event.get("creator", {}) or {}).get("email")
                        or (event.get("organizer", {}) or {}).get("email")
                        or "",
                        "customer": customer_fields.get("name") or "",
                        "invoice_number": invoice_number_display,
                        "slot_datetime": slot_text,
                        "payment_method": payment_method.upper() if payment_method else "",
                        "sales_item_desc": f"{line.get('Description') or ''} = £{ex_vat:.2f} ex VAT",
                        "sales_item_ex_vat": f"{ex_vat:.2f}",
                        "sales_item_inc_vat": f"{inc_vat:.2f}",
                        "sales_total_ex_vat": f"{sales_total_ex:.2f}",
                    },
                }
            )

        if not spreadsheet_id:
            print(f"Sales row skipped/queued for {event_key}: no sales sheet mapped for calendar '{calendar_id}' (cal_mapping keys={list(cal_mapping.keys())})", flush=True)
            backlog = get_sales_backlog(config.admin_db_file)
            row = {
                "event_key": event_key,
                "calendar_id": calendar_id,
                "submitter_email": submitter_email.lower(),
                "stats_fields": sales_stats_fields,
                "rows": payload_rows,
                "event_id_display": event_id_display,
                "invoice_id": invoice_id,
                "payment_method": payment_method,
                "sales_total_ex": sales_total_ex,
                "sales_total_inc": sales_total_inc,
            }
            replaced = False
            for idx, existing in enumerate(backlog):
                if existing.get("event_key") == event_key:
                    backlog[idx] = row
                    replaced = True
                    break
            if not replaced:
                backlog.append(row)
                print(
                    f"Sales row queued for {event_key}: no sales sheet mapped for calendar {calendar_id}",
                    flush=True,
                )
            set_sales_backlog(config.admin_db_file, backlog)
            return state
        print(f"Sales row writing for {event_key}: spreadsheet={spreadsheet_id} sheet={sheet_name} lines={len(sales_lines)}", flush=True)

        marker = (
            f"{invoice_id}:{payment_method}:sales:{spreadsheet_id}:{sheet_name}:{len(sales_lines)}:{sales_total_ex:.2f}:{sales_total_inc:.2f}"
        ).upper()
        if get_sales_log_marker(state, event_key) == marker:
            return state

        try:
            ensure_header(
                admin_creds,
                spreadsheet_id=spreadsheet_id,
                sheet_name=sheet_name,
                stats_fields=sales_stats_fields,
            )
            for row in payload_rows:
                append_stats_row(
                    admin_creds,
                    spreadsheet_id=spreadsheet_id,
                    sheet_name=sheet_name,
                    event_key=row["event_key"],
                    stats_fields=sales_stats_fields,
                    payload=row["payload"],
                    event_id_display=event_id_display,
                )
            print(f"Sales rows appended for {event_key}: {len(sales_lines)} item(s)")
            _feed.push(f"Sales logged ({len(sales_lines)} item(s)) for \"{event.get('summary', event_key)}\"", "success")
            backlog = get_sales_backlog(config.admin_db_file)
            backlog = [r for r in backlog if r.get("event_key") != event_key]
            set_sales_backlog(config.admin_db_file, backlog)
            return set_sales_log_marker(state, event_key, marker)
        except Exception as exc:
            print(f"Sales sheet append failed for {event_key}: {exc}")
            backlog = get_sales_backlog(config.admin_db_file)
            row = {
                "event_key": event_key,
                "calendar_id": calendar_id,
                "submitter_email": submitter_email.lower(),
                "stats_fields": sales_stats_fields,
                "rows": payload_rows,
                "event_id_display": event_id_display,
                "invoice_id": invoice_id,
                "payment_method": payment_method,
                "sales_total_ex": sales_total_ex,
                "sales_total_inc": sales_total_inc,
            }
            replaced = False
            for idx, existing in enumerate(backlog):
                if existing.get("event_key") == event_key:
                    backlog[idx] = row
                    replaced = True
                    break
            if not replaced:
                backlog.append(row)
            set_sales_backlog(config.admin_db_file, backlog)
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

        cash_stats_fields = [f for f in stats_fields if f != "paid_status"]
        if not cash_stats_fields:
            cash_stats_fields = [f for f in DEFAULT_STATS_FIELDS if f != "paid_status"]

        calendar_id = (event.get("_calendar_id") or config.google_calendar_id or "").strip()
        cal_mapping = get_calendar_cash_sheets(config.admin_db_file)
        route = cal_mapping.get(calendar_id) or {}
        spreadsheet_id = str(route.get("spreadsheet_id", "")).strip()
        sheet_name = str(route.get("sheet_name", "Sheet1")).strip() or "Sheet1"

        invoice = {}
        if xero_client:
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

        start = (event.get("start", {}) or {}).get("dateTime") or (event.get("start", {}) or {}).get("date") or ""
        end = (event.get("end", {}) or {}).get("dateTime") or (event.get("end", {}) or {}).get("date") or ""
        slot_text = f"{start} – {end}".strip(" –") if start != end else start
        customer_fields = parse_customer_fields(event.get("description"))
        payload = {
            "submitter": submitter_display or submitter_email,
            "customer": customer_fields.get("name") or "",
            "invoice_number": invoice.get("InvoiceNumber") or "",
            "receipt_details": "",
            "slot_datetime": slot_text,
            "payment_datetime": dt.datetime.now(dt.timezone.utc).strftime("%d/%m/%Y %H:%M"),
            "payment_method": "CASH",
            "job_cost_ex_vat": subtotal if subtotal is not None else "",
            "job_cost_inc_vat": total if total is not None else "",
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
            payload = row.get("payload") or {}
            event_key = str(row.get("event_key", "")).strip()
            event_id_display = str(row.get("event_id_display", "")).strip()
            if not event_key or not isinstance(payload, dict):
                continue
            try:
                ensure_header(
                    admin_creds,
                    spreadsheet_id=spreadsheet_id,
                    sheet_name=sheet_name,
                    stats_fields=[str(s) for s in stats] or [f for f in DEFAULT_STATS_FIELDS if f != "paid_status"],
                )
                append_stats_row(
                    admin_creds,
                    spreadsheet_id=spreadsheet_id,
                    sheet_name=sheet_name,
                    event_key=event_key,
                    stats_fields=[str(s) for s in stats] or [f for f in DEFAULT_STATS_FIELDS if f != "paid_status"],
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
                    if not event_key_row or not isinstance(payload, dict):
                        continue
                    append_stats_row(
                        admin_creds,
                        spreadsheet_id=spreadsheet_id,
                        sheet_name=sheet_name,
                        event_key=event_key_row,
                        stats_fields=[str(s) for s in stats] or DEFAULT_SALES_STATS_FIELDS,
                        payload=payload,
                        event_id_display=event_id_display,
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

    while True:
        now = dt.datetime.now(dt.timezone.utc)
        # Rebuild Xero client only when the cached one is stale or missing.
        _now_ts_for_xero = time.time()
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
        time_min = now - dt.timedelta(days=365)
        time_max = now + dt.timedelta(days=365)
        active_calendars = get_active_calendars(
            config.admin_db_file, config.google_calendar_id
        )

        # Auto-manage Google Calendar watches — run at most once per hour,
        # or immediately when calendar settings change (triggered by the settings page).
        _now_ts = time.time()
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
        calendar_fetch_failed = False
        # Intentionally overlap the updated_min window for reliability.
        # This prevents races where a calendar change happens right after a fetch
        # but before last_sync is advanced.
        query_updated_min = last_sync - _poll_overlap
        for calendar_id in active_calendars:
            try:
                cal_events = list_recent_events(
                    config=config,
                    updated_min=query_updated_min,
                    time_min=time_min,
                    time_max=time_max,
                    calendar_id=calendar_id,
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
                e["_calendar_id"] = calendar_id
                events.append(e)
        had_changes = bool(events)

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

        state = _flush_sheet_backlog(admin_creds, sheet_target, state)
        _flush_cash_backlog(admin_creds)
        _flush_sales_backlog(admin_creds)

        for event in events:
            try:
                event_id = event.get("id") or ""
                calendar_id = event.get("_calendar_id") or config.google_calendar_id
                event_key = f"{calendar_id}:{event_id}"
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
                # If user edited a prefilled entry (before submit), switch title dot
                # from blue to orange once.
                if (
                    event.get("id")
                    and is_prefilled(state, event_key)
                    and not has_done
                    and not has_send
                ):
                    prefill_marker = get_processed_update_marker(state, event_key) or ""
                    event_updated_now = event.get("updated") or ""
                    if prefill_marker and event_updated_now and event_updated_now != prefill_marker:
                        updated = safe_update(
                            event_id=event.get("id"),
                            description=event.get("description") or "",
                            label="Status updated",
                            summary_status="orange",
                            current_summary=event.get("summary"),
                            calendar_id=calendar_id,
                        )
                        if updated:
                            event["updated"] = updated.get("updated", event_updated_now)
                            state = set_processed_update_marker(
                                state,
                                event_key,
                                event["updated"] or event_updated_now,
                            )
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
                    if (
                        last_processed_update
                        and event_updated == last_processed_update
                        and not pending_draft
                    ):
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
                            address = parse_event_address(event.get("location"))
                            errors = validate_customer_fields(customer)
                            blocking_errors = {
                                k: v for k, v in errors.items() if k != "phone"
                            }
                            if not blocking_errors and customer.get("name"):
                                contact_result = xero_client.ensure_contact(
                                    name=customer.get("name", ""),
                                    email=customer.get("email", ""),
                                    phone=customer.get("phone", ""),
                                    address=address if address else None,
                                )
                                contact = contact_result.get("contact")
                                if contact and contact.get("ContactID"):
                                    existing_contact_id = contact.get("ContactID")
                                    state = set_contact_for_event(
                                        state, event_key, existing_contact_id
                                    )
                                    state = set_contact_fingerprint(
                                        state,
                                        event_key,
                                        f"{customer.get('name','')}|{customer.get('email','')}|{customer.get('phone','')}|{address or {}}",
                                    )
                                    print(
                                        f"Contact processed for event {event['id']}: {existing_contact_id}"
                                    )
                                    _feed.push(
                                        f"Customer saved to Xero: {customer.get('name', '')}",
                                        "success",
                                    )
                                else:
                                    print(f"Event {event_id}: contact not saved (no ContactID)")
                            elif blocking_errors:
                                print(f"Event {event_id}: contact validation failed {blocking_errors}")
                        if existing_contact_id and xero_client:
                            if event_updated and event_updated != last_contact_update:
                                customer = parse_customer_fields(event.get("description"))
                                address = parse_event_address(event.get("location"))
                                errors = validate_customer_fields(customer)
                                blocking_errors = {
                                    k: v for k, v in errors.items() if k != "phone"
                                }
                                if not blocking_errors:
                                    fingerprint = (
                                        f"{customer.get('name','')}"
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
                                if not invoice_id:
                                    print(f"Event {event_id}: creating draft invoice")
                                    _feed.push(f"Creating invoice in Xero for \"{event.get('summary', event_id)}\"…", "info")
                                    details = extract_event_details(event)
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
                                            event.get("description") or "",
                                            subtotal,
                                            total,
                                            sent=False,
                                            include_prompt=True,
                                            submitter=submitter_display,
                                            submitted_at=submitted_at_display,
                                        )
                                        if updated_description != (event.get("description") or ""):
                                            updated = safe_update(
                                                event_id=event.get("id"),
                                                description=updated_description,
                                                label="Invoice summary",
                                                summary_status="yellow",
                                                current_summary=event.get("summary"),
                                                calendar_id=calendar_id,
                                            )
                                            if updated:
                                                event["description"] = updated_description
                                                event_updated = updated.get("updated") or event_updated
                                elif event_updated and event_updated != last_invoice_update:
                                    mutable, status = _is_invoice_mutable(invoice_id)
                                    if not mutable:
                                        print(
                                            f"Event {event_id}: skip invoice update, status={status}"
                                        )
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
                                                event.get("description") or "",
                                                subtotal,
                                                total,
                                                sent=False,
                                                include_prompt=True,
                                                submitter=submitter_display,
                                                submitted_at=submitted_at_display,
                                            )
                                            if updated_description != (event.get("description") or ""):
                                                updated = safe_update(
                                                    event_id=event.get("id"),
                                                    description=updated_description,
                                                    label="Invoice summary",
                                                    summary_status="yellow",
                                                    current_summary=event.get("summary"),
                                                    calendar_id=calendar_id,
                                                )
                                                if updated:
                                                    event["description"] = updated_description
                                                    event_updated = updated.get("updated") or event_updated
                                        state = set_invoice_update_marker(
                                            state, event_key, event_updated
                                        )
                            elif has_done and not existing_contact_id:
                                print(f"Event {event_id}: skipping invoice (no contact_id)")
                            elif has_done and not xero_client:
                                print(f"Event {event_id}: skipping invoice (xero not configured)")

                            # If SEND keyword is present, mark as sent in notes.
                            if (
                                has_send
                                and invoice_lines
                                and xero_client
                                and not is_invoice_sent(state, event_key)
                                and get_invoice_for_event(state, event_key)
                            ):
                                invoice_id = get_invoice_for_event(state, event_key)
                                pay_mode = payment_choice(event.get("description"))
                                if pay_mode not in {"card", "invoice", "cash"}:
                                    failed_description = upsert_send_failure(
                                        event.get("description") or "",
                                        "Choose PAYMENT TYPE as CARD or INVOICE before SEND",
                                        invoice_url=(
                                            xero_client.get_online_invoice_url(invoice_id)
                                            if invoice_id
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
                                    try:
                                        xero_client.delete_draft_invoice(invoice_id)
                                    except Exception as exc:
                                        print(f"Event {event_id}: failed to remove draft for cash flow: {exc}")
                                        fail_reason = str(exc).splitlines()[0][:220]
                                        failed_description = upsert_send_failure(
                                            event.get("description") or "",
                                            f"Cash finalise failed: {fail_reason}",
                                            invoice_url=None,
                                            submitter=submitter_display,
                                            submitted_at=submitted_at_display,
                                        )
                                        updated = safe_update(
                                            event_id=event.get("id"),
                                            description=failed_description,
                                            label="Cash finalise failed",
                                            summary_status="yellow",
                                            current_summary=event.get("summary"),
                                            calendar_id=calendar_id,
                                        )
                                        if updated:
                                            event["description"] = failed_description
                                            event_updated = updated.get("updated") or event_updated
                                        state = set_processed_update_marker(state, event_key, event_updated)
                                        continue

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
                                    state = mark_invoice_sent(state, event_key)
                                    state = set_invoice_for_event(state, event_key, "")
                                    state = set_invoice_update_marker(state, event_key, event_updated)
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
                                    print(f"Cash payment finalised for event {event_id}: draft {invoice_id} deleted")
                                    _feed.push(f"Cash payment logged for \"{event.get('summary', event_id)}\"", "success")
                                    state = set_processed_update_marker(state, event_key, event_updated)
                                    continue
                                invoice_url = None
                                if invoice_id:
                                    try:
                                        xero_client.authorize_invoice(invoice_id)
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
                                        if pay_mode == "card":
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
                        address_debug = parse_event_address_debug(event.get("location"))
                        address = address_debug.get("address")
                        errors = validate_customer_fields(customer)
                        blocking_errors = {k: v for k, v in errors.items() if k != "phone"}
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
                            if customer.get("name"):
                                contact_result = xero_client.ensure_contact(
                                    name=customer.get("name", ""),
                                    email=customer.get("email", ""),
                                    phone=customer.get("phone", ""),
                                    address=address if address else None,
                                )
                                contact = contact_result.get("contact")
                                _cname = customer.get("name", "")
                                if contact_result.get("address_split"):
                                    _feed.push(
                                        f"New address for {_cname} — archived as \"{contact_result.get('orig_name', '')}\" and created \"{contact_result.get('new_name', '')}\"",
                                        "info",
                                    )
                                elif contact_result.get("created"):
                                    _feed.push(
                                        f"New customer saved to Xero: {_cname}",
                                        "success",
                                    )
                                else:
                                    _feed.push(
                                        f"Customer matched in Xero: {_cname}",
                                        "info",
                                    )
                                if contact and contact.get("ContactID"):
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
                            ):
                                event_updated = event.get("updated") or ""
                                invoice_id = get_invoice_for_event(state, event_key)
                                contact_ref = (
                                    {"ContactID": contact.get("ContactID")}
                                    if contact and contact.get("ContactID")
                                    else None
                                )
                                if not invoice_id:
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
                                            state = set_invoice_update_marker(
                                                state, event_key, event_updated
                                            )
                                            save_state(config.state_file, state)
                                    subtotal, total = _extract_totals(result)
                                    if subtotal is not None and total is not None:
                                        updated_description = upsert_invoice_summary(
                                            event.get("description") or "",
                                            subtotal,
                                            total,
                                            sent=False,
                                            include_prompt=True,
                                            submitter=submitter_display,
                                            submitted_at=submitted_at_display,
                                        )
                                        if updated_description != (event.get("description") or ""):
                                            updated = safe_update(
                                                event_id=event.get("id"),
                                                description=updated_description,
                                                label="Invoice summary",
                                                summary_status="yellow",
                                                current_summary=event.get("summary"),
                                                calendar_id=calendar_id,
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
                                    if event_updated and event_updated != last_invoice_update:
                                        mutable, status = _is_invoice_mutable(invoice_id)
                                        if not mutable:
                                            print(
                                                f"Event {event.get('id')}: skip invoice update, status={status}"
                                            )
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
                                                    event.get("description") or "",
                                                    subtotal,
                                                    total,
                                                    sent=False,
                                                    include_prompt=True,
                                                    submitter=submitter_display,
                                                    submitted_at=submitted_at_display,
                                                )
                                                if updated_description != (event.get("description") or ""):
                                                    updated = safe_update(
                                                        event_id=event.get("id"),
                                                        description=updated_description,
                                                        label="Invoice summary",
                                                        summary_status="yellow",
                                                        current_summary=event.get("summary"),
                                                        calendar_id=calendar_id,
                                                    )
                                                    if updated:
                                                        event["description"] = updated_description
                                                        event_updated = updated.get("updated") or event_updated
                                            state = set_invoice_update_marker(
                                                state, event_key, event_updated
                                            )

                            if has_send and invoice_lines and get_invoice_for_event(state, event_key):
                                invoice_id = get_invoice_for_event(state, event_key)
                                pay_mode = payment_choice(event.get("description"))
                                if pay_mode not in {"card", "invoice", "cash"}:
                                    failed_description = upsert_send_failure(
                                        event.get("description") or "",
                                        "Choose PAYMENT TYPE as CARD or INVOICE before SEND",
                                        invoice_url=(
                                            xero_client.get_online_invoice_url(invoice_id)
                                            if invoice_id
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
                                    try:
                                        xero_client.delete_draft_invoice(invoice_id)
                                    except Exception as exc:
                                        print(f"Event {event.get('id')}: failed to remove draft for cash flow: {exc}")
                                        fail_reason = str(exc).splitlines()[0][:220]
                                        failed_description = upsert_send_failure(
                                            event.get("description") or "",
                                            f"Cash finalise failed: {fail_reason}",
                                            invoice_url=None,
                                            submitter=submitter_display,
                                            submitted_at=submitted_at_display,
                                        )
                                        updated = safe_update(
                                            event_id=event.get("id"),
                                            description=failed_description,
                                            label="Cash finalise failed",
                                            summary_status="yellow",
                                            current_summary=event.get("summary"),
                                            calendar_id=calendar_id,
                                        )
                                        if updated:
                                            event["description"] = failed_description
                                            event_updated = updated.get("updated") or event_updated
                                        state = set_processed_update_marker(state, event_key, event_updated)
                                        continue

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
                                    state = mark_invoice_sent(state, event_key)
                                    state = set_invoice_for_event(state, event_key, "")
                                    state = set_invoice_update_marker(state, event_key, event_updated)
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
                                    print(f"Cash payment finalised for event {event.get('id')}: draft {invoice_id} deleted")
                                    _feed.push(f"Cash payment logged for \"{event.get('summary', event.get('id'))}\"", "success")
                                    state = set_processed_update_marker(state, event_key, event_updated)
                                    continue
                                invoice_url = None
                                if invoice_id:
                                    try:
                                        xero_client.authorize_invoice(invoice_id)
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
                                        if pay_mode == "card":
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
