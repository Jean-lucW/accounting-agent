# Staff and payroll

The payroll bookkeeping spec for every entity and every payroll source, and
the staff logic that decides which entity carries a person's costs. Extend it
here rather than in the entity files; the entity files say which sources
apply to them.

The control account codes themselves, and what a residual balance on each may
legitimately be, live in `config/group.toml` under each entity's
`[entities.payroll_controls]`. This file says how each source is booked
against them.

> Sections marked `<!-- FILL IN -->` are yours. The group principles, the
> bills-versus-journals rule, the zero check and the FX rules are generic
> method: keep them, edit what does not fit. Blocks headed
> `Illustration (fictional)` use the fictional group in
> `config/group.example.toml`; they are never rules.

## Group principles

- **Payroll is booked in total per pay run or provider invoice**, not
  employee by employee. Never create per-employee documents for normal
  payroll.
- **A payroll document is dated in the month the pay is for, never the month
  it is run, invoiced or paid.** Where the invoice, the pay run or the payment
  falls after the payroll month, date it on the last day of that month; a
  provider invoice dated inside the payroll month keeps its own date. Read the
  month off the document (the pay periods, the payslip dates, the file name),
  never off the bank date. A correction is dated in the month it corrects. The
  due date stays the document's own.

  ```
  no    a provider runs September's salaries on 1 October, so the bill is
        dated 1 October
  yes   the invoice covers the September pay periods, so the bill is dated
        30 September and the October payment settles it
  ```

- **Payroll document lines map to the same accounts every month.**
  Month-on-month mapping consistency is the primary review test. The mapping
  tables below are the standard; a line that is not in them is a query, not a
  guess.
- **Provider fees are not salary.** A payroll provider's own fees (an
  employer of record, a PEO, a payroll bureau) go to the payroll fees
  account, wherever the provider puts them on its invoice.
- **Prior periods stay as booked.**
  <!-- FILL IN: the date from which these tables govern; anything earlier
  (locked or closed periods) is left alone. -->

## Who employs whom

The entity that employs a person carries all of their payroll **and** every
cost attributable to them (`EXPENSES.md` § Staff expenses follow the
employing entity). Write it by role or location, never by name: the payroll
reports name the people.

<!-- FILL IN: one row per group of staff, from your employment contracts and
your payroll provider's reports. Include contractors and anyone engaged
through a provider. -->

| Staff group (role or location) | Employing entity | Engaged via | Notes |
|---|---|---|---|
| <staff group> | <entity key> | <payroll source> | |

> Illustration (fictional):
>
> | Staff group (role or location) | Employing entity | Engaged via | Notes |
> |---|---|---|---|
> | Leadership team | HOLDCO | in-house payroll | |
> | Sales and support, Office B | OPCO_US | payroll provider | |
> | Freelance contractors | the entity that engages them | own invoices | See § Contractors versus employees |

**Leavers** with ongoing compensation (notice pay, severance) stay with the
entity that employed them. Trailing costs of a former employee go to that
entity too.

## Payroll sources

One row per source of payroll documents, then one mapping table per source
below.

<!-- FILL IN: one row per payroll source, from your payroll provider
contracts. Mechanism is either `bills` or `controls + journal` (see the rule
below). Documents = what arrives and what gets attached. -->

| # | Source | Entity | Who | Mechanism | Documents attached |
|---|---|---|---|---|---|
| 1 | <source> | <entity key> | <staff group> | | |

> Illustration (fictional):
>
> | # | Source | Entity | Who | Mechanism | Documents attached |
> |---|---|---|---|---|---|
> | 1 | In-house payroll | HOLDCO | leadership team | controls + journal | the approved payroll report |
> | 2 | Litware Payroll (provider) | OPCO_US | sales and support | bills, one per pay run | the provider's invoice and its detail report |

### Where the payroll documents come from

<!-- FILL IN: where each source's documents are filed (a shared drive folder,
the accounting inbox, the provider's portal) and who approves them, by role.
If an approved report is versioned, say which version is the source. -->

- **Search the filing location before chasing anyone.** A payroll document
  "missing" from the inbox is usually filed where it always is. Chase only
  what is genuinely not there, and say in the chase that it was checked.
