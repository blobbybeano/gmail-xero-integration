import os
import tempfile
import unittest

from app.state import (
    get_invoice_for_event,
    is_invoice_paid,
    mark_invoice_paid,
    mark_invoice_sent,
    mark_recent_xero_webhook,
    save_state,
    save_state_merged,
    set_invoice_for_event,
    set_processed_update_marker,
    was_recent_xero_webhook,
)


class StateMergeTests(unittest.TestCase):
    def test_polling_save_preserves_webhook_paid_state(self):
        event_key = "cal:event-mia"
        fd, path = tempfile.mkstemp()
        os.close(fd)
        try:
            save_state(path, {"invoice_paid_event_ids": [], "event_processed_updates": {}})
            poller_state = {
                "invoice_paid_event_ids": [],
                "event_processed_updates": {},
            }
            webhook_state = {
                "invoice_paid_event_ids": [],
                "event_processed_updates": {},
            }

            webhook_state = mark_invoice_paid(webhook_state, event_key)
            save_state_merged(path, webhook_state)

            poller_state = set_processed_update_marker(
                poller_state,
                event_key,
                "2026-07-03T13:40:45Z",
            )
            final = save_state_merged(path, poller_state)

            self.assertTrue(is_invoice_paid(final, event_key))
            self.assertEqual(
                final["event_processed_updates"][event_key],
                "2026-07-03T13:40:45Z",
            )
        finally:
            os.unlink(path)

    def test_merge_keeps_sent_paid_invoice_mapping_and_webhook_marker(self):
        event_key = "cal:event"
        latest = {}
        latest = mark_invoice_sent(latest, event_key)
        latest = mark_invoice_paid(latest, event_key)
        latest = set_invoice_for_event(latest, event_key, "invoice-1")
        latest = mark_recent_xero_webhook(latest, event_key, "invoice-1", when_ts=1000)

        incoming = set_processed_update_marker({}, event_key, "2026-07-03T13:40:45Z")
        fd, path = tempfile.mkstemp()
        os.close(fd)
        try:
            save_state(path, latest)
            merged = save_state_merged(path, incoming)

            self.assertTrue(is_invoice_paid(merged, event_key))
            self.assertEqual(get_invoice_for_event(merged, event_key), "invoice-1")
            self.assertTrue(
                was_recent_xero_webhook(
                    merged,
                    event_key,
                    "invoice-1",
                    now_ts=1100,
                    within_seconds=900,
                )
            )
        finally:
            os.unlink(path)

    def test_recent_xero_webhook_expires(self):
        event_key = "cal:event"
        state = mark_recent_xero_webhook({}, event_key, "invoice-1", when_ts=1000)

        self.assertTrue(
            was_recent_xero_webhook(
                state,
                event_key,
                "invoice-1",
                now_ts=1100,
                within_seconds=900,
            )
        )
        self.assertFalse(
            was_recent_xero_webhook(
                state,
                event_key,
                "invoice-1",
                now_ts=2000,
                within_seconds=900,
            )
        )

    def test_merge_preserves_deferred_xero_targets(self):
        event_key = "cal:event-delayed"
        fd, path = tempfile.mkstemp()
        os.close(fd)
        try:
            save_state(
                path,
                {
                    "deferred_xero_event_targets": {
                        event_key: {"next_retry_at": 1000, "reason": "xero_slot_limit"}
                    }
                },
            )
            incoming = set_processed_update_marker({}, event_key, "2026-07-07T09:00:00Z")

            merged = save_state_merged(path, incoming)

            self.assertIn(event_key, merged["deferred_xero_event_targets"])
            self.assertEqual(
                merged["deferred_xero_event_targets"][event_key]["reason"],
                "xero_slot_limit",
            )
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
