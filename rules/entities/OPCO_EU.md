<!-- EXAMPLE: replace with your group's own entity file, or delete it; preflight fails while this marker is here -->

# OPCO_EU: Example Operations B.V.

> Fictional example of a filled-in `_TEMPLATE.md`, for the example group in
> `config/group.example.toml`.

## Role

Sales, support and the product team in its region. Revenue is customer
revenue from its region only. Cost base is mostly payroll, travel and the
office.

## What it recognises

- Everything attributable to its staff, including staff it engages through a
  provider in other countries.
- Its share of group software and insurance, by recharge from HOLDCO.

## Accounts

The OPCO_EU column of `EXPENSES.md` § Account table. No exceptions.

## Tax registration and tax types

Registered for VAT in its own country. The test is where the transaction
physically took place, never the tracking option:

| Transaction physically in | Treatment |
|---|---|
| The entity's own country, local tax on the document | recoverable, domestic input type |
| Another country, that country's tax on the document | not recoverable, gross into the cost |
| Cross-border service, reverse charge | reverse-charge type, no tax to split |

## Tracking categories

Category `Team` with options `Product` and `Commercial`. Every cost line
carries one, chosen by the team of the person the cost is for; office and
shared costs go to `Commercial`. FX lines are exempt.

## Payroll

A local payroll bureau and a provider for staff abroad, both bill sources in
`PAYROLL.md`. The provider's refundable deposits sit in the entity's deposit
account.

## Bank data

Bank with no API: statements are saved in `data/statements/` as CSV
(`config/group.toml`). The bank feed is only as current as the last CSV.

## Standing arrangements and balance sheet

- Funded by HOLDCO through the intercompany loan.
- The office lease is carried here in journals (`GROUP.md` § Leases).
