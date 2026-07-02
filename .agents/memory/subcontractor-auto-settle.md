---
name: Reference-based auto-settlement
description: Rules for auto-recognising an inbound/outbound payment from a bank feed by a text reference and zeroing a balance.
---

When auto-recognising a payment in a Plaid/bank feed by an app-generated
reference (e.g. `PWSUB<id>`) and using it to settle an owed balance:

- **Match the reference as a whole token, not a substring.** `PWSUB1` is a
  substring of `PWSUB12`, so a naive `ref in name` settles the wrong account.
  Normalise to alphanumerics, then anchor the trailing id with a negative
  lookahead (`re.escape(ref) + r"(?![0-9])"`).
- **Never settle on amount alone.** An unrelated payment of the same value must
  not zero a balance — require the reference.
- **Make settlement idempotent per transaction.** Persist the consumed
  `transaction_id` (unique index) and skip already-used txs. Cached feeds are
  re-scanned on every page load, so without this any new owed receipt added
  later would be wrongly settled by the same old payment.
- **Reconcile by amount, not all-or-nothing.** One payment usually covers a
  COMBINATION of receipts. Use a 0/1 subset-sum (in integer pennies, small
  tolerance) to settle only the receipts the payment covers; leave the rest
  owed. Classify + flag over/under payments (overpaid = paid > owed; review =
  no clean subset match) on a settlement note so the admin is warned, rather
  than silently zeroing or over-settling.

**Why:** all three were caught in architect review of the subcontractor portal;
the substring + no-dedupe versions silently mis-settled balances.

**How to apply:** any feature that reconciles a bank-feed line to a ledger
entry via a reference string (subcontractor payments, invoice refs, etc.).
