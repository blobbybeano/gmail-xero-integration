import os
import shutil
import tempfile
import unittest

from app.admin_web import _expense_image_cleanup_plan, _expense_image_cleanup_safe_reason
from app.receipts import expense_store


class ExpenseSettlementTests(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp()
        os.close(fd)
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        if os.path.exists(self.db_path):
            os.unlink(self.db_path)
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _image(self, name: str, data: bytes = b"receipt") -> str:
        path = os.path.join(self.tmpdir, name)
        with open(path, "wb") as fh:
            fh.write(data)
        return path

    def test_vat_cleanup_plan_only_archives_clean_xero_receipts(self):
        eng = expense_store.create_engineer(
            self.db_path,
            name="Dan",
            kind="company_card",
            xero_contact_name="Dan",
            expense_account_code="310",
            payment_account_code="090",
        )
        safe = expense_store.create_receipt(
            self.db_path,
            engineer_id=eng["id"],
            merchant="Safe",
            purchased_on="2026-04-10",
            amount_inc=12.00,
            stored_file=self._image("safe.jpg", b"safe-img"),
            status="submitted",
        )
        expense_store.update_receipt(
            self.db_path, safe["id"], xero_id="xero-safe", xero_type="BankTransaction"
        )
        pending = expense_store.create_receipt(
            self.db_path,
            engineer_id=eng["id"],
            merchant="Pending",
            purchased_on="2026-04-11",
            amount_inc=13.00,
            stored_file=self._image("pending.jpg"),
            status="pending_review",
        )
        expense_store.update_receipt(
            self.db_path, pending["id"], xero_id="xero-pending"
        )
        no_xero = expense_store.create_receipt(
            self.db_path,
            engineer_id=eng["id"],
            merchant="No Xero",
            purchased_on="2026-04-12",
            amount_inc=14.00,
            stored_file=self._image("no-xero.jpg"),
            status="submitted",
        )
        failed = expense_store.create_receipt(
            self.db_path,
            engineer_id=eng["id"],
            merchant="Failed",
            purchased_on="2026-04-13",
            amount_inc=15.00,
            stored_file=self._image("failed.jpg"),
            status="submitted",
        )
        expense_store.update_receipt(
            self.db_path,
            failed["id"],
            xero_id="xero-failed",
            xero_error="Xero failed",
        )

        plan = _expense_image_cleanup_plan(
            self.db_path, "2026-04-01", "2026-04-30"
        )

        self.assertEqual(
            [row["receipt"]["id"] for row in plan["safe"]],
            [safe["id"]],
        )
        retained = {row["receipt"]["id"]: row["reason"] for row in plan["retained"]}
        self.assertIn("still being reviewed", retained[pending["id"]])
        self.assertIn("no Xero record", retained[no_xero["id"]])
        self.assertIn("Xero error", retained[failed["id"]])
        self.assertEqual(
            _expense_image_cleanup_safe_reason(
                expense_store.get_receipt(self.db_path, safe["id"]) or {}
            ),
            "",
        )

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

    def test_prepared_subcontractor_batch_can_use_actual_paid_amount(self):
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
            amount_inc=37.95,
            status="approved",
        )
        expense_store.create_receipt(
            self.db_path,
            engineer_id=eng["id"],
            merchant="Materials",
            purchased_on="2026-07-11",
            amount_inc=48.61,
            status="approved",
        )

        settlement, receipts = expense_store.create_prepared_settlement_for_engineer(
            self.db_path,
            engineer_id=eng["id"],
            reference="TROY-649",
            amount_override=85.00,
        )

        self.assertIsNotNone(settlement)
        self.assertEqual(settlement["amount"], 85.00)
        self.assertIn("receipt total £86.56", settlement["note"])
        self.assertEqual(len(receipts), 2)
        self.assertEqual(
            expense_store.amount_unpaid_to_engineer(self.db_path, eng["id"]), 85.00
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
