from __future__ import annotations

import copy
from dataclasses import dataclass

from app.event_processor import (
    compute_invoice_totals,
    ensure_notes_template,
    event_contains_keyword,
    extract_invoice_lines,
    parse_customer_fields,
    send_choice_is_yes,
    upsert_invoice_summary,
    validate_customer_fields,
)


@dataclass
class SimEvent:
    summary: str
    description: str


def simulate_event(event: SimEvent) -> dict:
    result: dict = {
        "summary": event.summary,
        "original_description": event.description,
    }

    # Prefill logic
    prefilled = ensure_notes_template(event.description)
    result["prefilled_description"] = prefilled

    has_done = event_contains_keyword({"description": prefilled}, "DONE")
    has_send = send_choice_is_yes(prefilled)
    result["done_detected"] = has_done
    result["send_detected"] = has_send

    customer = parse_customer_fields(prefilled)
    errors = validate_customer_fields(customer)
    result["customer"] = customer
    result["customer_errors"] = errors

    invoice_lines = extract_invoice_lines(prefilled)
    result["invoice_lines"] = invoice_lines

    if invoice_lines:
        subtotal, total = compute_invoice_totals(invoice_lines)
        result["subtotal_ex_vat"] = subtotal
        result["total_inc_vat"] = total
    else:
        result["subtotal_ex_vat"] = 0.0
        result["total_inc_vat"] = 0.0

    # Summary update (as app would do when DONE is present)
    if has_done and invoice_lines:
        updated = upsert_invoice_summary(
            prefilled,
            result["subtotal_ex_vat"],
            result["total_inc_vat"],
            sent=False,
            include_prompt=True,
        )
        result["final_description"] = updated
    else:
        result["final_description"] = prefilled

    return result


def main() -> None:
    # Fake entries that mimic real-world user behavior
    samples = [
        SimEvent(
            summary="Job 1",
            description=(
                "[contact]\n"
                "Customer name: ben ben\n"
                "Customer email address: ben@powwash.co.uk\n"
                "Customer contact number: 075565803453\n"
                "[/contact]\n\n"
                "[invoice]\n"
                "gutter cleaning = £25+VAT\n"
                "[/invoice]\n\n"
                "DONE\n"
            ),
        ),
        SimEvent(
            summary="Job 2",
            description=(
                "[contact]\n"
                "Customer name: hi hi\n"
                "Customer email address:hi@gmail.com\n"
                "Customer contact number:0348724078\n"
                "[/contact]\n\n"
                "[invoice]\n"
                "gutter cleaning : £34+VAT\n"
                "materials = £10+VAT\n"
                "[/invoice]\n\n"
                "DONE\n"
            ),
        ),
        SimEvent(
            summary="Job 3",
            description=(
                "[contact]\n"
                "Customer name: Missing Email\n"
                "Customer email address:\n"
                "Customer contact number: 07000000000\n"
                "[/contact]\n\n"
                "[invoice]\n"
                "driveway clean - 100\n"
                "[/invoice]\n\n"
                "DONE\n"
            ),
        ),
        SimEvent(
            summary="Job 4",
            description=(
                "[contact]\n"
                "Customer name: No Done\n"
                "Customer email address: no@done.com\n"
                "Customer contact number: 07000000001\n"
                "[/contact]\n\n"
                "[invoice]\n"
                "softwash = £55+VAT\n"
                "[/invoice]\n\n"
            ),
        ),
        SimEvent(
            summary="Job 5",
            description=(
                "[contact]\n"
                "Customer name: Send Test\n"
                "Customer email address: send@test.com\n"
                "Customer contact number: 07000000002\n"
                "[/contact]\n\n"
                "[invoice]\n"
                "wash = £12.50+VAT\n"
                "[/invoice]\n\n"
                "DONE\n"
                "SEND Y/N = Y\n"
            ),
        ),
    ]

    for idx, ev in enumerate(samples, start=1):
        result = simulate_event(ev)
        print("=" * 80)
        print(f"Sample {idx}: {result['summary']}")
        print("DONE:", result["done_detected"], "SEND:", result["send_detected"])
        print("Customer:", result["customer"], "Errors:", result["customer_errors"])
        print("Lines:", result["invoice_lines"])
        print("Totals ex/inc VAT:", result["subtotal_ex_vat"], result["total_inc_vat"])
        print("Final description:\n", result["final_description"])


if __name__ == "__main__":
    main()
