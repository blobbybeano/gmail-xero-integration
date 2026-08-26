"""Persistent vehicle register for the admin web application."""

from __future__ import annotations

import calendar
import datetime as dt
import sqlite3
import uuid
from pathlib import Path
from typing import Any


DEADLINE_KINDS = ("mot", "tax", "service", "insurance", "rac")
DEADLINE_LABELS = {
    "mot": "MOT",
    "tax": "Vehicle tax",
    "service": "Service",
    "insurance": "Insurance",
    "rac": "RAC / breakdown cover",
}


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def normalise_registration(value: str) -> str:
    return "".join(ch for ch in (value or "").upper() if ch.isalnum())


def display_registration(value: str) -> str:
    value = normalise_registration(value)
    if len(value) > 4:
        return f"{value[:-3]} {value[-3:]}"
    return value


def one_month_before(day: dt.date) -> dt.date:
    year = day.year
    month = day.month - 1
    if month == 0:
        year -= 1
        month = 12
    return dt.date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def parse_date(value: str | None) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(value or "")[:10])
    except (TypeError, ValueError):
        return None


def deadline_state(deadline: dict | None, today: dt.date | None = None) -> dict[str, Any]:
    today = today or dt.date.today()
    due = parse_date((deadline or {}).get("due_date"))
    arranged = bool((deadline or {}).get("appointment_booked"))
    if not due:
        return {
            "status": "missing", "attention": False, "due": None,
            "days": None, "label": "Date not set",
        }
    days = (due - today).days
    if days < 0:
        return {
            "status": "overdue", "attention": True, "due": due,
            "days": days, "label": f"Overdue by {abs(days)} day{'s' if abs(days) != 1 else ''}",
        }
    if today >= one_month_before(due):
        if arranged:
            return {
                "status": "arranged", "attention": False, "due": due,
                "days": days, "label": "Arranged",
            }
        return {
            "status": "due", "attention": True, "due": due,
            "days": days, "label": f"Due in {days} day{'s' if days != 1 else ''}",
        }
    return {
        "status": "ok", "attention": False, "due": due,
        "days": days, "label": f"Due {due.strftime('%d %b %Y')}",
    }


