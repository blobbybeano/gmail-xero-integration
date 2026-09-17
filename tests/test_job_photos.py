import unittest

from app.job_photos import event_is_processed


class JobPhotoTests(unittest.TestCase):
    def test_completed_cash_entry_counts_as_processed_for_technician_photos(self):
        description = """[app-status]
Entry complete ✅
[/app-status]

App status: Cash complete - 1bc67525
[app]s=complete;r=cash;fp=d9653ee3a8;x=1;w=none;inv=1bc67525[/app]
"""

        self.assertTrue(event_is_processed(description))

    def test_initial_formatted_draft_is_customer_photo_mode(self):
        description = """[contact]
Customer name: Carol Canaan
[/contact]

[invoice]
Gutter cleaning = £120+VAT
[/invoice]
PROCESS DRAFT (Y/N) =
"""

        self.assertFalse(event_is_processed(description))


if __name__ == "__main__":
    unittest.main()
