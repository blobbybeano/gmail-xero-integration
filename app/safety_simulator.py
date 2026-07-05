from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import json
import random
from dataclasses import dataclass, field
from typing import Callable
from zoneinfo import ZoneInfo

from .event_processor import (
    done_choice_is_yes,
    ensure_notes_template,
    extract_invoice_lines,
    normalize_user_sections,
    parse_app_ledger,
    payment_choice,
    send_choice_is_yes,
    strip_app_ledger,
    upsert_app_ledger,
    upsert_invoice_summary,
    upsert_send_confirmation,
)
from .state import (
    bump_xero_action_attempts,
    get_invoice_for_event,
    get_processed_update_marker,
    get_xero_action_attempts,
    is_invoice_paid,
    is_invoice_sent,
    mark_invoice_paid,
    mark_invoice_sent,
    mark_recent_xero_webhook,
    set_invoice_for_event,
    set_processed_update_marker,
    was_recent_xero_webhook,
)

LONDON_TZ = ZoneInfo("Europe/London")
MAX_XERO_ATTEMPTS = 2
PAST_EVENT_AUTO_XERO_HOURS = 24


@dataclass
class SimClock:
    now: dt.datetime

    def advance(self, seconds: int) -> None:
        self.now += dt.timedelta(seconds=seconds)

    def iso(self) -> str:
        return self.now.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass
class FakeCalendarEvent:
    id: str
    calendar_id: str
    summary: str
    description: str
    start: dt.datetime
    end: dt.datetime
    created: dt.datetime
    updated: dt.datetime
    app_update_count: int = 0
    user_update_count: int = 0

    def to_google(self) -> dict:
        return {
            "id": self.id,
            "_calendar_id": self.calendar_id,
            "summary": self.summary,
            "description": self.description,
            "created": self.created.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
            "updated": self.updated.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
            "start": {"dateTime": self.start.isoformat()},
            "end": {"dateTime": self.end.isoformat()},
            "creator": {"email": "engineer@example.test"},
            "organizer": {"email": "engineer@example.test"},
        }


@dataclass
class FakeGoogleCalendar:
    clock: SimClock
    events: dict[str, FakeCalendarEvent] = field(default_factory=dict)
    webhook_queue: list[tuple[str, str]] = field(default_factory=list)
    read_count: int = 0
    write_count: int = 0

    def add_event(self, event: FakeCalendarEvent) -> None:
        self.events[self.key(event.calendar_id, event.id)] = event
        self.webhook_queue.append((event.calendar_id, event.id))

    def user_edit(self, event_key: str, editor: Callable[[FakeCalendarEvent], None]) -> None:
        event = self.events[event_key]
        editor(event)
        event.updated = self.clock.now
        event.user_update_count += 1
        self.webhook_queue.append((event.calendar_id, event.id))

    def app_patch(
        self,
        event_key: str,
        *,
        description: str | None = None,
        summary: str | None = None,
    ) -> None:
        event = self.events[event_key]
        if description is not None:
            event.description = description
        if summary is not None:
            event.summary = summary
        event.updated = self.clock.now
        event.app_update_count += 1
        self.write_count += 1
        # Google push notifications also happen after app writes. The simulator
        # intentionally queues this to prove app-owned updates do not loop.
        self.webhook_queue.append((event.calendar_id, event.id))

    def pop_webhooks(self) -> list[tuple[str, str]]:
        out = self.webhook_queue[:]
        self.webhook_queue.clear()
        return out

    def list_updated_since(self, calendar_id: str, updated_min: dt.datetime) -> list[dict]:
        self.read_count += 1
        return [
            copy.deepcopy(event.to_google())
            for event in self.events.values()
            if event.calendar_id == calendar_id and event.updated >= updated_min
        ]

    @staticmethod
    def key(calendar_id: str, event_id: str) -> str:
        return f"{calendar_id}:{event_id}"


class FakeXeroError(RuntimeError):
    pass


class FakeXeroRateLimit(FakeXeroError):
    pass


class FakeXeroSafetyViolation(FakeXeroError):
    pass


