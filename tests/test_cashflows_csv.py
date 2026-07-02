import os
import tempfile
import unittest
from decimal import Decimal
from types import SimpleNamespace

from app.admin_store import init_admin_store
from app.cashflows_csv import (
    CsvParseError,
    allocate_sales_to_payouts,
    build_csv_reconciliation_preview,
    parse_merchant_csv,
)


def _csv(rows: list[str]) -> str:
    header = "'Ref','Date','Time','Description','Type','Debit','Credit','Balance'"
    return "\n".join([header, *rows]) + "\n"


def _sale(ref, date, desc_ref, credit):
    return f'"{ref}","{date}","10:00","Sale {desc_ref}","Sale Settlement","","{credit}","0.00"'


def _fee(ref, date, desc_ref, debit):
    return (
        f'"{ref}","{date}","10:00","Merchant Service Charge Sale Ref: {desc_ref}",'
        f'"Merchant Service Charge","{debit}","","0.00"'
    )


def _remit(ref, date, debit):
    return f'"{ref}","{date}","18:00","Maturity of Sales","Transfer for Remittance","{debit}","","0.00"'


class _FakeXero:
    def __init__(self, invoices=None, bank=None, raise_on=None, paid=None):
        self._invoices = invoices or {"Invoices": []}
        self._bank = bank or {"BankTransactions": []}
        self._raise_on = raise_on
        self._paid = paid or {"Invoices": []}

    def get_bank_transactions(self, start_date=None, end_date=None):
        if self._raise_on == "bank":
            raise RuntimeError("403 AuthenticationUnsuccessful")
        return self._bank

    def get_open_invoices(self):
        if self._raise_on == "invoices":
            raise RuntimeError("403 AuthenticationUnsuccessful")
        return self._invoices

    def get_paid_invoices(self, start_date=None, end_date=None):
        return self._paid


def _bank_line(ref, date, amount):
    return {
        "BankTransactionID": ref,
        "Date": date,
        "Reference": f"CFE SETT {ref}",
        "Total": amount,
        "IsReconciled": False,
    }


def _invoice(inv_id, number, date, total, contact="Customer", reference=None):
    raw = {
        "InvoiceID": inv_id,
        "InvoiceNumber": number,
        "Date": date,
        "Status": "AUTHORISED",
        "AmountDue": total,
        "Total": total,
        "Contact": {"Name": contact},
    }
    if reference is not None:
        raw["Reference"] = reference
    return raw


