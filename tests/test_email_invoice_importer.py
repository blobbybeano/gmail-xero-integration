import unittest

from app.receipts.email_pipeline import is_own_company_sender, reconcile_amounts


OWN_NAMES = ["Power Wash", "Power Wash Ltd", "Pow Wash", "Powwash"]
OWN_DOMAINS = ["powwash.co.uk"]


class EmailInvoiceImporterTests(unittest.TestCase):
    def test_skips_invoice_sent_from_powwash_domain(self):
        self.assertTrue(
            is_own_company_sender(
                "accounts@powwash.co.uk",
                "Power Wash Ltd",
                "Invoice INV-1234",
                own_names=OWN_NAMES,
                own_domains=OWN_DOMAINS,
            )
        )

    def test_skips_xero_invoice_copy_from_powwash(self):
        self.assertTrue(
            is_own_company_sender(
                "messaging-service@post.xero.com",
                "Xero",
                "Invoice INV-1234 from Power Wash Ltd",
                own_names=OWN_NAMES,
                own_domains=OWN_DOMAINS,
            )
        )

    def test_does_not_skip_supplier_invoice_addressed_to_powwash(self):
        self.assertFalse(
            is_own_company_sender(
                "messaging-service@post.xero.com",
                "Xero",
                "Invoice INV-9999 to Power Wash Ltd",
                own_names=OWN_NAMES,
                own_domains=OWN_DOMAINS,
            )
        )

    def test_does_not_skip_supplier_email_that_mentions_powwash_subject(self):
        self.assertFalse(
            is_own_company_sender(
                "billing@example-supplier.com",
                "Example Supplier",
                "Invoice for Power Wash Ltd",
                own_names=OWN_NAMES,
                own_domains=OWN_DOMAINS,
            )
        )

    def test_impossible_tax_does_not_make_positive_invoice_net_negative(self):
        self.assertEqual(
            reconcile_amounts(13.83, None, 55.32, vat_rate=20.0),
            (13.83, 11.53, 2.30),
        )

    def test_impossible_net_does_not_make_positive_invoice_vat_too_large(self):
        self.assertEqual(
            reconcile_amounts(13.83, -41.49, 55.32, vat_rate=20.0),
            (13.83, 11.53, 2.30),
        )

    def test_negative_credit_note_amounts_are_preserved(self):
        self.assertEqual(
            reconcile_amounts(-13.79, -11.49, -2.30, vat_rate=20.0),
            (-13.79, -11.49, -2.30),
        )


if __name__ == "__main__":
    unittest.main()
