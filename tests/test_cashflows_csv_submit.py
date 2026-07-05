import os
import tempfile
import time
import unittest
from unittest.mock import patch


class FakeXeroClient:
    payment_account_code = "090"
    sales_account_code = "200"
    base_url = "https://api.xero.test"
    dry_run = True

    def __init__(self):
        self.created_credit_notes = []
        self.allocations = []
        self.batch_payments = []
        self.bank_transactions = []
        self.simple_invoices = []
        self.invoice_payments = {}
        self.payment_details = {}
        self.deleted_payments = []
        self.created_payments = []

    def create_credit_note_payload(self, payload):
        self.created_credit_notes.append(payload)
        return {"CreditNotes": [{"CreditNoteID": "credit-1"}]}

    def allocate_credit_note_payload(self, credit_note_id, payload):
        self.allocations.append((credit_note_id, payload))
        return {"Allocations": payload["Allocations"]}

    def create_batch_payment_payload(self, payload):
        self.batch_payments.append(payload)
        return {"BatchPayments": payload["BatchPayments"]}

    def create_bank_transaction_payload(self, payload):
        self.bank_transactions.append(payload)
        return {"BankTransactions": payload["BankTransactions"]}

    def create_simple_invoice(self, **payload):
        self.simple_invoices.append(payload)
        return {
            "Invoices": [
                {
                    "InvoiceID": "extra-inv-1",
                    "InvoiceNumber": "INV-EXTRA",
                    "AmountDue": payload["amount"],
                    "Total": payload["amount"],
                }
            ]
        }

    def get_invoice(self, invoice_id):
        return {"InvoiceID": invoice_id, "Payments": self.invoice_payments.get(invoice_id, [])}

    def _request(self, method, url, **kwargs):
        class _Resp:
            ok = True
            status_code = 200

            def __init__(self, data):
                self._data = data
                self.text = "{}"

            def json(self):
                return self._data

        if method == "POST" and str(url).rstrip("/").endswith("/Payments"):
            payload = kwargs.get("json") or {}
            self.created_payments.append(payload)
            created = []
            for idx, payment in enumerate(payload.get("Payments") or []):
                created.append(
                    {
                        **payment,
                        "PaymentID": f"created-payment-{len(self.created_payments)}-{idx}",
                        "Status": "AUTHORISED",
                    }
                )
            return _Resp({"Payments": created})
        payment_id = str(url).rstrip("/").split("/")[-1]
        if method == "POST":
            self.deleted_payments.append((payment_id, kwargs.get("json") or {}))
            detail = dict(self.payment_details.get(payment_id, {}))
            detail["Status"] = "DELETED"
            self.payment_details[payment_id] = detail
            return _Resp({"Payments": [detail]})
        return _Resp({"Payments": [self.payment_details.get(payment_id, {})]})