def _ensure(db_path: str) -> None:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS vehicles (
                id TEXT PRIMARY KEY,
                registration TEXT NOT NULL UNIQUE,
                make TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL DEFAULT '',
                year INTEGER,
                colour TEXT NOT NULL DEFAULT '',
                vin TEXT NOT NULL DEFAULT '',
                current_mileage INTEGER,
                notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS vehicle_deadlines (
                id TEXT PRIMARY KEY,
                vehicle_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                due_date TEXT NOT NULL DEFAULT '',
                provider TEXT NOT NULL DEFAULT '',
                reference TEXT NOT NULL DEFAULT '',
                appointment_booked INTEGER NOT NULL DEFAULT 0,
                appointment_date TEXT NOT NULL DEFAULT '',
                appointment_notes TEXT NOT NULL DEFAULT '',
                calendar_id TEXT NOT NULL DEFAULT '',
                calendar_event_id TEXT NOT NULL DEFAULT '',
                calendar_error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(vehicle_id, kind)
            );

            CREATE TABLE IF NOT EXISTS vehicle_service_history (
                id TEXT PRIMARY KEY,
                vehicle_id TEXT NOT NULL,
                serviced_on TEXT NOT NULL,
                mileage INTEGER,
                provider TEXT NOT NULL DEFAULT '',
                details TEXT NOT NULL DEFAULT '',
                next_due_date TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS vehicle_documents (
                id TEXT PRIMARY KEY,
                vehicle_id TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT 'other',
                service_history_id TEXT NOT NULL DEFAULT '',
                filename TEXT NOT NULL,
                stored_file TEXT NOT NULL,
                mime_type TEXT NOT NULL DEFAULT 'application/octet-stream',
                notes TEXT NOT NULL DEFAULT '',
                uploaded_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_vehicle_deadlines_vehicle
                ON vehicle_deadlines(vehicle_id);
            CREATE INDEX IF NOT EXISTS idx_vehicle_documents_vehicle
                ON vehicle_documents(vehicle_id, uploaded_at);
            CREATE INDEX IF NOT EXISTS idx_vehicle_service_vehicle
                ON vehicle_service_history(vehicle_id, serviced_on);
            """
        )
        conn.commit()


def _conn(db_path: str) -> sqlite3.Connection:
    _ensure(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def create_vehicle(
    db_path: str, *, registration: str, make: str = "", model: str = "",
    year: int | None = None, colour: str = "", vin: str = "",
    current_mileage: int | None = None, notes: str = "",
) -> dict:
    registration = normalise_registration(registration)
    if not registration:
        raise ValueError("Registration is required.")
    now = _now()
    vehicle_id = f"veh-{uuid.uuid4().hex[:12]}"
    try:
        with _conn(db_path) as conn:
            conn.execute(
                """INSERT INTO vehicles
                   (id, registration, make, model, year, colour, vin,
                    current_mileage, notes, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (vehicle_id, registration, make.strip(), model.strip(), year,
                 colour.strip(), vin.strip(), current_mileage, notes.strip(), now, now),
            )
            conn.commit()
    except sqlite3.IntegrityError as exc:
        raise ValueError("That registration is already in the vehicle register.") from exc
    return get_vehicle(db_path, vehicle_id) or {}


def update_vehicle(db_path: str, vehicle_id: str, **fields) -> dict | None:
    allowed = {"registration", "make", "model", "year", "colour", "vin", "current_mileage", "notes"}
    values = {key: fields[key] for key in allowed if key in fields}
    if "registration" in values:
        values["registration"] = normalise_registration(str(values["registration"]))
        if not values["registration"]:
            raise ValueError("Registration is required.")
    if not values:
        return get_vehicle(db_path, vehicle_id)
    values["updated_at"] = _now()
    assignments = ", ".join(f"{key} = ?" for key in values)
    try:
        with _conn(db_path) as conn:
            conn.execute(
                f"UPDATE vehicles SET {assignments} WHERE id = ?",
                (*values.values(), vehicle_id),
            )
            conn.commit()
    except sqlite3.IntegrityError as exc:
        raise ValueError("That registration is already in the vehicle register.") from exc
    return get_vehicle(db_path, vehicle_id)


def get_vehicle(db_path: str, vehicle_id: str) -> dict | None:
    with _conn(db_path) as conn:
        row = conn.execute("SELECT * FROM vehicles WHERE id = ?", (vehicle_id,)).fetchone()
    return dict(row) if row else None


def list_vehicles(db_path: str) -> list[dict]:
    with _conn(db_path) as conn:
        rows = conn.execute("SELECT * FROM vehicles ORDER BY registration").fetchall()
    return [dict(row) for row in rows]


def list_deadlines(db_path: str, vehicle_id: str) -> list[dict]:
    with _conn(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM vehicle_deadlines WHERE vehicle_id = ? ORDER BY kind",
            (vehicle_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def get_deadline(db_path: str, vehicle_id: str, kind: str) -> dict | None:
    with _conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM vehicle_deadlines WHERE vehicle_id = ? AND kind = ?",
            (vehicle_id, kind),
        ).fetchone()
    return dict(row) if row else None


def upsert_deadline(
    db_path: str, vehicle_id: str, kind: str, *, due_date: str = "",
    provider: str = "", reference: str = "", appointment_booked: bool = False,
    appointment_date: str = "", appointment_notes: str = "",
) -> dict:
    if kind not in DEADLINE_KINDS:
        raise ValueError("Unknown vehicle deadline type.")
    if due_date and not parse_date(due_date):
        raise ValueError("Choose a valid due date.")
    existing = get_deadline(db_path, vehicle_id, kind)
    if existing and str(existing.get("due_date") or "") != due_date:
        appointment_booked = False
        appointment_date = ""
        appointment_notes = ""
    now = _now()
    deadline_id = str((existing or {}).get("id") or f"vdl-{uuid.uuid4().hex[:12]}")
    with _conn(db_path) as conn:
        conn.execute(
            """INSERT INTO vehicle_deadlines
               (id, vehicle_id, kind, due_date, provider, reference,
                appointment_booked, appointment_date, appointment_notes,
                calendar_id, calendar_event_id, calendar_error, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(vehicle_id, kind) DO UPDATE SET
                 due_date=excluded.due_date,
                 provider=excluded.provider,
                 reference=excluded.reference,
                 appointment_booked=excluded.appointment_booked,
                 appointment_date=excluded.appointment_date,
                 appointment_notes=excluded.appointment_notes,
                 updated_at=excluded.updated_at""",
            (deadline_id, vehicle_id, kind, due_date, provider.strip(), reference.strip(),
             1 if appointment_booked else 0, appointment_date, appointment_notes.strip(),
             str((existing or {}).get("calendar_id") or ""),
             str((existing or {}).get("calendar_event_id") or ""),
             str((existing or {}).get("calendar_error") or ""), now, now),
        )
        conn.commit()
    return get_deadline(db_path, vehicle_id, kind) or {}


def set_calendar_result(
    db_path: str, vehicle_id: str, kind: str, *, calendar_id: str = "",
    event_id: str = "", error: str = "",
) -> None:
    with _conn(db_path) as conn:
        conn.execute(
            """UPDATE vehicle_deadlines
               SET calendar_id=?, calendar_event_id=?, calendar_error=?, updated_at=?
               WHERE vehicle_id=? AND kind=?""",
            (calendar_id, event_id, error[:500], _now(), vehicle_id, kind),
        )
        conn.commit()


def add_service_history(
    db_path: str, vehicle_id: str, *, serviced_on: str, mileage: int | None = None,
    provider: str = "", details: str = "", next_due_date: str = "",
) -> dict:
    if not parse_date(serviced_on):
        raise ValueError("Choose a valid service date.")
    if next_due_date and not parse_date(next_due_date):
        raise ValueError("Choose a valid next-service date.")
    service_id = f"vsh-{uuid.uuid4().hex[:12]}"
    with _conn(db_path) as conn:
        conn.execute(
            """INSERT INTO vehicle_service_history
               (id, vehicle_id, serviced_on, mileage, provider, details,
                next_due_date, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (service_id, vehicle_id, serviced_on, mileage, provider.strip(),
             details.strip(), next_due_date, _now()),
        )
        conn.commit()
    return get_service_history(db_path, service_id) or {}


def get_service_history(db_path: str, service_id: str) -> dict | None:
    with _conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM vehicle_service_history WHERE id = ?", (service_id,)
        ).fetchone()
    return dict(row) if row else None


def list_service_history(db_path: str, vehicle_id: str) -> list[dict]:
    with _conn(db_path) as conn:
        rows = conn.execute(
            """SELECT * FROM vehicle_service_history WHERE vehicle_id = ?
               ORDER BY serviced_on DESC, created_at DESC""",
            (vehicle_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def add_document(
    db_path: str, vehicle_id: str, *, category: str, filename: str,
    stored_file: str, mime_type: str, notes: str = "", service_history_id: str = "",
) -> dict:
    document_id = f"vdoc-{uuid.uuid4().hex[:12]}"
    with _conn(db_path) as conn:
        conn.execute(
            """INSERT INTO vehicle_documents
               (id, vehicle_id, category, service_history_id, filename,
                stored_file, mime_type, notes, uploaded_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (document_id, vehicle_id, category, service_history_id, filename,
             stored_file, mime_type, notes.strip(), _now()),
        )
        conn.commit()
    return get_document(db_path, document_id) or {}


def get_document(db_path: str, document_id: str) -> dict | None:
    with _conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM vehicle_documents WHERE id = ?", (document_id,)
        ).fetchone()
    return dict(row) if row else None


def list_documents(db_path: str, vehicle_id: str) -> list[dict]:
    with _conn(db_path) as conn:
        rows = conn.execute(
            """SELECT * FROM vehicle_documents WHERE vehicle_id = ?
               ORDER BY uploaded_at DESC""",
            (vehicle_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def attention_count(db_path: str, today: dt.date | None = None) -> int:
    today = today or dt.date.today()
    with _conn(db_path) as conn:
        rows = conn.execute(
            """SELECT d.* FROM vehicle_deadlines d
               JOIN vehicles v ON v.id = d.vehicle_id"""
        ).fetchall()
    return sum(1 for row in rows if deadline_state(dict(row), today)["attention"])
