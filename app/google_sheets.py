from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from zoneinfo import ZoneInfo
import re

from google.oauth2.credentials import Credentials

from .google_admin import build_sheets_service_from_creds


STAT_LABELS = {
    "diary_entry_name": "Diary Entry",
    "submitter": "Submitted By",
    "customer": "Customer",
    "invoice_number": "Invoice Number",
    "receipt_details": "Receipt Details",
    "slot_datetime": "Diary Slot",
    "payment_datetime": "Payment Date/Time",
    "payment_method": "Payment Method",
    "paid_status": "Paid",
    "job_cost_ex_vat": "Job Cost Ex VAT",
    "job_cost_inc_vat": "Job Cost Inc VAT",
    "job_cost": "Job Cost",
    "sales_item_desc": "Sales Item",
    "sales_total_ex_vat": "Sales Total",
}

_FIXED_HEADERS = ["Logged At", "Event ID"]
LONDON_TZ = ZoneInfo("Europe/London")


def _now_london_str() -> str:
    return datetime.now(timezone.utc).astimezone(LONDON_TZ).strftime("%d/%m/%Y %H:%M")


def _col_letter(idx: int) -> str:
    result = ""
    while True:
        result = chr(ord("A") + idx % 26) + result
        idx = idx // 26 - 1
        if idx < 0:
            break
    return result


def _sheet_range(sheet_name: str) -> str:
    safe = sheet_name.replace("'", "''")
    return f"'{safe}'!A:Z"


def _header_range(sheet_name: str) -> str:
    safe = sheet_name.replace("'", "''")
    return f"'{safe}'!A1:Z1"


def _sheet_id_by_title(service, spreadsheet_id: str, sheet_title: str) -> int | None:
    meta = (
        service.spreadsheets()
        .get(
            spreadsheetId=spreadsheet_id,
            fields="sheets.properties.sheetId,sheets.properties.title",
        )
        .execute()
    )
    for sheet in (meta.get("sheets") or []):
        props = sheet.get("properties", {}) or {}
        title = str(props.get("title") or "")
        if title == sheet_title:
            try:
                return int(props.get("sheetId"))
            except Exception:
                return None
    return None


def _extract_row_number_from_updated_range(updated_range: str) -> int | None:
    """
    Parse row number from A1 ranges like:
      'Sheet1'!A12:J12
    """
    if not updated_range:
        return None
    m = re.search(r"![A-Z]+(\d+)(?::[A-Z]+(\d+))?$", updated_range)
    if not m:
        return None
    try:
        return int(m.group(1))
    except Exception:
        return None


def _resolve_existing_sheet_name(
    service,
    spreadsheet_id: str,
    preferred_sheet_name: str,
) -> str:
    meta = (
        service.spreadsheets()
        .get(
            spreadsheetId=spreadsheet_id,
            fields="sheets.properties.title",
        )
        .execute()
    )
    titles = [
        ((s.get("properties", {}) or {}).get("title") or "").strip()
        for s in (meta.get("sheets") or [])
    ]
    titles = [t for t in titles if t]
    if not titles:
        return preferred_sheet_name

    preferred = preferred_sheet_name.strip()
    if preferred in titles:
        return preferred

    lower_map = {t.lower(): t for t in titles}
    if preferred.lower() in lower_map:
        return lower_map[preferred.lower()]

    return titles[0]


def _read_header(service, spreadsheet_id: str, resolved_sheet_name: str) -> list[str]:
    resp = (
        service.spreadsheets()
        .values()
        .get(
            spreadsheetId=spreadsheet_id,
            range=_header_range(resolved_sheet_name),
        )
        .execute()
    )
    values = resp.get("values", [])
    return values[0] if values else []


