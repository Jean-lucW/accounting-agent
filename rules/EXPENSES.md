# Expense coding: accounts, treatment and tax

How a cost is coded once `GROUP.md` has decided which entity recognises it:
which account, whether it is capitalised, prepaid or accrued, how VAT, GST or
sales tax is treated, and how foreign currency, receipts and refunds are
handled. Each entity's tax registration, tax types and tracking categories
live in its own file,
`rules/entities/<KEY>.md`; this file holds the generic method.

The supplier database is `SUPPLIERS.md`; shared-cost splits are
`ALLOCATION.md`; payroll is `PAYROLL.md`; intercompany routing is
`INTERCOMPANY.md`. Generic posting mechanics (the Xero API, attachments, the
asset register) are in the `xero` and `xero-bills` skills.

> Sections marked `<!-- FILL IN -->` are yours to write. The numbered rules
> under "Coding method" and "Treatment rules" are generic method that suits
> most groups: keep them, edit the thresholds and accounts they name, and
> delete any that do not apply. Blocks headed `Illustration (fictional)` use
> the fictional group in `config/group.example.toml`; they are never rules.

## Coding method: the order every cost is decided in

**The supplier database is guidance, never a substitute for judgement.** For
every transaction, in order:

1. **Read and understand the invoice**: what was actually bought, which entity
   is billed, what period it covers.
2. **Compare with previous invoices from the same supplier**: is this
   qualitatively the same thing, bookkept the same way?
3. **Judge the entity and account from all the rules in `rules/`**: the
   recognition matrix in `GROUP.md`, the account table below, capitalisation,
   prepayments, tax, the supplier's entry in `SUPPLIERS.md`.

The supplier entry is one input into step 3: strong guidance for the usual
case, but the invoice decides. If the invoice contradicts the entry, follow
the invoice and flag the divergence.

```
no    a supplier normally billing a monthly hosting fee sends an invoice for
      a laptop, and it is coded to Hosting because the entry says so
yes   the laptop is coded on the capitalisation rule, and the run flags that
      the supplier's invoice differs from its usual pattern
```

General rules that sit on top of the method:

1. **Only whitelisted accounts.** Every account used must be in the entity's
   own account table below. Never code to an account outside it without an
   admin's explicit permission.
2. **An admin's instruction overrides the database** for the item it names.
   Treat it as a special case and ask whether the rule itself should change;
   never silently rewrite the rule.
3. **Default entity is the paying entity, then verify.** The payer is read
   from the bank feed or bank API, never from the receipt or the card name.
   Then check the invoice's billed entity, the service description, the
   recognition matrix and the supplier entry, which may name the entity.
4. **Legal and professional fees go to the entity the advice is for**, as the
   invoice says, even where the supplier entry names a default.
5. **Anything ambiguous is queried, never guessed.**

## Account table: cost category to account, per entity

**Account codes differ between entities**, so never carry a code from one
entity to another. Messages to people name the account, not the code.

When someone says "put this to account Y", Y must resolve to an account in
the booking entity's column: match the intent to that entity's own account
name.

<!-- FILL IN: one row per cost category, matching the rows of the recognition
matrix in GROUP.md, one column per entity. In each cell, `<code> <name>` from
that entity's own chart of accounts in Xero, or `n/a` where the entity never
carries the category. Refresh the table when charts change; the agent reads
the codes from here. -->

| Cost category | <entity key> | <entity key> |
|---|---|---|
| <cost category> | <code> <name> | <code> <name> |

> Illustration (fictional):
>
> | Cost category | HOLDCO | OPCO_US | OPCO_EU |
> |---|---|---|---|
> | Staff travel | 5100 Travel | 6100 Travel | 6100 Travel |
> | Staff meals and welfare | 5110 Staff Welfare | 6110 Meals and Entertainment | 6110 Staff Welfare |
> | Office rent | 5200 Rent | 6200 Rent | 6200 Rent |
> | Office running costs (small items) | 5210 Office Expenses | 6210 Office Supplies | 6210 Office Expenses |
> | Software subscriptions | 5300 Subscriptions | 6300 Software | 6300 Subscriptions |
> | Small IT kit (below the threshold) | 5310 IT Costs | 6310 Computer Expenses | 6310 IT Costs |
> | Product cloud hosting | 5320 Hosting | n/a | n/a |
> | Legal and professional fees | 5400 Legal and Professional | 6400 Professional Fees | 6400 Legal and Professional |
> | Audit and accountancy | 5410 Accountancy | 6410 Accounting | 6410 Accountancy |
> | Insurance | 5500 Insurance | 6500 Insurance | 6500 Insurance |
> | Bank fees | 5600 Bank Fees | 6600 Bank Charges | 6600 Bank Fees |
> | FX gain / loss (posted by hand) | 4900 FX Gain/(Loss) | 7900 FX Gain/(Loss) | 7900 FX Gain/(Loss) |

