import unittest
from decimal import Decimal

from app.cashflows_calendar import (
    _is_explicit_card_event,
    _parse_event_structured,
    _score_parsed,
)


class CashflowsCalendarSuggestionTests(unittest.TestCase):
    def test_only_explicit_card_events_are_cashflows_suggestions(self):
        card_event = {
            "description": "PAYMENT TYPE (CARD/INVOICE) = CARD\nCustomer name: Tim"
        }
        invoice_event = {
            "description": "PAYMENT TYPE (CARD/INVOICE) = INVOICE\nCustomer name: Ingmar"
        }
        blank_event = {"description": "Customer name: Unknown\nPatio = £204"}

        self.assertTrue(_is_explicit_card_event(card_event))
        self.assertFalse(_is_explicit_card_event(invoice_event))
        self.assertFalse(_is_explicit_card_event(blank_event))

    def test_customer_falls_back_to_calendar_title(self):
        event = {
            "summary": "SW14 G.C Adam May",
            "description": """
[invoice]
Gutter cleaning = £145.00+VAT
[/invoice]
PROCESS DRAFT (Y/N) =Y
[app-status]
PAYMENT TYPE (CARD/INVOICE) = CARD
[/app-status]
""",
        }

        parsed = _parse_event_structured(event)

        self.assertEqual(parsed["customer"], "Adam May")
        self.assertEqual(str(parsed["event_gross"]), "174.00")

    def test_customer_receipt_amount_is_strong_cashflows_signal(self):
        no_receipt = _score_parsed(
            {"customer": "Adam May", "event_gross": Decimal("150.00")},
            Decimal("174.00"),
            None,
        )
        with_receipt = _score_parsed(
            {
                "customer": "Adam May",
                "event_gross": Decimal("150.00"),
                "customer_receipt_amount": Decimal("174.00"),
            },
            Decimal("174.00"),
            None,
        )

        self.assertGreater(with_receipt, no_receipt)


if __name__ == "__main__":
    unittest.main()
