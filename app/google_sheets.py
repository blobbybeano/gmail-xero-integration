from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from google.oauth2.credentials import Credentials

from .google_admin import build_sheets_service_from_creds


STAT_LABELS = {
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
}

_FIXED_HEADERS = ["Logged At", "Event ID"]


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
) -> None:
    """
    Append a data row to the sheet, placing each value under its correct column
    header regardless of column order. New columns added later won't misalign
    existing rows.
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

    _set("Logged At", datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M"))
    _set("Event ID", event_id_display or event_key)

    for field, label in STAT_LABELS.items():
        if field in payload:
            _set(label, payload[field])

    safe = resolved.replace("'", "''")
    (
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


def update_invoice_paid_in_sheet(
    creds: Credentials,
    spreadsheet_id: str,
    sheet_name: str,
    invoice_number: str,
) -> bool:
    """
    Find the row whose 'Invoice Number' matches and set 'Paid' to 'Paid'.
    Returns True if a row was updated.
    """
    if not invoice_number:
        return False
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
        return False

    header = rows[0]
    try:
        inv_col = header.index("Invoice Number")
        paid_col = header.index("Paid")
    except ValueError:
        return False

    safe = resolved.replace("'", "''")
    updates = []
    for row_idx, row in enumerate(rows[1:], start=2):
        cell_val = row[inv_col].strip() if len(row) > inv_col else ""
        if cell_val.upper() == invoice_number.upper():
            current_paid = row[paid_col].strip() if len(row) > paid_col else ""
            if current_paid != "Paid":
                updates.append({
                    "range": f"'{safe}'!{_col_letter(paid_col)}{row_idx}",
                    "values": [["Paid"]],
                })

    if not updates:
        return False

    service.spreadsheets().values().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"valueInputOption": "USER_ENTERED", "data": updates},
    ).execute()
    return True


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
