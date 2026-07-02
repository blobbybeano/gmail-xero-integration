---
name: Receipt date parsing is UK day-first
description: Why receipt dates must be re-parsed day-first instead of trusting Document AI
---

# Receipt dates: read them day-first (UK), never trust the US OCR date blindly

Document AI runs in a **US region** (`document_ai_location` defaults to "us") and
treats ambiguous numeric dates as month-first, or misses small-print dates at the
foot of a receipt entirely. When extraction returned nothing, downstream code fell
back to **today's date**, which silently broke card-feed matching (a receipt for an
11 May purchase showed as "today" and never lined up with the real card payment).

**Rule:** resolve a receipt date day-first from the printed text, in this order:
1. Re-parse the date entity's printed text day-first (`_parse_uk_date`).
2. Document AI's normalised date (for long-form dates like "11 May 2026").
3. Re-parse the *whole* OCR text day-first (catches dates Document AI missed).

**Why:** the printed string is the ground truth; the US-region normaliser is the
unreliable layer. Day-first only swaps to month-first when the second number can't
be a month (>12). A future-date guard rejects parses ahead of today (usually a
mis-swap). Switching the region to "eu" would help data residency but does NOT make
date parsing reliable — the day-first re-parse is what fixes it.

**How to apply:** any change to receipt OCR field extraction must keep the day-first
re-parse ahead of the normalised value, and must never fall back to today's date as
a real purchase date without flagging it as uncertain.
