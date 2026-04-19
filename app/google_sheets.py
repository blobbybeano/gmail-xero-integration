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
    "slot_datetime": "Diary Slot Date/Time",
    "payment_datetime": "Payment Date/Time",
    "payment_method": "Payment Method",
    "job_cost_ex_vat": "Job Cost Ex VAT",
    "job_cost_inc_vat": "Job Cost Inc VAT",
}


def _sheet_range(sheet_name: str) -> str:
    safe = sheet_name.replace("'", "''")
    return f"'{safe}'!A:Z"


def _header_range(sheet_name: str) -> str:
    safe = sheet_name.replace("'", "''")
    return f"'{safe}'!A1:Z1"


def _header_cell(sheet_name: str) -> str:
    safe = sheet_name.replace("'", "''")
    return f"'{safe}'!A1"


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


def ensure_header(
    creds: Credentials,
    spreadsheet_id: str,
    sheet_name: str,
    stats_fields: list[str],
) -> None:
    service = build_sheets_service_from_creds(creds)
    resolved_sheet_name = _resolve_existing_sheet_name(service, spreadsheet_id, sheet_name)
    header_resp = (
        service.spreadsheets()
        .values()
        .get(
            spreadsheetId=spreadsheet_id,
            range=_header_range(resolved_sheet_name),
        )
        .execute()
    )
    values = header_resp.get("values", [])
    expected = ["Logged At", "Event ID"] + [STAT_LABELS.get(f, f) for f in stats_fields]
    if values and values[0] == expected:
        return
    (
        service.spreadsheets()
        .values()
        .update(
            spreadsheetId=spreadsheet_id,
            range=_header_cell(resolved_sheet_name),
            valueInputOption="USER_ENTERED",
            body={"values": [expected]},
        )
        .execute()
    )


def append_stats_row(
    creds: Credentials,
    spreadsheet_id: str,
    sheet_name: str,
    event_key: str,
    stats_fields: list[str],
    payload: dict[str, Any],
    event_id_display: str = "",
) -> None:
    service = build_sheets_service_from_creds(creds)
    resolved_sheet_name = _resolve_existing_sheet_name(service, spreadsheet_id, sheet_name)
    display_id = event_id_display or event_key
    row = [datetime.now(timezone.utc).isoformat(), display_id]
    for field in stats_fields:
        value = payload.get(field, "")
        row.append("" if value is None else str(value))

    (
        service.spreadsheets()
        .values()
        .append(
            spreadsheetId=spreadsheet_id,
            range=_sheet_range(resolved_sheet_name),
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": [row]},
        )
        .execute()
    )


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
    col_letter = chr(ord("A") + sub_col)

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
