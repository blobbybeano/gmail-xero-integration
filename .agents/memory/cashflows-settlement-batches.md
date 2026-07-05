---
name: Cashflows settlement-batch grouping
description: How Cashflows merchant CSV remittance rows map to Xero's single CFE SETT bank deposit
---

# Cashflows remittance → Xero CFE SETT batch grouping

Cashflows merchant-account CSV writes ONE small "Transfer for Remittance" row per
matured sale (e.g. £148.70, £172.49). Xero instead shows ONE large daily
"CFE SETT SAFEGUARD CASHFLOWS FPI" bank deposit (e.g. £934.50) per settlement run.

**Rule:** Group consecutive remittance rows into one settlement batch; the run ends
when the merchant-account `Balance` column returns to ~0.00. The sum of a run's
remittance debits equals exactly the one CFE SETT amount Xero is waiting to
reconcile. Verified against the real sample CSV: runs sum to £862.77, £934.50,
£1,129.81 etc. with zero allocation variance.

**Why:** Treating each remittance row as its own payout produces dozens of tiny
payouts that don't exist in Xero — the user reconciles against the aggregated
daily deposit, not per-sale transfers. The zero-balance boundary is the settlement
signal because Cashflows remits all matured funds each overnight cycle, bringing
the available balance to zero.

**How to apply:** In `app/cashflows_csv.py parse_merchant_csv`, accumulate
REMIT_TYPE rows and emit a single `CsvPayout` when `abs(balance) < MONEY/2`, plus a
trailing flush for statements cut mid-run. Settlement run date = last (closing)
remittance row's date; the real bank deposit lands 1-2 days later, absorbed by the
bank-match tolerance window. Closure tolerance is `MONEY/2` (~£0.005); if a real
CSV ever leaves ±£0.01 residuals, runs would merge until the next exact zero or EOF
— revisit as a named threshold if that surfaces.