def ensure_header(
    creds: Credentials,
    spreadsheet_id: str,
    sheet_name: str,
    stats_fields: list[str],
) -> list[str]:
    """
    Ensure the header row exists and contains all expected columns.

    - If no header exists: write the full expected header.
    - If a header already exists: only APPEND any missing columns to the right.
      Existing columns are never moved or removed, so old data rows stay aligned.

    Returns the final header list (existing + any newly added columns).
    """
    service = build_sheets_service_from_creds(creds)
    resolved = _resolve_existing_sheet_name(service, spreadsheet_id, sheet_name)
    existing = _read_header(service, spreadsheet_id, resolved)

    expected_labels = _FIXED_HEADERS + [STAT_LABELS.get(f, f) for f in stats_fields]

    if not existing:
        safe = resolved.replace("'", "''")
        (
            service.spreadsheets()
            .values()
            .update(
                spreadsheetId=spreadsheet_id,
                range=f"'{safe}'!A1",
                valueInputOption="USER_ENTERED",
                body={"values": [expected_labels]},
            )
            .execute()
        )
        return expected_labels

    missing = [lbl for lbl in expected_labels if lbl not in existing]
    if not missing:
        return existing

    next_col = len(existing)
    safe = resolved.replace("'", "''")
    start_letter = _col_letter(next_col)
    end_letter = _col_letter(next_col + len(missing) - 1)
    (
        service.spreadsheets()
        .values()
        .update(
            spreadsheetId=spreadsheet_id,
            range=f"'{safe}'!{start_letter}1:{end_letter}1",
            valueInputOption="USER_ENTERED",
            body={"values": [missing]},
        )
        .execute()
    )
    return existing + missing


def append_stats_row(
    creds: Credentials,
    spreadsheet_id: str,
    sheet_name: str,
    event_key: str,
    stats_fields: list[str],
    payload: dict[str, Any],
    event_id_display: str = "",
    dedupe_signature: dict[str, Any] | None = None,
    update_existing: bool = False,
) -> None:
    """
    Append a data row to the sheet, placing each value under its correct column
    header regardless of column order. New columns added later won't misalign
    existing rows.

    When update_existing is true, a row matching dedupe_signature is updated in
    place instead of skipped. Core calendar/Xero logging leaves this False.
    """
    service = build_sheets_service_from_creds(creds)
    resolved = _resolve_existing_sheet_name(service, spreadsheet_id, sheet_name)
    header = _read_header(service, spreadsheet_id, resolved)

    if not header:
        return

    col_index: dict[str, int] = {label: i for i, label in enumerate(header)}

    row: list[str] = [""] * len(header)

    def _set(label: str, value: Any) -> None:
        idx = col_index.get(label)
        if idx is not None:
            row[idx] = "" if value is None else str(value)

    _set("Logged At", _now_london_str())
    _set("Event ID", event_id_display or event_key)

    # Fill columns based on the configured field list so custom/advanced
    # fields (e.g. sales_item_desc) are written too.
    for field in stats_fields:
        label = STAT_LABELS.get(field, field)
        if field in payload:
            _set(label, payload[field])

    # Backward-compat fallback: if payload includes known labels not present in
    # stats_fields, still write them when the column exists.
    for field, label in STAT_LABELS.items():
        if field in payload and field not in stats_fields:
            _set(label, payload[field])

    safe = resolved.replace("'", "''")

    def _update_existing_row(row_number: int, existing_row: list[Any]) -> None:
        logged_at_idx = col_index.get("Logged At")
        if logged_at_idx is not None and logged_at_idx < len(existing_row):
            row[logged_at_idx] = str(existing_row[logged_at_idx])
        padded_existing = [
            str(existing_row[i]) if i < len(existing_row) else ""
            for i in range(len(header))
        ]
        if padded_existing == row:
            return
        end_letter = _col_letter(len(header) - 1)
        (
            service.spreadsheets()
            .values()
            .update(
                spreadsheetId=spreadsheet_id,
                range=f"'{safe}'!A{row_number}:{end_letter}{row_number}",
                valueInputOption="USER_ENTERED",
                body={"values": [row]},
            )
            .execute()
        )

    # Optional idempotency guard: if a row already exists with the same
    # signature columns, skip or update it to prevent duplicate submissions.
    if dedupe_signature:
        existing_values = (
            service.spreadsheets()
            .values()
            .get(spreadsheetId=spreadsheet_id, range=f"'{safe}'!A:Z")
            .execute()
            .get("values", [])
        )
        if existing_values:
            for row_number, existing_row in enumerate(existing_values[1:], start=2):
                matched = True
                for label, expected in dedupe_signature.items():
                    idx = col_index.get(label)
                    if idx is None:
                        matched = False
                        break
                    actual_val = existing_row[idx] if idx < len(existing_row) else ""
                    if str(actual_val).strip() != str(expected).strip():
                        matched = False
                        break
                if matched:
                    if update_existing:
                        _update_existing_row(row_number, existing_row)
                    return

    append_result = (
        service.spreadsheets()
        .values()
        .append(
            spreadsheetId=spreadsheet_id,
            range=f"'{safe}'!A:A",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": [row]},
        )
        .execute()
    )

    # Make unpaid marker visually clear on master sheet:
    # when Payment Date/Time is N/A, render it red.
    payment_col = col_index.get("Payment Date/Time")
    if payment_col is not None:
        payment_raw = str(row[payment_col] if payment_col < len(row) else "").strip()
        payment_norm = payment_raw.upper().replace("🔴", "").strip()
        if payment_norm == "N/A":
            updated_range = ((append_result.get("updates") or {}).get("updatedRange") or "")
            row_num = _extract_row_number_from_updated_range(updated_range)
            if row_num:
                col_letter = _col_letter(payment_col)
                # Keep visible value clean as plain "N/A".
                (
                    service.spreadsheets()
                    .values()
                    .update(
                        spreadsheetId=spreadsheet_id,
                        range=f"'{safe}'!{col_letter}{row_num}",
                        valueInputOption="USER_ENTERED",
                        body={"values": [["N/A"]]},
                    )
                    .execute()
                )
                sheet_id = _sheet_id_by_title(service, spreadsheet_id, resolved)
                if sheet_id is not None:
                    service.spreadsheets().batchUpdate(
                        spreadsheetId=spreadsheet_id,
                        body={
                            "requests": [
                                {
                                    "repeatCell": {
                                        "range": {
                                            "sheetId": sheet_id,
                                            "startRowIndex": row_num - 1,
                                            "endRowIndex": row_num,
                                            "startColumnIndex": payment_col,
                                            "endColumnIndex": payment_col + 1,
                                        },
                                        "cell": {
                                            "userEnteredFormat": {
                                                "textFormat": {
                                                    "foregroundColor": {
                                                        "red": 0.84,
                                                        "green": 0.15,
                                                        "blue": 0.16,
                                                    }
                                                }
                                            }
                                        },
                                        "fields": "userEnteredFormat.textFormat.foregroundColor",
                                    }
                                }
                            ]
                        },
                    ).execute()