@dataclass
class FakeXero:
    fail_plan: dict[str, list[str]] = field(default_factory=dict)
    invoices: dict[str, dict] = field(default_factory=dict)
    call_log: list[dict] = field(default_factory=list)
    safety_violations: list[str] = field(default_factory=list)
    externally_paid_invoices: set[str] = field(default_factory=set)
    next_invoice_number: int = 1
    token_expired: bool = False
    refresh_plan: list[str] = field(default_factory=list)
    refresh_count: int = 0

    def ensure_connected(self) -> None:
        if not self.token_expired:
            return
        self.refresh_count += 1
        self.call_log.append({"action": "refresh_token", "event_key": "", "invoice_id": ""})
        outcome = self.refresh_plan.pop(0) if self.refresh_plan else "ok"
        if outcome != "ok":
            raise FakeXeroError(f"Xero simulated token refresh failed: {outcome}")
        self.token_expired = False

    def _call(self, action: str, event_key: str = "", invoice_id: str = "") -> None:
        self.ensure_connected()
        if invoice_id and invoice_id in self.externally_paid_invoices and action in {
            "update_invoice",
            "authorize_invoice",
            "email_invoice",
            "record_payment",
        }:
            message = (
                f"blocked unsafe {action} against externally paid invoice "
                f"{invoice_id} for {event_key}"
            )
            self.safety_violations.append(message)
            raise FakeXeroSafetyViolation(message)
        self.call_log.append(
            {"action": action, "event_key": event_key, "invoice_id": invoice_id}
        )
        plan = self.fail_plan.get(action) or []
        if plan:
            outcome = plan.pop(0)
            if outcome == "429":
                raise FakeXeroRateLimit("Xero simulated 429")
            if outcome == "timeout":
                raise FakeXeroError("Xero simulated timeout")
            if outcome == "email_failed":
                raise FakeXeroError("Xero simulated email failure")

    def create_invoice(self, *, event_key: str, line_items: list[dict]) -> dict:
        self._call("create_invoice", event_key=event_key)
        invoice_id = f"fake-inv-{self.next_invoice_number:04d}"
        invoice_number = f"SIM-{self.next_invoice_number:04d}"
        self.next_invoice_number += 1
        subtotal = sum(float(line.get("UnitAmount") or 0.0) for line in line_items)
        total = subtotal + sum(
            float(line.get("UnitAmount") or 0.0) * (0.2 if line.get("TaxType") else 0.0)
            for line in line_items
        )
        invoice = {
            "InvoiceID": invoice_id,
            "InvoiceNumber": invoice_number,
            "Status": "DRAFT",
            "SubTotal": subtotal,
            "Total": total,
            "AmountDue": total,
            "LineItems": copy.deepcopy(line_items),
        }
        self.invoices[invoice_id] = invoice
        return {"Invoices": [copy.deepcopy(invoice)]}

    def update_invoice(self, *, event_key: str, invoice_id: str, line_items: list[dict]) -> dict:
        self._call("update_invoice", event_key=event_key, invoice_id=invoice_id)
        invoice = self.invoices[invoice_id]
        invoice["LineItems"] = copy.deepcopy(line_items)
        return {"Invoices": [copy.deepcopy(invoice)]}

    def authorize_invoice(self, *, event_key: str, invoice_id: str) -> dict:
        self._call("authorize_invoice", event_key=event_key, invoice_id=invoice_id)
        invoice = self.invoices[invoice_id]
        invoice["Status"] = "AUTHORISED"
        return {"Invoices": [copy.deepcopy(invoice)]}

    def email_invoice(self, *, event_key: str, invoice_id: str) -> bool:
        try:
            self._call("email_invoice", event_key=event_key, invoice_id=invoice_id)
        except FakeXeroError:
            return False
        return True

    def record_payment(self, *, event_key: str, invoice_id: str) -> dict:
        self._call("record_payment", event_key=event_key, invoice_id=invoice_id)
        invoice = self.invoices[invoice_id]
        invoice["Status"] = "PAID"
        invoice["AmountDue"] = 0.0
        return {"Payments": [{"Invoice": {"InvoiceID": invoice_id}}]}

    def mark_invoice_paid_external(self, invoice_id: str) -> None:
        invoice = self.invoices[invoice_id]
        invoice["Status"] = "PAID"
        invoice["AmountDue"] = 0.0
        self.externally_paid_invoices.add(invoice_id)

    def get_invoice(self, *, event_key: str, invoice_id: str) -> dict:
        self._call("get_invoice", event_key=event_key, invoice_id=invoice_id)
        return copy.deepcopy(self.invoices[invoice_id])

    def online_url(self, invoice_id: str) -> str:
        return f"https://fake.xero.test/{invoice_id}"


@dataclass
class ScenarioResult:
    name: str
    passed: bool
    notes: list[str]
    xero_calls: int
    calendar_writes: int


