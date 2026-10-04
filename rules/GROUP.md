# Group structure and cost recognition

The group in prose: what each entity is for, which entity recognises which
cost, who pays whom and how the entities are funded. Per-entity detail lives
in `rules/entities/<KEY>.md`; coding detail in `rules/EXPENSES.md`; shared-cost
splits in `rules/ALLOCATION.md`; the intercompany accounts each flow uses in
`rules/INTERCOMPANY.md`; review checks in `docs/REVIEW_METHOD.md`.

The agent reads this file on every bookkeeping and review run. The structure
(entity keys, Xero names, currencies, bank accounts, intercompany pairs) is in
`config/group.toml`: do not repeat it here, describe what it means.

> Sections marked `<!-- FILL IN -->` are yours to write. Blocks headed
> `Illustration (fictional)` show the shape with the fictional group from
> `config/group.example.toml` (HOLDCO, OPCO_US, OPCO_EU); they are never
> rules.

## What the group is

<!-- FILL IN: two or three sentences. What the group does, where revenue is
earned, which entity is the parent, and why the other entities exist. From
your group structure chart and whoever owns the group's legal structure. -->

> Illustration (fictional): Example Group sells a subscription product.
> HOLDCO owns the product and the other entities, employs the leadership team
> and holds the group's cash. OPCO_US and OPCO_EU each sell to and support
> customers in their own region; OPCO_EU also runs the product team.

## What each entity does

One row per entity in `config/group.toml`.

<!-- FILL IN: one row per entity, from your group structure and each
entity's statutory accounts. Role = what it is for. Revenue = what revenue it
may carry (anything else in its revenue lines is a finding). Staff = who it
employs, by role or location, never by name. -->

| Entity key | Role | Revenue it may carry | Staff it employs |
|---|---|---|---|
| <entity key> | | | |

> Illustration (fictional):
>
> | Entity key | Role | Revenue it may carry | Staff it employs |
> |---|---|---|---|
> | HOLDCO | Parent; owns the product, holds group cash, funds the subsidiaries | Management fees from the subsidiaries, interest | Leadership team |
> | OPCO_US | Sales and support in its region | Customer revenue from its region | Sales and support staff |
> | OPCO_EU | Sales, support and the product team | Customer revenue from its region | Sales, support and product staff |

## Which entity recognises which cost

This is the most important table in `rules/`. It answers "whose cost is
this?" before anyone asks "which account?". The agent decides the
recognising entity from this matrix first, then codes the cost on that
entity's own rules in `EXPENSES.md`.

<!-- FILL IN: one row per cost category, one column per entity in
config/group.toml. Take the categories from your chart of accounts and keep
them the same as the account table in EXPENSES.md so the two files line up
row for row. In each cell write one of: `recognises` (this entity carries the
cost), `allocated` (split per ALLOCATION.md; name the key in the Rule
column), `pays` (pays and recharges, carries none of it), `never` (this
entity must not carry it), or a short condition such as `own staff`. -->

| Cost category | <entity key> | <entity key> | Rule |
|---|---|---|---|
| <cost category> | | | |

> Illustration (fictional):
>
> | Cost category | HOLDCO | OPCO_US | OPCO_EU | Rule |
> |---|---|---|---|---|
> | Staff travel, meals, welfare | own staff | own staff | own staff | Follows the spender's employing entity (principle 2) |
> | Group software (used by everyone) | pays | allocated | allocated | Paid by HOLDCO, split by headcount (`ALLOCATION.md`) |
> | Product cloud hosting | recognises | never | never | Recognised by HOLDCO as owner of the product |
> | Legal and professional fees | default | if the advice is for it | if the advice is for it | The entity the advice relates to, per the invoice |

A cost that fits no row is a query, not a guess. Once an admin rules, add the
row.

### Costs paid by one entity for another

The entity whose bank or card paid is not always the entity that recognises
the cost. The rule:

1. **Decide the recognising entity on the matrix above**, never from the card
   that happened to be used. Who paid is a fact read from the bank feed or
   the bank API, never assumed from the receipt.
2. **The recogniser carries the bill**, coded and taxed on its own rules.
3. **The payer's bank line goes to the intercompany account** with the
   recogniser, never to an expense. Which intercompany account:
   `rules/INTERCOMPANY.md`.
4. **The recogniser's bill is settled against the mirror intercompany
   account**, not a bank account. That settlement is a payment, so it is the
   user's step in the Xero UI; the agent leaves the bill AUTHORISED and reports
   it under bill payments.

Both legs, always. Recognising the cost in the right entity is only half the
entry; without the funding leg the payer's bank line can never be matched and
the two entities' intercompany accounts disagree.

```
no    OPCO_US card buys a laptop for an OPCO_EU staff member; the bill sits in
      OPCO_EU and the OPCO_US bank line is left to age on the feed
yes   bill in OPCO_EU; OPCO_US bank line posted as spend money to the
      OPCO_US / OPCO_EU intercompany account; the user settles the bill from
      the mirror account
```

