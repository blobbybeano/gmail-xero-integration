import unittest

from app.xero_client import XeroClient


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


if __name__ == "__main__":
    unittest.main()
