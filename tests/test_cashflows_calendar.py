import unittest

from app.cashflows_calendar import _is_explicit_card_event


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


if __name__ == "__main__":
    unittest.main()