class SafetySimulator:
    def __init__(self, *, clock: SimClock, calendar: FakeGoogleCalendar, xero: FakeXero):
        self.clock = clock
        self.calendar = calendar
        self.xero = xero
        self.state: dict = {}
        self.last_sync = self.clock.now - dt.timedelta(seconds=1)
        self.notes: list[str] = []

    def run_until_idle(self, *, max_cycles: int = 50) -> None:
        for _ in range(max_cycles):
            webhooks = self.calendar.pop_webhooks()
            if not webhooks:
                return
            calendar_ids = sorted({calendar_id for calendar_id, _event_id in webhooks})
            targeted_by_calendar: dict[str, set[str]] = {}
            for calendar_id, event_id in webhooks:
                targeted_by_calendar.setdefault(calendar_id, set()).add(event_id)
            for calendar_id in calendar_ids:
                seen: set[str] = set()
                for event_id in sorted(targeted_by_calendar.get(calendar_id, set())):
                    event_obj = self.calendar.events.get(FakeGoogleCalendar.key(calendar_id, event_id))
                    if event_obj:
                        seen.add(event_id)
                        self.process_event(copy.deepcopy(event_obj.to_google()), targeted=True)
                for event in sorted(
                    self.calendar.list_updated_since(calendar_id, self.last_sync),
                    key=lambda item: str(item.get("updated") or ""),
                ):
                    if str(event.get("id") or "") in seen:
                        continue
                    self.process_event(event, targeted=True)
            self.last_sync = self.clock.now
            self.clock.advance(5)
        raise AssertionError("simulator did not become idle; likely webhook loop")

    def run_hourly_sweep(self) -> None:
        """Simulate the app's broad/hourly calendar sweep without a targeted save."""
        for event in sorted(
            (copy.deepcopy(item.to_google()) for item in self.calendar.events.values()),
            key=lambda item: str(item.get("updated") or ""),
            reverse=True,
        ):
            self.process_event(event, targeted=False)

    def process_event(self, event: dict, *, targeted: bool = True) -> None:
        event_key = FakeGoogleCalendar.key(event["_calendar_id"], event["id"])
        raw_description = event.get("description") or ""
        has_done = done_choice_is_yes(raw_description)
        has_send = send_choice_is_yes(raw_description)
        if not has_done and not has_send:
            return

        event_end = self._parse_event_end(event)
        if (
            not targeted
            and event_end
            and event_end < (self.clock.now - dt.timedelta(hours=PAST_EVENT_AUTO_XERO_HOURS))
        ):
            self.state = set_processed_update_marker(
                self.state,
                event_key,
                str(event.get("updated") or ""),
            )
            return

        description = normalize_user_sections(raw_description)
        if description != raw_description:
            self.calendar.app_patch(event_key, description=description)
            event["description"] = description

        invoice_lines = extract_invoice_lines(description)
        if has_done and not invoice_lines:
            fingerprint = self._fingerprint("format", description)
            ledger = parse_app_ledger(description)
            if (
                ledger.get("s") == "needs_input"
                and ledger.get("r") == "missing_lines"
                and ledger.get("fp") == fingerprint
            ):
                self.state = set_processed_update_marker(self.state, event_key, str(event.get("updated") or ""))
                return
            blocked = upsert_app_ledger(
                description,
                message="Needs input - missing invoice lines",
                state="needs_input",
                reason="missing_lines",
                fingerprint=fingerprint,
                xero_attempts=0,
                wait="human_save",
            )
            self.calendar.app_patch(event_key, description=blocked, summary=self._with_marker(event["summary"]))
            self.state = set_processed_update_marker(self.state, event_key, self.clock.iso())
            return

        processed_marker = get_processed_update_marker(self.state, event_key)
        invoice_id = get_invoice_for_event(self.state, event_key) or ""
        if (
            processed_marker == str(event.get("updated") or "")
            and invoice_id
            and is_invoice_sent(self.state, event_key)
            and not is_invoice_paid(self.state, event_key)
            and not was_recent_xero_webhook(
                self.state,
                event_key,
                invoice_id,
                now_ts=self.clock.now.timestamp(),
                within_seconds=900,
            )
        ):
            paid_sync_fp = self._fingerprint(
                "paid_sync",
                f"{event.get('updated') or ''}|{invoice_id}|{payment_choice(description)}",
            )
            attempts = get_xero_action_attempts(
                self.state,
                event_key,
                "paid_sync",
                paid_sync_fp,
            )
            if attempts >= MAX_XERO_ATTEMPTS:
                return
            try:
                invoice = self.xero.get_invoice(event_key=event_key, invoice_id=invoice_id)
            except FakeXeroError:
                self.state, _attempts = bump_xero_action_attempts(
                    self.state,
                    event_key,
                    "paid_sync",
                    paid_sync_fp,
                )
                return
            if str(invoice.get("Status") or "").upper() == "PAID":
                self.state = mark_invoice_paid(self.state, event_key)
                self.calendar.app_patch(event_key, summary=self._set_status(event["summary"], "green"))

        if processed_marker == str(event.get("updated") or "") and not self._has_pending_work(event_key, description):
            return

        draft_fp = self._fingerprint(
            "draft",
            json.dumps(
                {
                    "lines": invoice_lines,
                    "payment": payment_choice(description),
                },
                sort_keys=True,
            ),
        )
        if has_done and invoice_lines and not invoice_id and not is_invoice_sent(self.state, event_key):
            if self._waiting_for_human(description, draft_fp):
                self.state = set_processed_update_marker(self.state, event_key, str(event.get("updated") or ""))
                return
            if not self._allow_xero_action(event_key, "draft", draft_fp, description):
                self._block_action(event_key, event, "draft", draft_fp, "draft_attempt_limit")
                return
            try:
                result = self.xero.create_invoice(event_key=event_key, line_items=invoice_lines)
            except FakeXeroError as exc:
                self._record_action_failure(event_key, event, "draft", draft_fp, str(exc))
                return
            invoice = result["Invoices"][0]
            invoice_id = invoice["InvoiceID"]
            self.state = set_invoice_for_event(self.state, event_key, invoice_id)
            desc = upsert_invoice_summary(
                description,
                float(invoice.get("SubTotal") or 0.0),
                float(invoice.get("Total") or 0.0),
                sent=False,
                invoice_url=self.xero.online_url(invoice_id),
                include_prompt=True,
            )
            desc = upsert_app_ledger(
                desc,
                message="Draft ready",
                state="draft",
                reason="ok",
                fingerprint=draft_fp,
                xero_attempts=get_xero_action_attempts(self.state, event_key, "draft", draft_fp),
                wait="send",
                invoice=invoice_id,
            )
            self.calendar.app_patch(event_key, description=desc, summary=self._set_status(event["summary"], "orange"))
            self.state = set_processed_update_marker(self.state, event_key, self.clock.iso())
            return

        if has_send and invoice_lines and invoice_id and not is_invoice_sent(self.state, event_key):
            mode = payment_choice(description)
            send_fp = self._fingerprint("send", f"{draft_fp}|{invoice_id}|{mode}")
            if self._waiting_for_human(description, send_fp):
                self.state = set_processed_update_marker(self.state, event_key, str(event.get("updated") or ""))
                return
            if not self._allow_xero_action(event_key, "send", send_fp, description):
                self._block_action(event_key, event, "send", send_fp, "send_attempt_limit")
                return
            try:
                status = (self.xero.invoices[invoice_id].get("Status") or "").upper()
                if status == "PAID":
                    desc = upsert_send_confirmation(description, invoice_url=self.xero.online_url(invoice_id))
                    desc = upsert_app_ledger(
                        desc,
                        message=f"Already paid - {invoice_id}",
                        state="sent",
                        reason="already_paid",
                        fingerprint=send_fp,
                        xero_attempts=get_xero_action_attempts(self.state, event_key, "send", send_fp),
                        wait="none",
                        invoice=invoice_id,
                    )
                    self.state = mark_invoice_sent(self.state, event_key)
                    self.state = mark_invoice_paid(self.state, event_key)
                    self.calendar.app_patch(
                        event_key,
                        description=desc,
                        summary=self._set_status(event["summary"], "green"),
                    )
                    self.state = set_processed_update_marker(self.state, event_key, self.clock.iso())
                    return
                if status in {"DRAFT", "SUBMITTED"}:
                    self.xero.update_invoice(event_key=event_key, invoice_id=invoice_id, line_items=invoice_lines)
                    self.xero.authorize_invoice(event_key=event_key, invoice_id=invoice_id)
                elif status != "AUTHORISED":
                    raise FakeXeroError(f"Unsupported invoice status for send: {status or 'UNKNOWN'}")
                if mode == "card" and (self.xero.invoices[invoice_id].get("Status") or "").upper() != "PAID":
                    self.xero.record_payment(event_key=event_key, invoice_id=invoice_id)
                if not self.xero.email_invoice(event_key=event_key, invoice_id=invoice_id):
                    raise FakeXeroError("Xero simulated email failure")
            except FakeXeroError as exc:
                self._record_action_failure(event_key, event, "send", send_fp, str(exc))
                return

            desc = upsert_send_confirmation(description, invoice_url=self.xero.online_url(invoice_id))
            desc = upsert_app_ledger(
                desc,
                message=f"Sent - {invoice_id}",
                state="sent",
                reason="ok",
                fingerprint=send_fp,
                xero_attempts=get_xero_action_attempts(self.state, event_key, "send", send_fp),
                wait="none",
                invoice=invoice_id,
            )
            self.state = mark_invoice_sent(self.state, event_key)
            if mode == "card":
                self.state = mark_invoice_paid(self.state, event_key)
            self.calendar.app_patch(
                event_key,
                description=desc,
                summary=self._set_status(event["summary"], "green" if mode == "card" else "yellow"),
            )
            self.state = set_processed_update_marker(self.state, event_key, self.clock.iso())

    def _allow_xero_action(
        self,
        event_key: str,
        action: str,
        fingerprint: str,
        description: str,
    ) -> bool:
        attempts = get_xero_action_attempts(self.state, event_key, action, fingerprint)
        ledger = parse_app_ledger(description)
        if ledger.get("fp") == fingerprint:
            try:
                attempts = max(attempts, int(ledger.get("x") or 0))
            except Exception:
                pass
        if attempts >= MAX_XERO_ATTEMPTS:
            return False
        self.state, _attempts = bump_xero_action_attempts(
            self.state, event_key, action, fingerprint
        )
        return True

    @staticmethod
    def _waiting_for_human(description: str, fingerprint: str) -> bool:
        ledger = parse_app_ledger(description)
        return bool(
            ledger.get("fp") == fingerprint
            and ledger.get("w") == "human_save"
            and ledger.get("s") in {"error", "needs_input"}
        )

    def _record_action_failure(
        self,
        event_key: str,
        event: dict,
        action: str,
        fingerprint: str,
        reason: str,
    ) -> None:
        attempts = get_xero_action_attempts(self.state, event_key, action, fingerprint)
        desc = upsert_app_ledger(
            event.get("description") or "",
            message=f"Needs input - {action} failed",
            state="error",
            reason=self._slug(reason),
            fingerprint=fingerprint,
            xero_attempts=attempts,
            wait="human_save",
        )
        self.calendar.app_patch(event_key, description=desc, summary=self._set_status(event["summary"], "orange"))
        self.state = set_processed_update_marker(self.state, event_key, self.clock.iso())

    def _block_action(
        self,
        event_key: str,
        event: dict,
        action: str,
        fingerprint: str,
        reason: str,
    ) -> None:
        attempts = get_xero_action_attempts(self.state, event_key, action, fingerprint)
        desc = upsert_app_ledger(
            event.get("description") or "",
            message="Needs input - repeated Xero attempts stopped",
            state="needs_input",
            reason=reason,
            fingerprint=fingerprint,
            xero_attempts=attempts,
            wait="human_save",
        )
        self.calendar.app_patch(event_key, description=desc, summary=self._set_status(event["summary"], "orange"))
        self.state = set_processed_update_marker(self.state, event_key, self.clock.iso())

    def _has_pending_work(self, event_key: str, description: str) -> bool:
        return bool(
            done_choice_is_yes(description)
            and extract_invoice_lines(description)
            and not get_invoice_for_event(self.state, event_key)
        )

    @staticmethod
    def _fingerprint(kind: str, value: str) -> str:
        stable = strip_app_ledger(value).strip()
        return hashlib.sha1(f"{kind}:{stable}".encode("utf-8")).hexdigest()[:10]

    @staticmethod
    def _slug(value: str) -> str:
        return "".join(ch.lower() if ch.isalnum() else "_" for ch in value)[:48].strip("_")

    @staticmethod
    def _with_marker(summary: str) -> str:
        if "(Check Formatting)" in summary:
            return summary
        return f"{summary} (Check Formatting)"

    @staticmethod
    def _set_status(summary: str, status: str) -> str:
        emoji = {
            "blue": "🔵",
            "orange": "🟠",
            "yellow": "🟡",
            "green": "🟢",
            "red": "🔴",
        }.get(status, "🟠")
        text = summary
        while text and text[0] in "🔵🟠🟡🟢🔴":
            text = text[1:].strip()
        return f"{emoji} {text}".strip()

    @staticmethod
    def _parse_event_end(event: dict) -> dt.datetime | None:
        raw = ((event.get("end") or {}).get("dateTime") or "").strip()
        if not raw:
            return None
        try:
            return dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None