- **Only the approved version of a report is the source.** A journal posted
  from a superseded version is corrected by the difference, not voided and
  reposted, and proved by the control accounts clearing.
- **A scanned or stamped report with no text layer is not empty.** Render the
  pages and read them visually; never report it as unreadable or fall back to
  an older month.

## Bills versus journals: the rule

- **Controls + journal sources** (typically a payroll the group runs itself):
  bank-feed lines for net pay, tax remittances and pension are spend money
  **to the control accounts only**, never straight to P&L, and one monthly
  manual journal from the payroll report clears them. Never create bills for
  these.
- **Bill sources** (a provider that invoices: an employer of record, a PEO, a
  payroll bureau; contractors): one bill (ACCPAY) per provider invoice, coded
  per the source's mapping table, with the source document attached. Never
  book these as manual journals, and never per employee.
- **Every net-pay bank line goes to the net-pay control**, including a
  leaver's and including one later returned. A net-pay line coded straight to
  salaries nets against nothing and strands the journal's credit.
- **A payroll connector that posts its own draft journals** for runs already
  booked as bills is a second recording: delete those drafts once the bill
  exists, and build the bill from the run's report where the drafts are the
  only record.

## Control accounts

The codes and their allowed residuals are in `config/group.toml`
(`payroll_controls` per entity); `scripts/check_payroll_controls.py` reads
them.

<!-- FILL IN: for each control account in config, what flows through it
(debits from the bank, credits from the journal or bill), so a reviewer can
read a balance. -->

| Entity | Control (name) | Debited by | Credited by |
|---|---|---|---|
| <entity key> | <control account> | | |

> Illustration (fictional):
>
> | Entity | Control (name) | Debited by | Credited by |
> |---|---|---|---|
> | HOLDCO | Wages Payable | net-pay bank lines | the monthly payroll journal |
> | HOLDCO | Payroll Taxes Payable | the tax authority payment | the monthly payroll journal |
> | OPCO_US | Payroll Liabilities | the provider's debit | the pay-run bill |

### Control-account zero check: at the end of every payroll run

Every run that touches payroll (a journal, a payroll bill, net-pay or
remittance bank lines) ends by running:

```bash
.venv/bin/python scripts/check_payroll_controls.py
```

