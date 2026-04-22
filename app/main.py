from __future__ import annotations

import datetime as dt
import time

from .admin_store import (
    add_seen_submitter,
    get_active_calendars,
    get_enabled,
    get_google_watches,
    get_seen_submitters,
    get_sheet_target,
    get_stats_fields,
    get_submitter_aliases,
    init_admin_store,
    set_google_watch,
    delete_google_watch,
)
from .config import load_config
from .event_processor import (
    apply_validation_hints,
    done_choice_is_yes,
    ensure_notes_template,
    normalize_user_sections,
    send_choice_is_yes,
    extract_event_details,
    extract_invoice_lines,
    parse_customer_fields,
    parse_event_address,
    parse_event_address_debug,
    payment_choice,
    upsert_send_failure,
    upsert_invoice_summary,
    upsert_send_confirmation,
    validate_customer_fields,
)
from .google_calendar import (
    list_recent_events,
    update_event_description,
    register_calendar_watch,
    stop_calendar_watch,
    RateLimitError,
)
from .trigger import wait_for_poll
from .google_sheets import append_stats_row, ensure_header
from .google_admin import load_admin_credentials
from .state import (
    get_contact_for_event,
    get_contact_update_marker,
    get_contact_fingerprint,
    get_invoice_for_event,
    get_invoice_update_marker,
    get_last_sync,
    get_processed_update_marker,
    get_sheet_log_marker,
    is_prefilled,
    is_invoice_sent,
    is_processed,
    load_state,
    mark_prefilled,
    mark_invoice_sent,
    mark_processed,
    save_state,
    set_contact_update_marker,
    set_contact_for_event,
    set_contact_fingerprint,
    set_invoice_for_event,
    set_invoice_update_marker,
    set_last_sync,
    set_processed_update_marker,
    set_sheet_log_marker,
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

    xero_client = build_xero_client(config)

    backoff_seconds = max(config.poll_seconds, 5)
    max_backoff = max(backoff_seconds, 60)

    def safe_update(
        event_id: str,
        description: str,
        label: str | None = None,
        calendar_id: str | None = None,
    ):
        try:
            return update_event_description(
                config=config,
                event_id=event_id,
                description=description,
                calendar_id=calendar_id,
            )
        except RateLimitError:
            prefix = f"{label}: " if label else ""
            print(f"{prefix}Google Calendar rate limit hit. Skipping update for {event_id}.")
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
        submitter_display: str,
        admin_creds,
        sheet_target: dict[str, str],
        stats_fields: list[str],
        state: dict,
    ) -> dict:
        if not admin_creds or not stats_fields:
            return state
        spreadsheet_id = sheet_target.get("spreadsheet_id", "").strip()
        sheet_name = sheet_target.get("sheet_name", "Sheet1").strip() or "Sheet1"
        if not spreadsheet_id:
            return state

        marker = f"{invoice_id}:{payment_method}".upper()
        if get_sheet_log_marker(state, event_key) == marker:
            return state

        invoice = {}
        if xero_client:
            try:
                invoice = xero_client.get_invoice(invoice_id)
            except Exception as exc:
                print(f"Sheets: failed to read invoice {invoice_id}: {exc}")
                return state

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

        start = (event.get("start", {}) or {}).get("dateTime") or (event.get("start", {}) or {}).get("date") or ""
        end = (event.get("end", {}) or {}).get("dateTime") or (event.get("end", {}) or {}).get("date") or ""
        start_fmt = _fmt_british(start)
        end_fmt = _fmt_british(end)
        slot_text = f"{start_fmt} – {end_fmt}".strip(" –") if start_fmt != end_fmt else start_fmt
        customer_fields = parse_customer_fields(event.get("description"))
        is_card = payment_method.lower() == "card"
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
            if is_card
            else "N/A",
            "payment_method": payment_method.upper() if is_card else "N/A",
            "paid_status": "Paid" if is_card else "Outstanding",
            "job_cost_ex_vat": invoice.get("SubTotal") or "",
            "job_cost_inc_vat": invoice.get("Total") or "",
        }
        event_id_raw = event.get("id") or ""
        date_part = start.split("T", 1)[0].replace("-", "")
        suffix = event_id_raw[-4:] if event_id_raw else "0000"
        event_id_display = f"GC-{date_part}-{suffix}" if date_part else (event_id_raw or event_key)
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
            return set_sheet_log_marker(state, event_key, marker)
        except Exception as exc:
            print(f"Sheets append failed for {event_key}: {exc}")
            return state

    _was_enabled = True

    while True:
        xero_client = build_xero_client(config)
        now = dt.datetime.now(dt.timezone.utc)

        # Auto-renew Google Calendar watches expiring within 24 hours
        try:
            watches = get_google_watches(config.admin_db_file)
            now_ms = int(now.timestamp() * 1000)
            for cal_id, winfo in list(watches.items()):
                exp_ms = int(winfo.get("expiration_ms") or 0)
                if exp_ms and (exp_ms - now_ms) < 24 * 3600 * 1000:
                    wurl = winfo.get("webhook_url", "")
                    if wurl:
                        try:
                            stop_calendar_watch(config, winfo["channel_id"], winfo["resource_id"])
                        except Exception:
                            pass
                        try:
                            resp = register_calendar_watch(config, cal_id, wurl)
                            set_google_watch(
                                config.admin_db_file, cal_id,
                                resp["id"], resp["resourceId"],
                                int(resp.get("expiration") or 0),
                                webhook_url=wurl,
                            )
                            print(f"[watch] Renewed Google Calendar watch for {cal_id}", flush=True)
                        except Exception as exc:
                            print(f"[watch] Failed to renew watch for {cal_id}: {exc}", flush=True)
        except Exception:
            pass

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
        events: list[dict] = []
        for calendar_id in active_calendars:
            cal_events = list_recent_events(
                config=config,
                updated_min=last_sync,
                time_min=time_min,
                time_max=time_max,
                calendar_id=calendar_id,
            )
            for e in cal_events:
                e["_calendar_id"] = calendar_id
                events.append(e)
        had_changes = bool(events)

        admin_creds = load_admin_credentials(config)
        sheet_target = get_sheet_target(config.admin_db_file)
        stats_fields = get_stats_fields(config.admin_db_file)
        submitter_aliases = get_submitter_aliases(config.admin_db_file)
        sheet_enabled = bool(
            admin_creds
            and sheet_target.get("spreadsheet_id")
            and sheet_target.get("sheet_name")
            and stats_fields
        )
        if sheet_enabled and admin_creds:
            try:
                ensure_header(
                    admin_creds,
                    spreadsheet_id=sheet_target["spreadsheet_id"],
                    sheet_name=sheet_target["sheet_name"],
                    stats_fields=stats_fields,
                )
            except Exception as exc:
                print(f"Sheets header setup failed: {exc}")
                sheet_enabled = False

        for event in events:
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
                            calendar_id=calendar_id,
                        )
                        if updated:
                            print(f"Prefilled notes for new event {updated.get('id')}")
                            event["description"] = updated.get("description", new_description)
                            event["updated"] = updated.get("updated", event.get("updated"))
                    state = mark_prefilled(state, event_key)

            # Process any event with DONE/SEND, regardless of when it was created.
            has_done = done_choice_is_yes(event.get("description"))
            # Only send when user explicitly answers Y/YES.
            has_send = send_choice_is_yes(event.get("description"))
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
                            if pay_mode not in {"card", "invoice"}:
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
                    calendar_id=calendar_id,
                                )
                                if updated:
                                    event["description"] = failed_description
                                    event_updated = updated.get("updated") or event_updated
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
                    calendar_id=calendar_id,
                                        )
                                        if updated:
                                            event["description"] = failed_description
                                            event_updated = updated.get("updated") or event_updated
                                        state = set_processed_update_marker(state, event_key, event_updated)
                                        continue
                                emailed = xero_client.email_invoice(invoice_id)
                                if not emailed:
                                    print(f"Event {event_id}: failed to email invoice {invoice_id}")
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
                            if pay_mode not in {"card", "invoice"}:
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
                    calendar_id=calendar_id,
                                )
                                if updated:
                                    event["description"] = failed_description
                                    event_updated = updated.get("updated") or event_updated
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
                    calendar_id=calendar_id,
                                        )
                                        if updated:
                                            event["description"] = failed_description
                                            event_updated = updated.get("updated") or event_updated
                                        state = set_processed_update_marker(state, event_key, event_updated)
                                        continue
                                emailed = xero_client.email_invoice(invoice_id)
                                if not emailed:
                                    print(f"Event {event.get('id')}: failed to email invoice {invoice_id}")
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

            # Persist state after each event so Ctrl+C doesn't lose progress and
            # cause duplicate retries/drafts on restart.
            save_state(config.state_file, state)

        last_sync = now
        state = set_last_sync(state, now)
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