def _event(
    *,
    event_id: str,
    summary: str,
    description: str,
    clock: SimClock,
    days_from_now: int = 0,
    calendar_id: str = "fake-calendar",
) -> FakeCalendarEvent:
    start = (clock.now + dt.timedelta(days=days_from_now)).astimezone(LONDON_TZ)
    return FakeCalendarEvent(
        id=event_id,
        calendar_id=calendar_id,
        summary=summary,
        description=description,
        start=start,
        end=start + dt.timedelta(hours=1),
        created=clock.now,
        updated=clock.now,
    )


def _draft_description(*, name: str = "Test Customer", price: int = 120) -> str:
    return ensure_notes_template(
        f"""[contact]
Customer name: {name}
Customer email address: {name.lower().replace(' ', '.')}@example.test
Customer contact number: 07000000000
[/contact]

[invoice]
Gutter clean = £{price}+VAT
[/invoice]

PROCESS DRAFT (Y/N) = Y
PAYMENT TYPE (CARD/INVOICE) = INVOICE
SEND NOW (Y/N) =
"""
    )


def run_default_suite() -> list[ScenarioResult]:
    results = [
        scenario_successful_invoice_send(),
        scenario_already_paid_send_does_not_mutate_xero(),
        scenario_missing_lines_hold(),
        scenario_repeated_xero_429_stops(),
        scenario_token_refresh_before_draft(),
        scenario_xero_disconnect_does_not_red_flicker(),
        scenario_old_event_ignored_until_touched(),
        scenario_webhook_storm_no_duplicate_invoice(),
        scenario_xero_webhook_echo_does_not_recheck_xero(),
        scenario_paid_sync_failures_stop_until_resave(),
        scenario_fast_forward_current_diary_load(),
        scenario_long_running_mixed_diary_stress(),
    ]
    return results


def _new_sim(
    fail_plan: dict[str, list[str]] | None = None,
    *,
    token_expired: bool = False,
    refresh_plan: list[str] | None = None,
) -> SafetySimulator:
    clock = SimClock(dt.datetime(2026, 6, 22, 8, 0, tzinfo=LONDON_TZ))
    calendar = FakeGoogleCalendar(clock)
    xero = FakeXero(
        fail_plan=copy.deepcopy(fail_plan or {}),
        token_expired=token_expired,
        refresh_plan=list(refresh_plan or []),
    )
    return SafetySimulator(clock=clock, calendar=calendar, xero=xero)


