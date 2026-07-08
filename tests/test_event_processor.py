import unittest

from app.event_processor import (
    compute_invoice_totals,
    extract_invoice_lines,
    extract_sales_lines,
    normalize_user_sections,
    parse_invoice_contact_overrides,
    preserve_latest_user_controls,
    send_choice_is_no,
    send_choice_is_yes,
    sync_invoice_block_from_xero,
    upsert_invoice_summary,
    upsert_invoice_profile_missing_hint,
    upsert_no_email_confirmation,
    upsert_send_failure,
)


SALES_MARKER = "\u2b07Sales\u2b07"


class InvoiceSalesParsingTests(unittest.TestCase):
    def test_sales_section_enters_invoice_totals_once(self):
        description = (
            "[invoice]\n"
            "Driveway clean = \u00a3100+VAT\n"
            f"{SALES_MARKER}\n"
            "Upsell commission = \u00a350+VAT\n"
            "[/invoice]"
        )

        invoice_lines = extract_invoice_lines(description)
        sales_lines = extract_sales_lines(description)

        self.assertEqual(len(invoice_lines), 2)
        self.assertEqual(invoice_lines[0]["Description"], "Driveway clean")
        self.assertEqual(invoice_lines[0]["UnitAmount"], 100.0)
        self.assertEqual(invoice_lines[1]["Description"], "Upsell commission")
        self.assertEqual(invoice_lines[1]["UnitAmount"], 50.0)
        self.assertEqual(compute_invoice_totals(invoice_lines), (150.0, 180.0))
        self.assertEqual(len(sales_lines), 1)
        self.assertEqual(sales_lines[0]["Description"], "Upsell commission")
        self.assertEqual(sales_lines[0]["UnitAmount"], 50.0)

    def test_amount_only_sales_line_is_counted_below_sales_marker(self):
        description = (
            "[invoice]\n"
            "Pressure washing = \u00a3240.00+VAT\n"
            f"{SALES_MARKER}\n"
            "\u00a325+VAT\n"
            "[/invoice]"
        )

        invoice_lines = extract_invoice_lines(description)
        sales_lines = extract_sales_lines(description)

        self.assertEqual(len(invoice_lines), 2)
        self.assertEqual(invoice_lines[1]["Description"], "Sales item")
        self.assertEqual(invoice_lines[1]["UnitAmount"], 25.0)
        self.assertEqual(invoice_lines[1]["TaxType"], "OUTPUT2")
        self.assertEqual(compute_invoice_totals(invoice_lines), (265.0, 318.0))
        self.assertEqual(len(sales_lines), 1)
        self.assertEqual(sales_lines[0]["Description"], "Sales item")

    def test_amount_only_invoice_line_above_sales_marker_is_ignored(self):
        description = (
            "[invoice]\n"
            "\u00a325+VAT\n"
            f"{SALES_MARKER}\n"
            "[/invoice]"
        )

        self.assertEqual(extract_invoice_lines(description), [])
        self.assertEqual(extract_sales_lines(description), [])

    def test_sales_only_section_is_still_an_invoice(self):
        description = (
            "[invoice]\n"
            f"{SALES_MARKER}\n"
            "Sales tracking only = \u00a375\n"
            "[/invoice]"
        )

        self.assertEqual(len(extract_invoice_lines(description)), 1)
        self.assertEqual(len(extract_sales_lines(description)), 1)

    def test_non_vat_lines_send_explicit_no_vat_tax_type(self):
        description = (
            "[invoice]\n"
            "No VAT item = \u00a3100\n"
            "[/invoice]"
        )

        invoice_lines = extract_invoice_lines(description)

        self.assertEqual(invoice_lines[0]["TaxType"], "NONE")
        self.assertEqual(compute_invoice_totals(invoice_lines), (100.0, 100.0))

    def test_cash_marker_forces_explicit_no_vat_tax_type(self):
        description = (
            "[invoice]\n"
            "*cash*\n"
            "Driveway clean = \u00a3100+VAT\n"
            "[/invoice]"
        )

        invoice_lines = extract_invoice_lines(description)

        self.assertEqual(invoice_lines[0]["TaxType"], "NONE")
        self.assertEqual(compute_invoice_totals(invoice_lines), (100.0, 100.0))

    def test_blank_payment_prompt_is_not_prefilled_from_options(self):
        description = (
            "[contact]\n"
            "Customer name: Vishal Gupta\n"
            "[/contact]\n"
            "[app-status]\n"
            "PAYMENT TYPE (CARD/INVOICE) =\n"
            "SEND NOW (Y/N) =\n"
            "[/app-status]"
        )

        updated = upsert_invoice_summary(description, 155.0, 186.0, sent=False)

        self.assertIn("PAYMENT TYPE (CARD/INVOICE) =", updated)
        self.assertNotIn("PAYMENT TYPE (CARD/INVOICE) = CARD", updated)

    def test_explicit_payment_value_is_preserved(self):
        description = (
            "[app-status]\n"
            "PAYMENT TYPE (CARD/INVOICE) = CARD\n"
            "SEND NOW (Y/N) =\n"
            "[/app-status]"
        )

        updated = upsert_invoice_summary(description, 155.0, 186.0, sent=False)

        self.assertIn("PAYMENT TYPE (CARD/INVOICE) = CARD", updated)

    def test_send_now_no_is_explicit_and_blank_is_not_no(self):
        blank = "[app-status]\nSEND NOW (Y/N) =\n[/app-status]"
        yes = "[app-status]\nSEND NOW (Y/N) = Y\n[/app-status]"
        no = "[app-status]\nSEND NOW (Y/N) = N\n[/app-status]"

        self.assertFalse(send_choice_is_yes(blank))
        self.assertFalse(send_choice_is_no(blank))
        self.assertTrue(send_choice_is_yes(yes))
        self.assertFalse(send_choice_is_no(yes))
        self.assertFalse(send_choice_is_yes(no))
        self.assertTrue(send_choice_is_no(no))

    def test_no_email_confirmation_does_not_claim_invoice_was_emailed(self):
        description = (
            "[app-status]\n"
            "Invoice total (ex VAT): \u00a3100.00\n"
            "Invoice total (inc VAT): \u00a3120.00\n"
            "PAYMENT TYPE (CARD/INVOICE) = INVOICE\n"
            "SEND NOW (Y/N) = N\n"
            "[/app-status]"
        )

        updated = upsert_no_email_confirmation(description, invoice_url="https://xero.example/inv")

        self.assertIn("Invoice processed \u2705", updated)
        self.assertIn("Email skipped by SEND NOW = N", updated)
        self.assertIn("Invoice link: https://xero.example/inv", updated)
        self.assertNotIn("Invoice sent \u2705", updated)
        self.assertNotIn("SEND NOW (Y/N)", updated)

    def test_xero_503_send_failure_gets_temporary_guidance(self):
        updated = upsert_send_failure(
            "[app-status]\nPAYMENT TYPE (CARD/INVOICE) = CARD\n[/app-status]",
            "Xero invoice authorise failed: 503 upstream connect error or disconnect/reset before headers. reset reason: overflow",
        )

        self.assertIn("Xero send temporarily failed", updated)
        self.assertIn("Temporary Xero/API issue", updated)
        self.assertNotIn("Invoice send failed", updated)
        self.assertNotIn("Check customer e-mail", updated)
        self.assertIn("SEND NOW (Y/N) =", updated)

    def test_preserve_latest_process_y_over_stale_app_update(self):
        proposed = (
            "[invoice]\nGC = \u00a3100+VAT\n[/invoice]\n"
            "PROCESS DRAFT (Y/N) =\n\n"
            "[app-status]\nInvoice total (inc VAT): \u00a3120.00\n[/app-status]"
        )
        latest = (
            "[invoice]\nGC = \u00a3100+VAT\n[/invoice]\n"
            "PROCESS DRAFT (Y/N) =Y"
        )

        merged = preserve_latest_user_controls(proposed, latest)

        self.assertIn("PROCESS DRAFT (Y/N) =Y", merged)
        self.assertNotIn("PROCESS DRAFT (Y/N) =\n", merged)

    def test_preserve_latest_blank_process_over_stale_y(self):
        proposed = (
            "[invoice]\nGC = \u00a3100+VAT\n[/invoice]\n"
            "PROCESS DRAFT (Y/N) =Y"
        )
        latest = (
            "[invoice]\nGC = \u00a3100+VAT\n[/invoice]\n"
            "PROCESS DRAFT (Y/N) ="
        )

        merged = preserve_latest_user_controls(proposed, latest)

        self.assertIn("PROCESS DRAFT (Y/N) =", merged)
        self.assertNotIn("PROCESS DRAFT (Y/N) =Y", merged)

    def test_preserve_latest_controls_dedupes_duplicate_process_lines(self):
        proposed = (
            "[invoice]\nGC = \u00a3100+VAT\n[/invoice]\n"
            "PROCESS DRAFT (Y/N) =Y\n"
            "PROCESS DRAFT (Y/N) =Y"
        )
        latest = "PROCESS DRAFT (Y/N) =YY"

        merged = preserve_latest_user_controls(proposed, latest)

        self.assertEqual(merged.count("PROCESS DRAFT (Y/N) =Y"), 1)

    def test_email_send_failure_keeps_customer_email_guidance(self):
        updated = upsert_send_failure(
            "[app-status]\nPAYMENT TYPE (CARD/INVOICE) = INVOICE\n[/app-status]",
            None,
        )

        self.assertIn("Invoice send failed", updated)
        self.assertIn("Check customer e-mail", updated)
        self.assertIn("SEND NOW (Y/N) =", updated)

    def test_mirrored_sales_above_marker_are_ignored(self):
        description = (
            "[invoice]\n"
            "Gutter clean = \u00a3135+VAT\n"
            "Materials = \u00a314+VAT\n"
            "Gutter fix x2 = \u00a350+VAT\n"
            "Gutter fix x2 = \u00a350+VAT\n"
            f"{SALES_MARKER}\n"
            "Gutter fix x2 = \u00a350+VAT\n"
            "[/invoice]"
        )

        invoice_lines = extract_invoice_lines(description)

        self.assertEqual([li["Description"] for li in invoice_lines], [
            "Gutter clean",
            "Materials",
            "Gutter fix x2",
        ])
        self.assertEqual(compute_invoice_totals(invoice_lines), (199.0, 238.8))

    def test_normalize_removes_mirrored_sales_from_invoice_section(self):
        description = (
            "[invoice]\n"
            "Gutter clean = \u00a3135+VAT\n"
            "Gutter fix x2 = \u00a350+VAT\n"
            "Gutter fix x2 = \u00a350+VAT\n"
            f"{SALES_MARKER}\n"
            "Gutter fix x2 = \u00a350\n"
            "[/invoice]"
        )

        normalized = normalize_user_sections(description)

        self.assertEqual(normalized.count("Gutter fix x2"), 1)
        self.assertIn(SALES_MARKER, normalized)
        self.assertIn("Gutter fix x2 = <b>\u00a350</b>", normalized)

    def test_xero_sync_keeps_chargeable_sales_below_marker_only(self):
        description = (
            "[invoice]\n"
            "Gutter clean = \u00a3135+VAT\n"
            f"{SALES_MARKER}\n"
            "Gutter fix x2 = \u00a350\n"
            "[/invoice]"
        )
        line_items = [
            {"Description": "Gutter clean", "Quantity": 1, "UnitAmount": 135, "LineAmount": 135, "TaxType": "OUTPUT2"},
            {"Description": "Gutter fix x2", "Quantity": 1, "UnitAmount": 50, "LineAmount": 50, "TaxType": "OUTPUT2"},
            {"Description": "Gutter fix x2", "Quantity": 1, "UnitAmount": 50, "LineAmount": 50, "TaxType": "OUTPUT2"},
        ]

        synced = sync_invoice_block_from_xero(description, line_items)

        invoice_block = synced.split("[invoice]", 1)[1].split("[/invoice]", 1)[0]
        above_sales = invoice_block.split(SALES_MARKER, 1)[0]
        self.assertIn("Gutter clean = \u00a3135.00+VAT", above_sales)
        self.assertNotIn("Gutter fix x2", above_sales)
        self.assertEqual(synced.count("Gutter fix x2"), 1)

    def test_invoice_profile_typo_is_parsed_and_canonicalized(self):
        description = (
            "[contact]\n"
            "Customer contact number:07984431386\n"
            "Invoce profile: Sinead Gloster\n"
            "[/contact]"
        )

        overrides = parse_invoice_contact_overrides(description)
        normalized = normalize_user_sections(description)

        self.assertEqual(overrides["invoice_profile"], "Sinead Gloster")
        self.assertIn("Invoice profile: Sinead Gloster", normalized)
        self.assertNotIn("Invoce profile:", normalized)

    def test_invoice_profile_missing_hint_handles_typo(self):
        description = (
            "[contact]\n"
            "Invoce profile: Sinead Gloster\n"
            "[/contact]"
        )

        updated = upsert_invoice_profile_missing_hint(description, missing=True)

        self.assertIn("Invoice profile: Sinead Gloster \u274c Customer does not exist", updated)
        self.assertNotIn("Invoce profile:", updated)


if __name__ == "__main__":
    unittest.main()
