import unittest
import tempfile

from app.receipts.email_pipeline import (
    dedup_against_receipts,
    derive_supplier_merchant,
    explicit_total_from_text,
    import_batch_items,
    is_own_company_sender,
    non_payable_document_reason,
    reconcile_amounts,
    reconcile_email_amounts_from_text,
    rule_based_categorise,
    tax_computation_not_supplier_invoice,
)
from app.receipts.email_store import (
    STATUS_POSSIBLE_DUP,
    STATUS_SUSPICIOUS,
    create_batch,
    create_item,
    list_items,
)
from app.receipts.expense_store import create_receipt


OWN_NAMES = ["Power Wash", "Power Wash Ltd", "Pow Wash", "Powwash"]
OWN_DOMAINS = ["powwash.co.uk"]

ACCOUNTS = [
    {"Code": "400", "Name": "Cleaning"},
    {"Code": "410", "Name": "Materials"},
    {"Code": "420", "Name": "Advertising"},
    {"Code": "430", "Name": "Motor Vehicle Expenses"},
    {"Code": "440", "Name": "Bank Charges"},
    {"Code": "450", "Name": "IT and Software Consumables"},
    {"Code": "460", "Name": "Machinery Fuel"},
    {"Code": "470", "Name": "Van Fuel"},
    {"Code": "480", "Name": "Rates"},
]


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

    def test_supplier_domain_fixes_ocr_merchant_typo(self):
        self.assertEqual(
            derive_supplier_merchant(
                "SCREVFIX",
                from_name="Screwfix",
                from_addr="online@screwfix.com",
                subject="Copy of invoice A26025085697. Please find your invoice attached.",
                own_names=OWN_NAMES,
            ),
            "Screwfix",
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

    def test_tax_computation_is_not_imported_as_supplier_invoice(self):
        reason = tax_computation_not_supplier_invoice(
            "Example Accountants",
            "Corporation Tax Computation CT600 tax payable amount due to HMRC £4,812.00",
        )
        self.assertIn("Tax computation", reason)

    def test_accountant_fee_invoice_is_still_allowed(self):
        reason = tax_computation_not_supplier_invoice(
            "Example Accountants",
            "Invoice number 1092 professional fees for preparing corporation tax return £450.00",
        )
        self.assertEqual(reason, "")

    def test_motor_insurance_with_tax_wording_is_still_allowed(self):
        reason = tax_computation_not_supplier_invoice(
            "David Llewelyn",
            "Motor Fleet Insurance Renewal outstanding direct debit mandate "
            "including insurance premium tax",
        )
        self.assertEqual(reason, "")

    def test_insurance_renewal_quote_is_not_imported_as_invoice(self):
        reason = non_payable_document_reason(
            "David Llewelyn",
            "Fleet Insurance Thank you for insuring with us for the last 12 months "
            "your policy is due for renewal. I recommend that you move your "
            "insurance to Zurich. Statement of Demands and Needs. Quote Schedule. "
            "We require confirmation that you wish to proceed with the renewal.",
        )
        self.assertIn("Insurance renewal quote", reason)

    def test_contract_document_is_not_imported_as_supplier_invoice(self):
        reason = non_payable_document_reason(
            "Indigo Service Solutions",
            "INDIGO SERVICE SOLUTIONS - CONTRACT.pdf Indigo Contracts "
            "service contract agreement schedule of services start date 21/04/2026",
        )
        self.assertIn("Contract/agreement", reason)

    def test_service_invoice_with_amount_due_is_still_allowed(self):
        reason = non_payable_document_reason(
            "Indigo Service Solutions",
            "Invoice number 8841 service contract monthly charge amount due £120.00",
        )
        self.assertEqual(reason, "")

    def test_supplier_statement_is_not_imported_as_new_invoice(self):
        reason = non_payable_document_reason(
            "CJH",
            "Statement from Redwood Wales Limited T/a CJH for POW Services Limited "
            "Statement for POW Services Limited As At 15Jun2026 balance 356.00",
        )
        self.assertIn("Supplier statement", reason)

    def test_bank_statement_is_not_imported_as_supplier_invoice(self):
        reason = non_payable_document_reason(
            "Lloyds",
            "LLOYDS BUSINESS ACCOUNT Your Transactions Sort Code Account Number "
            "Money In Money Out Balance on 01 March 2026 Statement period "
            "01 March 2026 to 31 March 2026",
        )
        self.assertIn("Bank statement", reason)

    def test_membership_statement_can_still_be_payable(self):
        reason = non_payable_document_reason(
            "Checkatrade",
            "Your latest Checkatrade membership statement subscription charge amount due £1599.58",
        )
        self.assertEqual(reason, "")

    def test_rac_final_total_wins_over_repeated_line_totals(self):
        raw = """
        CONFIRMATION OF PAYMENT
        Cover Period
        Total
        FD66LSK
        13/07/2026 - 12/08/2026
        £13.83
        CK12CZS
        13/07/2026 - 12/08/2026
        £13.83
        NV69DZL
        13/07/2026 - 12/08/2026
        £13.83
        NV21EJL
        13/07/2026 - 12/08/2026
        £13.83
        Premiums include IPT at the prevailing rate.
        Total including all applicable taxes
        £55.32
        """
        self.assertEqual(explicit_total_from_text(raw), 55.32)
        self.assertEqual(
            reconcile_email_amounts_from_text(13.83, None, 55.32, raw),
            (55.32, 55.32, 0.0),
        )

    def test_import_rechecks_duplicates_before_creating_receipt(self):
        with tempfile.NamedTemporaryFile(suffix=".sqlite") as tmp:
            create_receipt(
                tmp.name,
                engineer_id=1,
                merchant="Macmillan",
                purchased_on="2026-05-07",
                amount_inc=15.74,
                status="submitted",
            )
            batch = create_batch(tmp.name, label="Email scan")
            item = create_item(
                tmp.name,
                batch_id=batch["id"],
                seq=1,
                status="new",
                merchant="Macmillan",
                purchased_on="2026-05-07",
                amount_inc=15.74,
                amount_ex=13.12,
                vat_amount=2.62,
                currency="GBP",
            )

            self.assertEqual(
                import_batch_items(batch["id"], tmp.name, default_engineer_id=1),
                0,
            )
            refreshed = {
                row["id"]: row
                for row in list_items(tmp.name, batch["id"])
            }[item["id"]]
            self.assertEqual(refreshed["status"], STATUS_POSSIBLE_DUP)
            self.assertIn("Same merchant/date/amount", refreshed["dup_reason"])

    def test_import_refuses_ready_item_without_total(self):
        with tempfile.NamedTemporaryFile(suffix=".sqlite") as tmp:
            batch = create_batch(tmp.name, label="Email scan")
            item = create_item(
                tmp.name,
                batch_id=batch["id"],
                seq=1,
                status="new",
                merchant="P&P Pumps and Pressure",
                purchased_on="2026-05-06",
                amount_inc=None,
                amount_ex=None,
                vat_amount=None,
                currency="GBP",
                category_account_code="410",
                category_account_name="Materials",
            )

            self.assertEqual(
                import_batch_items(batch["id"], tmp.name, default_engineer_id=1),
                0,
            )
            refreshed = {
                row["id"]: row
                for row in list_items(tmp.name, batch["id"])
            }[item["id"]]
            self.assertEqual(refreshed["status"], STATUS_SUSPICIOUS)
            self.assertIn("No invoice total found", refreshed["dup_reason"])

    def test_import_only_selected_email_invoice_items(self):
        with tempfile.NamedTemporaryFile(suffix=".sqlite") as tmp:
            batch = create_batch(tmp.name, label="Email scan")
            chosen = create_item(
                tmp.name,
                batch_id=batch["id"],
                seq=1,
                status="new",
                merchant="Chosen Supplier",
                purchased_on="2026-07-01",
                amount_inc=12.0,
                amount_ex=10.0,
                vat_amount=2.0,
                currency="GBP",
            )
            skipped = create_item(
                tmp.name,
                batch_id=batch["id"],
                seq=2,
                status="new",
                merchant="Skipped Supplier",
                purchased_on="2026-07-02",
                amount_inc=18.0,
                amount_ex=15.0,
                vat_amount=3.0,
                currency="GBP",
            )

            self.assertEqual(
                import_batch_items(
                    batch["id"],
                    tmp.name,
                    default_engineer_id=1,
                    selected_item_ids={chosen["id"]},
                ),
                1,
            )
            rows = {
                row["id"]: row
                for row in list_items(tmp.name, batch["id"])
            }
            self.assertEqual(rows[chosen["id"]]["status"], "imported")
            self.assertEqual(rows[skipped["id"]]["status"], "new")

    def test_blank_hash_does_not_make_everything_duplicate(self):
        status, reason, match_id = dedup_against_receipts(
            "",
            "racsupplier",
            "2026-07-13",
            55.32,
            [
                {
                    "id": "exp-existing",
                    "stored_file": "/data/receipt_uploads/1234_abcd.jpg",
                    "merchant": "B&Q",
                    "purchased_on": "2026-04-03",
                    "amount_inc": 96.15,
                }
            ],
            [],
        )
        self.assertEqual(status, "new")
        self.assertEqual(reason, "")
        self.assertEqual(match_id, "")

    def test_supplier_rules_override_bad_generic_accounts(self):
        self.assertEqual(
            rule_based_categorise(
                "noreply@checkatrade.com",
                "Your latest Checkatrade membership statement",
                ACCOUNTS,
            ),
            ("420", "Advertising"),
        )
        self.assertEqual(
            rule_based_categorise("RAC Business Club", "Breakdown cover", ACCOUNTS),
            ("430", "Motor Vehicle Expenses"),
        )
        self.assertEqual(
            rule_based_categorise("Tender POS", "Card terminal transaction fee", ACCOUNTS),
            ("440", "Bank Charges"),
        )
        self.assertEqual(
            rule_based_categorise("ECA Cleaning Ltd", "Softwash cleaning solution", ACCOUNTS),
            ("410", "Materials"),
        )
        self.assertEqual(
            rule_based_categorise("Google Ads", "Search advertising campaign", ACCOUNTS),
            ("420", "Advertising"),
        )
        self.assertEqual(
            rule_based_categorise("Stripe", "Payment processing fees", ACCOUNTS),
            ("440", "Bank Charges"),
        )
        self.assertEqual(
            rule_based_categorise("Microsoft", "Microsoft 365 subscription", ACCOUNTS),
            ("450", "IT and Software Consumables"),
        )
        self.assertEqual(
            rule_based_categorise(
                "Merton Council",
                "Residents Permit (Pricing Band 6) controlled parking zone",
                ACCOUNTS,
            ),
            ("430", "Motor Vehicle Expenses"),
        )


if __name__ == "__main__":
    unittest.main()
