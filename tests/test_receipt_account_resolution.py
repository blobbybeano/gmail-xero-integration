import unittest

from app.admin_web import (
    _apply_receipt_account_guardrails,
    _owner_paid_acct_options,
    _owner_paid_accounts_from,
    _resolve_expense_account_choice,
)


class ReceiptAccountResolutionTests(unittest.TestCase):
    def setUp(self):
        self.accounts = [
            {"Code": "310", "Name": "Materials"},
            {"Code": "400", "Name": "Motor Vehicle Expenses"},
            {"Code": "401", "Name": "Motor Vehicle Fuel"},
            {"Code": "402", "Name": "Van Fuel"},
            {"Code": "403", "Name": "Machinery Fuel"},
            {"Code": "404", "Name": "Vehicle Repairs and Maintenance"},
        ]

    def test_accepts_real_xero_code(self):
        self.assertEqual(
            _resolve_expense_account_choice("310", self.accounts),
            ("310", "Materials"),
        )

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
            "Corner Shop", "milk bread cleaning cloths",
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


if __name__ == "__main__":
    unittest.main()
