import os
import tempfile
import time
import unittest


class XeroBusyTests(unittest.TestCase):
    def test_busy_marker_expires_and_can_be_cleared_by_owner(self):
        from app.admin_store import init_admin_store
        from app.xero_busy import clear_xero_busy, mark_xero_busy, xero_busy_status

        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "admin.db")
            init_admin_store(db_path)

            mark_xero_busy(db_path, owner="cashflows:test", reason="Testing", ttl_seconds=60)
            active = xero_busy_status(db_path)
            self.assertTrue(active["active"])
            self.assertEqual(active["owner"], "cashflows:test")
            self.assertEqual(active["reason"], "Testing")

            clear_xero_busy(db_path, owner="other")
            self.assertTrue(xero_busy_status(db_path)["active"])

            clear_xero_busy(db_path, owner="cashflows:test")
            self.assertFalse(xero_busy_status(db_path)["active"])

    def test_busy_marker_ignores_expired_record(self):
        from app.admin_store import init_admin_store, set_json_setting
        from app.xero_busy import xero_busy_status

        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "admin.db")
            init_admin_store(db_path)
            set_json_setting(
                db_path,
                "xero_busy_gate",
                {"owner": "cashflows:test", "reason": "Old", "until_ts": time.time() - 1},
            )
            self.assertFalse(xero_busy_status(db_path)["active"])


if __name__ == "__main__":
    unittest.main()
