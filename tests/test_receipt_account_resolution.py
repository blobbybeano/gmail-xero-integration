import unittest

from app.admin_web import _resolve_expense_account_choice


class ReceiptAccountResolutionTests(unittest.TestCase):
    def setUp(self):
        self.accounts = [
            {"Code": "310", "Name": "Materials"},
            {"Code": "400", "Name": "Motor Vehicle Expenses"},
            {"Code": "401", "Name": "Motor Vehicle Fuel"},
        ]

    def test_accepts_real_xero_code(self):
        self.assertEqual(
            _resolve_expense_account_choice("310", self.accounts),
            ("310", "Materials"),
        )

    def test_resolves_unambiguous_account_name(self):
        self.assertEqual(
            _resolve_expense_account_choice("materials", self.accounts),
            ("310", "Materials"),
        )

    def test_rejects_ambiguous_name_fragment(self):
        self.assertEqual(
            _resolve_expense_account_choice("motor vehicle", self.accounts),
            ("", ""),
        )

    def test_rejects_unknown_saved_text(self):
        self.assertEqual(
            _resolve_expense_account_choice("random fallback", self.accounts),
            ("", ""),
        )


if __name__ == "__main__":
    unittest.main()
