# Staff Calendar & Invoicing Manual

This guide is for office/admin staff using Google Calendar entries.
Do not change app settings unless asked by management.

## 1) Standard entry format

Use this structure in the event notes:

```text
[notes]

[/notes]

[contact]
Customer name:
Customer email address:
Customer contact number:
[/contact]

[invoice]

⬇Sales⬇

[/invoice]
PROCESS DRAFT (Y/N) =
```

## 2) Add job lines

Inside `[invoice]`, add normal job lines above `⬇Sales⬇`:

```text
Gutter cleaning = £125+VAT
Materials = £25+VAT
```

Accepted forms:
- `Item = £125+VAT`
- `Item = £125`
- `Item £125+VAT`

Rows below `⬇Sales⬇` are field-agent upsells. They are added to the customer
invoice and also written to the sales tracking spreadsheet.

## 3) Optional cash marker (no VAT mode)

If the whole job should be treated as no VAT, add this once inside `[invoice]`:

```text
*cash*
```

## 4) Process the draft

When ready, set:

```text
PROCESS DRAFT (Y/N) =Y
```

The app then creates/updates the draft invoice and writes totals in `[app-status]`.

## 5) Choose payment type and send

In `[app-status]`, set payment type:

```text
PAYMENT TYPE (CARD/INVOICE) = CARD
```

or:

```text
PAYMENT TYPE (CARD/INVOICE) = INVOICE
```

Then send:

```text
SEND NOW (Y/N) =Y
```

## 6) Optional invoice-only business details

When invoice details are different from customer details, add optional lines inside `[contact]`:

```text
Invoice name: Jo&Co Ltd
Invoice address line 1: 12 High Street
Invoice address line 2: Unit 4
Invoice city: London
Invoice postcode: SW1A 1AA
Invoice country: UK
```

These override only invoice addressing.

After successful processing, this section may be compacted to:
`Alternate Invoice Address ✅`

## 7) Shortcut for known business profiles

If that business already exists in Xero, use:

```text
Invoice profile: Jo&Co Ltd
```

Behavior:
- If profile exists in Xero: app uses that existing contact profile.
- If profile does not exist: entry stays blue, processing is blocked, and the `Invoice profile:` line shows `❌ Customer does not exist`.

## 8) Title light meanings

- `🔵` Waiting to be processed.
- `🟠` Draft created.
- `🟠.` / `🟠..` / `🟠...` Draft edited again.
- `🟡` Authorised/sent, waiting for payment.
- `🟢` Paid/finalised.
- `🔴` Integration problem (Xero/Google/Sheets/Webhook issue).
- `✉️` Email send failed (shown next to light).

## 9) Common checks

If entry is not progressing:
- Confirm `PROCESS DRAFT (Y/N) =Y` is present.
- Ensure invoice lines are inside `[invoice]`.
- Confirm payment type is set before sending.
- For `Invoice profile`, confirm exact business name exists in Xero.
- If `🔴` appears, report to management (integration issue).

If you see this warning in `[notes]`:
`!!! PAYMENT TYPE EMPTY !!!!`
set `PAYMENT TYPE (CARD/INVOICE) = CARD` or `INVOICE`, then set `SEND NOW (Y/N) =Y`.

## 10) Backward compatibility

Older prompts still work:
- `Y/N =Y`
- `SEND Y/N =Y`

Use the new prompts going forward:
- `PROCESS DRAFT (Y/N) =`
- `SEND NOW (Y/N) =`
