import unittest

from app.event_processor import (
    parse_app_ledger,
    upsert_app_ledger,
    upsert_invoice_summary,
    upsert_job_photo_links,
)
from app.state import bump_xero_action_attempts, get_xero_action_attempts


class AppLedgerTests(unittest.TestCase):
    def test_upsert_replaces_existing_ledger_compactly(self):
        original = """[notes]
Customer: Test
[/notes]

App status: Old status
[app]s=old;r=old;fp=abc;x=1[/app]
"""
        updated = upsert_app_ledger(
            original,
            message="Needs input - missing invoice lines",
            state="needs_input",
            reason="missing_lines",
            fingerprint="def456",
            xero_attempts=0,
            wait="human_save",
        )

        self.assertEqual(updated.count("[app]"), 1)
        self.assertIn("App status: Needs input - missing invoice lines", updated)
        self.assertEqual(
            parse_app_ledger(updated),
            {
                "s": "needs_input",
                "r": "missing_lines",
                "fp": "def456",
                "x": "0",
                "w": "human_save",
            },
        )

    def test_xero_action_attempts_are_keyed_by_fingerprint(self):
        state = {}
        state, attempts = bump_xero_action_attempts(state, "cal:event", "send", "aaa")
        self.assertEqual(attempts, 1)
        state, attempts = bump_xero_action_attempts(state, "cal:event", "send", "aaa")
        self.assertEqual(attempts, 2)

        self.assertEqual(get_xero_action_attempts(state, "cal:event", "send", "aaa"), 2)
        self.assertEqual(get_xero_action_attempts(state, "cal:event", "send", "bbb"), 0)

    def test_status_rewrite_preserves_app_ledger_at_bottom(self):
        original = """[invoice]
GC = £100+VAT
[/invoice]
PROCESS DRAFT (Y/N) =

[app-status]
Invoice total (ex VAT): £100.00
Invoice total (inc VAT): £120.00
[/app-status]

App status: Sent - 39e93301
[app]s=sent;r=ok;fp=3c0562b811;x=1;w=none;inv=39e93301[/app]
"""

        updated = upsert_invoice_summary(original, 100.0, 120.0, sent=False)

        self.assertEqual(updated.count("[app]"), 1)
        self.assertIn("App status: Sent - 39e93301", updated)
        self.assertTrue(updated.rstrip().endswith("[app]s=sent;r=ok;fp=3c0562b811;x=1;w=none;inv=39e93301[/app]"))
        self.assertEqual(parse_app_ledger(updated)["inv"], "39e93301")

    def test_job_photo_links_use_one_stable_calendar_link(self):
        original = """[contact]
Customer name: Carol Canaan
[/contact]

Photos upload: https://old/upload
Technician photos: https://old/tech
View photos: https://old/gallery

App status: Sent - 39e93301
[app]s=sent;r=ok;fp=3c0562b811;x=1;w=none;inv=39e93301[/app]
"""

        updated = upsert_job_photo_links(
            original,
            upload_url="https://app/j/abc",
            gallery_url="https://app/jp/abc",
            has_photos=True,
            processed=True,
        )

        self.assertIn("Photos: https://app/j/abc", updated)
        self.assertIn("View photos: https://app/jp/abc", updated)
        self.assertNotIn("Photos upload:", updated)
        self.assertNotIn("Technician photos:", updated)
        self.assertTrue(updated.rstrip().endswith("[app]s=sent;r=ok;fp=3c0562b811;x=1;w=none;inv=39e93301[/app]"))


if __name__ == "__main__":
    unittest.main()