def update_invoice_paid_in_sheet(
    creds: Credentials,
    spreadsheet_id: str,
    sheet_name: str,
    invoice_number: str,
) -> Literal["updated", "already_paid", "not_found", "missing_headers"]:
    """
    Find rows whose 'Invoice Number' matches and mark payment columns as paid.
    - If a 'Paid' column exists: set it to 'Paid'
    - If a 'Payment Date/Time' column exists: set current timestamp
    Returns:
    - "updated": one or more cells were changed
    - "already_paid": invoice row exists and was already marked paid
    - "not_found": no row matched invoice number
    - "missing_headers": required columns are missing
    """
    if not invoice_number:
        return "not_found"
    service = build_sheets_service_from_creds(creds)
    resolved = _resolve_existing_sheet_name(service, spreadsheet_id, sheet_name)

    resp = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range=_sheet_range(resolved))
        .execute()
    )
    rows = resp.get("values", [])
    if len(rows) < 2:
        return "not_found"

    header = rows[0]
    try:
        inv_col = header.index("Invoice Number")
    except ValueError:
        return "missing_headers"
    paid_col = header.index("Paid") if "Paid" in header else None
    paid_dt_col = (
        header.index("Payment Date/Time") if "Payment Date/Time" in header else None
    )
    if paid_col is None and paid_dt_col is None:
        return "missing_headers"

    safe = resolved.replace("'", "''")
    updates = []
    payment_dt_rows_to_black: list[int] = []
    matched_row = False
    now_str = _now_london_str()
    for row_idx, row in enumerate(rows[1:], start=2):
        cell_val = row[inv_col].strip() if len(row) > inv_col else ""
        if cell_val.upper() == invoice_number.upper():
            matched_row = True
            row_needs_update = False
            if paid_col is not None:
                current_paid = row[paid_col].strip() if len(row) > paid_col else ""
            else:
                current_paid = "Paid"
            if current_paid != "Paid" and paid_col is not None:
                updates.append({
                    "range": f"'{safe}'!{_col_letter(paid_col)}{row_idx}",
                    "values": [["Paid"]],
                })
                row_needs_update = True
            if paid_dt_col is not None:
                current_paid_dt = (
                    row[paid_dt_col].strip() if len(row) > paid_dt_col else ""
                )
                normalized_paid_dt = current_paid_dt.upper().replace("🔴", "").strip()
                if not current_paid_dt or normalized_paid_dt == "N/A":
                    updates.append({
                        "range": f"'{safe}'!{_col_letter(paid_dt_col)}{row_idx}",
                        "values": [[now_str]],
                    })
                    payment_dt_rows_to_black.append(row_idx)
                    row_needs_update = True

    if not matched_row:
        return "not_found"
    if not updates:
        return "already_paid"

    service.spreadsheets().values().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"valueInputOption": "USER_ENTERED", "data": updates},
    ).execute()

    # If we replaced an unpaid N/A marker, reset text color back to neutral.
    if paid_dt_col is not None and payment_dt_rows_to_black:
        sheet_id = _sheet_id_by_title(service, spreadsheet_id, resolved)
        if sheet_id is not None:
            requests = []
            for row_idx in payment_dt_rows_to_black:
                requests.append(
                    {
                        "repeatCell": {
                            "range": {
                                "sheetId": sheet_id,
                                "startRowIndex": row_idx - 1,
                                "endRowIndex": row_idx,
                                "startColumnIndex": paid_dt_col,
                                "endColumnIndex": paid_dt_col + 1,
                            },
                            "cell": {
                                "userEnteredFormat": {
                                    "textFormat": {
                                        "foregroundColor": {
                                            "red": 0.0,
                                            "green": 0.0,
                                            "blue": 0.0,
                                        }
                                    }
                                }
                            },
                            "fields": "userEnteredFormat.textFormat.foregroundColor",
                        }
                    }
                )
            if requests:
                service.spreadsheets().batchUpdate(
                    spreadsheetId=spreadsheet_id,
                    body={"requests": requests},
                ).execute()
    return "updated"


