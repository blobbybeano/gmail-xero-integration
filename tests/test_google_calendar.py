import unittest

from googleapiclient.errors import HttpError

from app import google_calendar


class _Config:
    google_calendar_id = "default-calendar"


class _Resp:
    def __init__(self, status):
        self.status = status
        self.reason = "test"


class _Request:
    def __init__(self, response=None, exc=None):
        self.response = response
        self.exc = exc

    def execute(self):
        if self.exc:
            raise self.exc
        return self.response


class _Events:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.patch_calls = []

    def list(self, **kwargs):
        self.calls.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            return _Request(exc=response)
        return _Request(response=response)

    def patch(self, **kwargs):
        self.patch_calls.append(kwargs)
        return _Request(response={"ok": True})


class _Service:
    def __init__(self, events):
        self._events = events

    def events(self):
        return self._events


class GoogleCalendarIncrementalSyncTests(unittest.TestCase):
    def _with_service(self, responses):
        events = _Events(responses)
        service = _Service(events)
        original = google_calendar.build_calendar_service
        google_calendar.build_calendar_service = lambda _config: service
        self.addCleanup(lambda: setattr(google_calendar, "build_calendar_service", original))
        return events

    def test_incremental_sync_uses_sync_token_without_time_window(self):
        events = self._with_service(
            [
                {"items": [{"id": "a"}], "nextPageToken": "page-2"},
                {"items": [{"id": "b"}], "nextSyncToken": "new-token"},
            ]
        )

        items, token = google_calendar.list_incremental_events(
            _Config(),
            "old-token",
            calendar_id="calendar-1",
        )

        self.assertEqual(items, [{"id": "a"}, {"id": "b"}])
        self.assertEqual(token, "new-token")
        self.assertEqual(events.calls[0]["calendarId"], "calendar-1")
        self.assertEqual(events.calls[0]["syncToken"], "old-token")
        self.assertTrue(events.calls[0]["showDeleted"])
        self.assertEqual(events.calls[1]["pageToken"], "page-2")
        for forbidden in ("orderBy", "timeMin", "timeMax", "updatedMin"):
            self.assertNotIn(forbidden, events.calls[0])

    def test_prime_sync_token_consumes_pages_without_sync_token(self):
        events = self._with_service(
            [
                {"items": [{"id": "old"}], "nextPageToken": "page-2"},
                {"items": [{"id": "older"}], "nextSyncToken": "fresh-token"},
            ]
        )

        token = google_calendar.prime_calendar_sync_token(
            _Config(),
            calendar_id="calendar-1",
        )

        self.assertEqual(token, "fresh-token")
        self.assertEqual(events.calls[0]["calendarId"], "calendar-1")
        self.assertTrue(events.calls[0]["showDeleted"])
        self.assertNotIn("syncToken", events.calls[0])

    def test_expired_sync_token_raises_specific_error(self):
        events = self._with_service(
            [
                HttpError(_Resp(410), b"gone"),
            ]
        )

        with self.assertRaises(google_calendar.CalendarSyncTokenExpired):
            google_calendar.list_incremental_events(
                _Config(),
                "expired-token",
                calendar_id="calendar-1",
            )
        self.assertEqual(events.calls[0]["syncToken"], "expired-token")

    def test_update_event_description_can_patch_color(self):
        events = self._with_service([])

        google_calendar.update_event_description(
            _Config(),
            "event-1",
            "notes",
            summary="new title",
            calendar_id="calendar-1",
            color_id="2",
        )

        self.assertEqual(events.patch_calls[0]["calendarId"], "calendar-1")
        self.assertEqual(events.patch_calls[0]["eventId"], "event-1")
        self.assertEqual(
            events.patch_calls[0]["body"],
            {"description": "notes", "summary": "new title", "colorId": "2"},
        )


if __name__ == "__main__":
    unittest.main()