**The pass condition is that each control clears to zero once its cycle
completes**: today's balance may hold only what config says its residual may
be (the current month's accrual until the remittance, for example). Anything
else (a residual from a prior month, a debit balance on a liability control,
a small FX stub, a bill still carrying an amount after its remittance was
paid) is a finding: investigate, fix the journal or flag the payment
application, post the FX write-off, and report it. Do not declare a payroll
month reconciled without the check passing or its residuals explained.

Payments that clear a payroll bill from a control account are payments, so
they are the user's step in the Xero UI. Report them with the bill, the
control account, the amount, the date and the rate.

## Employer taxes, pensions and benefits

<!-- FILL IN: the employer-cost accounts per entity, `<code> <name>` from
each entity's chart of accounts, so every mapping table can name them by
category. One column per entity. -->

| Category | <entity key> | <entity key> |
|---|---|---|
| Salaries (gross pay incl. all employee deductions) | | |
| Bonus | | |
| Employer payroll taxes and social security | | |
| Employer pension | | |
| Health care and benefits | | |
| Payroll provider fees | | |

Generic decomposition, for any source and any country:

- Everything the **employee** bears (net pay, income tax withheld, the
  employee's payroll taxes and social security, employee pension, other
  deductions) is part of **gross pay** and goes to salaries (or bonus for the
  bonus-attributable share).
- Employer add-ons (employer payroll taxes and social security, employer
  pension, employer health contributions) go to their own expense accounts.
- Credits go to the control accounts (journal sources) or make up the bill
  total (bill sources).
- Statutory insurance attached to a social-security scheme is an employer
  tax, not health care, unless your rules say otherwise. Trivial statutory
  charges go with employer taxes.
- **Health care is booked once.** Where health care is inside the payroll
  document, a separately billed health care invoice to the same entity double
  counts it and is a query. Each entity resolves health care to its own
  account.

## Mapping tables, one per source

One table per row of the payroll sources table: every line the source's
document can carry, what it is, the account it goes to and the entity that
carries it. For a controls + journal source, the Account column is the debit
and the Credit column names the control; for a bill source, leave Credit
blank (the bill total is the credit).

<!-- FILL IN: copy the template once per source, from a recent approved
payroll report or provider invoice. Add rows as admins rule; never guess a
new line. -->

### <#>. <source>, <entity key>

| Source line (as on the document) | What it is | Account | Credit | Entity |
|---|---|---|---|---|
| <line> | | <category from the table above> | | <entity key> |

> Illustration (fictional): source 2, Litware Payroll, OPCO_US (a bill
> source).
>
> | Source line (as on the document) | What it is | Account | Credit | Entity |
> |---|---|---|---|---|
> | Gross wages | gross pay per pay period | Salaries | | OPCO_US |
> | Employer taxes | employer payroll taxes | Employer payroll taxes and social security | | OPCO_US |
> | Retirement match | employer pension | Employer pension | | OPCO_US |
> | Service fee | the provider's fee | Payroll provider fees | | OPCO_US |

- **A one-off prefunded charge and its later reversal are a pair.** Before
  booking a reversal, check the original charge has not already been
  reversed.
- **A refundable deposit a provider holds** goes to the employing entity's
  deposit account, never to salaries.

## Contractors versus employees

- **A contractor's invoice carries no employer taxes, pension or benefits.**
  The contractor bears their own tax and insurance out of the gross fee. If
  such lines ever appear on a contractor or contractor-of-record invoice, the
  arrangement has changed: query it.
- Contractor fees for ongoing work go to
  <!-- FILL IN: salaries, or a separate contractor-fees account -->; one-off
  bonuses to bonus.
- A contractor-of-record provider's management and payment fees go to payroll
  provider fees; a refundable deposit to the deposit account.
- A contractor invoice that arrives twice (the contractor's own document and
  a portal notification of the same number) is one document.

## FX handling

**A payroll document is entered in the payroll's own currency, never the
entity's base currency**, with Xero's own rate on it. Check the currency code
and rate on every payroll document before and after posting: a non-base
currency at a rate of 1.0 is the error signature.

<!-- FILL IN: one row per source, its document currency, from the source's
documents. -->

| Source | Document currency |
|---|---|
| <source> | <ISO currency code> |

> Illustration (fictional):
>
> | Source | Document currency |
> |---|---|
> | In-house payroll, HOLDCO | GBP |
> | Litware Payroll, OPCO_US | USD |
> | Staff abroad paid by a provider, OPCO_EU | SGD |

- **A manual journal is base currency only.** Where a journal clears controls
  that were debited in another currency, derive its credits from the landed
  base amounts on the bank lines (each line's own rate, added up line by
  line), never from the report total at one hand-typed rate. Then the control
  clears to the cent.
- **A conversion error is not FX.** A report figure taken in the wrong
  currency is corrected against salaries, not the FX account. Use the FX
  account only for a genuine rate movement between accrual and payment.
- Small genuine FX residues on a control are swept to the FX gain / loss
  account by journal on a settled block of months, never on one still waiting
  for its clearing entry.
- **A payroll bill paid from a bank account in another currency never
  appears in that account's match screen.** The bill is right; the
  settlement is entered from the bill's own payment screen by the user. Never
  re-enter the bill in the bank's currency to make matching easier.

## What only the user does, in the Xero UI

- Every payment onto a payroll bill, including clearing a bill from a control
  account and settling it from an intercompany account.
- Reconciling statement lines to payroll bills.
- Voiding or editing anything behind a lock date.

The agent posts journals, bills and spend money to controls, attaches the
documents, and reports each user step with the bill, the account, the amount,
the date and the rate.

## Creating a month's payroll bookkeeping

1. Identify the source (payroll sources table) and pull its document from its
   filing location.
2. Map every line with the source's mapping table, and **compare with the
   prior two or three months of the same source**. A line not in the table,
   or a mapping that would differ from last month, is a query.
3. Book one aggregated document for the period: the journal for a controls
   source (present the line-by-line mapping before posting), one bill per
   provider invoice for a bill source.
4. Attach the payroll document.
5. Run the control-account zero check.
6. When an admin rules on a new line or mapping, write it into the source's
   mapping table so it becomes the standard.
