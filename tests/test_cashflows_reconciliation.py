import datetime as dt
import io
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch

from app.admin_web import create_app
from app.cashflows_reconciliation import (
    CashflowsClient,
    CashflowsReconciliationService,
    CashflowsSettlement,
    XeroBankLine,
    XeroInvoiceCandidate,
    match_reconciliation,
    parse_cashflows_settlements,
    parse_xero_bank_lines,
)


class _FakeCashflows:
    def __init__(self, settlements):
        self._settlements = settlements

    def fetch_settlements(self, start_date, end_date):
        return self._settlements


class _FakeXero:
    payment_account_code = "090"
    sales_account_code = "200"

    def __init__(self, bank_payload=None, invoices_payload=None):
        self.bank_payload = bank_payload or {"BankTransactions": []}
        self.invoices_payload = invoices_payload or {"Invoices": []}

    def get_bank_transactions(self, *, start_date, end_date):
        return self.bank_payload

    def get_open_invoices(self):
        return self.invoices_payload


class CashflowsReconciliationTests(unittest.TestCase):
    def test_parses_and_filters_only_cfe_sett_bank_lines(self):
        lines = parse_xero_bank_lines(
            {
                "BankTransactions": [
                    {
                        "BankTransactionID": "bank-1",
                        "Date": "2026-06-01",
                        "Reference": "CFE SETT 123",
                        "Total": "98.50",
                    },
                    {
                        "BankTransactionID": "bank-2",
                        "Date": "2026-06-01",
                        "Reference": "OTHER",
                        "Total": "50.00",
                    },
                    {
                        "BankTransactionID": "bank-3",
                        "Date": "2026-06-01",
                        "Reference": "CFE SETT RECONCILED",
                        "Total": "50.00",
                        "IsReconciled": True,
                    },
                ]
            }
        )

        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0].id, "bank-1")
        self.assertEqual(float(lines[0].amount), 98.5)

    def test_parses_cashflows_settlement_amounts(self):
        settlements = parse_cashflows_settlements(
            {
                "Settlements": [
                    {
                        "SettlementID": "sett-1",
                        "SettlementDate": "2026-06-02",
                        "GrossAmount": "120.00",
                        "NetAmount": "115.00",
                        "Fees": "5.00",
                    }
                ]
            }
        )

        self.assertEqual(len(settlements), 1)
        self.assertEqual(settlements[0].id, "sett-1")
        self.assertEqual(float(settlements[0].fees), 5.0)

    def test_strict_exact_net_match_links_invoice(self):
        bank = XeroBankLine(
            id="bank-1",
            date=dt.date(2026, 6, 5),
            description="CFE SETT 123",
            amount="115.00",
        )
        settlement = CashflowsSettlement(
            id="sett-1",
            settlement_date=dt.date(2026, 6, 2),
            gross_amount="120.00",
            net_amount="115.00",
            fees="5.00",
        )
        invoice = XeroInvoiceCandidate(
            id="inv-1",
            number="INV-001",
            contact_name="Customer",
            date=dt.date(2026, 6, 1),
            due_date=dt.date(2026, 6, 8),
            amount_due="120.00",
            total="120.00",
        )

        matches = match_reconciliation([bank], [settlement], [invoice])

        self.assertEqual(matches[0].method, "strict_exact_net")
        self.assertEqual(matches[0].confidence, 100)
        self.assertFalse(matches[0].missing_invoice_required)
        self.assertEqual(matches[0].invoices[0].number, "INV-001")

    def test_combination_layer_matches_multiple_settlements(self):
        bank = XeroBankLine(
            id="bank-1",
            date=dt.date(2026, 6, 5),
            description="CFE SETT COMBO",
            amount="300.00",
        )
        settlements = [
            CashflowsSettlement("sett-1", dt.date(2026, 6, 3), "110.00", "100.00", "10.00"),
            CashflowsSettlement("sett-2", dt.date(2026, 6, 4), "220.00", "200.00", "20.00"),
        ]
        invoice = XeroInvoiceCandidate(
            id="inv-1",
            number="INV-002",
            contact_name="Customer",
            date=dt.date(2026, 6, 4),
            due_date=dt.date(2026, 6, 10),
            amount_due="330.00",
            total="330.00",
        )

        matches = match_reconciliation([bank], settlements, [invoice])

        self.assertEqual(matches[0].method, "combination_net_sum")
        self.assertEqual([s.id for s in matches[0].settlements], ["sett-1", "sett-2"])
        self.assertEqual(matches[0].invoices[0].number, "INV-002")

    def test_missing_invoice_gets_auto_creation_warning(self):
        bank = XeroBankLine("bank-1", dt.date(2026, 6, 5), "CFE SETT 123", "115.00")
        settlement = CashflowsSettlement("sett-1", dt.date(2026, 6, 5), "120.00", "115.00", "5.00")

        matches = match_reconciliation([bank], [settlement], [])

        self.assertTrue(matches[0].missing_invoice_required)
        self.assertIn("Auto-Creation Required", matches[0].warning)

    def test_scan_and_confirm_stay_in_testing_mode_by_default(self):
        config = SimpleNamespace(dry_run=True)
        fake_xero = _FakeXero(
            bank_payload={
                "BankTransactions": [
                    {
                        "BankTransactionID": "bank-1",
                        "Date": "2026-06-05",
                        "Reference": "CFE SETT 123",
                        "Total": "115.00",
                    }
                ]
            },
            invoices_payload={"Invoices": []},
        )
        settlements = [
            CashflowsSettlement("sett-1", dt.date(2026, 6, 5), "120.00", "115.00", "5.00")
        ]
        service = CashflowsReconciliationService(
            config,
            xero_client=fake_xero,
            cashflows_client=_FakeCashflows(settlements),
            ai_matcher=None,
        )

        preview = service.scan(start_date=dt.date(2026, 6, 1), end_date=dt.date(2026, 6, 7))
        with redirect_stdout(io.StringIO()):
            result = service.confirm(preview["matches"][0])

        self.assertTrue(preview["testing_mode"])
        self.assertIn("submission_preview", preview["matches"][0])
        self.assertIn("placeholder_invoice", preview["matches"][0]["submission_preview"])
        self.assertEqual(result["mode"], "testing")
        self.assertIn("placeholder_invoice", result["payloads"])
        self.assertIn("batch_payment", result["payloads"])

    def test_cashflows_diagnostic_reports_http_status_without_throwing(self):
        class _Resp:
            status_code = 404
            text = ""

        client = CashflowsClient(
            {
                "base_url": "https://example.invalid",
                "configuration_id": "cfg",
                "api_key": "secret",
                "settlements_action": "ReadSettlements",
            }
        )

        with patch("app.cashflows_reconciliation.requests.post", return_value=_Resp()):
            diag = client.diagnose_settlements(
                dt.date(2026, 6, 1),
                dt.date(2026, 6, 2),
            )

        self.assertFalse(diag["ok"])
        self.assertEqual(diag["status_code"], 404)
        self.assertEqual(diag["action"], "ReadSettlements")
        self.assertNotIn("secret", str(diag))

    def test_preview_route_can_use_manual_settlement_json(self):
        fake_xero = _FakeXero(
            bank_payload={
                "BankTransactions": [
                    {
                        "BankTransactionID": "bank-1",
                        "Date": "2026-06-05",
                        "Reference": "CFE SETT 123",
                        "Total": "115.00",
                    }
                ]
            },
            invoices_payload={"Invoices": []},
        )
        app = create_app()
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["logged_in"] = True

        manual_json = (
            '{"Settlements":[{"SettlementID":"sett-1","SettlementDate":"2026-06-05",'
            '"GrossAmount":"120.00","NetAmount":"115.00","Fees":"5.00"}]}'
        )

        with patch("app.admin_web.build_xero_client", return_value=fake_xero):
            resp = client.post(
                "/cashflows-sync/preview",
                json={
                    "date_from": "2026-06-01",
                    "date_to": "2026-06-07",
                    "manual_settlements_json": manual_json,
                },
            )

        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["cashflows_source"], "manual_json")
        self.assertEqual(data["manual_settlement_count"], 1)
        self.assertEqual(data["matches"][0]["method"], "strict_exact_net")


if __name__ == "__main__":
    unittest.main()