def scenario_successful_invoice_send() -> ScenarioResult:
    sim = _new_sim()
    event = _event(
        event_id="success-send",
        summary="🔵 SW19 G.C Rachel",
        description=_draft_description(name="Rachel Test", price=140),
        clock=sim.clock,
    )
    key = FakeGoogleCalendar.key(event.calendar_id, event.id)
    sim.calendar.add_event(event)
    sim.run_until_idle()
    sim.clock.advance(60)
    sim.calendar.user_edit(
        key,
        lambda ev: setattr(
            ev,
            "description",
            ev.description.replace("SEND NOW (Y/N) =", "SEND NOW (Y/N) = Y"),
        ),
    )
    sim.run_until_idle()
    calls = [row["action"] for row in sim.xero.call_log]
    final = sim.calendar.events[key]
    passed = (
        calls.count("create_invoice") == 1
        and calls.count("authorize_invoice") == 1
        and "Invoice sent" in final.description
        and parse_app_ledger(final.description).get("s") == "sent"
    )
    return ScenarioResult(
        "successful_invoice_send",
        passed,
        [f"calls={calls}", f"ledger={parse_app_ledger(final.description)}"],
        len(calls),
        sim.calendar.write_count,
    )


def scenario_already_paid_send_does_not_mutate_xero() -> ScenarioResult:
    sim = _new_sim()
    event = _event(
        event_id="already-paid-send",
        summary="🟢 PW W5 Barbara",
        description=_draft_description(name="Barbara Paid", price=185),
        clock=sim.clock,
    )
    key = FakeGoogleCalendar.key(event.calendar_id, event.id)
    sim.calendar.add_event(event)
    sim.run_until_idle()
    invoice_id = get_invoice_for_event(sim.state, key) or ""
    sim.xero.mark_invoice_paid_external(invoice_id)
    before_calls = len(sim.xero.call_log)
    sim.clock.advance(60)
    sim.calendar.user_edit(
        key,
        lambda ev: setattr(
            ev,
            "description",
            ev.description.replace("SEND NOW (Y/N) =", "SEND NOW (Y/N) = Y"),
        ),
    )
    sim.run_until_idle()
    new_calls = sim.xero.call_log[before_calls:]
    final = sim.calendar.events[key]
    ledger = parse_app_ledger(final.description)
    passed = (
        not new_calls
        and not sim.xero.safety_violations
        and ledger.get("s") == "sent"
        and ledger.get("r") == "already_paid"
        and final.summary.startswith("🟢")
    )
    return ScenarioResult(
        "already_paid_send_does_not_mutate_xero",
        passed,
        [
            f"new_calls={new_calls}",
            f"safety_violations={sim.xero.safety_violations}",
            f"ledger={ledger}",
            f"summary={final.summary}",
        ],
        len(sim.xero.call_log),
        sim.calendar.write_count,
    )


def scenario_missing_lines_hold() -> ScenarioResult:
    sim = _new_sim()
    description = ensure_notes_template(
        """[contact]
Customer name: Missing Lines
Customer email address: missing.lines@example.test
Customer contact number: 07000000000
[/contact]

[invoice]
[/invoice]

PROCESS DRAFT (Y/N) = Y
PAYMENT TYPE (CARD/INVOICE) = INVOICE
SEND NOW (Y/N) =
"""
    )
    event = _event(
        event_id="missing-lines",
        summary="🔵 GC E1 Michelle",
        description=description,
        clock=sim.clock,
    )
    key = FakeGoogleCalendar.key(event.calendar_id, event.id)
    sim.calendar.add_event(event)
    sim.run_until_idle()
    sim.run_until_idle()
    final = sim.calendar.events[key]
    ledger = parse_app_ledger(final.description)
    passed = (
        len(sim.xero.call_log) == 0
        and ledger.get("s") == "needs_input"
        and ledger.get("r") == "missing_lines"
        and "(Check Formatting)" in final.summary
    )
    return ScenarioResult(
        "missing_lines_hold",
        passed,
        [f"ledger={ledger}", f"summary={final.summary}"],
        len(sim.xero.call_log),
        sim.calendar.write_count,
    )


def scenario_repeated_xero_429_stops() -> ScenarioResult:
    sim = _new_sim({"create_invoice": ["429", "429", "429", "429"]})
    event = _event(
        event_id="rate-limited",
        summary="🔵 SW15 G.C Gordon",
        description=_draft_description(name="Gordon Retry", price=145),
        clock=sim.clock,
    )
    key = FakeGoogleCalendar.key(event.calendar_id, event.id)
    sim.calendar.add_event(event)
    for _ in range(4):
        sim.run_until_idle()
        sim.clock.advance(60)
        sim.calendar.user_edit(key, lambda ev: setattr(ev, "description", ev.description))
    final = sim.calendar.events[key]
    ledger = parse_app_ledger(final.description)
    create_calls = [row for row in sim.xero.call_log if row["action"] == "create_invoice"]
    passed = (
        0 < len(create_calls) <= MAX_XERO_ATTEMPTS
        and ledger.get("s") in {"error", "needs_input"}
        and ledger.get("w") == "human_save"
    )
    return ScenarioResult(
        "repeated_xero_429_stops",
        passed,
        [f"create_calls={len(create_calls)}", f"ledger={ledger}"],
        len(sim.xero.call_log),
        sim.calendar.write_count,
    )


def scenario_token_refresh_before_draft() -> ScenarioResult:
    sim = _new_sim(token_expired=True, refresh_plan=["ok"])
    event = _event(
        event_id="token-refresh",
        summary="🔵 GC Token Refresh",
        description=_draft_description(name="Token Refresh", price=110),
        clock=sim.clock,
    )
    key = FakeGoogleCalendar.key(event.calendar_id, event.id)
    sim.calendar.add_event(event)
    sim.run_until_idle()
    calls = [row["action"] for row in sim.xero.call_log]
    final = sim.calendar.events[key]
    passed = (
        calls.count("refresh_token") == 1
        and calls.count("create_invoice") == 1
        and not sim.xero.token_expired
        and not final.summary.startswith("🔴")
    )
    return ScenarioResult(
        "token_refresh_before_draft",
        passed,
        [f"calls={calls}", f"summary={final.summary}"],
        len(calls),
        sim.calendar.write_count,
    )


