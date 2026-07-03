import unittest
import time
from types import SimpleNamespace
from unittest.mock import patch

from app.xero_client import XeroClient, build_xero_client


class _Client(XeroClient):
    def __init__(self, invoice):
        super().__init__("token", "tenant", dry_run=False)
        self.invoice = invoice

    def get_invoice(self, invoice_id):
        return self.invoice


class XeroLineItemUpdateTests(unittest.TestCase):
    def test_existing_line_item_ids_are_reused_once_per_desired_line(self):
        client = _Client(
            {
                "LineItems": [
                    {
                        "LineItemID": "keep-main",
                        "Description": "Gutter clean",
                        "Quantity": 1,
                        "UnitAmount": 135,
                        "TaxType": "OUTPUT2",
                        "AccountCode": "200",
                    },
                    {
                        "LineItemID": "keep-sale",
                        "Description": "Gutter fix x2",
                        "Quantity": 1,
                        "UnitAmount": 50,
                        "TaxType": "OUTPUT2",
                        "AccountCode": "200",
                    },
                    {
                        "LineItemID": "delete-duplicate-sale",
                        "Description": "Gutter fix x2",
                        "Quantity": 1,
                        "UnitAmount": 50,
                        "TaxType": "OUTPUT2",
                        "AccountCode": "200",
                    },
                ]
            }
        )

        prepared = client._attach_existing_line_item_ids(
            "invoice",
            [
                {
                    "Description": "Gutter clean",
                    "Quantity": 1,
                    "UnitAmount": 135,
                    "TaxType": "OUTPUT2",
                    "AccountCode": "200",
                },
                {
                    "Description": "Gutter fix x2",
                    "Quantity": 1,
                    "UnitAmount": 50,
                    "TaxType": "OUTPUT2",
                    "AccountCode": "200",
                },
            ],
        )

        self.assertEqual(prepared[0]["LineItemID"], "keep-main")
        self.assertEqual(prepared[1]["LineItemID"], "keep-sale")
        self.assertNotIn("delete-duplicate-sale", [li.get("LineItemID") for li in prepared])


class XeroClientBuilderSafetyTests(unittest.TestCase):
    def _config(self):
        return SimpleNamespace(
            state_file="missing-state.json",
            xero_token_file="xero-token.json",
            xero_client_id="cid",
            xero_client_secret="secret",
            xero_access_token="",
            xero_tenant_id="tenant",
            admin_db_file="admin.db",
            dry_run=False,
        )

    @patch.dict("os.environ", {"XERO_DISABLED": "true"})
    @patch("app.xero_client.refresh_xero_token")
    @patch("app.xero_client.load_xero_token")
    def test_disabled_xero_does_not_refresh_token(self, load_token, refresh_token):
        load_token.return_value = {"refresh_token": "rt", "expires_at": 0}

        self.assertIsNone(build_xero_client(self._config()))

        load_token.assert_not_called()
        refresh_token.assert_not_called()

    @patch.dict("os.environ", {}, clear=False)
    @patch("app.xero_client.refresh_xero_token")
    @patch("app.xero_client.load_xero_token")
    @patch("app.xero_client.persisted_xero_lockout_until")
    def test_active_persisted_lockout_does_not_refresh_token(
        self, lockout_until, load_token, refresh_token
    ):
        lockout_until.return_value = time.time() + 600
        load_token.return_value = {"refresh_token": "rt", "expires_at": 0}

        self.assertIsNone(build_xero_client(self._config()))

        load_token.assert_not_called()
        refresh_token.assert_not_called()


if __name__ == "__main__":
    unittest.main()
