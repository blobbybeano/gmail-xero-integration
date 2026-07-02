---
name: Xero bank-feed reconciliation for receipts
description: How "would this receipt reconcile against the card feed?" is determined via the Xero API
---

# Matching receipts to a Xero bank/card feed

**The Xero public API does NOT expose raw unreconciled bank-statement (feed) lines.**
The `BankTransactions` endpoint returns *entered* transactions (SPEND/RECEIVE), each
with an `IsReconciled` flag and `HasAttachments`. So "what would reconcile against the
card feed" is approximated by matching receipts against `Type=="SPEND"` BankTransactions.

**Why:** there is no reliable API for the unreconciled feed, and the existing
`_dump_card_feed_check` already treats entered BankTransactions as the "card feed".
Stay consistent with that rather than inventing a statement-lines source.

**Consequence to surface to the user:** if their card spend is still sitting as
*unreconciled* feed lines in Xero, it is invisible to this API, so every receipt reads
"no card match" / gets routed to "needs an account". That is the most likely cause of a
batch where *everything* fails to match — not a tolerance bug. Both the recon preview and
`_dump_card_feed_check` log diagnostics (`[recon]` / `[card-feed-check]`) showing how many
tx / SPEND tx the window returned and which account names were seen — check those before
assuming the matching logic is broken.

**How to apply:**
- A bank feed lives on the *card/bank account the money was paid from* (Xero account
  `Type=="BANK"`), NOT on the expense category. Group/filter by `BankAccount.Name`, not
  code (BANK accounts have no `Code`). Normalise names (strip punctuation/whitespace) and
  match leniently (substring either way) before deciding a tx is on a different card —
  exact-equality filtering silently wipes out every match on tiny naming differences.
- **Do NOT match on date alone — receipt OCR dates are the unreliable field.** They get
  day/month swapped (UK 05/11 read as 11/05, pushing the date ~6 months out) and there are
  settlement delays. A strict date window (±10d) was the real cause of "no card match" on
  receipts that genuinely matched. Treat the **merchant name** as the strong signal instead.
  Current `_dump_bank_feed_recon` qualifies a candidate (amount within ±£1) if ANY of:
  (a) date within 10d, (b) the day/month-**swapped** receipt date within 10d (handles the
  OCR swap), (c) merchant matches AND amount exact (±£0.05) AND date within 90d (settlement
  delay — the 90d cap stops recurring same-amount merchants like fuel/supermarkets matching
  months apart by fluke), or (d) merchant matches AND amount within ±£1 AND date within 45d.
  Rank candidates: merchant-match first, then closest amount, then closest date; consume each
  Xero tx at most once (greedy). Merchant compare via `_norm_merch`/`_merch_match` (lowercase,
  strip punctuation + stopwords like ltd/limited, token-overlap). Fetch a wide window so
  swapped/settlement-delayed tx are present. Keep `_dump_card_feed_check` tolerances in step
  with the preview or receipts that *do* match still get flagged "missing".
- **Two-pass matcher (confident + suggested).** Pass 1 = the qualify rules above; a confident
  match CLAIMS its tx (`used=True`) and renders an emerald "Matched to card" pill. Pass 2 (for
  receipts left unmatched) surfaces SUGGESTIONS: a still-unclaimed tx whose amount is EXACT
  (±£0.05) AND whose name `_merch_match`es, with ANY date — this is the "match even when the
  date is wrong" path. Suggestions must NEVER claim the tx and must NEVER auto-confirm:
  `used` stays False so the tx STILL appears in the bottom outstanding list, and the same tx
  may be suggested on multiple competing receipts (deliberately NO per-tx suppression flag —
  that made results order-dependent and dropped legit later matches to "no_match"). Exactly one
  candidate → amber "Possible match — confirm" (kind `suggested`). 2+ candidates → orange
  "Possible match — needs a check" (kind `suggested` + `ambiguous`/`n_sugg`); don't guess.
  **Why:** OCR can put a date months off (day/month swap ~6mo, year misread ~12mo) so the true
  card payment sits far outside the 90/45d caps — exact-amount+name is specific enough to
  suggest safely. **The fetch window MUST be wide (±365d) for this** — the old ±12d window
  never even loaded the far-off tx, so the suggestion path was silently dead; `get_bank_transactions`
  paginates (100/page) so a wide range is safe. `no_match` pill was recoloured slate (was amber)
  to free amber for suggestions. `_dump_bank_feed_recon` is a nested closure (not importable);
  verify branch behaviour with a standalone synthetic harness replicating the two passes.
- **Price-only suggestion tier (`price_only`).** OCR routinely mangles the shop name
  (an Esso fuel receipt parsed as "PUMP"), so the name-based suggestion in Pass 2 never
  fires even though the exact amount is sitting in the feed. After the name+amount
  suggestion path, fall back to **exact-amount-only** unclaimed tx as a weaker
  `price_only` hint (one candidate → "Possible match by price — check"; 2+ → ambiguous
  "needs a check" with `n_sugg`). Like `suggested`, it never claims the tx and never
  auto-confirms — it's purely a "there's a similar receipt for £X in the feed, may this
  be it?" prompt for the admin. **Why:** the user explicitly wanted price cross-referencing
  for receipts whose other fields (name/date) are unreliable.
- `BankTransactions` `Date` comes as `/Date(ms+0000)/` — parse the epoch ms.
- Gate everything on `xero_is_disabled()`; the preview is read-only and degrades to a
  "paused" message when Xero is off.
- Recon status is shown as a coloured **pill on each receipt's item card** in the grouped
  sections (matched / no-card-match / already-in-Xero / duplicate-upload) — NOT in a
  separate standalone panel. Compute the recon ONCE in `expense_dump_results` and pass a
  `{item_id: row}` map into `_dump_item_card`. When live recon says matched, suppress any
  stale contradictory "not found in card feed" `dup_reason`. The only standalone list is a
  single collapsed `<details>` ("still unreconciled on this card") at the BOTTOM of the
  results page (`_dump_outstanding_panel`). Do NOT re-add big standalone Xero-transaction
  lists at the top — the user found them confusing and they bury their actual receipts.
- "Already in Xero" vs "Duplicate upload": both are `STATUS_DUPLICATE`, but an in-batch
  re-upload (`dup_reason` contains "another uploaded receipt") is NOT in Xero. Only treat
  out-of-batch / prior-submission duplicates as "already in Xero".
- Live "reconcile + attach receipt image in Xero" is intentionally deferred until the
  user is live (would be a POST attachment + reconcile write).
