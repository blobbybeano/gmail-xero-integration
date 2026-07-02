import unittest

from app.receipts.email_pipeline import is_own_company_sender


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


if __name__ == "__main__":
    unittest.main()
