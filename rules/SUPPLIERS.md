# Supplier database

One entry per real-world supplier, name variants merged. Each entry records
where that supplier's costs are recognised, the account they go to, the tax
treatment, the billing pattern, and anything special about its documents.

**Use it per the coding method in `EXPENSES.md`: the invoice always decides;
this database is one input.** A qualitatively different invoice from a known
supplier is coded on its own merits and the divergence is flagged.

How the agent uses this file:

- It **searches** it by supplier name and alias (`grep -i`) for every invoice
  and bank line; it never reads it whole.
- A match gives the default entity, account and tax treatment, which the agent
  then checks against the invoice and the supplier's previous bills in Xero.
- **No match means a new supplier.** The run that meets it codes the invoice
  on the general rules (querying where unsure) and adds an entry here with
  what it learnt, for an admin to review.
- An admin's ruling about a supplier is written into its entry as a rule
  (`rules/README.md`).

Maintenance rules:

- **This file is the master; edit entries here.** Refine an existing entry
  rather than adding a second one for a name variant: add the variant to
  `Aliases`.
- **Recognised in is history and default, not permission.** Never code an
  account that is not in the entity's account table in `EXPENSES.md`, and
  never let an entry put a cost in an entity the recognition matrix in
  `GROUP.md` forbids (an entity with no staff never carries staff costs, for
  example, whichever card paid).
- Not in this file: payroll providers' salary documents (`PAYROLL.md`), staff
  members appearing as reimbursement contacts (`EXPENSES.md` § Staff
  expenses), and group entities as counterparties (`INTERCOMPANY.md`).
- Entries for suppliers that bill per person (restaurants, hotels, taxis) say
  `per employing entity` under Recognised in: the employing-entity rule
  decides, not the supplier.

## Entry format

Every entry is a level-3 heading with the supplier name exactly as it appears
on its invoices, then the fixed field list below. Leave a field as `-` rather
than deleting it, so every entry reads the same way. Write rules, not history:
no dates of rulings, no invoice numbers except as the shape of a number.

```
### <Supplier name as on the invoice>

- **Aliases / bank payee names:** other names on invoices, the payee text on
  bank statements and card lines, a brand name, a parent company
- **Recognised in:** entity key(s), or `per employing entity`, or
  `allocated (ALLOCATION.md)`; plus the condition when it varies
- **Default account:** the cost category from EXPENSES.md (resolved to the
  entity's own account there), or a balance-sheet account
- **Tax treatment:** what tax the invoice carries and whether it is
  recoverable in the recognising entity, or `gross, no tax`
- **Billing frequency:** monthly / annual / per order / per usage, and the
  period an invoice covers relative to its date
- **Currency:** the invoice currency
- **Paid from:** which entity's bank or card normally pays, and how (card,
  transfer, direct debit)
- **Prepays:** `no`, or the period, entity and monthly release
- **Special handling:** anything that would trip a run: two documents per
  charge, an invoice-number shape, an order confirmation that is not an
  invoice, a dual-treatment supplier, a duplicate trap
```

## Entries

<!-- FILL IN: one entry per supplier, alphabetically, in the format above.
Seed with your twenty or thirty largest and most frequent suppliers, from
Xero's supplier contacts and the last few months of bills; the agent adds the
rest as it meets them. -->

> Illustration (fictional): two entries in the format above. Not live
> entries; the agent never matches a supplier against them.
>
> **Contoso Cloud**
>
> - **Aliases / bank payee names:** CONTOSO CLOUD, CONTOSOCLOUD.EXAMPLE
> - **Recognised in:** HOLDCO pays; allocated by headcount (`ALLOCATION.md`)
> - **Default account:** Software subscriptions
> - **Tax treatment:** domestic tax on HOLDCO's invoice, recoverable by HOLDCO
> - **Billing frequency:** monthly, in advance, invoice dated the first of the
>   month it covers
> - **Currency:** GBP
> - **Paid from:** HOLDCO card
> - **Prepays:** no; an annual plan, if ever chosen, prepays over twelve
>   months in HOLDCO
> - **Special handling:** a usage overage invoice can arrive mid-month with
>   the same amount as an earlier one; distinct invoice numbers mean two real
>   invoices, not a duplicate
>
> **Northwind Office Supplies**
>
> - **Aliases / bank payee names:** NORTHWIND OFFICE, Northwind Store
> - **Recognised in:** the entity whose staff member the item is for, read
>   from the delivery address or the order notes; otherwise the paying entity
> - **Default account:** IT or office equipment (capitalised) at or above the
>   threshold; small IT kit or office running costs below it (`EXPENSES.md`
>   rule 1)
> - **Tax treatment:** domestic tax recoverable only where the buying entity
>   is registered in the supplier's country; otherwise booked gross
> - **Billing frequency:** per order
> - **Currency:** CHF
> - **Paid from:** any entity's card
> - **Prepays:** no
> - **Special handling:** the order confirmation email is not an invoice;
>   book the invoice issued on dispatch. One order can ship and charge in
>   parts: match each charge to its own dispatch invoice
