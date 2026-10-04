<!-- EXAMPLE: replace with your group's own entity file, or delete it; preflight fails while this marker is here -->

# HOLDCO: Example Holdings Limited

> Fictional example of a filled-in `_TEMPLATE.md`, for the example group in
> `config/group.example.toml`.

## Role

Parent company. Owns the product and the subsidiaries, employs the leadership
team, holds the group's cash and funds the subsidiaries. Revenue is
management fees from the subsidiaries and interest; customer revenue here is
a finding.

## What it recognises

- Its own staff costs and its own office.
- Product cloud hosting, as owner of the product.
- Group software and the group insurance policy are paid here and allocated
  out (`ALLOCATION.md`); only HOLDCO's own share stays in its P&L.

## Accounts

The HOLDCO column of `EXPENSES.md` § Account table. No exceptions.

## Tax registration and tax types

Registered for VAT in its own country. Domestic VAT on the invoice is
recoverable; foreign tax is never recoverable here and stays in the cost.

```
no    a foreign hotel's tax entered as an input-tax line in HOLDCO
yes   the hotel booked gross to travel, no tax line
```

## Tracking categories

None.

## Payroll

Source 1 in `PAYROLL.md` (in-house payroll, controls + journal). Health care
is billed separately and coded to HOLDCO's own health care account.

## Bank data

Read from the bank API; a saved statement CSV covers the period before the
API cut-over (`config/group.toml`).

## Standing arrangements and balance sheet

- Intercompany loans to OPCO_US and OPCO_EU, topped up monthly.
- Recharge accounts with each subsidiary for allocated costs.
