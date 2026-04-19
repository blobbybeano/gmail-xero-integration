from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from google.oauth2.credentials import Credentials

from .google_admin import build_sheets_service_from_creds


STAT_LABELS = {
    "submitter": "Submitted By",
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
    if values:
        return
    headers = ["Logged At", "Event ID"] + [STAT_LABELS.get(f, f) for f in stats_fields]
    (
        service.spreadsheets()
        .values()
        .update(
            spreadsheetId=spreadsheet_id,
            range=_header_cell(resolved_sheet_name),
            valueInputOption="USER_ENTERED",
            body={"values": [headers]},
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
) -> None:
    service = build_sheets_service_from_creds(creds)
    resolved_sheet_name = _resolve_existing_sheet_name(service, spreadsheet_id, sheet_name)
    row = [datetime.now(timezone.utc).isoformat(), event_key]
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
