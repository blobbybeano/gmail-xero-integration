import os
import tempfile
import unittest

from app.admin_store import (
    get_expense_settings,
    init_admin_store,
    set_expense_settings,
)


class AdminStoreSettingsTests(unittest.TestCase):
    def test_expense_xero_submission_settings_are_sanitised(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = os.path.join(td, "admin.sqlite3")
            init_admin_store(db_path)

            set_expense_settings(
                db_path,
                {
                    "xero_submission_mode": "manual",
                    "xero_submission_time": "8:5",
                },
            )

            settings = get_expense_settings(db_path)
            self.assertEqual(settings["xero_submission_mode"], "manual")
            self.assertEqual(settings["xero_submission_time"], "08:05")

            set_expense_settings(
                db_path,
                {
                    "xero_submission_mode": "anything",
                    "xero_submission_time": "99:99",
                },
            )

            settings = get_expense_settings(db_path)
            self.assertEqual(settings["xero_submission_mode"], "scheduled")
            self.assertEqual(settings["xero_submission_time"], "17:00")


if __name__ == "__main__":
    unittest.main()