def backfill_submitter_in_sheet(
    creds: Credentials,
    spreadsheet_id: str,
    sheet_name: str,
    aliases: dict[str, str],
) -> int:
    """Find rows where 'Submitted By' matches a known email and replace with the alias."""
    if not aliases:
        return 0
    service = build_sheets_service_from_creds(creds)
    resolved = _resolve_existing_sheet_name(service, spreadsheet_id, sheet_name)

    resp = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range=_sheet_range(resolved))
        .execute()
    )
    rows = resp.get("values", [])
    if len(rows) < 2:
        return 0

    header = rows[0]
    try:
        sub_col = header.index("Submitted By")
    except ValueError:
        return 0

    lower_aliases = {k.lower(): v for k, v in aliases.items()}
    safe = resolved.replace("'", "''")
    col_letter = _col_letter(sub_col)

    updates = []
    for row_idx, row in enumerate(rows[1:], start=2):
        if len(row) <= sub_col:
            continue
        current = row[sub_col].strip()
        new_val = lower_aliases.get(current.lower())
        if new_val and new_val != current:
            updates.append({
                "range": f"'{safe}'!{col_letter}{row_idx}",
                "values": [[new_val]],
            })

    if not updates:
        return 0

    service.spreadsheets().values().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"valueInputOption": "USER_ENTERED", "data": updates},
    ).execute()
    return len(updates)