def scenario_xero_disconnect_does_not_red_flicker() -> ScenarioResult:
    sim = _new_sim(token_expired=True, refresh_plan=["invalid_refresh_token"])
    event = _event(
        event_id="xero-disconnect",
        summary="🔵 GC Xero Down",
        description=_draft_description(name="Xero Down", price=115),
        clock=sim.clock,
    )
    key = FakeGoogleCalendar.key(event.calendar_id, event.id)
    sim.calendar.add_event(event)
    sim.run_until_idle()
    sim.run_until_idle()
    final = sim.calendar.events[key]
    ledger = parse_app_ledger(final.description)
    passed = (
        sim.xero.refresh_count == 1
        and not final.summary.startswith("🔴")
        and ledger.get("s") == "error"
        and ledger.get("w") == "human_save"
        and final.app_update_count <= 2
    )
    return ScenarioResult(
        "xero_disconnect_does_not_red_flicker",
        passed,
        [
            f"refresh_count={sim.xero.refresh_count}",
            f"summary={final.summary}",
            f"ledger={ledger}",
            f"app_updates={final.app_update_count}",
        ],
        len(sim.xero.call_log),
        sim.calendar.write_count,
    )


def scenario_old_event_ignored_until_touched() -> ScenarioResult:
    sim = _new_sim()
    old_event = _event(
        event_id="old-event",
        summary="🔵 GC Lynn Barber WS4",
        description=_draft_description(name="Lynn Old", price=95),
        clock=sim.clock,
        days_from_now=-14,
    )
    old_event.updated = sim.clock.now - dt.timedelta(days=10)
    key = FakeGoogleCalendar.key(old_event.calendar_id, old_event.id)
    sim.calendar.events[key] = old_event
    sim.run_until_idle()
    untouched_calls = len(sim.xero.call_log)
    sim.clock.advance(60)
    sim.calendar.user_edit(key, lambda ev: setattr(ev, "description", ev.description + "\n"))
    sim.run_until_idle()
    touched_calls = len(sim.xero.call_log)
    passed = untouched_calls == 0 and touched_calls == 1
    return ScenarioResult(
        "old_event_ignored_until_touched",
        passed,
        [f"untouched_calls={untouched_calls}", f"touched_calls={touched_calls}"],
        len(sim.xero.call_log),
        sim.calendar.write_count,
    )


def scenario_webhook_storm_no_duplicate_invoice() -> ScenarioResult:
    sim = _new_sim()
    event = _event(
        event_id="storm",
        summary="🔵 PW B90 Angela King",
        description=_draft_description(name="Angela Storm", price=288),
        clock=sim.clock,
    )
    key = FakeGoogleCalendar.key(event.calendar_id, event.id)
    sim.calendar.add_event(event)
    for _ in range(30):
        sim.calendar.webhook_queue.append((event.calendar_id, event.id))
    sim.run_until_idle()
    sim.run_until_idle()
    create_calls = [row for row in sim.xero.call_log if row["action"] == "create_invoice"]
    passed = len(create_calls) == 1 and sim.calendar.events[key].app_update_count <= 3
    return ScenarioResult(
        "webhook_storm_no_duplicate_invoice",
        passed,
        [
            f"create_calls={len(create_calls)}",
            f"app_updates={sim.calendar.events[key].app_update_count}",
        ],
        len(sim.xero.call_log),
        sim.calendar.write_count,
    )


def scenario_xero_webhook_echo_does_not_recheck_xero() -> ScenarioResult:
    sim = _new_sim()
    event = _event(
        event_id="xero-webhook-echo",
        summary="🟡 SW19 WC Mia",
        description=_draft_description(name="Mia Paid", price=78),
        clock=sim.clock,
        days_from_now=-1,
    )
    key = FakeGoogleCalendar.key(event.calendar_id, event.id)
    sim.calendar.add_event(event)
    sim.run_until_idle()
    invoice_id = get_invoice_for_event(sim.state, key) or ""
    sim.state = mark_invoice_sent(sim.state, key)
    sim.xero.mark_invoice_paid_external(invoice_id)
    sim.state = mark_invoice_paid(sim.state, key)
    sim.state = mark_recent_xero_webhook(
        sim.state,
        key,
        invoice_id,
        when_ts=sim.clock.now.timestamp(),
    )
    sim.calendar.app_patch(key, summary="🟢 SW19 WC Mia")
    updated = sim.calendar.events[key].updated.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    sim.state = set_processed_update_marker(sim.state, key, updated)
    before = len(sim.xero.call_log)
    sim.run_until_idle()
    new_calls = sim.xero.call_log[before:]
    passed = not [row for row in new_calls if row["action"] == "get_invoice"]
    return ScenarioResult(
        "xero_webhook_echo_does_not_recheck_xero",
        passed,
        [f"new_calls={new_calls}", f"recent={sim.state.get('recent_xero_webhook_events', {}).get(key)}"],
        len(sim.xero.call_log),
        sim.calendar.write_count,
    )


def scenario_paid_sync_failures_stop_until_resave() -> ScenarioResult:
    sim = _new_sim({"get_invoice": ["429", "429", "429", "429"]})
    event = _event(
        event_id="paid-sync-fails",
        summary="🟡 SW15 G.C Payment Pending",
        description=_draft_description(name="Payment Pending", price=120),
        clock=sim.clock,
        days_from_now=-1,
    )
    key = FakeGoogleCalendar.key(event.calendar_id, event.id)
    sim.calendar.add_event(event)
    sim.run_until_idle()
    invoice_id = get_invoice_for_event(sim.state, key) or ""
    sim.state = mark_invoice_sent(sim.state, key)
    final = sim.calendar.events[key]
    sim.state = set_processed_update_marker(
        sim.state,
        key,
        final.updated.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
    )

    for _ in range(6):
        sim.calendar.webhook_queue.append((final.calendar_id, final.id))
        sim.run_until_idle()
        sim.clock.advance(60)

    get_calls_before_resave = [
        row for row in sim.xero.call_log if row["action"] == "get_invoice"
    ]
    sim.calendar.user_edit(key, lambda ev: setattr(ev, "description", ev.description + "\nStaff checked"))
    sim.run_until_idle()
    final = sim.calendar.events[key]
    sim.state = set_processed_update_marker(
        sim.state,
        key,
        final.updated.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
    )
    sim.calendar.webhook_queue.append((final.calendar_id, final.id))
    sim.run_until_idle()
    get_calls_after_resave = [
        row for row in sim.xero.call_log if row["action"] == "get_invoice"
    ]
    passed = (
        len(get_calls_before_resave) == MAX_XERO_ATTEMPTS
        and len(get_calls_after_resave) == MAX_XERO_ATTEMPTS + 1
        and bool(invoice_id)
    )
    return ScenarioResult(
        "paid_sync_failures_stop_until_resave",
        passed,
        [
            f"before_resave={len(get_calls_before_resave)}",
            f"after_resave={len(get_calls_after_resave)}",
        ],
        len(sim.xero.call_log),
        sim.calendar.write_count,
    )


