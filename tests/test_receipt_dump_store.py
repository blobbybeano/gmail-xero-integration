import os
import tempfile
import unittest

from app.admin_web import _xero_bank_transaction_url
from app.receipts import dump_store


class ReceiptDumpStoreTests(unittest.TestCase):
    def test_xero_bank_transaction_id_is_stored_on_dump_item(self):
        fd, path = tempfile.mkstemp()
        os.close(fd)
        try:
            batch = dump_store.create_batch(path, label="test")
            item = dump_store.create_item(
                path,
                batch_id=batch["id"],
                merchant="Shell",
                purchased_on="2026-05-05",
                amount_inc=20.00,
                xero_bank_transaction_id="5adf26ce-f721-4717-93dc-8d48ccf45022",
            )

            self.assertEqual(
                item["xero_bank_transaction_id"],
                "5adf26ce-f721-4717-93dc-8d48ccf45022",
            )
        finally:
            if os.path.exists(path):
                os.unlink(path)

    def test_xero_bank_transaction_url_points_to_xero_bank_transaction(self):
        self.assertEqual(
            _xero_bank_transaction_url("abc 123"),
            "https://go.xero.com/Bank/ViewTransaction.aspx?bankTransactionID=abc%20123",
        )

    def test_supplier_profile_is_persisted_on_dump_batch(self):
        fd, path = tempfile.mkstemp()
        os.close(fd)
        try:
            batch = dump_store.create_batch(
                path, label="RingGo batch", supplier_profile="ringgo"
            )
            self.assertEqual(batch["supplier_profile"], "ringgo")
        finally:
            if os.path.exists(path):
                os.unlink(path)


if __name__ == "__main__":
    unittest.main()
