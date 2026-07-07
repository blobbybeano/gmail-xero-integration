import unittest
from pathlib import Path
import re


class DeferredXeroQueueSourceTests(unittest.TestCase):
    def test_due_deferred_targets_are_not_removed_before_processing(self):
        source = (Path(__file__).resolve().parents[1] / "app" / "main.py").read_text()
        due_block = source[
            source.index("_due_deferred_event_targets: list[str] = []") :
            source.index("_queued_calendar_targets = {")
        ]
        self.assertIn("_due_deferred_event_targets.append(str(_key))", due_block)
        due_if = re.search(
            r"if _next_retry_at <= _now_ts:\n(?P<body>(?: {16}.+\n)+)",
            due_block,
        )
        self.assertIsNotNone(due_if)
        self.assertNotIn("_deferred_target_rows.pop(_key, None)", due_if.group("body"))

    def test_deferred_targets_clear_only_on_real_outcome(self):
        source = (Path(__file__).resolve().parents[1] / "app" / "main.py").read_text()
        self.assertIn("def _clear_deferred_xero_target(event_key: str) -> None:", source)
        self.assertIn("_clear_deferred_xero_target(event_key)\n                    continue", source)
        self.assertIn("if not _needs_xero_event_work:\n                    _clear_deferred_xero_target(event_key)", source)
        self.assertIn("_clear_deferred_xero_target(event_key)\n                    _xero_events_used += 1", source)

    def test_draft_attempt_marker_is_persisted_before_xero_create(self):
        source = (Path(__file__).resolve().parents[1] / "app" / "main.py").read_text()
        create_call = "result = xero_client.create_invoice_from_event("
        search_from = 0
        count = 0
        while True:
            idx = source.find(create_call, search_from)
            if idx == -1:
                break
            prelude = source[max(0, idx - 700) : idx]
            self.assertIn("set_draft_sync_attempted_at(state, event_key, now_ts)", prelude)
            self.assertIn("set_draft_sync_fingerprint(", prelude)
            self.assertIn("state = save_state_merged(config.state_file, state)", prelude)
            count += 1
            search_from = idx + len(create_call)
        self.assertGreaterEqual(count, 1)


if __name__ == "__main__":
    unittest.main()