class ParseTests(unittest.TestCase):
    def test_parse_totals_and_fee_assignment(self):
        text = _csv(
            [
                _sale("1", "2026-05-01", "AAA", "100.00"),
                _fee("2", "2026-05-01", "AAA", "1.00"),
                _sale("3", "2026-05-02", "BBB", "1,200.50"),
                _fee("4", "2026-05-02", "BBB", "12.00"),
                _remit("5", "2026-05-03", "1,287.50"),
            ]
        )
        st = parse_merchant_csv(text)
        self.assertEqual(len(st.sales), 2)
        self.assertEqual(len(st.payouts), 1)
        self.assertEqual(st.gross_total, Decimal("1300.50"))
        self.assertEqual(st.fee_total, Decimal("13.00"))
        self.assertEqual(st.sales[1].gross, Decimal("1200.50"))
        self.assertEqual(st.sales[1].fee, Decimal("12.00"))
        self.assertEqual(st.sales[1].net, Decimal("1188.50"))

    def test_rejects_non_statement_file(self):
        with self.assertRaises(CsvParseError):
            parse_merchant_csv("just,some,random\n1,2,3\n")

    def test_decline_absorbed_into_batch(self):
        text = _csv(
            [
                _sale("1", "2026-05-01", "AAA", "100.00"),
                _fee("2", "2026-05-01", "AAA", "1.00"),
                '"3","2026-05-01","10:00","Decline","Decline Fee","0.04","","0.00"',
                _remit("4", "2026-05-02", "98.96"),
            ]
        )
        st = parse_merchant_csv(text)
        alloc, leftover = allocate_sales_to_payouts(st)
        self.assertEqual(leftover, [])
        batch = alloc["4"]
        self.assertEqual(len(batch["sales"]), 1)
        self.assertEqual(batch["decline"], Decimal("0.04"))
        self.assertEqual(batch["variance"], Decimal("0.00"))

    def test_remittances_aggregate_into_one_settlement_batch(self):
        # Cashflows writes one small "Transfer for Remittance" row per matured
        # sale; they accumulate until Balance returns to 0.00. That whole run is
        # paid to the bank as ONE CFE SETT deposit, so it must parse as ONE
        # payout equal to the run total — not three tiny payouts.
        def _remit_bal(ref, date, debit, balance):
            return (
                f'"{ref}","{date}","18:00","Maturity of Sales",'
                f'"Transfer for Remittance","{debit}","","{balance}"'
            )

        text = _csv(
            [
                _sale("1", "2026-06-01", "AAA", "100.00"),
                _sale("2", "2026-06-01", "BBB", "100.00"),
                _sale("3", "2026-06-01", "CCC", "100.00"),
                _remit_bal("4", "2026-06-02", "100.00", "200.00"),
                _remit_bal("5", "2026-06-02", "100.00", "100.00"),
                _remit_bal("6", "2026-06-02", "100.00", "0.00"),
            ]
        )
        st = parse_merchant_csv(text)
        self.assertEqual(len(st.payouts), 1)
        self.assertEqual(st.payouts[0].amount, Decimal("300.00"))
        alloc, leftover = allocate_sales_to_payouts(st)
        self.assertEqual(leftover, [])
        self.assertEqual(len(alloc[st.payouts[0].csv_ref]["sales"]), 3)

    def test_two_settlement_runs_split_at_zero_balance(self):
        def _remit_bal(ref, date, debit, balance):
            return (
                f'"{ref}","{date}","18:00","Maturity of Sales",'
                f'"Transfer for Remittance","{debit}","","{balance}"'
            )

        text = _csv(
            [
                _sale("1", "2026-06-01", "AAA", "100.00"),
                _sale("2", "2026-06-01", "BBB", "50.00"),
                _remit_bal("3", "2026-06-02", "100.00", "50.00"),
                _remit_bal("4", "2026-06-02", "50.00", "0.00"),
                _sale("5", "2026-06-03", "CCC", "200.00"),
                _remit_bal("6", "2026-06-04", "200.00", "0.00"),
            ]
        )
        st = parse_merchant_csv(text)
        self.assertEqual(len(st.payouts), 2)
        self.assertEqual(st.payouts[0].amount, Decimal("150.00"))
        self.assertEqual(st.payouts[1].amount, Decimal("200.00"))

    def test_trailing_run_without_zero_balance_still_flushes(self):
        # Statement cut mid-settlement-run: the last remittances never close to
        # zero. They must still surface as one final batch (nothing dropped).
        def _remit_bal(ref, date, debit, balance):
            return (
                f'"{ref}","{date}","18:00","Maturity of Sales",'
                f'"Transfer for Remittance","{debit}","","{balance}"'
            )

        text = _csv(
            [
                _sale("1", "2026-06-01", "AAA", "300.00"),
                _remit_bal("2", "2026-06-02", "100.00", "200.00"),
                _remit_bal("3", "2026-06-02", "100.00", "100.00"),
            ]
        )
        st = parse_merchant_csv(text)
        self.assertEqual(len(st.payouts), 1)
        self.assertEqual(st.payouts[0].amount, Decimal("200.00"))

    def test_unpaid_sale_flagged(self):
        text = _csv(
            [
                _sale("1", "2026-05-01", "AAA", "100.00"),
                _fee("2", "2026-05-01", "AAA", "1.00"),
                _remit("3", "2026-05-02", "99.00"),
                _sale("4", "2026-05-03", "BBB", "50.00"),
                _fee("5", "2026-05-03", "BBB", "0.50"),
            ]
        )
        st = parse_merchant_csv(text)
        _alloc, leftover = allocate_sales_to_payouts(st)
        self.assertEqual([s.sale_ref for s in leftover], ["BBB"])


