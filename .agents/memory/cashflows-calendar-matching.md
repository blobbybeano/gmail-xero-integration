---
name: Cashflows CSV ↔ calendar payment matching
description: How card payments are tied to calendar jobs, and why time is weak
---

The Cashflows CSV settlement TIME often lags the actual job by hours (card
batched/settled later), so time proximity is only a weak secondary signal.
Amount match must dominate the score; name overlap confirms the customer.

**Why:** real case — two same-amount jobs on the same day (one in the morning,
one in the afternoon). A payment settling in the afternoon is genuinely
ambiguous; time alone would wrongly "confirm" the afternoon job. The honest UI
annotates EACH tied invoice with its own calendar entry rather than declaring
one winner.

**How to apply:** for ambiguous/tied rows, match every tied option to its best
calendar event by name+amount and show a "Calendar entry - {date, time}" line
per option. Reorder the closest match to the top but still surface the others.
Build ONE CalendarPool per CSV preview (one fetch + one AI batch), not per sale.
