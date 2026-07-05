import re
import unittest

import app.google_sheets as google_sheets


class _Call:
    def __init__(self, fn):
        self._fn = fn

    def execute(self):
        return self._fn()


class _FakeValues:
    def __init__(self, service):
        self.service = service

    def get(self, spreadsheetId, range):
        def _execute():
            if range.endswith("!A1:Z1"):
                return {"values": [self.service.rows[0]]}
            return {"values": self.service.rows}

        return _Call(_execute)

    def update(self, spreadsheetId, range, valueInputOption, body):
        def _execute():
            self.service.update_calls.append(range)
            match = re.search(r"![A-Z]+(\d+):", range)
            if match:
                row_idx = int(match.group(1)) - 1
                self.service.rows[row_idx] = body["values"][0]
            return {}

        return _Call(_execute)

    def append(self, spreadsheetId, range, valueInputOption, insertDataOption, body):
        def _execute():
            self.service.append_calls.append(range)
            self.service.rows.append(body["values"][0])
            row_num = len(self.service.rows)
            return {"updates": {"updatedRange": f"'Sales'!A{row_num}:D{row_num}"}}

        return _Call(_execute)


class _FakeSpreadsheets:
    def __init__(self, service):
        self.service = service

    def get(self, spreadsheetId, fields):
        return _Call(
            lambda: {
                "sheets": [
                    {"properties": {"title": "Sales", "sheetId": 123}},
                ]
            }
        )

    def values(self):
        return _FakeValues(self.service)


class _FakeService:
    def __init__(self):
        self.rows = [
            ["Logged At", "Event ID", "Customer", "Sales Total"],
            ["01/01/2026 10:00", "GC-event-S", "Old customer", "10.00"],
        ]
        self.update_calls = []
        self.append_calls = []

    def spreadsheets(self):
        return _FakeSpreadsheets(self)


class GoogleSheetsUpsertTests(unittest.TestCase):
    def test_update_existing_signature_row_does_not_append(self):
        service = _FakeService()
        original_builder = google_sheets.build_sheets_service_from_creds
        google_sheets.build_sheets_service_from_creds = lambda _creds: service
        try:
            google_sheets.append_stats_row(
                None,
                spreadsheet_id="spreadsheet",
                sheet_name="Sales",
                event_key="calendar:event:sales",
                stats_fields=["customer", "sales_total_ex_vat"],
                payload={"customer": "New customer", "sales_total_ex_vat": "12.00"},
                event_id_display="GC-event-S",
                dedupe_signature={"Event ID": "GC-event-S"},
                update_existing=True,
            )
        finally:
            google_sheets.build_sheets_service_from_creds = original_builder

        self.assertEqual(len(service.rows), 2)
        self.assertEqual(service.append_calls, [])
        self.assertEqual(service.update_calls, ["'Sales'!A2:D2"])
        self.assertEqual(
            service.rows[1],
            ["01/01/2026 10:00", "GC-event-S", "New customer", "12.00"],
        )


if __name__ == "__main__":
    unittest.main()
