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


if __name__ == "__main__":
    unittest.main()
