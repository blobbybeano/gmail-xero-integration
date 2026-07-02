import unittest

from app.event_processor import parse_app_ledger, upsert_app_ledger
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


if __name__ == "__main__":
    unittest.main()
