---
name: Card-feed bank-connection provider
description: Why the card feed is a manual CSV upload (not open banking or Xero), the full map of dead-end providers for a Lloyds business account, and the invariants any feed source must satisfy.
---

# Card feed source: manual CSV upload (all free automated routes are dead)

Dump receipts are matched to the company card's REAL payments. The source is a
**manual CSV upload** of the Lloyds internet-banking export — because as of
July 2026 every free automated route was checked and is closed:

| Route | Verdict |
|---|---|
| Xero standard API | Never returns *unreconciled* statement lines (contractual block, not a scope gap) |
| Xero Finance API (`BankStatementsPlus`) | Scope `finance.bankstatementsplus.read` is real, endpoint is real, BUT gated behind Xero partner certification. Including the scope makes Xero reject the ENTIRE auth URL with a generic error, even for private apps on your own org. Verified by live probe. No self-serve path. |
| Enable Banking | Free tier is genuine, JWT auth works, but UK **production** coverage is only Barclays, HSBC, NatWest/RBS, Coutts, ABN AMRO — **no Lloyds**. Also self-serve app activation has no UK option post-Brexit. Marketing coverage claims ≠ production coverage: always check the coverage page with the Production filter. |
| GoCardless Bank Account Data | Covers Lloyds, free — but **closed to new sign-ups since July 2025** |
| Plaid | Covers Lloyds — no free production tier |
| TrueLayer | Covers Lloyds — paid, sales-gated, AIS only as add-on to a Payments plan |

**Why:** the business banks with Lloyds; user explicitly rejected paid options.

**How to apply:**
- Any future feed source must plug in behind the stable facade surface
  (`is_connected`, `connection_status`, `get_cached_transactions`) and emit the
  one normalised tx shape `{transaction_id, account_id, date, amount, name,
  pending}` — the matcher/settlement/engineer-feed consumers never care about
  the source. Enable Banking client is kept dormant behind the facade.
- Normalised amounts: money-OUT positive, money-IN negative, so credits fail
  the (positive) receipt matcher and the engineer feed's `amt <= 0` skip works.
- The subcontractor auto-settle scans tx `name` for the `PWSUB<id>` reference,
  so `name` MUST carry the full description/remittance text, not just merchant.
- The engineer column `plaid_account_id` and settlement column `plaid_tx_id`
  are provider-agnostic ("linked account id" / "settled tx id") — names kept to
  avoid a migration. For CSV the account id is the bank account number.
- CSV rows have no bank tx id: ids are deterministic hashes of row content +
  in-file occurrence index, which makes overlapping re-uploads collapse onto
  the same ids (dedupe) while still allowing genuine identical rows.
- Receipt OCR dates are unreliable, so matching stays **price (penny-exact) →
  ±~1 month window → name similarity**, NOT date-first; ambiguous hits are
  surfaced for a human, never silently matched.
- CSV store retention is ~a year so the matcher always has history even with
  irregular uploads; prune on ingest and on read.