<!-- FILL IN: any standing cross-paid arrangement (an entity that always pays
a supplier on another's behalf), with the rule that decides it. Delete if
none. -->

## Who pays whom, and how the entities are funded

<!-- FILL IN: how cash moves inside the group. Which entity funds which, by
what route (loan, capital, recharge), how often, and which intercompany pair
in config/group.toml carries each flow. From whoever manages the group's
cash. The detailed routing is INTERCOMPANY.md; say here only the shape. -->

> Illustration (fictional): HOLDCO funds OPCO_US and OPCO_EU by intercompany
> loan, topped up monthly against the next month's payroll forecast. The
> subsidiaries do not fund each other; a cost one pays for the other runs
> through their own pair (`INTERCOMPANY.md`). Shared costs paid by HOLDCO are
> recharged monthly through the `recharge` pair, per `ALLOCATION.md`.

## Cost recognition principles

These are generic and suit most groups. Edit them where yours differ.

1. **Benefit principle.** A cost is recognised in the entity that receives
   the benefit of it, except where `ALLOCATION.md` splits it on a key.
2. **Staff expenses follow the employing entity.** Travel, meals, welfare and
   reimbursements for a specific person are recognised by the entity that
   employs them, whoever paid. Generic spend not attributable to a person
   defaults to the paying entity. Detail: `EXPENSES.md` § Staff expenses.
3. **Paying entity is not always the recognising entity.** A payment leaving
   one entity's bank with the cost recognised in another is not automatically
   an error. Check the established pattern for that supplier before flagging,
   and conversely check that known cross-paid costs really are recognised
   where they belong.
4. **Supplier consistency.** A supplier generally goes to the same account in
   the same entity every time (`SUPPLIERS.md`). Deviations need a reason. Some
   accounts legitimately have many different suppliers (meals, travel).
5. **Period consistency** is the primary quality test: compare with the prior
   two or three months of the same supplier, account or pattern, further back
   when needed.
6. **Accuracy period.**
   <!-- FILL IN: the period your costs must be accurate to, `month` or
   `quarter`, as your finance lead or auditor expects. With `quarter`, a cost
   covering Jul-Sep booked entirely in one of those months is fine, but one
   spanning a quarter boundary must be split, accrued or prepaid so each
   quarter carries its share. -->

## Transaction-type rule: invoices become bills

**Default: every supplier invoice is bookkept as a bill (ACCPAY)** with the
source document attached. The bank feed imports into Xero by itself; the user
then reconciles the statement line to the bill in the Xero UI.

**The agent never pays a bill.** It creates the bill AUTHORISED, document
attached, `AmountDue` equal to the total. An agent-created payment is a
phantom bank entry that competes with the real feed line and double counts the
cost. It is never correct, not even when the receipt says paid or the matching
bank line is visible. Enforced by the `xero` skill and a guard hook.

Exceptions, NOT bookkept as bills:

1. **Payroll**, per source, as `PAYROLL.md` says. Some sources are bank lines
   coded to control accounts and cleared by a journal; others are bills.
2. **Leases**, per the lease rule below.
3. **Naturally invoice-less payments**: bank fees, interest, card scheme
   charges and card spend with no invoice (a taxi, a coffee). Spend money
   directly on the bank feed.
4. **Cash movements**: transfers between an entity's own accounts are bank
   transfers; intercompany cash goes through the intercompany accounts per
   `INTERCOMPANY.md`.

A supplier invoice sitting as spend money outside these exceptions is a
finding; so is a bill created for something naturally invoice-less.

### Leases

<!-- FILL IN: which entity is the lessee for each lease, whether the payer
and the lessee differ, and where the lease schedules are kept. From the lease
agreements. Delete this section if the group has no leases. -->

Generic shape when the payer and the lessee differ:

- **The paying entity books a bill** for the lessor's invoice, every line
  coded to the intercompany account with the lessee, nothing to its own P&L.
  There is an invoice, so it is a bill, not spend money.
- **The lessee carries the lease in journals**: one recognising the
  right-of-use asset against the lease liability, then one per payment
  debiting the liability, the period's interest and any fee, and crediting
  the same intercompany account.
- The two sides meet on the intercompany account and neither double counts.
  A lease invoice in the payer's own expense or asset accounts, or a lease
  payment with no journal in the lessee, is a finding. A translation
  difference on the intercompany account between a foreign-currency bill and
  base-currency journals is expected and cleared by the periodic intercompany
  FX sweep.

## Upstream tools

<!-- FILL IN: what else posts into Xero (a receipt-capture tool, an expenses
app, a payroll connector) and what it posts (bills, spend money, journals).
From Xero's connected apps list.
Review effort concentrates on whether those items landed in the right account
and on the manual items the tools do not cover, mostly payroll. -->

## Payroll

Everything about payroll, including the group-wide principles, is in
`PAYROLL.md`; it is the only copy.
