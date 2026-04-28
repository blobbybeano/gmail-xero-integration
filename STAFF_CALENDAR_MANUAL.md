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

Y/N =
```

## 2) Add job lines

Inside `[invoice]`, add one line per item:

```text
Gutter cleaning = £125+VAT
Materials = £25+VAT
```

Accepted forms:
- `Item = £125+VAT`
- `Item = £125`
- `Item £125+VAT`

## 3) Optional cash marker (no VAT mode)

If the whole job should be treated as no VAT, add this once inside `[invoice]`:

```text
*cash*
```

## 4) Finalise the entry

When ready, set:

```text
Y/N =Y
```

The app then creates/updates draft invoice details automatically.

## 5) Optional invoice-only business details

When invoice details are different from the customer details, add optional lines inside `[contact]`:

```text
Invoice name: Jo&Co Ltd
Invoice address line 1: 12 High Street
Invoice address line 2: Unit 4
Invoice city: London
Invoice postcode: SW1A 1AA
Invoice country: UK
```

These override only invoice addressing.

## 6) Shortcut for known business profiles

If that business already exists in Xero, use:

```text
Invoice profile: Jo&Co Ltd
```

Behavior:
- If profile exists in Xero: app uses that existing contact profile.
- If profile does not exist: entry stays blue and processing is blocked until corrected.

## 7) Title light meanings

- `🔵` New/formatted entry.
- `🟠` Entry edited / in-progress.
- `🟡` Invoice submitted, waiting for payment.
- `🟢` Paid/finalised.
- `🔴` Integration problem (Xero/Google/Sheets/Webhook issue).
- `✉️` Email send failed (shown next to light).

## 8) Common checks

If entry is not progressing:
- Confirm `Y/N =Y` is present.
- Ensure invoice lines are inside `[invoice]`.
- For `Invoice profile`, confirm exact business name exists in Xero.
- If `🔴` appears, report to management (integration issue).
