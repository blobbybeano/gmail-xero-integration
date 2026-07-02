import datetime as dt
import unittest

from app.main import _event_start_date_iso


class PaymentDateTests(unittest.TestCase):
    def test_timed_event_uses_london_appointment_date(self):
        event = {"start": {"dateTime": "2026-06-12T23:30:00+00:00"}}

        self.assertEqual(_event_start_date_iso(event), "2026-06-13")

    def test_all_day_event_uses_calendar_date(self):
        event = {"start": {"date": "2026-06-12"}}

        self.assertEqual(_event_start_date_iso(event), "2026-06-12")

    def test_missing_start_uses_fallback_date(self):
        self.assertEqual(
            _event_start_date_iso({}, fallback=dt.date(2026, 6, 12)),
            "2026-06-12",
        )


if __name__ == "__main__":
    unittest.main()