class CashflowsCsvSubmitTests(unittest.TestCase):
    def _app_with_preview(self, preview, *, dry_run=True, production=False):
        from app.admin_store import set_json_setting
        from app.admin_web import create_app

        self.tmp = tempfile.TemporaryDirectory()
        db_path = os.path.join(self.tmp.name, "admin.db")
        auth_path = os.path.join(self.tmp.name, "admin_auth.json")
        env = patch.dict(
            os.environ,
            {
                "ADMIN_DB_FILE": db_path,
                "ADMIN_AUTH_FILE": auth_path,
                "ADMIN_USERNAME": "test",
                "ADMIN_PASSWORD": "test",
                "WEB_SECRET_KEY": "test-secret",
                "DRY_RUN": "true" if dry_run else "false",
                "CASHFLOWS_CSV_SUBMIT_PRODUCTION": "true" if production else "false",
            },
        )
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(self.tmp.cleanup)
        app = create_app()
        app.testing = True
        set_json_setting(db_path, "cashflows_csv_preview", preview)
        return app

    def _wait_for_submit_job(self, client, timeout=8.0):
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            resp = client.get("/cashflows-sync/submit-progress")
            self.assertEqual(resp.status_code, 200)
            last = resp.get_json()
            if last.get("status") in {"done", "error"}:
                return last
            time.sleep(0.01)
        self.fail(f"Cashflows submit job did not finish: {last}")

    def test_checked_batch_builds_test_mode_xero_payload(self):
        preview = {
            "preview_id": "preview-1",
            "batches": [
                {
                    "id": "batch-1",
                    "status": "ready",
                    "payout": {"csv_ref": "pay-1", "date": "2026-06-30"},
                    "gross": 100.0,
                    "net": 98.0,
                    "bank_line": {"id": "bank-1", "date": "2026-07-01", "amount": 98.0},
                    "sales": [
                        {
                            "sale_ref": "sale-1",
                            "date": "2026-06-30",
                            "gross": 100.0,
                            "fee": 2.0,
                            "invoice": {
                                "id": "inv-1",
                                "number": "INV-1",
                                "contact_name": "Customer",
                                "total": 100.0,
                                "amount_due": 100.0,
                                "is_open": True,
                            },
                            "candidates": [],
                            "tied_candidates": [],
                        }
                    ],
                }
            ],
        }
        app = self._app_with_preview(preview)
        with app.test_client() as client:
            with client.session_transaction() as session:
                session["logged_in"] = True
            with patch("app.admin_web.build_xero_client", return_value=FakeXeroClient()):
                resp = client.post(
                    "/cashflows-sync/submit-csv-batches",
                    json={
                        "preview_id": "preview-1",
                        "batches": [
                            {
                                "batch_id": "batch-1",
                                "sales": [
                                    {
                                        "sale_index": 0,
                                        "selected_invoice_id": "inv-1",
                                        "selected_invoice_number": "INV-1",
                                    }
                                ],
                            }
                        ],
                    },
                )
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["mode"], "testing")
        batch = data["plans"][0]["payloads"]["batch_payment"]["BatchPayments"][0]
        self.assertEqual(batch["Payments"][0]["Invoice"], {"InvoiceID": "inv-1"})
        self.assertEqual(batch["Payments"][0]["Amount"], 100.0)
        fee = data["plans"][0]["payloads"]["bank_fee"]["BankTransactions"][0]
        self.assertEqual(fee["LineItems"][0]["UnitAmount"], 2.0)

    def test_underpayment_builds_credit_note_allocation_payload(self):
        preview = {
            "preview_id": "preview-1",
            "batches": [
                {
                    "id": "batch-1",
                    "status": "ready",
                    "payout": {"csv_ref": "pay-1", "date": "2026-06-30"},
                    "gross": 96.0,
                    "net": 94.0,
                    "sales": [
                        {
                            "sale_ref": "sale-1",
                            "date": "2026-06-30",
                            "gross": 96.0,
                            "fee": 2.0,
                            "invoice": {
                                "id": "inv-1",
                                "number": "INV-1",
                                "contact_name": "Customer",
                                "contact_id": "contact-1",
                                "total": 100.0,
                                "amount_due": 100.0,
                                "is_open": True,
                            },
                            "candidates": [],
                            "tied_candidates": [],
                        }
                    ],
                }
            ],
        }
        app = self._app_with_preview(preview)
        with app.test_client() as client:
            with client.session_transaction() as session:
                session["logged_in"] = True
            with patch("app.admin_web.build_xero_client", return_value=FakeXeroClient()):
                resp = client.post(
                    "/cashflows-sync/submit-csv-batches",
                    json={
                        "preview_id": "preview-1",
                        "batches": [
                            {
                                "batch_id": "batch-1",
                                "sales": [
                                    {
                                        "sale_index": 0,
                                        "selected_invoice_id": "inv-1",
                                        "adjustment": {"type": "discount", "amount": 4.0},
                                    }
                                ],
                            }
                        ],
                    },
                )
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        plan = data["plans"][0]
        credit = plan["credit_note_payloads"][0]
        self.assertEqual(credit["amount"], 4.0)
        self.assertEqual(
            credit["credit_note"]["CreditNotes"][0]["Contact"],
            {"ContactID": "contact-1"},
        )
        self.assertEqual(
            credit["allocation"]["Allocations"][0]["Invoice"],
            {"InvoiceID": "inv-1"},
        )
        batch = plan["payloads"]["batch_payment"]["BatchPayments"][0]
        self.assertEqual(batch["Payments"][0]["Amount"], 96.0)

    def test_production_underpayment_creates_credit_note_before_batch_payment(self):
        preview = {
            "preview_id": "preview-1",
            "batches": [
                {
                    "id": "batch-1",
                    "status": "ready",
                    "payout": {"csv_ref": "pay-1", "date": "2026-06-30"},
                    "gross": 96.0,
                    "net": 94.0,
                    "sales": [
                        {
                            "sale_ref": "sale-1",
                            "date": "2026-06-30",
                            "gross": 96.0,
                            "fee": 2.0,
                            "invoice": {
                                "id": "inv-1",
                                "number": "INV-1",
                                "contact_name": "Customer",
                                "contact_id": "contact-1",
                                "total": 100.0,
                                "amount_due": 100.0,
                                "is_open": True,
                            },
                            "candidates": [],
                            "tied_candidates": [],
                        }
                    ],
                }
            ],
        }
        app = self._app_with_preview(preview, dry_run=False, production=True)
        fake = FakeXeroClient()
        fake.dry_run = False
        with app.test_client() as client:
            with client.session_transaction() as session:
                session["logged_in"] = True
            with patch("app.admin_web.build_xero_client", return_value=fake), patch(
                "app.admin_web._CF_SUBMIT_PACE_SECONDS", 0
            ):
                resp = client.post(
                    "/cashflows-sync/submit-csv-batches",
                    json={
                        "preview_id": "preview-1",
                        "batches": [
                            {
                                "batch_id": "batch-1",
                                "sales": [
                                    {
                                        "sale_index": 0,
                                        "selected_invoice_id": "inv-1",
                                        "adjustment": {"type": "discount", "amount": 4.0},
                                    }
                                ],
                            }
                        ],
                    },
                )
                progress = self._wait_for_submit_job(client)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["async"], True)
        self.assertEqual(progress["status"], "done")
        self.assertEqual(len(fake.created_credit_notes), 1)
        self.assertEqual(len(fake.allocations), 1)
        self.assertEqual(fake.allocations[0][0], "credit-1")
        self.assertEqual(len(fake.batch_payments), 1)

    def test_production_blocks_already_paid_invoice_without_existing_bank_payment(self):
        preview = {
            "preview_id": "preview-1",
            "batches": [
                {
                    "id": "batch-1",
                    "status": "ready",
                    "payout": {"csv_ref": "pay-1", "date": "2026-07-02"},
                    "gross": 270.0,
                    "net": 267.68,
                    "sales": [
                        {
                            "sale_ref": "sale-1",
                            "date": "2026-07-02",
                            "gross": 270.0,
                            "fee": 2.32,
                            "invoice": {
                                "id": "inv-paid",
                                "number": "INV-5727",
                                "contact_name": "Lucy Telfer",
                                "contact_id": "contact-1",
                                "total": 270.0,
                                "amount_due": 0.0,
                                "is_open": False,
                            },
                            "candidates": [],
                            "tied_candidates": [],
                        }
                    ],
                }
            ],
        }
        app = self._app_with_preview(preview, dry_run=False, production=True)
        fake = FakeXeroClient()
        fake.dry_run = False
        with app.test_client() as client:
            with client.session_transaction() as session:
                session["logged_in"] = True
            with patch("app.admin_web.build_xero_client", return_value=fake), patch(
                "app.admin_web._CF_SUBMIT_PACE_SECONDS", 0
            ):
                resp = client.post(
                    "/cashflows-sync/submit-csv-batches",
                    json={
                        "preview_id": "preview-1",
                        "batches": [
                            {
                                "batch_id": "batch-1",
                                "sales": [
                                    {
                                        "sale_index": 0,
                                        "selected_invoice_id": "inv-paid",
                                        "selected_invoice_number": "INV-5727",
                                    }
                                ],
                            }
                        ],
                    },
                )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("did not return enough existing bank-account payments", resp.get_json()["error"])
        self.assertEqual(len(fake.batch_payments), 0)
        self.assertEqual(len(fake.bank_transactions), 0)

    def test_production_paid_invoice_moves_payment_to_clearing_and_creates_net_match(self):
        preview = {
            "preview_id": "preview-1",
            "batches": [
                {
                    "id": "batch-1",
                    "status": "ready",
                    "payout": {"csv_ref": "pay-1", "date": "2026-07-02"},
                    "gross": 270.0,
                    "net": 267.68,
                    "sales": [
                        {
                            "sale_ref": "sale-1",
                            "date": "2026-07-02",
                            "gross": 270.0,
                            "fee": 2.32,
                            "invoice": {
                                "id": "inv-paid",
                                "number": "INV-5727",
                                "contact_name": "Lucy Telfer",
                                "total": 270.0,
                                "amount_due": 0.0,
                                "is_open": False,
                            },
                            "candidates": [],
                            "tied_candidates": [],
                        }
                    ],
                }
            ],
        }
        app = self._app_with_preview(preview, dry_run=False, production=True)
        fake = FakeXeroClient()
        fake.dry_run = False
        fake.invoice_payments["inv-paid"] = [{"PaymentID": "pay-1"}]
        fake.payment_details["pay-1"] = {
            "PaymentID": "pay-1",
            "HasAccount": True,
            "Status": "AUTHORISED",
            "IsReconciled": False,
            "Amount": 270.0,
            "Account": {"Code": "090", "Name": "Pow Wash"},
        }
        with app.test_client() as client:
            with client.session_transaction() as session:
                session["logged_in"] = True
            with patch("app.admin_web.build_xero_client", return_value=fake):
                resp = client.post(
                    "/cashflows-sync/submit-csv-batches",
                    json={
                        "preview_id": "preview-1",
                        "batches": [
                            {
                                "batch_id": "batch-1",
                                "sales": [
                                    {
                                        "sale_index": 0,
                                        "selected_invoice_id": "inv-paid",
                                        "selected_invoice_number": "INV-5727",
                                    }
                                ],
                            }
                        ],
                    },
                )
                progress = self._wait_for_submit_job(client)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["async"], True)
        self.assertEqual(progress["status"], "done")
        self.assertEqual(len(fake.batch_payments), 0)
        self.assertEqual(len(fake.deleted_payments), 1)
        self.assertEqual(fake.deleted_payments[0][0], "pay-1")
        self.assertEqual(len(fake.created_payments), 1)
        created_payment = fake.created_payments[0]["Payments"][0]
        self.assertEqual(created_payment["Invoice"]["InvoiceID"], "inv-paid")
        self.assertEqual(created_payment["Account"], {"Code": "780"})
        self.assertEqual(created_payment["Amount"], 270.0)
        self.assertEqual(len(fake.bank_transactions), 1)
        tx = fake.bank_transactions[0]["BankTransactions"][0]
        self.assertEqual(tx["Type"], "RECEIVE")
        self.assertEqual(tx["Reference"], "Cashflows pay-1")
        self.assertEqual(tx["LineItems"][0]["AccountCode"], "780")
        self.assertEqual(tx["LineItems"][0]["UnitAmount"], 270.0)
        self.assertEqual(tx["LineItems"][1]["AccountCode"], "404")
        self.assertEqual(tx["LineItems"][1]["UnitAmount"], -2.32)
        self.assertEqual(progress["completed"], 1)

    def test_production_paid_invoice_already_in_clearing_creates_net_match_without_moving_payment(self):
        preview = {
            "preview_id": "preview-1",
            "batches": [
                {
                    "id": "batch-1",
                    "status": "ready",
                    "payout": {"csv_ref": "pay-1", "date": "2026-07-02"},
                    "gross": 174.0,
                    "net": 172.50,
                    "sales": [
                        {
                            "sale_ref": "sale-1",
                            "date": "2026-06-24",
                            "gross": 174.0,
                            "fee": 1.50,
                            "invoice": {
                                "id": "inv-paid",
                                "number": "INV-5730",
                                "contact_name": "Raj Rana",
                                "total": 174.0,
                                "amount_due": 0.0,
                                "is_open": False,
                            },
                            "candidates": [],
                            "tied_candidates": [],
                        }
                    ],
                }
            ],
        }
        app = self._app_with_preview(preview, dry_run=False, production=True)
        fake = FakeXeroClient()
        fake.dry_run = False
        fake.invoice_payments["inv-paid"] = [{"PaymentID": "pay-clearing"}]
        fake.payment_details["pay-clearing"] = {
            "PaymentID": "pay-clearing",
            "HasAccount": True,
            "Status": "AUTHORISED",
            "IsReconciled": False,
            "Amount": 174.0,
            "Account": {"Code": "780", "Name": "Cashflow reconciliation"},
        }
        with app.test_client() as client:
            with client.session_transaction() as session:
                session["logged_in"] = True
            with patch("app.admin_web.build_xero_client", return_value=fake), patch(
                "app.admin_web._CF_SUBMIT_PACE_SECONDS", 0
            ):
                resp = client.post(
                    "/cashflows-sync/submit-csv-batches",
                    json={
                        "preview_id": "preview-1",
                        "batches": [
                            {
                                "batch_id": "batch-1",
                                "sales": [
                                    {
                                        "sale_index": 0,
                                        "selected_invoice_id": "inv-paid",
                                        "selected_invoice_number": "INV-5730",
                                    }
                                ],
                            }
                        ],
                    },
                )
                progress = self._wait_for_submit_job(client)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["async"], True)
        self.assertEqual(progress["status"], "done")
        self.assertEqual(len(fake.deleted_payments), 0)
        self.assertEqual(len(fake.created_payments), 0)
        self.assertEqual(len(fake.batch_payments), 0)
        self.assertEqual(len(fake.bank_transactions), 1)
        tx = fake.bank_transactions[0]["BankTransactions"][0]
        self.assertEqual(tx["Type"], "RECEIVE")
        self.assertEqual(tx["LineItems"][0]["AccountCode"], "780")
        self.assertEqual(tx["LineItems"][0]["UnitAmount"], 174.0)
        self.assertEqual(tx["LineItems"][1]["UnitAmount"], -1.5)
        self.assertEqual(progress["completed"], 1)

    def test_invented_invoice_id_is_rejected(self):
        preview = {
            "preview_id": "preview-1",
            "batches": [
                {
                    "id": "batch-1",
                    "status": "ready",
                    "payout": {"csv_ref": "pay-1", "date": "2026-06-30"},
                    "gross": 100.0,
                    "net": 98.0,
                    "sales": [
                        {
                            "sale_ref": "sale-1",
                            "date": "2026-06-30",
                            "gross": 100.0,
                            "fee": 2.0,
                            "invoice": {"id": "inv-1", "number": "INV-1", "total": 100.0},
                            "candidates": [],
                            "tied_candidates": [],
                        }
                    ],
                }
            ],
        }
        app = self._app_with_preview(preview)
        with app.test_client() as client:
            with client.session_transaction() as session:
                session["logged_in"] = True
            with patch("app.admin_web.build_xero_client", return_value=FakeXeroClient()):
                resp = client.post(
                    "/cashflows-sync/submit-csv-batches",
                    json={
                        "preview_id": "preview-1",
                        "batches": [
                            {
                                "batch_id": "batch-1",
                                "sales": [
                                    {
                                        "sale_index": 0,
                                        "selected_invoice_id": "made-up",
                                    }
                                ],
                            }
                        ],
                    },
                )
        self.assertEqual(resp.status_code, 400)

    def test_refresh_csv_preview_rebuilds_from_cached_csv(self):
        cached_csv = (
            "'Ref','Date','Time','Description','Type','Debit','Credit','Balance'\n"
            '"1","2026-05-01","10:00","Sale AAA","Sale Settlement","","100.00","100.00"\n'
            '"2","2026-05-01","10:00","Merchant Service Charge Sale Ref: AAA","Merchant Service Charge","1.00","","99.00"\n'
            '"3","2026-05-02","18:00","Maturity of Sales","Transfer for Remittance","99.00","","0.00"\n'
        )
        preview = {"preview_id": "preview-1", "_source_csv_text": cached_csv}
        app = self._app_with_preview(preview)
        fake = FakeXeroClient()
        with app.test_client() as client:
            with client.session_transaction() as session:
                session["logged_in"] = True
            with patch("app.admin_web.build_xero_client", return_value=fake):
                resp = client.post(
                    "/cashflows-sync/refresh-csv-preview",
                    json={"preview_id": "preview-1", "focus_batch_id": "batch-1"},
                )
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["totals"]["payout_count"], 1)
        self.assertEqual(data["status_counts"]["no_bank_line"], 1)
        self.assertNotIn("_source_csv_text", data)

    def test_refresh_csv_preview_requires_cached_csv(self):
        app = self._app_with_preview({"preview_id": "preview-1", "batches": []})
        with app.test_client() as client:
            with client.session_transaction() as session:
                session["logged_in"] = True
            resp = client.post(
                "/cashflows-sync/refresh-csv-preview",
                json={"preview_id": "preview-1"},
            )
        self.assertEqual(resp.status_code, 409)


if __name__ == "__main__":
    unittest.main()
