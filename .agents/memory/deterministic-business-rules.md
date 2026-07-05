---
name: Deterministic business rules over AI prompt hints
description: Hard business rules (e.g. fuel <£40 → Machinery Fuel) must be enforced in code after AI coding, not via prompt hints.
---

**Rule:** Any hard, threshold-style business rule (e.g. "fuel under £40 goes to
Machinery Fuel, £40+ goes to Van Fuel") must be enforced deterministically in
code AFTER the AI classification step — never rely on the rule being stated in
the AI prompt hints.

**Why:** The £40 fuel rule was injected into both AI coding prompts via the
admin-configurable hints, yet the model still coded ALL fuel receipts
(including £130+) to Machinery Fuel. Prompt hints bias but do not guarantee.

**How to apply:** After every AI account-coding call, run a small post-pass
that checks the deterministic condition (amount, per-segment for splits) and
overrides the account. Locate target accounts by name keywords but skip
ambiguous names matching both categories. Same panel lesson: reconciliation
views must filter to ONE chosen bank account; if none is chosen, refuse and
prompt the user rather than mixing all accounts.

**Fuel rule (current form):** machinery fuel MUST be unleaded — if the OCR
text mentions "diesel" the receipt is ALWAYS Van Fuel regardless of amount;
otherwise unleaded/unknown fuel under £40 → Machinery Fuel, £40+ → Van Fuel.

**AI-output validation lesson:** when an AI helper claims structured facts
about a document (e.g. splitting one photo into multiple receipts with
amounts), demand textual evidence — every claimed amount must literally
appear in the OCR text, else reject the whole result. Don't validate by
summing against the document's single OCR total: for multi-receipt photos
that total is legitimately just one of the receipts.