def scenario_fast_forward_current_diary_load() -> ScenarioResult:
    calendars = ["ben-calendar", "troy-calendar", "patrick-calendar"]
    sim = _new_sim(token_expired=True, refresh_plan=["ok", "ok", "ok"])

    # Seed old completed jobs that should never be re-opened by broad sweeps.
    for idx in range(18):
        calendar_id = calendars[idx % len(calendars)]
        event = _event(
            event_id=f"old-complete-{idx}",
            calendar_id=calendar_id,
            summary=f"🟢 GC Old Complete {idx}",
            description=_draft_description(name=f"Old Complete {idx}", price=90 + idx),
            clock=sim.clock,
            days_from_now=-(3 + (idx % 20)),
        )
        event.updated = sim.clock.now - dt.timedelta(days=2 + (idx % 12))
        key = FakeGoogleCalendar.key(calendar_id, event.id)
        sim.calendar.events[key] = event
        sim.state = set_invoice_for_event(sim.state, key, f"old-inv-{idx}")
        sim.state = mark_invoice_sent(sim.state, key)
        sim.state = mark_invoice_paid(sim.state, key)

    old_unfinished = _event(
        event_id="old-unfinished-lynn",
        calendar_id="ben-calendar",
        summary="🟠 GC Lynn Barber WS4",
        description=_draft_description(name="Lynn Old", price=125),
        clock=sim.clock,
        days_from_now=-10,
    )
    old_unfinished.updated = sim.clock.now - dt.timedelta(days=6)
    old_key = FakeGoogleCalendar.key(old_unfinished.calendar_id, old_unfinished.id)
    sim.calendar.events[old_key] = old_unfinished

    malformed = _event(
        event_id="current-missing-lines",
        calendar_id="troy-calendar",
        summary="🔵 GC E1 Michelle",
        description=ensure_notes_template(
            """[contact]
Customer name: Missing Current
Customer email address: missing.current@example.test
Customer contact number: 07000000000
[/contact]

[invoice]
[/invoice]

PROCESS DRAFT (Y/N) = Y
PAYMENT TYPE (CARD/INVOICE) = INVOICE
SEND NOW (Y/N) =
"""
        ),
        clock=sim.clock,
    )
    malformed_key = FakeGoogleCalendar.key(malformed.calendar_id, malformed.id)
    sim.calendar.add_event(malformed)

    live_keys: list[str] = []
    for idx in range(12):
        calendar_id = calendars[idx % len(calendars)]
        event = _event(
            event_id=f"current-job-{idx}",
            calendar_id=calendar_id,
            summary=f"🔵 {'PW' if idx % 2 else 'GC'} Current Job {idx}",
            description=_draft_description(name=f"Current Customer {idx}", price=100 + idx * 5),
            clock=sim.clock,
            days_from_now=idx % 2,
        )
        key = FakeGoogleCalendar.key(calendar_id, event.id)
        live_keys.append(key)
        sim.calendar.add_event(event)

    sim.run_hourly_sweep()
    old_calls_after_sweep = [
        row for row in sim.xero.call_log if row["event_key"] == old_key
    ]

    for minute in range(0, 240, 15):
        sim.clock.advance(15 * 60)
        if minute == 30:
            sim.calendar.user_edit(
                old_key,
                lambda ev: setattr(ev, "description", ev.description + "\nStaff re-save"),
            )
        if minute == 60:
            sim.calendar.user_edit(
                malformed_key,
                lambda ev: setattr(
                    ev,
                    "description",
                    ev.description.replace(
                        "[/invoice]",
                        "Gutter clean = £125+VAT\n[/invoice]",
                        1,
                    ),
                ),
            )
        for idx, key in enumerate(live_keys):
            if minute == 90 + (idx % 4) * 15:
                sim.calendar.user_edit(
                    key,
                    lambda ev: setattr(
                        ev,
                        "description",
                        ev.description.replace("SEND NOW (Y/N) =", "SEND NOW (Y/N) = Y"),
                    ),
                )
        for _ in range(3):
            for key in live_keys[:4]:
                event = sim.calendar.events[key]
                sim.calendar.webhook_queue.append((event.calendar_id, event.id))
        sim.run_until_idle(max_cycles=100)
        sim.run_hourly_sweep()

    create_by_event: dict[str, int] = {}
    for row in sim.xero.call_log:
        if row["action"] == "create_invoice":
            create_by_event[row["event_key"]] = create_by_event.get(row["event_key"], 0) + 1
    duplicate_creates = {k: v for k, v in create_by_event.items() if v > 1}
    red_titles = [
        event.summary
        for event in sim.calendar.events.values()
        if event.summary.startswith("🔴")
    ]
    old_calls_total = [row for row in sim.xero.call_log if row["event_key"] == old_key]
    missing_ledger = parse_app_ledger(sim.calendar.events[malformed_key].description)
    passed = (
        len(old_calls_after_sweep) == 0
        and len(old_calls_total) == 1
        and not duplicate_creates
        and not red_titles
        and missing_ledger.get("s") in {"draft", "sent"}
        and sim.xero.refresh_count == 1
    )
    return ScenarioResult(
        "fast_forward_current_diary_load",
        passed,
        [
            f"events={len(sim.calendar.events)}",
            f"xero_actions={len(sim.xero.call_log)}",
            f"old_calls_after_broad_sweep={len(old_calls_after_sweep)}",
            f"old_calls_after_staff_resave={len(old_calls_total)}",
            f"duplicate_creates={duplicate_creates}",
            f"red_titles={red_titles}",
            f"missing_ledger={missing_ledger}",
            f"refresh_count={sim.xero.refresh_count}",
        ],
        len(sim.xero.call_log),
        sim.calendar.write_count,
    )


