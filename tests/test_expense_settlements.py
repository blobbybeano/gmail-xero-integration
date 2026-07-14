import os
import tempfile
import unittest

from app.receipts import expense_store


class ExpenseSettlementTests(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp()
        os.close(fd)

    def tearDown(self):
        if os.path.exists(self.db_path):
            os.unlink(self.db_path)

    def test_prepared_subcontractor_batch_stays_unpaid_until_marked_paid(self):
        eng = expense_store.create_engineer(
            self.db_path,
            name="Troy",
            kind="subcontractor",
            xero_contact_name="Troy",
            expense_account_code="310",
            payment_account_code="090",
        )
        expense_store.create_receipt(
            self.db_path,
            engineer_id=eng["id"],
            merchant="Fuel",
            purchased_on="2026-07-10",
            amount_inc=12.50,
            amount_ex=10.42,
            vat_amount=2.08,
            status="approved",
        )
        expense_store.create_receipt(
            self.db_path,
            engineer_id=eng["id"],
            merchant="Parts",
            purchased_on="2026-07-11",
            amount_inc=30.00,
            amount_ex=25.00,
            vat_amount=5.00,
            status="submitted",
        )

        self.assertEqual(
            expense_store.amount_owed_to_engineer(self.db_path, eng["id"]), 42.50
        )
        self.assertEqual(
            expense_store.amount_unpaid_to_engineer(self.db_path, eng["id"]), 42.50
        )

        settlement, receipts = expense_store.create_prepared_settlement_for_engineer(
            self.db_path,
            engineer_id=eng["id"],
            reference="PWSUB1-20260714-ABCD",
        )

        self.assertIsNotNone(settlement)
        self.assertEqual(settlement["amount"], 42.50)
        self.assertEqual(settlement["status"], "prepared")
        self.assertEqual(len(receipts), 2)
        self.assertEqual(
            expense_store.amount_owed_to_engineer(self.db_path, eng["id"]), 0.00
        )
        self.assertEqual(
            expense_store.amount_unpaid_to_engineer(self.db_path, eng["id"]), 42.50
        )

        for receipt in receipts:
            expense_store.update_receipt(self.db_path, receipt["id"], status="settled")
        expense_store.update_settlement(
            self.db_path,
            settlement["id"],
            status="paid",
            paid_on="2026-07-14",
            plaid_tx_id="tx_123",
        )

        self.assertEqual(
            expense_store.amount_unpaid_to_engineer(self.db_path, eng["id"]), 0.00
        )
        self.assertEqual(
            [r["merchant"] for r in expense_store.list_receipts_for_settlement(
                self.db_path, settlement["id"]
            )],
            ["Fuel", "Parts"],
        )

    def test_owner_paid_company_card_receipts_can_be_batched_separately(self):
        eng = expense_store.create_engineer(
            self.db_path,
            name="Dan",
            kind="company_card",
            xero_contact_name="Dan",
            expense_account_code="310",
            payment_account_code="090",
            allow_owner_paid=True,
            owner_paid_account_code="835",
        )
        expense_store.create_receipt(
            self.db_path,
            engineer_id=eng["id"],
            merchant="Company card fuel",
            purchased_on="2026-07-10",
            amount_inc=20.00,
            status="approved",
            payment_source="company_card",
        )
        expense_store.create_receipt(
            self.db_path,
            engineer_id=eng["id"],
            merchant="Paid personally",
            purchased_on="2026-07-11",
            amount_inc=15.00,
            status="approved",
            payment_source="owner_paid",
            owner_paid_account_code="835",
        )

        self.assertEqual(
            expense_store.amount_owed_to_engineer(
                self.db_path, eng["id"], payment_source="owner_paid"
            ),
            15.00,
        )

        settlement, receipts = expense_store.create_prepared_settlement_for_engineer(
            self.db_path,
            engineer_id=eng["id"],
            reference="DAN-482",
            payment_source="owner_paid",
        )

        self.assertIsNotNone(settlement)
        self.assertEqual(settlement["amount"], 15.00)
        self.assertEqual([r["merchant"] for r in receipts], ["Paid personally"])
        self.assertEqual(
            expense_store.amount_owed_to_engineer(self.db_path, eng["id"]),
            20.00,
        )


if __name__ == "__main__":
    unittest.main()