Balance-sheet accounts named by the treatment rules (equipment, prepayments,
deposits, accruals, payroll controls, intercompany) are permitted as well.

<!-- FILL IN: the balance-sheet accounts the rules below use, per entity,
from each entity's chart of accounts. -->

| Balance-sheet account | <entity key> | <entity key> |
|---|---|---|
| <account purpose> | <code> <name> | <code> <name> |

> Illustration (fictional):
>
> | Balance-sheet account | HOLDCO | OPCO_US | OPCO_EU |
> |---|---|---|---|
> | IT equipment (asset register) | 1500 Computer Equipment | 1500 Computer Equipment | 1500 Computer Equipment |
> | Office equipment (asset register) | 1510 Office Equipment | 1510 Furniture and Fixtures | 1510 Office Equipment |
> | Prepayments | 1300 Prepayments | 1300 Prepaid Expenses | 1300 Prepayments |
> | Accruals | 2200 Accruals | 2200 Accrued Liabilities | 2200 Accruals |
> | Deposits | 1400 Deposits | 1400 Deposits | 1400 Deposits |

Xero's system foreign-currency accounts (bank revaluations, unrealised and
realised currency gains) are posted by Xero and never coded by hand.

## Treatment rules

### 1. Capitalisation

**Threshold:**
<!-- FILL IN: the per-item amount at or above which equipment is capitalised,
with its currency, e.g. "EUR 1,000 per item, group-wide". From your
accounting policies or your auditor. -->

- At or above the threshold, IT and office equipment goes to the entity's
  equipment account on the balance sheet and onto the fixed asset register.
  Below it, to the P&L account (small IT kit for computer equipment and
  peripherals, office running costs for furniture and machines).
- An invoice in another currency is converted at the Xero rate on the invoice
  date; an approximate figure is fine, the test is not to the cent.
- The threshold is per item: a bill line with a quantity of several units is
  tested per unit, and coding to an asset-type account makes Xero raise one
  draft asset per unit of quantity.
- After posting a capitalising bill, register the auto-created draft assets
  (mechanics in the `xero` skill). Depreciation start date is the purchase
  date. Draft assets created by a small-item miscoding can only be deleted in
  the UI: flag them.
- **A correcting journal out of a fixed-asset account does not reach the
  register.** Whenever anything is journalled out of a capitalised line (a tax
  amount, a discount, a misposting), reduce the asset's purchase price too, in
  the same run, or the register and the ledger diverge silently. Depreciation
  already posted to the ledger is not recomputed: that gap is closed only by
  rolling depreciation back in the UI, and the run that created it reports it.
- Tax recoverable as input tax is never part of an asset's cost: capitalise
  the net amount.

<!-- FILL IN: depreciation method and life per asset type, per entity where
they differ. From your accounting policies or the fixed asset register
settings in Xero. -->

| Asset type | Entity | Method | Life |
|---|---|---|---|
| <asset type> | <entity key or all> | | |

> Illustration (fictional):
>
> | Asset type | Entity | Method | Life |
> |---|---|---|---|
> | Computer equipment | all | Straight line | 3 years |
> | Office furniture | all | Straight line | 5 years |

### 2. Prepayments

An invoice covering a period longer than
<!-- FILL IN: "a month" or "a quarter", matching the accuracy period in
GROUP.md --> is set up as a **prepayment**: code the bill to the entity's
Prepayments account with the coverage in the line description, then release it
evenly by monthly journal to the proper expense account over the covered
period. The accuracy target is the accuracy period in `GROUP.md` principle 6:
a release that lands the right total in each period is correct even if months
within it are not split; a cost in the wrong period is an error even if it is
only one month out.

- **The release is worked out on the period the deferred amount covers, never
  as a fraction of the balance in the account.** Where part of an invoice was
  already expensed, only the rest is deferred, over the shorter period that is
  left. Take the release count from the stated coverage, and check what has
  already been expensed before releasing a month at all.
- **A one-off charge bundled into a period invoice is recognised when it is
  incurred, not spread.** A setup, activation or joining fee buys nothing over
  time; only the period charge on the same invoice is deferred.

  ```
  no    an annual fee plus a one-off setup fee deferred in full and released
        over twelve months
  yes   the setup fee expensed in the month of the invoice, the annual fee
        released over the year
  ```