def scenario_long_running_mixed_diary_stress() -> ScenarioResult:
    rng = random.Random(20260629)
    calendars = ["ben-calendar", "troy-calendar", "patrick-calendar", "sam-calendar"]
    sim = _new_sim(token_expired=True, refresh_plan=["ok"] * 20)
    live_keys: list[str] = []
    paid_keys: list[str] = []
    missing_keys: list[str] = []
    old_keys: list[str] = []

    for idx in range(70):
        calendar_id = calendars[idx % len(calendars)]
        mode = "CARD" if idx % 3 == 0 else "INVOICE"
        desc = _draft_description(name=f"Stress Customer {idx}", price=80 + (idx % 17) * 7)
        desc = desc.replace(
            "PAYMENT TYPE (CARD/INVOICE) = INVOICE",
            f"PAYMENT TYPE (CARD/INVOICE) = {mode}",
        )
        event = _event(
            event_id=f"stress-live-{idx}",
            calendar_id=calendar_id,
            summary=f"🔵 {'PW' if idx % 2 else 'GC'} Stress {idx}",
            description=desc,
            clock=sim.clock,
            days_from_now=rng.choice([-1, 0, 1]),
        )
        key = FakeGoogleCalendar.key(calendar_id, event.id)
        live_keys.append(key)
        sim.calendar.add_event(event)

    for idx in range(8):
        calendar_id = calendars[idx % len(calendars)]
        event = _event(
            event_id=f"stress-missing-{idx}",
            calendar_id=calendar_id,
            summary=f"🔵 GC Missing Stress {idx}",
            description=ensure_notes_template(
                f"""[contact]
Customer name: Missing Stress {idx}
Customer email address: missing.stress.{idx}@example.test
Customer contact number: 07000000000
[/contact]

[invoice]
[/invoice]

PROCESS DRAFT (Y/N) = Y
PAYMENT TYPE (CARD/INVOICE) = INVOICE
SEND NOW (Y/N) =
"""
            ),
            clock=sim.clock,
        )
        key = FakeGoogleCalendar.key(calendar_id, event.id)
        missing_keys.append(key)
        sim.calendar.add_event(event)

    for idx in range(18):
        calendar_id = calendars[idx % len(calendars)]
        event = _event(
            event_id=f"stress-old-{idx}",
            calendar_id=calendar_id,
            summary=f"🟢 GC Old Stress {idx}",
            description=_draft_description(name=f"Old Stress {idx}", price=90 + idx),
            clock=sim.clock,
            days_from_now=-(3 + idx),
        )
        event.updated = sim.clock.now - dt.timedelta(days=3 + idx)
        key = FakeGoogleCalendar.key(calendar_id, event.id)
        old_keys.append(key)
        sim.calendar.events[key] = event

    sim.run_until_idle(max_cycles=300)

    for key in live_keys[::5]:
        invoice_id = get_invoice_for_event(sim.state, key) or ""
        if invoice_id:
            sim.xero.mark_invoice_paid_external(invoice_id)
            paid_keys.append(key)

    for tick in range(96):
        sim.clock.advance(10 * 60)

        for key in rng.sample(live_keys, k=min(10, len(live_keys))):
            if rng.random() < 0.55:
                sim.calendar.user_edit(
                    key,
                    lambda ev: setattr(
                        ev,
                        "description",
                        ev.description.replace("SEND NOW (Y/N) =", "SEND NOW (Y/N) = Y"),
                    ),
                )
            else:
                sim.calendar.user_edit(key, lambda ev: setattr(ev, "description", ev.description + "\n"))

        if tick % 8 == 0:
            key = rng.choice(missing_keys)
            sim.calendar.user_edit(
                key,
                lambda ev: setattr(
                    ev,
                    "description",
                    ev.description.replace("[/invoice]", "Gutter clean = £99+VAT\n[/invoice]", 1),
                ),
            )

        if tick in {24, 48, 72}:
            key = old_keys[(tick // 24) - 1]
            sim.calendar.user_edit(key, lambda ev: setattr(ev, "description", ev.description + "\nStaff re-save"))

        for _ in range(12):
            key = rng.choice(live_keys + missing_keys)
            event = sim.calendar.events[key]
            sim.calendar.webhook_queue.append((event.calendar_id, event.id))

        sim.run_until_idle(max_cycles=500)
        if tick % 6 == 0:
            sim.run_hourly_sweep()

    create_by_event: dict[str, int] = {}
    forbidden_after_paid = [
        row
        for row in sim.xero.call_log
        if row["event_key"] in paid_keys
        and row["action"] in {"update_invoice", "authorize_invoice", "email_invoice", "record_payment"}
    ]
    for row in sim.xero.call_log:
        if row["action"] == "create_invoice":
            create_by_event[row["event_key"]] = create_by_event.get(row["event_key"], 0) + 1
    duplicate_creates = {k: v for k, v in create_by_event.items() if v > 1}
    red_titles = [
        event.summary
        for event in sim.calendar.events.values()
        if event.summary.startswith("🔴")
    ]
    old_calls_without_staff_touch = [
        row
        for row in sim.xero.call_log
        if row["event_key"] in set(old_keys[3:])
    ]
    passed = (
        not sim.xero.safety_violations
        and not forbidden_after_paid
        and not duplicate_creates
        and not red_titles
        and not old_calls_without_staff_touch
        and sim.calendar.write_count < 1200
    )
    return ScenarioResult(
        "long_running_mixed_diary_stress",
        passed,
        [
            f"events={len(sim.calendar.events)}",
            f"simulated_minutes={96 * 10}",
            f"xero_actions={len(sim.xero.call_log)}",
            f"calendar_writes={sim.calendar.write_count}",
            f"externally_paid={len(paid_keys)}",
            f"forbidden_after_paid={forbidden_after_paid[:5]}",
            f"safety_violations={sim.xero.safety_violations[:5]}",
            f"duplicate_creates={duplicate_creates}",
            f"red_titles={red_titles}",
            f"old_calls_without_staff_touch={len(old_calls_without_staff_touch)}",
        ],
        len(sim.xero.call_log),
        sim.calendar.write_count,
    )


def format_report(results: list[ScenarioResult]) -> str:
    payload = {
        "passed": all(result.passed for result in results),
        "scenario_count": len(results),
        "scenarios": [
            {
                "name": result.name,
                "passed": result.passed,
                "notes": result.notes,
                "xero_calls": result.xero_calls,
                "calendar_writes": result.calendar_writes,
            }
            for result in results
        ],
    }
    return json.dumps(payload, indent=2)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the offline Xero/Google safety simulator.")
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print JSON report only.",
    )
    args = parser.parse_args(argv)
    results = run_default_suite()
    report = format_report(results)
    if args.json:
        print(report)
    else:
        print("Offline Xero/Google safety simulation")
        print(report)
    return 0 if all(result.passed for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
