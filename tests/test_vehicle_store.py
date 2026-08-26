import datetime as dt
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app import vehicle_store
from app.admin_web import (
    create_app,
    _sync_vehicle_calendar_reminder,
    _vehicle_calendar_event_body,
)


class _CalendarCall:
    def __init__(self, result):
        self.result = result

    def execute(self):
        return self.result


class _FakeEvents:
    def __init__(self):
        self.inserts = []
        self.patches = []
        self.deletes = []

    def insert(self, **kwargs):
        self.inserts.append(kwargs)
        return _CalendarCall({"id": "calendar-event-1"})

    def patch(self, **kwargs):
        self.patches.append(kwargs)
        return _CalendarCall({"id": kwargs["eventId"]})

    def delete(self, **kwargs):
        self.deletes.append(kwargs)
        return _CalendarCall({})


class _FakeCalendar:
    def __init__(self):
        self.calls = _FakeEvents()

    def events(self):
        return self.calls


class VehicleStoreTests(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp()
        os.close(fd)
        self.vehicle = vehicle_store.create_vehicle(
            self.db_path,
            registration="FD66 LSK",
            make="Ford",
            model="Transit",
            year=2016,
        )

    def tearDown(self):
        if os.path.exists(self.db_path):
            os.unlink(self.db_path)

    def test_registration_is_normalised_and_displayed(self):
        self.assertEqual(self.vehicle["registration"], "FD66LSK")
        self.assertEqual(
            vehicle_store.display_registration(self.vehicle["registration"]),
            "FD66 LSK",
        )

    def test_one_calendar_month_before_handles_month_end(self):
        self.assertEqual(
            vehicle_store.one_month_before(dt.date(2026, 3, 31)),
            dt.date(2026, 2, 28),
        )

    def test_arranged_suppresses_due_soon_but_not_overdue(self):
        due_soon = {"due_date": "2026-08-20", "appointment_booked": 1}
        state = vehicle_store.deadline_state(due_soon, dt.date(2026, 7, 27))
        self.assertEqual(state["status"], "arranged")
        self.assertFalse(state["attention"])

        overdue = {"due_date": "2026-07-26", "appointment_booked": 1}
        state = vehicle_store.deadline_state(overdue, dt.date(2026, 7, 27))
        self.assertEqual(state["status"], "overdue")
        self.assertTrue(state["attention"])

    def test_new_due_date_resets_old_appointment_acknowledgement(self):
        vehicle_store.upsert_deadline(
            self.db_path,
            self.vehicle["id"],
            "mot",
            due_date="2026-08-20",
            appointment_booked=True,
            appointment_date="2026-08-10",
        )
        changed = vehicle_store.upsert_deadline(
            self.db_path,
            self.vehicle["id"],
            "mot",
            due_date="2027-08-20",
            appointment_booked=True,
            appointment_date="2026-08-10",
        )
        self.assertFalse(changed["appointment_booked"])
        self.assertEqual(changed["appointment_date"], "")

    def test_attention_count_tracks_due_deadlines(self):
        vehicle_store.upsert_deadline(
            self.db_path, self.vehicle["id"], "tax", due_date="2026-08-15"
        )
        vehicle_store.upsert_deadline(
            self.db_path,
            self.vehicle["id"],
            "insurance",
            due_date="2026-08-15",
            appointment_booked=True,
        )
        self.assertEqual(
            vehicle_store.attention_count(self.db_path, dt.date(2026, 7, 27)),
            1,
        )

    def test_service_history_and_documents_are_persisted(self):
        service = vehicle_store.add_service_history(
            self.db_path,
            self.vehicle["id"],
            serviced_on="2026-06-01",
            mileage=81000,
            provider="Ford",
            next_due_date="2027-06-01",
        )
        vehicle_store.add_document(
            self.db_path,
            self.vehicle["id"],
            category="service",
            filename="service.pdf",
            stored_file="/data/vehicles/service.pdf",
            mime_type="application/pdf",
            service_history_id=service["id"],
        )
        self.assertEqual(len(vehicle_store.list_service_history(self.db_path, self.vehicle["id"])), 1)
        self.assertEqual(len(vehicle_store.list_documents(self.db_path, self.vehicle["id"])), 1)

    def test_calendar_event_is_one_month_before_due_date(self):
        body = _vehicle_calendar_event_body(
            self.vehicle,
            {"kind": "mot", "due_date": "2026-08-31", "appointment_booked": 0},
        )
        self.assertEqual(body["start"]["date"], "2026-07-31")
        self.assertEqual(body["end"]["date"], "2026-08-01")
        self.assertIn("FD66 LSK", body["summary"])
        self.assertIn("MOT due: 31 August 2026", body["description"])

    def test_calendar_sync_creates_then_updates_same_event(self):
        deadline = vehicle_store.upsert_deadline(
            self.db_path, self.vehicle["id"], "mot", due_date="2026-08-31"
        )
        config = SimpleNamespace(
            admin_db_file=self.db_path,
            google_calendar_id="fleet@example.com",
        )
        fake = _FakeCalendar()
        ok, _message = _sync_vehicle_calendar_reminder(
            config, self.vehicle, deadline, service=fake
        )
        self.assertTrue(ok)
        self.assertEqual(len(fake.calls.inserts), 1)
        saved = vehicle_store.get_deadline(self.db_path, self.vehicle["id"], "mot")
        self.assertEqual(saved["calendar_event_id"], "calendar-event-1")

        saved = vehicle_store.upsert_deadline(
            self.db_path, self.vehicle["id"], "mot", due_date="2027-08-31"
        )
        ok, _message = _sync_vehicle_calendar_reminder(
            config, self.vehicle, saved, service=fake
        )
        self.assertTrue(ok)
        self.assertEqual(len(fake.calls.inserts), 1)
        self.assertEqual(len(fake.calls.patches), 1)
        self.assertEqual(fake.calls.patches[0]["eventId"], "calendar-event-1")

    def test_vehicle_web_flow_flashes_then_stops_when_arranged(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "ADMIN_DB_FILE": os.path.join(tmp, "admin.db"),
                "STATE_FILE": os.path.join(tmp, "state.json"),
                "RECEIPTS_UPLOAD_DIR": os.path.join(tmp, "uploads"),
                "RECEIPTS_STORE_FILE": os.path.join(tmp, "receipts.json"),
                "GOOGLE_TOKEN_FILE": os.path.join(tmp, "google.json"),
                "GOOGLE_ADMIN_TOKEN_FILE": os.path.join(tmp, "google-admin.json"),
                "GOOGLE_CREDENTIALS_FILE": os.path.join(tmp, "credentials.json"),
                "XERO_TOKEN_FILE": os.path.join(tmp, "xero.json"),
                "WEB_SECRET_KEY": "vehicle-test-secret",
            }
            with patch.dict(os.environ, env, clear=False):
                app = create_app()
                app.testing = True
                client = app.test_client()
                with client.session_transaction() as session:
                    session["logged_in"] = True

                response = client.post(
                    "/vehicles",
                    data={"registration": "FD66 LSK", "make": "Ford", "model": "Transit"},
                    follow_redirects=True,
                )
                self.assertEqual(response.status_code, 200)
                self.assertIn(b"FD66 LSK", response.data)
                db_path = env["ADMIN_DB_FILE"]
                vehicle = vehicle_store.list_vehicles(db_path)[0]
                today = dt.date.today().isoformat()
                with patch(
                    "app.admin_web._sync_vehicle_calendar_reminder",
                    return_value=(True, "Calendar reminder synced."),
                ):
                    response = client.post(
                        f"/vehicles/{vehicle['id']}/deadline/mot",
                        data={"due_date": today},
                        follow_redirects=True,
                    )
                self.assertIn(b"Vehicles (1)", response.data)
                self.assertIn(b"ACTION DUE", response.data)

                with patch(
                    "app.admin_web._sync_vehicle_calendar_reminder",
                    return_value=(True, "Calendar reminder synced."),
                ):
                    response = client.post(
                        f"/vehicles/{vehicle['id']}/deadline/mot",
                        data={"due_date": today, "appointment_booked": "1"},
                        follow_redirects=True,
                    )
                self.assertNotIn(b"Vehicles (1)", response.data)
                self.assertIn(b"ARRANGED", response.data)


if __name__ == "__main__":
    unittest.main()
