import unittest
from pathlib import Path

import app.admin_web as admin_web

from app.admin_web import (
    _apply_receipt_account_guardrails,
    _bank_upload_nav_reminder,
    _cardfeed_csv_upload_summary,
    _exp_apply_vat_mode,
    _exp_repair_dump_item_values_from_raw,
    _exp_reconcile_amounts_from_text,
    _exp_vat_mode,
    _field_expense_account_is_bank_feed,
    _field_expense_bank_feed_candidate,
    _field_expense_xero_resolved_reason,
    _grouped_receipts_for_card_payment,
    _engineer_recon_cache_is_fresh,
    _marketplace_receipt_matches_card_name,
    _owner_paid_acct_options,
    _owner_paid_accounts_from,
    _parse_ringgo_dump_receipt,
    _payment_acct_options,
    _recon_cache_range_status,
    _receipt_xero_bill_near_duplicate,
    _receipt_xero_reference,
    _receipt_names_share_token,
    _receipt_feed_is_still_settling,
    _receipt_supplier_date_compatible,
    _receipt_segment_xero_tax_type,
    _resolve_expense_account_choice,
    _shared_company_card_receipts_for_account,
    _xero_contact_ref_for_merchant,
)
from app.receipts.service import _clean_receipt_merchant


class ReceiptAccountResolutionTests(unittest.TestCase):
    def setUp(self):
        self.accounts = [
            {"Code": "310", "Name": "Materials"},
            {"Code": "400", "Name": "Motor Vehicle Expenses"},
            {"Code": "401", "Name": "Motor Vehicle Fuel"},
            {"Code": "402", "Name": "Van Fuel"},
            {"Code": "403", "Name": "Machinery Fuel"},
            {"Code": "404", "Name": "Vehicle Repairs and Maintenance"},
            {"Code": "495", "Name": "Staff Amenities"},
            {"Code": "480", "Name": "Rates"},
        ]

    def test_accepts_real_xero_code(self):
        self.assertEqual(
            _resolve_expense_account_choice("310", self.accounts),
            ("310", "Materials"),
        )

    def test_normal_bank_feed_is_not_treated_like_credit_card_statement(self):
        self.assertTrue(_field_expense_account_is_bank_feed("60563768", "Pow Wash"))
        self.assertTrue(_field_expense_account_is_bank_feed("12345678", "Ben - Personal Bank"))
        self.assertFalse(_field_expense_account_is_bank_feed("0757", "Charge Card - Dan"))

    def test_bank_feed_filter_ignores_customer_receipts_and_keeps_card_markers(self):
        self.assertFalse(
            _field_expense_bank_feed_candidate(
                "NIGEL FOWLER 200000001652068235 RECEIPTS 601005 10 31OCT25 10:30",
                101.64,
            )
        )
        self.assertFalse(
            _field_expense_bank_feed_candidate("STRIPE PAYMENTS UK LTD", 295.10)
        )
        self.assertTrue(
            _field_expense_bank_feed_candidate("ASDA CD 6083 WALSALL GBR", 14.48)
        )

    def test_bank_feed_filter_keeps_known_supplier_spends_without_card_marker(self):
        self.assertTrue(
            _field_expense_bank_feed_candidate("CHECKATRADE 6T83NRZ", 851.40)
        )
        self.assertTrue(
            _field_expense_bank_feed_candidate(
                "BROWN&BROWN 600000001780878410 86724492 400194 10 09JUN26 09:09",
                125.00,
            )
        )
        self.assertTrue(
            _field_expense_bank_feed_candidate("XERO UK LTD 3NCYGRB3FTBGYAPM1W", 50.40)
        )
        self.assertTrue(
            _field_expense_bank_feed_candidate(
                "TFL CONGESTN CHRGE 201002109000894353", 30.50
            )
        )
        self.assertFalse(
            _field_expense_bank_feed_candidate("LOAN - 03213746", 335.99)
        )

    def test_recent_receipt_waits_for_card_feed_to_settle(self):
        receipt_day = admin_web.dt.date(2026, 7, 27)
        self.assertTrue(
            _receipt_feed_is_still_settling(
                receipt_day, admin_web.dt.date(2026, 7, 27)
            )
        )
        self.assertTrue(
            _receipt_feed_is_still_settling(
                receipt_day, admin_web.dt.date(2026, 7, 29)
            )
        )
        self.assertFalse(
            _receipt_feed_is_still_settling(
                receipt_day, admin_web.dt.date(2026, 7, 30)
            )
        )

    def test_receipt_feed_settlement_window_skips_weekends(self):
        friday = admin_web.dt.date(2026, 7, 24)
        self.assertTrue(
            _receipt_feed_is_still_settling(
                friday, admin_web.dt.date(2026, 7, 27)
            )
        )
        self.assertFalse(
            _receipt_feed_is_still_settling(
                friday, admin_web.dt.date(2026, 7, 29)
            )
        )

    def test_grouped_amazon_receipts_match_one_card_payment(self):
        receipts = [
            {
                "id": f"r{idx}",
                "merchant": "Amazon",
                "amount_inc": amount,
                "purchased_on": day,
                "status": "approved",
            }
            for idx, (amount, day) in enumerate(
                [
                    (14.99, "2026-04-02"),
                    (14.19, "2026-04-03"),
                    (21.99, "2026-04-02"),
                    (12.34, "2026-04-02"),
                    (11.98, "2026-04-03"),
                ]
            )
        ]
        grouped = _grouped_receipts_for_card_payment(
            75.49,
            admin_web.dt.date(2026, 4, 7),
            "AMAZON* NB7YI6M74 CD 6075",
            receipts,
        )
        self.assertEqual({r["id"] for r in grouped}, {"r0", "r1", "r2", "r3", "r4"})

    def test_grouped_matching_does_not_combine_unrelated_suppliers(self):
        grouped = _grouped_receipts_for_card_payment(
            30.00,
            admin_web.dt.date(2026, 4, 7),
            "SCREWFIX CD 6075",
            [
                {
                    "id": "r1",
                    "merchant": "Screwfix",
                    "amount_inc": 10.00,
                    "purchased_on": "2026-04-06",
                },
                {
                    "id": "r2",
                    "merchant": "Screwfix",
                    "amount_inc": 20.00,
                    "purchased_on": "2026-04-06",
                },
            ],
        )
        self.assertEqual(grouped, [])

    def test_amazon_receipt_cannot_consume_unrelated_same_value_payment(self):
        self.assertFalse(
            _marketplace_receipt_matches_card_name(
                "Amazon", "SCREWFIX CD 6075"
            )
        )
        self.assertFalse(
            _marketplace_receipt_matches_card_name(
                "Screwfix", "AMZNMktplace*NB7YI6M74 CD 6075"
            )
        )
        self.assertTrue(
            _marketplace_receipt_matches_card_name(
                "Amazon (Supplier - MCD Company Ltd)",
                "AMAZON* NB7YI6M74 CD 6075",
            )
        )

    def test_distant_same_value_receipt_requires_supplier_overlap(self):
        receipt_day = admin_web.dt.date(2026, 7, 17)
        self.assertFalse(
            _receipt_supplier_date_compatible(
                "Jennychem",
                "REPLIT INC. US 73.80 VISAXR",
                receipt_day,
                admin_web.dt.date(2026, 6, 22),
            )
        )
        self.assertTrue(
            _receipt_supplier_date_compatible(
                "Jennychem",
                "SP JENNYCHEM CD 5311 18JUL26",
                receipt_day,
                admin_web.dt.date(2026, 7, 20),
            )
        )
        self.assertTrue(
            _receipt_supplier_date_compatible(
                "Unclear OCR name",
                "CARD PURCHASE",
                receipt_day,
                admin_web.dt.date(2026, 7, 18),
            )
        )

    def test_shared_feed_uses_both_owners_receipts_without_changing_ownership(self):
        engineers = [
            {"id": 3, "name": "Ben", "plaid_account_id": "shared"},
            {"id": 4, "name": "Yasmin", "plaid_account_id": "shared"},
            {"id": 5, "name": "Dan", "plaid_account_id": "other"},
        ]
        receipts = [
            {"id": "ben", "engineer_id": 3, "payment_source": "company_card"},
            {"id": "yas", "engineer_id": 4, "payment_source": "company_card"},
            {"id": "personal", "engineer_id": 4, "payment_source": "owner_paid"},
            {"id": "dan", "engineer_id": 5, "payment_source": "company_card"},
            {
                "id": "override",
                "engineer_id": 5,
                "payment_source": "company_card",
                "feed_account_id_override": "shared",
            },
        ]
        shared = _shared_company_card_receipts_for_account(
            receipts, engineers, "shared"
        )
        self.assertEqual([r["id"] for r in shared], ["ben", "yas", "override"])
        self.assertEqual([r["engineer_id"] for r in shared], [3, 4, 5])

    def test_engineer_recon_cache_refreshes_legacy_six_hour_entries_early(self):
        now = 10_000.0
        self.assertTrue(
            _engineer_recon_cache_is_fresh(
                {"refreshed_at": now - 60, "until": now + 840}, now
            )
        )
        self.assertFalse(
            _engineer_recon_cache_is_fresh(
                {"refreshed_at": now - 901, "until": now + 1000}, now
            )
        )
        legacy_created = now - 901
        self.assertFalse(
            _engineer_recon_cache_is_fresh(
                {"until": legacy_created + 21600}, now
            )
        )

    def test_xero_resolved_match_allows_grouped_amazon_payments(self):
        reason = _field_expense_xero_resolved_reason(
            39.98,
            __import__("datetime").date(2025, 12, 5),
            "AMAZON* Z14M22T44 CD 2215",
            [
                [17.01, "2025-12-05", "reconciled", "Amazon"],
                [6.98, "2025-12-05", "payment", "Amazon DS-AEU-INV-GB-2025-701251396"],
                [15.99, "2025-12-05", "payment", "Amazon DS-AEU-INV-GB-2025-701251377"],
            ],
        )
        self.assertEqual(reason, "reconciled")

    def test_xero_resolved_match_treats_amzn_marketplace_as_amazon(self):
        reason = _field_expense_xero_resolved_reason(
            207.42,
            __import__("datetime").date(2025, 12, 16),
            "AMZNMktplace*ZE769 CD 6067",
            [
                [163.78, "2025-12-16", "payment", "Amazon GB500J4JY3FRXI"],
                [17.07, "2025-12-16", "payment", "Amazon GB500UKA45Y251"],
                [26.57, "2025-12-16", "payment", "Amazon DS-AEU-INV-GB-2025-731892959"],
            ],
        )
        self.assertEqual(reason, "reconciled")

    def test_xero_resolved_match_allows_supplier_payment_plus_adjustment(self):
        reason = _field_expense_xero_resolved_reason(
            30.59,
            __import__("datetime").date(2026, 5, 11),
            "SP EQUIP2CLEAN CD 6083",
            [
                [25.64, "2026-05-11", "payment", "Payment: Equip2Clean"],
                [4.95, "2026-05-11", "reconciled", "Reconciliation adjustment"],
            ],
        )
        self.assertEqual(reason, "reconciled")

    def test_xero_resolved_match_rejects_adjustment_without_supplier_overlap(self):
        reason = _field_expense_xero_resolved_reason(
            30.59,
            __import__("datetime").date(2026, 5, 11),
            "SP EQUIP2CLEAN CD 6083",
            [
                [25.64, "2026-05-11", "payment", "Completely Different Supplier"],
                [4.95, "2026-05-11", "reconciled", "Reconciliation adjustment"],
            ],
        )
        self.assertEqual(reason, "")

    def test_xero_resolution_rejects_a_different_bank_account(self):
        reason = _field_expense_xero_resolved_reason(
            55.00,
            admin_web.dt.date(2026, 7, 20),
            "SP JENNYCHEM CD 5311",
            [[
                55.00,
                "2026-07-20",
                "reconciled",
                "Jennychem",
                "different-account-id",
                "Other bank",
            ]],
            xero_account_id="pow-account-id",
            xero_account_name="Pow Wash",
        )
        self.assertEqual(reason, "")

    def test_xero_resolution_accepts_the_configured_bank_account(self):
        reason = _field_expense_xero_resolved_reason(
            55.00,
            admin_web.dt.date(2026, 7, 20),
            "SP JENNYCHEM CD 5311",
            [[
                55.00,
                "2026-07-20",
                "reconciled",
                "Jennychem",
                "pow-account-id",
                "Pow Wash",
            ]],
            xero_account_id="id:pow-account-id",
            xero_account_name="Pow Wash",
        )
        self.assertEqual(reason, "reconciled")

    def test_attached_supplier_bill_can_match_its_later_card_posting(self):
        reason = _field_expense_xero_resolved_reason(
            55.00,
            admin_web.dt.date(2026, 7, 20),
            "SP JENNYCHEM CD 5311 18JUL26",
            [[55.00, "2026-07-17", "xero bill", "Jennychem"]],
        )
        self.assertEqual(reason, "xero bill")

    def test_attached_supplier_bill_requires_supplier_overlap(self):
        reason = _field_expense_xero_resolved_reason(
            55.00,
            admin_web.dt.date(2026, 7, 20),
            "SP JENNYCHEM CD 5311 18JUL26",
            [[55.00, "2026-07-20", "xero bill", "Different Supplier"]],
        )
        self.assertEqual(reason, "")

    def test_recon_cache_overlap_is_not_full_coverage(self):
        cache = {
            "start": "2026-01-01",
            "end": "2026-07-24",
            "lines": [[30.59, "2026-05-11", "reconciled", "Equip2Clean"]],
        }
        fully_covers, overlaps = _recon_cache_range_status(
            cache, "2025-09-01", "2026-07-24"
        )
        self.assertFalse(fully_covers)
        self.assertTrue(overlaps)

    def test_recon_cache_full_coverage_is_detected(self):
        cache = {
            "start": "2025-09-01",
            "end": "2026-07-24",
            "lines": [[30.59, "2026-05-11", "reconciled", "Equip2Clean"]],
        }
        fully_covers, overlaps = _recon_cache_range_status(
            cache, "2025-09-01", "2026-07-24"
        )
        self.assertTrue(fully_covers)
        self.assertTrue(overlaps)

    def test_resolves_unambiguous_account_name(self):
        self.assertEqual(
            _resolve_expense_account_choice("materials", self.accounts),
            ("310", "Materials"),
        )

    def test_rejects_ambiguous_name_fragment(self):
        self.assertEqual(
            _resolve_expense_account_choice("motor vehicle", self.accounts),
            ("", ""),
        )

    def test_rejects_unknown_saved_text(self):
        self.assertEqual(
            _resolve_expense_account_choice("random fallback", self.accounts),
            ("", ""),
        )

    def test_tyres_override_materials_to_vehicle_maintenance(self):
        _segments, code, name = _apply_receipt_account_guardrails(
            [], "310", "Materials", 144.00, self.accounts,
            "Kwik Fit Tyres", "wheel alignment tyre replacement",
        )
        self.assertEqual((code, name), ("404", "Vehicle Repairs and Maintenance"))

    def test_non_fuel_receipt_cannot_stay_as_fuel(self):
        _segments, code, name = _apply_receipt_account_guardrails(
            [], "402", "Van Fuel", 18.00, self.accounts,
            "Corner Shop", "cleaning cloths storage box",
        )
        self.assertEqual((code, name), ("", ""))

    def test_diesel_goes_to_van_fuel(self):
        _segments, code, name = _apply_receipt_account_guardrails(
            [], "310", "Materials", 35.00, self.accounts,
            "Shell", "diesel pump 4 litres",
        )
        self.assertEqual((code, name), ("402", "Van Fuel"))

    def test_unleaded_overrides_generic_account_to_machinery_fuel(self):
        _segments, code, name = _apply_receipt_account_guardrails(
            [], "449", "Motor Vehicle Expenses", 15.74, self.accounts,
            "MFG Queens Service Station",
            "SALE Unleaded Pump6 10.23L Total 15.74",
        )
        self.assertEqual((code, name), ("403", "Machinery Fuel"))

    def test_unleaded_uses_machinery_fuel_even_without_van_fuel(self):
        accounts = [
            {"Code": "310", "Name": "Materials"},
            {"Code": "449", "Name": "Machinery Expenses"},
            {"Code": "451", "Name": "Machinery Fuel"},
        ]
        _segments, code, name = _apply_receipt_account_guardrails(
            [], "449", "Machinery Expenses", 20.00, accounts,
            "Shell Co-op Hilton Park",
            "FS Unleaded Pump 6 12.91L Total GBP 20.00",
        )
        self.assertEqual((code, name), ("451", "Machinery Fuel"))

    def test_shell_garages_unleaded_overrides_repairs_to_fuel(self):
        _segments, code, name = _apply_receipt_account_guardrails(
            [], "473", "Repairs & Maintenance", 9.81, self.accounts,
            "Shell Rainbow Salford",
            "Rainbow Garages Ltd CUSTOMER RECEIPT ARTICLE *FS Unleaded "
            "PUMP 6 6.33 litres amount GBP 9.81",
        )
        self.assertEqual((code, name), ("403", "Machinery Fuel"))

    def test_adblue_vehicle_fluid_overrides_van_fuel_to_maintenance(self):
        _segments, code, name = _apply_receipt_account_guardrails(
            [], "402", "Van Fuel", 7.99, self.accounts,
            "Home Bargains",
            "10L ADBLUE-SCR DIESEL VEHICLES total to pay 7.99",
        )
        self.assertEqual((code, name), ("404", "Vehicle Repairs and Maintenance"))

    def test_food_receipt_overrides_materials_to_staff_amenities(self):
        _segments, code, name = _apply_receipt_account_guardrails(
            [], "310", "Materials", 12.40, self.accounts,
            "Tesco Express",
            "meal deal sandwich crisps drink total 12.40",
        )
        self.assertEqual((code, name), ("495", "Staff Amenities"))

    def test_food_segment_recoded_without_splitting_payment(self):
        segments = [
            {
                "label": "Diesel fuel",
                "account_code": "402",
                "account_name": "Van Fuel",
                "gross": 55.00,
                "net": 45.83,
                "vat": 9.17,
                "vat_rate": 20,
            },
            {
                "label": "Food and drinks",
                "account_code": "310",
                "account_name": "Materials",
                "gross": 8.50,
                "net": 8.50,
                "vat": 0,
                "vat_rate": 0,
            },
        ]
        new_segments, code, name = _apply_receipt_account_guardrails(
            segments, "402", "Van Fuel", 63.50, self.accounts,
            "Service Station",
            "diesel pump sandwich drink total 63.50",
        )
        self.assertEqual(new_segments[1]["account_code"], "495")
        self.assertEqual(new_segments[1]["account_name"], "Staff Amenities")
        self.assertEqual((code, name), ("402", "Van Fuel"))

    def test_gsf_car_parts_overrides_fuel_to_repairs(self):
        _segments, code, name = _apply_receipt_account_guardrails(
            [], "450", "Van Fuel", 57.08, self.accounts,
            "GSF Car Parts Limited", "car parts battery wiper blade",
        )
        self.assertEqual((code, name), ("404", "Vehicle Repairs and Maintenance"))

    def test_vehicle_repairs_fallback_uses_repairs_account(self):
        accounts = [
            {"Code": "310", "Name": "Materials"},
            {"Code": "449", "Name": "Motor Vehicle Expenses"},
            {"Code": "473", "Name": "Repairs & Maintenance"},
            {"Code": "450", "Name": "Van Fuel"},
        ]
        _segments, code, name = _apply_receipt_account_guardrails(
            [], "310", "Materials", 72.00, accounts,
            "E&M Commercial Repairs", "van service diagnostic repair",
        )
        self.assertEqual((code, name), ("473", "Repairs & Maintenance"))

    def test_screwfix_overrides_vehicle_default_to_materials(self):
        _segments, code, name = _apply_receipt_account_guardrails(
            [], "449", "Motor Vehicle Expenses", 28.54, self.accounts,
            "SCREWFIX", "sealant screws fixings",
        )
        self.assertEqual((code, name), ("310", "Materials"))

    def test_screwfix_gutter_parts_override_repairs_to_materials(self):
        accounts = [
            {"Code": "310", "Name": "Materials"},
            {"Code": "473", "Name": "Repairs & Maintenance"},
            {"Code": "404", "Name": "Vehicle Repairs and Maintenance"},
        ]
        _segments, code, name = _apply_receipt_account_guardrails(
            [], "473", "Repairs & Maintenance", 28.54, accounts,
            "Screwfix Direct Ltd",
            "Union Bracket 112mm Run Outlet Black 112mm 90 Angle Black 112mm "
            "returns cancellation policy repair replacement faulty goods",
        )
        self.assertEqual((code, name), ("310", "Materials"))

    def test_wickes_defaults_to_materials_not_repairs(self):
        accounts = [
            {"Code": "310", "Name": "Materials"},
            {"Code": "473", "Name": "Repairs & Maintenance"},
            {"Code": "404", "Name": "Vehicle Repairs and Maintenance"},
        ]
        _segments, code, name = _apply_receipt_account_guardrails(
            [], "473", "Repairs & Maintenance", 42.18, accounts,
            "Wickes",
            "Trade receipt assorted sundries decorators caulk packers fittings",
        )
        self.assertEqual((code, name), ("310", "Materials"))

    def test_b_and_q_defaults_to_materials_not_repairs(self):
        accounts = [
            {"Code": "310", "Name": "Materials"},
            {"Code": "473", "Name": "Repairs & Maintenance"},
            {"Code": "404", "Name": "Vehicle Repairs and Maintenance"},
        ]
        _segments, code, name = _apply_receipt_account_guardrails(
            [], "473", "Repairs & Maintenance", 18.95, accounts,
            "B&Q",
            "paint brush roller tray wall plugs assorted hardware",
        )
        self.assertEqual((code, name), ("310", "Materials"))

    def test_halfords_defaults_to_vehicle_maintenance_not_materials(self):
        _segments, code, name = _apply_receipt_account_guardrails(
            [], "310", "Materials", 26.50, self.accounts,
            "Halfords", "screenwash wiper blade bulb",
        )
        self.assertEqual((code, name), ("404", "Vehicle Repairs and Maintenance"))

    def test_residents_permit_overrides_rates_to_vehicle(self):
        _segments, code, name = _apply_receipt_account_guardrails(
            [], "480", "Rates", 71.00, self.accounts,
            "Merton Council",
            "Residents Permit (Pricing Band 6) controlled parking zone",
        )
        self.assertEqual((code, name), ("400", "Motor Vehicle Expenses"))

    def test_diesel_without_van_fuel_uses_vehicle_not_machinery(self):
        accounts = [
            {"Code": "310", "Name": "Materials"},
            {"Code": "449", "Name": "Motor Vehicle Expenses"},
            {"Code": "451", "Name": "Machinery Fuel"},
        ]
        _segments, code, name = _apply_receipt_account_guardrails(
            [], "451", "Machinery Fuel", 35.00, accounts,
            "Shell", "diesel pump 4 litres",
        )
        self.assertEqual((code, name), ("449", "Motor Vehicle Expenses"))

    def test_receipt_amount_uses_final_total_not_first_row(self):
        total, net, tax, zero_rated = _exp_reconcile_amounts_from_text(
            6.50,
            None,
            None,
            "B&Q\nLine item £6.50\nLine item £15.20\nTotal £96.15",
            20.0,
        )
        self.assertEqual((total, net, tax, zero_rated), (96.15, 80.13, 16.02, 0.0))

    def test_receipt_amount_drops_impossible_vat_from_ocr(self):
        total, net, tax, zero_rated = _exp_reconcile_amounts_from_text(
            15.49,
            12.91,
            26.00,
            "MFG Foxley Service Station\nPump4 10.2L\nTotal £15.49\nVAT Rate Ex.VAT Inc. VAT",
            20.0,
        )
        self.assertEqual((total, net, tax, zero_rated), (15.49, 12.91, 2.58, 0.0))

    def test_card_receipt_uses_paid_total_over_vat_summary_net(self):
        total, net, tax, zero_rated = _exp_reconcile_amounts_from_text(
            5.48,
            5.48,
            0.62,
            "CUSTOMER RECEIPT\nTotal 6.10\nVAT rate Excl VAT Incl\n"
            "Total 5.48 0.62 6.10\nCard payment APPROVED Amount 6.10 GBP",
            20.0,
        )
        self.assertEqual((total, net, tax, zero_rated), (6.10, 5.48, 0.62, 0.0))

    def test_general_receipt_repairs_net_misread_as_total(self):
        total, net, tax, zero_rated = _exp_reconcile_amounts_from_text(
            5.48,
            5.48,
            0.62,
            "Food store receipt\nTotal paid £6.10\nVAT £0.62",
            20.0,
        )
        self.assertEqual((total, net, tax, zero_rated), (6.10, 5.48, 0.62, 0.0))

    def test_strong_paid_total_overrides_wrong_ocr_total(self):
        total, net, tax, zero_rated = _exp_reconcile_amounts_from_text(
            28.37,
            24.44,
            3.93,
            "Toolstation\nTotal paid £23.60\nVAT £3.93",
            20.0,
        )
        self.assertEqual((total, net, tax, zero_rated), (23.60, 19.67, 3.93, 0.0))

    def test_amazon_receipt_uses_total_payable_over_vat_row(self):
        raw = """
        amazon.co.uk
        Paid
        Sold by Graff-City Ltd
        Total payable
        11.05.2026
        GB64UB0DVAEUD
        £19.99
        Unit price (excl. VAT) £16.66
        VAT rate 20%
        Invoice total
        £0.00
        £0.00
        £19.99
        VAT rate
        Item subtotal (excl. VAT)
        VAT subtotal
        Total
        20%
        £16.66
        £3.33
        """
        self.assertEqual(
            _exp_reconcile_amounts_from_text(16.66, 13.88, 2.78, raw, 20.0),
            (19.99, 16.66, 3.33, 0.0),
        )

    def test_receipt_merchant_cleans_duplicate_logo_text(self):
        self.assertEqual(_clean_receipt_merchant("B&Q\nB&Q", "B&Q till receipt"), "B&Q")

    def test_receipt_merchant_uses_supplier_when_ocr_is_screen_noise(self):
        self.assertEqual(_clean_receipt_merchant("08:59", "SCREWFIX DIRECT LTD invoice"), "Screwfix")
        self.assertEqual(_clean_receipt_merchant("Hello!", "Toolstation order receipt"), "Toolstation")

    def test_receipt_merchant_fixes_known_supplier_ocr_typo(self):
        self.assertEqual(_clean_receipt_merchant("ZY SCREWFIX", "www.screwfix.com"), "Screwfix")

    def test_receipt_merchant_normalises_ringgo_contact_variants(self):
        for value in (
            "Ringgo", "Ring Go", "RINGGO PARKING", "Ringo",
            "RingGo Parking Limited",
        ):
            self.assertEqual(
                _clean_receipt_merchant(value, value),
                "RingGo Parking",
            )
        self.assertEqual(
            _clean_receipt_merchant("Parking", "Payment to RINGGO CD 6067"),
            "RingGo Parking",
        )

    def test_ringgo_dump_profile_extracts_and_reconciles_mixed_vat(self):
        raw = """VAT RECEIPT (COPY)
Date of issue: 06 Jan 2026
Receipt number: LBMERTW-2026-01-06-04239
Quantity Description Cost VAT rate VAT net Total
London Borough of Merton charges
1 Parking Session £1.10 0% nil £1.10
Total Operator Charges £1.10 £0.00 £1.10
RingGo charges
1 RingGo Convenience Fee £0.17 20% £0.03 £0.20
2 Text Messages £0.17 20% £0.03 £0.20
Total RingGo Charges £0.34 £0.06 £0.40
Total £1.44 £0.06 £1.50
Please note that on-street parking charges are not subject to VAT.
RingGo Ltd
VAT Registration Number GB 636 1371 49"""
        parsed = _parse_ringgo_dump_receipt(
            raw, [{"Code": "449", "Name": "Motor Vehicle Expenses", "Status": "ACTIVE"}]
        )

        self.assertTrue(parsed["ok"])
        self.assertEqual(parsed["merchant"], "RingGo Parking")
        self.assertEqual(parsed["date"], "2026-01-06")
        self.assertEqual(
            (parsed["net"], parsed["tax"], parsed["total"]),
            (1.44, 0.06, 1.50),
        )
        self.assertEqual(len(parsed["segments"]), 3)
        self.assertEqual(
            [(s["gross"], s["net"], s["vat"], s["vat_rate"])
             for s in parsed["segments"]],
            [(1.10, 1.10, 0.00, 0.0),
             (0.20, 0.17, 0.03, 20.0),
             (0.20, 0.17, 0.03, 20.0)],
        )
        self.assertEqual(
            [_receipt_segment_xero_tax_type(s["vat_rate"])
             for s in parsed["segments"]],
            ["NONE", "INPUT2", "INPUT2"],
        )

    def test_ringgo_dump_profile_fails_closed_on_one_penny_mismatch(self):
        raw = """VAT RECEIPT (COPY)
Date of issue: 06 Jan 2026
Receipt number: LBMERTW-2026-01-06-04239
Total Operator Charges £1.10 £0.00 £1.10
RingGo charges
1 RingGo Convenience Fee £0.17 20% £0.03 £0.20
2 Text Messages £0.17 20% £0.03 £0.20
Total RingGo Charges £0.34 £0.06 £0.40
Total £1.44 £0.06 £1.51
RingGo Ltd"""
        parsed = _parse_ringgo_dump_receipt(
            raw, [{"Code": "449", "Name": "Parking"}]
        )
        self.assertFalse(parsed["ok"])
        self.assertIn("reconcile", parsed["error"].lower())

    def test_ringgo_dump_profile_requires_configured_xero_account(self):
        raw = """VAT RECEIPT
Date of issue: 06 Jan 2026
Receipt number: LBMERTW-2026-01-06-04239
Total Operator Charges £1.10 £0.00 £1.10
RingGo charges
1 RingGo Convenience Fee £0.17 20% £0.03 £0.20
2 Text Messages £0.17 20% £0.03 £0.20
Total RingGo Charges £0.34 £0.06 £0.40
Total £1.44 £0.06 £1.50
RingGo Ltd"""
        parsed = _parse_ringgo_dump_receipt(
            raw, [{"Code": "400", "Name": "Motor Vehicle Expenses"}]
        )
        self.assertFalse(parsed["ok"])
        self.assertIn("parking expense account", parsed["error"].lower())

    def test_ringgo_xero_writes_use_the_existing_canonical_contact(self):
        self.assertEqual(
            _xero_contact_ref_for_merchant("Ringo"),
            {"ContactID": "211f5735-c074-4528-b85a-d46caa67206d"},
        )
        self.assertEqual(
            _xero_contact_ref_for_merchant("Screwfix"),
            {"Name": "Screwfix"},
        )

    def test_receipt_merchant_normalises_amazon_marketplace_invoice(self):
        raw = """
        amazon.co.uk
        Sold by Cleva Europe Limited
        Order # 202-1234567-1234567
        Payment reference ID GB123
        Total payable £19.99
        """
        self.assertEqual(
            _clean_receipt_merchant("Cleva Europe Limited", raw),
            "Amazon (Supplier - Cleva Europe Limited)",
        )
        self.assertEqual(
            _clean_receipt_merchant("BENJAMIN OLIVER", raw),
            "Amazon (Supplier - Cleva Europe Limited)",
        )

    def test_receipt_merchant_keeps_amazon_anchor_when_supplier_is_only_ocr_merchant(self):
        raw = """
        amazon.co.uk
        Order # 202-1234567-1234567
        Payment reference ID GB123
        Total payable £13.49
        """
        self.assertEqual(
            _clean_receipt_merchant("Graff-City Ltd", raw),
            "Amazon (Supplier - Graff-City Ltd)",
        )

    def test_receipt_merchant_uses_bank_details_when_own_company_was_read(self):
        raw = (
            "Pow Services Limited\n"
            "Flat 24 Stretford Court\n"
            "INVOICE\n"
            "Invoice No : 144203\n"
            "Invoice Date : 12.06.2026\n"
            "Bank Details: INDIGO SERVICE SOLUTIONS LTD A/c No: 95520011 "
            "Sort Code: 406384\n"
            "Invoice Total 84.00"
        )
        self.assertEqual(_clean_receipt_merchant("Pow Services Limited", raw), "Indigo Group")

    def test_xero_payment_card_line_requires_supplier_overlap(self):
        self.assertTrue(
            _receipt_names_share_token(
                "TESCO PAY AT PUMP 3832 STOKE-ON-TREN GBR",
                "Tesco HD 885614644",
            )
        )
        self.assertFalse(
            _receipt_names_share_token(
                "TESCO PAY AT PUMP 3832 STOKE-ON-TREN GBR",
                "Asda HD 885698745",
            )
        )
        self.assertTrue(
            _receipt_names_share_token(
                "THE RANGE STOKE ON TRENT GBR",
                "The Range HD 885614639",
            )
        )

    def test_xero_bill_near_duplicate_flags_supplier_date_amount_window(self):
        rec = {
            "merchant": "Checkatrade",
            "purchased_on": "2026-06-17",
            "amount_inc": 1599.58,
        }
        bill = {
            "contact": "Checkatrade Ltd",
            "date": "2026-06-20",
            "amount": 1599.58,
        }
        self.assertTrue(_receipt_xero_bill_near_duplicate(rec, bill))

    def test_xero_bill_near_duplicate_requires_supplier_overlap(self):
        rec = {
            "merchant": "Checkatrade",
            "purchased_on": "2026-06-17",
            "amount_inc": 1599.58,
        }
        bill = {
            "contact": "Amazon",
            "date": "2026-06-18",
            "amount": 1599.58,
        }
        self.assertFalse(_receipt_xero_bill_near_duplicate(rec, bill))

    def test_amazon_marketplace_invoice_defaults_to_materials(self):
        _segments, code, name = _apply_receipt_account_guardrails(
            [], "400", "Motor Vehicle Expenses", 19.99, self.accounts,
            "Graff-City Ltd",
            "amazon.co.uk Sold by Graff-City Ltd Total payable £19.99 marker pen tape physical goods",
        )
        self.assertEqual((code, name), ("310", "Materials"))

    def test_receipt_xero_reference_uses_submitter_supplier_date_amount(self):
        ref = _receipt_xero_reference(
            {
                "id": "exp-123456",
                "merchant": "Amazon Marketplace",
                "purchased_on": "2026-07-22",
                "amount_inc": 39.98,
                "filename": "IMG_2087.jpg",
            },
            {"name": "Ben Oliver"},
        )
        self.assertEqual(ref, "Receipt Ben 2026-07-22 Amazon Marketplace £39.98")
        self.assertNotIn("IMG_2087", ref)

    def test_vat_mode_detects_no_standard_and_mixed(self):
        self.assertEqual(_exp_vat_mode(12.00, 12.00, 0.00, 20.0), "no_vat")
        self.assertEqual(_exp_vat_mode(120.00, 100.00, 20.00, 20.0), "standard")
        self.assertEqual(_exp_vat_mode(20.00, 10.00, 2.00, 20.0), "mixed")

    def test_vat_mode_save_applies_user_choice(self):
        self.assertEqual(
            _exp_apply_vat_mode(15.49, 12.91, 2.58, "no_vat", 20.0),
            (15.49, 15.49, 0.0),
        )
        self.assertEqual(
            _exp_apply_vat_mode(120.00, 111.11, 8.89, "standard", 20.0),
            (120.00, 100.00, 20.00),
        )
        self.assertEqual(
            _exp_apply_vat_mode(20.00, 10.00, 2.00, "mixed", 20.0),
            (20.00, 10.00, 2.00),
        )

    def test_engineer_review_has_mixed_vat_allocation_and_linked_fields(self):
        source = Path(admin_web.__file__).read_text(encoding="utf-8")

        self.assertIn("id='mixed_vat_fields'", source)
        self.assertIn("VAT-rated portion (inc VAT)", source)
        self.assertIn("No-VAT portion", source)
        self.assertIn("vat.addEventListener('input'", source)
        self.assertIn("ex.addEventListener('input'", source)
        self.assertIn("selectMode('mixed')", source)
        self.assertIn('>Total paid</label>', source)
        self.assertIn('>VAT amount</label>', source)
        self.assertNotIn('>Total paid (inc VAT)</label>', source)

    def test_csv_upload_summary_separates_new_and_existing_items(self):
        self.assertEqual(
            _cardfeed_csv_upload_summary(2, 137, 14),
            "Processed 2 files: 137 new items added; "
            "14 items already existed in the app",
        )
        self.assertEqual(
            _cardfeed_csv_upload_summary(1, 0, 1, 2),
            "Processed 1 file: 0 new items added; "
            "1 item already existed in the app; "
            "2 items outside the 12-month window",
        )

    def test_bank_upload_nav_reminder_targets_bank_statement_only(self):
        self.assertTrue(_bank_upload_nav_reminder("/cardfeed", True))
        self.assertFalse(
            _bank_upload_nav_reminder("/receipts/expenses", True)
        )
        self.assertFalse(_bank_upload_nav_reminder("/cardfeed", False))

    def test_card_receipt_ignores_stray_ocr_fragment_before_real_total(self):
        raw = """
        Asda Stores Ltd
        Tue 12 May 2026 07:51:58
        Unleaded £7.13
        Total 996 £7.13
        Mastercard £7.13
        VAT Rate Ex.VAT VAT Inc.VAT
        20% 5.94 1.19 7.13
        """
        self.assertEqual(
            _exp_reconcile_amounts_from_text(996.00, 830.00, 166.00, raw, 20.0),
            (7.13, 5.94, 1.19, False),
        )

    def test_stale_dump_item_repairs_from_raw_before_import(self):
        raw = """
        Asda Stores Ltd
        Tue 12 May 2026 07:51:58
        Unleaded £7.13
        Total 996 £7.13
        Mastercard £7.13
        VAT Rate Ex.VAT VAT Inc.VAT
        20% 5.94 1.19 7.13
        """
        item = {
            "amount_inc": 996.00,
            "amount_ex": 830.00,
            "vat_amount": 166.00,
            "purchased_on": "2020-05-12",
            "ocr_raw": raw,
        }

        self.assertEqual(
            _exp_repair_dump_item_values_from_raw(item, 20.0),
            {
                "amount_inc": 7.13,
                "amount_ex": 5.94,
                "vat_amount": 1.19,
                "purchased_on": "2026-05-12",
            },
        )

    def test_owner_paid_accounts_are_bank_asset_or_liability_not_expense(self):
        accounts = [
            {"AccountID": "dan-card-id", "Name": "Charge Card - Dan", "Type": "BANK", "Status": "ACTIVE"},
            {"AccountID": "gocardless-id", "Name": "GoCardless-GBP", "Type": "BANK", "Status": "ACTIVE"},
            {"AccountID": "cash-id", "Name": "Cash account", "Type": "BANK", "Status": "ACTIVE"},
            {"AccountID": "powwash-id", "Code": "", "Name": "Pow Wash", "Type": "BANK", "Status": "ACTIVE"},
            {"AccountID": "ben-bank-id", "Code": "", "Name": "Ben - Personal Bank", "Type": "BANK", "Status": "ACTIVE"},
            {"Code": "700", "Name": "Cash", "Type": "CURRENT", "Status": "ACTIVE"},
            {"Code": "780", "Name": "Cashflow reconciliation", "Type": "CURRENT", "Status": "ACTIVE"},
            {"Code": "835", "Name": "Directors' Loan Account", "Type": "CURRLIAB", "Status": "ACTIVE"},
            {"Code": "310", "Name": "Materials", "Type": "EXPENSE", "Status": "ACTIVE"},
            {"Code": "200", "Name": "Sales", "Type": "REVENUE", "Status": "ACTIVE"},
        ]

        rows = _owner_paid_accounts_from(accounts)
        self.assertEqual(
            [r["Name"] for r in rows[:5]],
            [
                "Ben - Personal Bank",
                "Cash account",
                "Charge Card - Dan",
                "GoCardless-GBP",
                "Pow Wash",
            ],
        )
        self.assertEqual([r["Code"] for r in rows[-3:]], ["700", "780", "835"])

        html = _owner_paid_acct_options(accounts, "835", default_label="Choose")
        self.assertIn("<optgroup label='Bank'>", html)
        self.assertIn("value='id:dan-card-id'>Charge Card - Dan", html)
        self.assertIn("value='id:gocardless-id'>GoCardless-GBP", html)
        self.assertIn("value='id:cash-id'>Cash account", html)
        self.assertIn("value='id:ben-bank-id'>Ben - Personal Bank", html)
        self.assertIn("value='id:powwash-id'>Pow Wash", html)
        self.assertIn("<optgroup label='Assets'>", html)
        self.assertIn("<optgroup label='Liabilities'>", html)
        self.assertIn("Directors&#x27; Loan Account (835)", html)
        self.assertNotIn("Materials", html)
        self.assertNotIn("Sales", html)

    def test_payment_account_options_support_xero_bank_account_ids(self):
        accounts = [
            {"AccountID": "dan-card-id", "Name": "Charge Card - Dan", "Type": "BANK", "Status": "ACTIVE"},
            {"AccountID": "ben-bank-id", "Code": "", "Name": "Ben - Personal Bank", "Type": "BANK", "Status": "ACTIVE"},
            {"Code": "700", "Name": "Cash", "Type": "CURRENT", "Status": "ACTIVE"},
        ]

        html = _payment_acct_options(
            accounts, "id:ben-bank-id",
            default_label="Choose payment/bank account",
        )

        self.assertIn("value='id:dan-card-id'>Charge Card - Dan", html)
        self.assertIn("value='id:ben-bank-id' selected>Ben - Personal Bank", html)
        self.assertIn("value='700'>Cash (700)", html)


if __name__ == "__main__":
    unittest.main()
