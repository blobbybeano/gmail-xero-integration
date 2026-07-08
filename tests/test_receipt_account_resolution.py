import unittest

from app.admin_web import (
    _apply_receipt_account_guardrails,
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


if __name__ == "__main__":
    unittest.main()