- **Post in the entity's base currency, and never carry a foreign invoice
  figure into a schedule kept in another currency.** A foreign-currency
  premium posted in a base-currency ledger releases a fraction of the base
  amount the ledger holds, not of the invoice figure.

  ```
  no    a EUR 3,000.00 premium posted at USD 3,240.00 and released at
        1,000.00 a month
  yes   released at 1,080.00 a month, one third of what the ledger holds
  ```

- **Twelve months of cover is twelve calendar months from the month it
  starts**, and **a part month is pro-rated by days**. A 10 Jun to 9 Jun
  policy releases Jun to May, not across the thirteen months it touches.
- **A recurring release journal expires with the subscription.** It keeps
  posting after the term ends until somebody stops it; the month after the
  last release due is where that shows.
- **One item posted twice is one item.** A prepayment raised in two lines, or
  topped up later by a correction, is a single item for the schedule.
- **A one-off booking paid in advance of a later period is deferred too.** A
  deposit or full payment for a dated event (a team event, a venue, a
  conference place) falling in a later accuracy period goes to Prepayments on
  payment and is released in the month it happens. Within the same period
  there is nothing to defer.
- **Staff expense claims are never prepayments.** A reimbursement is a person
  being paid back; the period the receipt covers is irrelevant. Book the claim
  in full on the claim date, to the expense account, with no split. This
  applies to expense claims only, not supplier invoices.

The prepayments schedule itself is the prepayments report (the `reports`
domain). A supplier that habitually prepays says so in its `SUPPLIERS.md`
entry, with period and monthly amount.

### 3. Accruals

<!-- FILL IN: when the group accrues (a material cost incurred in the period
with no invoice yet, above what amount). From your accounting policies or
month-end close checklist. The accruals account per entity is in the
balance-sheet table above. -->

- **An accrual is cleared against the account it was raised to, never
  against Prepayments.** A period accrued before the invoice arrives is
  reversed out of the same expense account, so the invoice's own line is the
  only recognition left.
- **A late-paid invoice is recognised in the month it is bookkept**, with no
  backdating, unless it is material to a closed period.
  <!-- FILL IN: confirm or replace this policy. -->

### 4. Deposits

Deposits (office, landlord, supplier) are never expensed: they go to the
entity's deposits account. **A deposit returned in full still leaves a
residual where it was held in a foreign currency**: the difference between the
rate when it went out and the rate when it came back is written off to the
entity's FX gain / loss account so the deposit account clears to nil. Never to
rent or to the cost the deposit related to.

### 5. Sales tax, VAT and GST

This is the generic method for VAT, GST or sales tax, as your jurisdiction
applies it. What each entity is registered for and which tax types it uses
are in its own file, `rules/entities/<KEY>.md` § Tax registration and tax
types.

**Recognise only what is on the invoice.** Never default a tax rate from the
account or the entity; read the invoice's tax section every time.

**The governing test is jurisdiction, not the presence of a tax line.** Input
tax is recognised only where the tax charged is the jurisdiction's own tax, on
a transaction taking place in that jurisdiction, for an entity registered
there. Any other tax on the document is not recoverable and is not a separate
tax line at all: it goes into the total of the expense, on the expense's own
account, never split out.

```
no    an OPCO_EU staff lunch abroad booked with the local sales tax as a
      separate input-tax line
yes   the same lunch booked gross to staff meals, no tax line
```

Corollaries:

- **A foreign seller registered for the local tax is still a domestic
  transaction.** Read the tax line, not the supplier's address or the invoice
  currency.
- **Tax declared under another country's registration (a one-stop-shop or
  similar scheme) is not the local tax** and is not recoverable: leave it in
  the cost.
- **Bank charges and FX fees normally carry no tax.** Never derive
  a tax figure by applying the standard rate to a bank fee.
- **Travel consumed outside the entity's own country is never its input
  tax**, even when a domestic travel agent issued the document and a domestic
  card paid it. Book the gross to travel.
- **Cross-border invoices carry no recoverable tax** and are booked with the
  entity's no-tax or zero type, gross equal to net. A foreign tax amount
  printed on a cross-border invoice is booked into the expense and flagged
  (the supplier may need the correct billing country).
- **Check the supplier's previous bills** for the established treatment
  before deviating; when the treatment is unclear, query.
- **Tax type codes are organisation-specific**: the same code can mean
  different rates in different entities. Resolve from the entity's own tax
  rates or copy from a prior bill of the same supplier in the same entity.
- **A bill already posted with tax wrongly split is corrected by journal**,
  not by re-splitting the bill (Xero ignores line edits on a paid bill): one
  journal dated to the bill, debit the expense, credit the tax account, at the
  bill's own currency rate.

Per entity: what it is registered for, its domestic, zero-rated and no-tax
types, and any entity where tax is always part of the cost are in
`rules/entities/<KEY>.md` § Tax registration and tax types. Fill them in
there, not here.