class PreviewTests(unittest.TestCase):
    def setUp(self):
        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        init_admin_store(self._db_path)
        self.config = SimpleNamespace(admin_db_file=self._db_path)

    def tearDown(self):
        try:
            os.remove(self._db_path)
        except OSError:
            pass

    def test_ready_when_bank_and_invoice_match(self):
        text = _csv(
            [
                _sale("1", "2026-05-01", "AAA", "100.00"),
                _fee("2", "2026-05-01", "AAA", "1.00"),
                _remit("3", "2026-05-02", "99.00"),
            ]
        )
        xero = _FakeXero(
            invoices={"Invoices": [_invoice("inv-1", "GC-1", "2026-05-01", "100.00")]},
            bank={"BankTransactions": [_bank_line("b1", "2026-05-02", "99.00")]},
        )
        result = build_csv_reconciliation_preview(self.config, text, xero_client=xero)
        self.assertTrue(result["xero_connected"])
        self.assertEqual(result["status_counts"]["ready"], 1)
        self.assertEqual(result["batches"][0]["status"], "ready")

    def test_ambiguous_match_is_needs_review_not_ready(self):
        # Two open invoices share the same amount AND the same date -> ambiguous.
        text = _csv(
            [
                _sale("1", "2026-05-01", "AAA", "100.00"),
                _fee("2", "2026-05-01", "AAA", "1.00"),
                _remit("3", "2026-05-02", "99.00"),
            ]
        )
        xero = _FakeXero(
            invoices={
                "Invoices": [
                    _invoice("inv-1", "GC-1", "2026-05-01", "100.00"),
                    _invoice("inv-2", "GC-2", "2026-05-01", "100.00"),
                ]
            },
            bank={"BankTransactions": [_bank_line("b1", "2026-05-02", "99.00")]},
        )
        result = build_csv_reconciliation_preview(self.config, text, xero_client=xero)
        self.assertEqual(result["status_counts"]["ready"], 0)
        self.assertEqual(result["status_counts"]["needs_review"], 1)
        batch = result["batches"][0]
        self.assertEqual(batch["status"], "needs_review")
        self.assertEqual(batch["ambiguous_count"], 1)

    def test_ambiguous_row_offers_unaccounted_rival(self):
        # One sale, two same-amount invoices: the rival stays unaccounted and
        # must be surfaced as a tied candidate so the user can swap to it.
        text = _csv(
            [
                _sale("1", "2026-05-01", "AAA", "100.00"),
                _fee("2", "2026-05-01", "AAA", "1.00"),
                _remit("3", "2026-05-02", "99.00"),
            ]
        )
        xero = _FakeXero(
            invoices={
                "Invoices": [
                    _invoice("inv-1", "GC-1", "2026-05-01", "100.00"),
                    _invoice("inv-2", "GC-2", "2026-05-01", "100.00"),
                ]
            },
            bank={"BankTransactions": [_bank_line("b1", "2026-05-02", "99.00")]},
        )
        result = build_csv_reconciliation_preview(self.config, text, xero_client=xero)
        row = result["batches"][0]["sales"][0]
        self.assertTrue(row["ambiguous"])
        self.assertEqual(len(row["tied_candidates"]), 1)
        # The rival is unaccounted (not on another sale), so no assigned_to label.
        self.assertIsNone(row["tied_candidates"][0].get("assigned_to"))

    def test_tied_candidate_includes_sibling_assigned_invoice(self):
        # Two same-amount sales + two same-amount invoices in one batch. The app
        # assigns one invoice to each, so the rival for the ambiguous row is the
        # invoice now sitting on the sibling sale — it must still be offered,
        # labelled with that sibling's customer.
        text = _csv(
            [
                _sale("1", "2026-05-01", "AAA", "100.00"),
                _sale("2", "2026-05-01", "BBB", "100.00"),
                _fee("3", "2026-05-01", "AAA", "1.00"),
                _fee("4", "2026-05-01", "BBB", "1.00"),
                _remit("5", "2026-05-02", "198.00"),
            ]
        )
        xero = _FakeXero(
            invoices={
                "Invoices": [
                    _invoice("inv-1", "GC-1", "2026-05-01", "100.00", contact="Alice"),
                    _invoice("inv-2", "GC-2", "2026-05-01", "100.00", contact="Bob"),
                ]
            },
            bank={"BankTransactions": [_bank_line("b1", "2026-05-02", "198.00")]},
        )
        result = build_csv_reconciliation_preview(self.config, text, xero_client=xero)
        batch = result["batches"][0]
        ambiguous_rows = [r for r in batch["sales"] if r["ambiguous"]]
        self.assertEqual(len(ambiguous_rows), 1)
        tied = ambiguous_rows[0]["tied_candidates"]
        self.assertTrue(tied)
        # The rival invoice is labelled with the sibling sale's customer.
        labelled = [c.get("assigned_to") for c in tied if c.get("assigned_to")]
        self.assertTrue(labelled)

    def test_sheet_configured_gc_fallback_accepts_paid_invoice(self):
        # The correlation sheet is configured but does NOT contain this booking.
        # A GC-referenced PAID invoice must still be accepted via the fallback,
        # rather than being dropped (which previously hid genuine card payments).
        from unittest.mock import patch

        text = _csv(
            [
                _sale("1", "2026-05-01", "AAA", "100.00"),
                _fee("2", "2026-05-01", "AAA", "1.00"),
                _remit("3", "2026-05-02", "99.00"),
            ]
        )
        xero = _FakeXero(
            invoices={"Invoices": []},
            bank={"BankTransactions": [_bank_line("b1", "2026-05-02", "99.00")]},
            paid={
                "Invoices": [
                    _invoice("p1", "INV-900", "2026-05-01", "100.00", reference="GC-900")
                ]
            },
        )
        empty_lookup = SimpleNamespace(
            gc_refs=set(), inv_numbers=set(), total_card=0, total_rows=0
        )
        with patch(
            "app.cashflows_csv.fetch_card_lookup", return_value=empty_lookup
        ):
            result = build_csv_reconciliation_preview(
                self.config, text, xero_client=xero, correlation_sheet_id="sheet-1"
            )
        row = result["batches"][0]["sales"][0]
        self.assertIsNotNone(row["invoice"])
        self.assertEqual(row["invoice"]["reference"], "GC-900")

    def test_missing_invoice_is_waiting(self):
        text = _csv(
            [
                _sale("1", "2026-05-01", "AAA", "100.00"),
                _fee("2", "2026-05-01", "AAA", "1.00"),
                _remit("3", "2026-05-02", "99.00"),
            ]
        )
        xero = _FakeXero(
            invoices={"Invoices": []},
            bank={"BankTransactions": [_bank_line("b1", "2026-05-02", "99.00")]},
        )
        result = build_csv_reconciliation_preview(self.config, text, xero_client=xero)
        self.assertEqual(result["status_counts"]["waiting_invoices"], 1)
        self.assertEqual(result["batches"][0]["status"], "waiting_invoices")

    def test_missing_sale_gets_ranked_candidates(self):
        # One sale with no exact-amount invoice -> still missing, but several
        # unaccounted invoices should be offered as candidates, ranked with the
        # closest invoice date to the Cashflows payment date first.
        text = _csv(
            [
                _sale("1", "2026-05-10", "AAA", "100.00"),
                _fee("2", "2026-05-10", "AAA", "1.00"),
                _remit("3", "2026-05-11", "99.00"),
            ]
        )
        xero = _FakeXero(
            invoices={
                "Invoices": [
                    _invoice("inv-far", "GC-FAR", "2026-05-01", "55.00"),
                    _invoice("inv-near", "GC-NEAR", "2026-05-09", "42.00"),
                    _invoice("inv-mid", "GC-MID", "2026-05-05", "73.00"),
                ]
            },
            bank={"BankTransactions": [_bank_line("b1", "2026-05-11", "99.00")]},
        )
        result = build_csv_reconciliation_preview(self.config, text, xero_client=xero)
        batch = result["batches"][0]
        self.assertEqual(batch["status"], "waiting_invoices")
        row = batch["sales"][0]
        self.assertIsNone(row["invoice"])
        cands = row["candidates"]
        self.assertEqual(len(cands), 3)
        # No exact-amount match here, so ranking is purely by date proximity to
        # the 2026-05-10 sale: 05-09 (1d) < 05-05 (5d) < 05-01 (9d).
        self.assertEqual([c["number"] for c in cands], ["GC-NEAR", "GC-MID", "GC-FAR"])
        self.assertEqual(cands[0]["days_apart"], 1)
        self.assertEqual(batch["missing_candidate_count"], 3)

    def test_candidate_is_open_flag_reflects_amount_due(self):
        # A missing sale (no exact-amount invoice to auto-match) is offered two
        # unaccounted invoices: one AUTHORISED (unpaid -> is_open=True) and one
        # already PAID (is_open=False). None match the amount exactly.
        text = _csv(
            [
                _sale("1", "2026-05-10", "AAA", "100.00"),
                _fee("2", "2026-05-10", "AAA", "1.00"),
                _remit("3", "2026-05-11", "99.00"),
            ]
        )
        paid = _invoice("inv-paid", "GC-PAID", "2026-05-09", "42.00")
        paid["Status"] = "PAID"
        paid["AmountDue"] = "0.00"
        xero = _FakeXero(
            invoices={
                "Invoices": [
                    paid,
                    _invoice("inv-open", "GC-OPEN", "2026-05-08", "73.00"),
                ]
            },
            bank={"BankTransactions": [_bank_line("b1", "2026-05-11", "99.00")]},
        )
        result = build_csv_reconciliation_preview(self.config, text, xero_client=xero)
        cands = result["batches"][0]["sales"][0]["candidates"]
        self.assertEqual(len(cands), 2)
        self.assertFalse(any(c["amount_match"] for c in cands))
        open_cand = next(c for c in cands if c["number"] == "GC-OPEN")
        paid_cand = next(c for c in cands if c["number"] == "GC-PAID")
        self.assertTrue(open_cand["is_open"])
        self.assertFalse(paid_cand["is_open"])

    def test_auto_matched_invoice_not_offered_as_candidate(self):
        # Batch A auto-matches its invoice; that invoice must not appear as a
        # candidate for batch B's still-missing sale.
        text = _csv(
            [
                _sale("1", "2026-05-01", "AAA", "100.00"),
                _fee("2", "2026-05-01", "AAA", "1.00"),
                _remit("3", "2026-05-02", "99.00"),
                _sale("4", "2026-05-03", "BBB", "200.00"),
                _fee("5", "2026-05-03", "BBB", "2.00"),
                _remit("6", "2026-05-04", "198.00"),
            ]
        )
        xero = _FakeXero(
            invoices={
                "Invoices": [
                    _invoice("inv-a", "GC-A", "2026-05-01", "100.00"),
                ]
            },
            bank={
                "BankTransactions": [
                    _bank_line("b1", "2026-05-02", "99.00"),
                    _bank_line("b2", "2026-05-04", "198.00"),
                ]
            },
        )
        result = build_csv_reconciliation_preview(self.config, text, xero_client=xero)
        missing_batch = next(
            b for b in result["batches"] if b["missing_invoice_count"] > 0
        )
        cand_numbers = [
            c["number"]
            for r in missing_batch["sales"]
            for c in r.get("candidates", [])
        ]
        self.assertNotIn("GC-A", cand_numbers)

    def test_temp_sale_handle_not_leaked_in_preview(self):
        # The internal `_sale` handle used by the candidate post-pass must be
        # stripped before the preview is serialised to JSON.
        text = _csv(
            [
                _sale("1", "2026-05-10", "AAA", "100.00"),
                _fee("2", "2026-05-10", "AAA", "1.00"),
                _remit("3", "2026-05-11", "99.00"),
            ]
        )
        xero = _FakeXero(
            invoices={"Invoices": [_invoice("inv-x", "GC-X", "2026-05-01", "55.00")]},
            bank={"BankTransactions": [_bank_line("b1", "2026-05-11", "99.00")]},
        )
        result = build_csv_reconciliation_preview(self.config, text, xero_client=xero)
        for batch in result["batches"]:
            for row in batch["sales"]:
                self.assertNotIn("_sale", row)

    def test_403_degrades_gracefully(self):
        text = _csv(
            [
                _sale("1", "2026-05-01", "AAA", "100.00"),
                _fee("2", "2026-05-01", "AAA", "1.00"),
                _remit("3", "2026-05-02", "99.00"),
            ]
        )
        xero = _FakeXero(raise_on="bank")
        result = build_csv_reconciliation_preview(self.config, text, xero_client=xero)
        self.assertFalse(result["xero_connected"])
        self.assertIn("403", result["xero_error"])
        # Still produced a valid preview body with totals and batches.
        self.assertEqual(result["totals"]["sale_count"], 1)
        self.assertEqual(len(result["batches"]), 1)
        self.assertEqual(result["batches"][0]["status"], "no_bank_line")
        self.assertTrue(result["testing_mode"])


if __name__ == "__main__":
    unittest.main()
