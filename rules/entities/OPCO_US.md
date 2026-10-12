<!-- EXAMPLE: replace with your group's own entity file, or delete it; preflight fails while this marker is here -->

# OPCO_US: Example Operations Inc.

> Fictional example of a filled-in `_TEMPLATE.md`, for the example group in
> `config/group.example.toml`.

## Role

Sales and support in its region. Revenue is customer revenue from its region
only. Cost base is mostly payroll, travel and office costs.

## What it recognises

- Everything attributable to its staff, whichever entity's card paid
  (`GROUP.md` principle 2).
- Its share of group software and insurance, by recharge from HOLDCO
  (`ALLOCATION.md`); never a second supplier bill.

## Accounts

The OPCO_US column of `EXPENSES.md` § Account table. No exceptions.

## Tax registration and tax types

Not registered for any recoverable purchase tax: every tax rate in this
organisation is 0%. Sales tax is part of the cost, so a bill line carries the
gross the card was charged.

```
no    receipt 50.00 plus 4.00 sales tax, line entered at 50.00
yes   the same receipt, line entered at 54.00, no-tax type
```

## Tracking categories

None.

## Payroll

Source 2 in `PAYROLL.md` (Litware Payroll, one bill per pay run). Health
care is inside the payroll invoices; a separately billed health care invoice
is a query, not a bill.

## Bank data

The bank API reports failed and cancelled rows that are not cash. Foreign
card charges are converted on the statement and the bank adds its foreign
transaction fee as a separate line: spend money to bank fees, matched to the
purchase by timestamp.

## Standing arrangements and balance sheet

- Funded by HOLDCO through the intercompany loan.