### 6. Foreign currency

- A bill is entered in the **invoice's own currency**, with Xero's rate on it.
  A non-base currency showing a rate of 1.0 is the error signature.
- A card charge in one currency for a receipt in another never matches the
  bill by amount: match on the converted value before concluding a payment has
  no invoice behind it.
- A bank's foreign-transaction fee shown as its own statement line is a bank
  charge in its own right: spend money to bank fees, dated to the statement,
  without waiting for the underlying receipt.

### 7. Receipts and evidence

- **An illegible date on routine day-to-day spend is not a blocker.** Date the
  bill to the day it was submitted and say so in the line description. What
  must be legible on any receipt: the **amount**, the **supplier** and the
  **currency**. The date still gets queried when it changes the answer: a
  period end, a period of cover, a capitalised item, or a material amount
  whose month it decides. Query what changes the accounting, not what merely
  looks untidy.
- **An unnamed card slip is named from the bank.** Match the slip to the
  statement line by amount and date, take the merchant from the line, and say
  in the description where the name came from. Query only when no statement
  line matches. Payment-processor prefixes on a bank line name the merchant
  after the prefix.
- **A payment processor's own receipt is a receipt** where the supplier never
  sends an invoice: post it gross, say which processor issued it, and chase
  the supplier's tax invoice so recoverable tax can be journalled out later.
- **An order confirmation is not an invoice.** Book the invoice or the
  dispatch-level documents, never an order total.
- **A cost with a bank line but no invoice is not a bill.** An invoice-less
  bill leaves nothing to attach and guarantees a duplicate when the real
  invoice lands. Chase the owner (the routing is in the `xero-bills` skill);
  genuinely invoice-less card spend is spend money per `GROUP.md`.

### 8. Refunds and credit notes

**A refund mirrors whatever the original was, not whatever the supplier calls
it.** Find the original debit in Xero first and copy its shape:

- original was a **bill**: a supplier **credit note**, same account, tax
  type, tracking, currency, and the **same tax amount** to the cent (set the
  tax amount explicitly and re-read).
- original was **not an expense** (parked on the balance sheet because
  someone else was always going to bear it): the refund is **receive money to
  that same account**, not a credit note, which would invent a payable.
- Either way the refund is left unallocated and unreconciled; the user
  matches the statement line.
- A refund is only a refund if the credit is on the statement against the
  original debit, by amount and counterparty. Check the bank first.

### 9. Intake

- **Duplicate guard.** Before creating a bill, check the entity for an
  existing bill with the same supplier and invoice number, or the same
  supplier, amount and date. Two documents for one charge (a card reference
  and the supplier's own invoice; a booking platform's invoice and the
  merchant's) are one cost: compare the invoice number printed inside both.
- **Bills are created AUTHORISED with the source document attached.**
  Anything the agent is unsure of goes to the queried list instead.

## Staff expenses follow the employing entity

**Expenses for a specific person (travel, meals, welfare, conference tickets,
reimbursements) are recognised by the entity employing that person, even if
another entity paid.** The supplier's location never overrides this; it
drives the tax treatment on the bill and nothing else. Where the payer
differs, both intercompany legs are posted per `GROUP.md`.

- Hotels and transport are always travel, whatever platform billed them; only
  food and drink lines on a hotel folio go to meals and welfare.
- A conference ticket and the travel to it are recognised by the attendee's
  employing entity, split by attendee where several entities attend.
- **A cost incurred to set up an employment follows the employment it is
  for** (visa, work permit, relocation): the entity that will employ the
  person recognises it, as the document itself shows.
- Which entity employs whom: `PAYROLL.md` § Who employs whom.

### Intake presumption by sender

<!-- FILL IN: whether a document submitted in Slack or by email presumes the
sender's employing entity (typical when staff submit their own card spend),
and which roles carry no presumption because they pay for the whole group
(e.g. the finance team). Roles only, never names: the Slack member list in
config/group.toml says who holds them. -->

## Tracking categories

Each entity's tracking categories (department, branch, cost centre), which
lines need them and how the option is chosen are in
`rules/entities/<KEY>.md` § Tracking categories. A cost line missing its
tracking, or tracked against the evidence, is a finding.

## Office locations

<!-- FILL IN: one row per office, from your lease agreements or office list,
so local day-to-day spend near an office can be recognised as staff spend of
the entity that occupies it. -->

| Office | Location | Occupying entity |
|---|---|---|
| <office name> | <city or area> | <entity key> |

> Illustration (fictional):
>
> | Office | Location | Occupying entity |
> |---|---|---|
> | Office A | City A | HOLDCO |
> | Office B | City B | OPCO_US |
