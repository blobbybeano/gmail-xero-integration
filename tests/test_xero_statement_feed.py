import datetime as dt
import tempfile
import unittest

from app.admin_store import init_admin_store
from app import xero_statement_feed as feed


class XeroStatementFeedTests(unittest.TestCase):
    def test_parses_reconciled_account_transactions_export(self):
        text = (
            "Date,Description,Reference,Spent,Received,Status\n"
            "11/05/2026,SP EQUIP2CLEAN CD 6083,HD 123,30.59,,Reconciled\n"
            "12/05/2026,Customer payment,, ,50.00,Reconciled\n"
            "13/05/2026,Something pending,,12.00,,Unreconciled\n"
        )
        rows = feed.parse_csv(
            text,
            xero_account_id="acct-1",
            xero_account_name="Ben - Personal Bank",
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["date"], "2026-05-11")
        self.assertEqual(rows[0]["amount"], 30.59)
        self.assertEqual(rows[0]["direction"], "spent")
        self.assertEqual(rows[1]["direction"], "received")

    def test_exact_spend_match_requires_date_amount_and_account(self):
        rows = feed.parse_csv(
            "Date,Description,Spent,Status\n"
            "11/05/2026,SP EQUIP2CLEAN CD 6083,30.59,Reconciled\n",
            xero_account_id="acct-1",
            xero_account_name="Ben - Personal Bank",
        )
        self.assertIsNotNone(
            feed.find_reconciled_match(
                rows,
                amount=30.59,
                date=dt.date(2026, 5, 11),
                xero_account_id="acct-1",
                xero_account_name="Ben - Personal Bank",
                description="SP EQUIP2CLEAN CD 6083",
            )
        )
        self.assertIsNone(
            feed.find_reconciled_match(
                rows,
                amount=30.59,
                date=dt.date(2026, 5, 12),
                xero_account_id="acct-1",
                xero_account_name="Ben - Personal Bank",
            )
        )
        self.assertIsNone(
            feed.find_reconciled_match(
                rows,
                amount=30.59,
                date=dt.date(2026, 5, 11),
                xero_account_id="other-account",
                xero_account_name="Pow Wash",
            )
        )

    def test_received_reconciled_line_does_not_clear_expense_spend(self):
        rows = feed.parse_csv(
            "Date,Description,Received,Status\n"
            "11/05/2026,Customer receipt,30.59,Reconciled\n",
            xero_account_id="acct-1",
            xero_account_name="Pow Wash",
        )
        self.assertIsNone(
            feed.find_reconciled_match(
                rows,
                amount=30.59,
                date=dt.date(2026, 5, 11),
                xero_account_id="acct-1",
                xero_account_name="Pow Wash",
            )
        )

    def test_accepts_transaction_date_header(self):
        rows = feed.parse_csv(
            "Transaction Date,Transaction Description,Debit Amount,Reconciled\n"
            "11/05/2026,SP EQUIP2CLEAN CD 6083,30.59,Yes\n",
            xero_account_name="Ben - Personal Bank",
        )
        self.assertEqual(rows[0]["date"], "2026-05-11")
        self.assertEqual(rows[0]["amount"], 30.59)

    def test_ingest_is_idempotent(self):
        with tempfile.NamedTemporaryFile(suffix=".db") as f:
            init_admin_store(f.name)
            text = (
                "Date,Description,Debit,Status\n"
                "11/05/2026,SP EQUIP2CLEAN CD 6083,30.59,Reconciled\n"
            )
            first = feed.ingest_csv(
                f.name,
                text,
                xero_account_id="acct-1",
                xero_account_name="Ben - Personal Bank",
            )
            second = feed.ingest_csv(
                f.name,
                text,
                xero_account_id="acct-1",
                xero_account_name="Ben - Personal Bank",
            )
            self.assertEqual(first["added"], 1)
            self.assertEqual(second["added"], 0)
            self.assertEqual(second["skipped_duplicates"], 1)
            self.assertEqual(feed.status(f.name)["row_count"], 1)


if __name__ == "__main__":
    unittest.main()
