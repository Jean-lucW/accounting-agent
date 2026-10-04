---
name: report-prepayments
description: The prepayments report. Every prepaid invoice across every entity in config/group.toml, with the amount posted journals released in each month, checked against the Balance Sheet. Use when an admin asks for the prepayments report, the prepayment schedule, what is sitting in the prepayments account, whether a prepayment is being released, or a prepayments workbook for one entity.
---

# Prepayments report

One workbook, every entity, one row per prepaid invoice, one column per month
holding **what posted journals actually released in that month**. Not a plan.
A row whose releases stop halfway shows as a gap, and finding those gaps is
why the report exists.

Admins only, and one report per run: `reports` has the rules that hold across
reports. Read-only against Xero: this report never posts a journal.

## Active and inactive, and the from_year floor

Every row carries a **Status**: `active` while a balance remains **as at this
month** and nobody has settled it, `inactive` once it is released in full,
cleared by hand, or raised before a month end at which the account was nil.
As at this month, not after every posted journal: a subscription whose twelve
releases are all posted ahead still has money in the account until the last
one falls due, and the `Remaining at <month>` column shows that figure beside
`Remaining`, which is what is left to post. Inactive rows stay on the report
rather than leaving it, so the history of the year is on one sheet, but the
workbook **opens filtered to `active`**: the Status filter is set and the
inactive rows are hidden, and clearing the filter shows the rest. The totals
sum every row, hidden or not.

**Every row from `from_year` on is on the report; an inactive row from before
it is not.** `from_year` is `[reports] from_year` in `config/group.toml`. A row
counts when it was posted or moved in that year or later. An active older row
(a two-year subscription still releasing) is on the report because it is
active. What is left out is carried by "rows not shown", so the balance is
still the whole account.

**An account cleared to nil settles everything before it.** Where the Balance
Sheet says the account held nothing at a month end, every row raised up to
that month is inactive, with the checks it raised; Checks carries one line
saying so. `report-accruals` has the rule in full: it is the shared engine's,
so it holds here too.

## The master and the Drive copy

`data/reports/Prepayments.xlsx` on the server is the **master**. Every build,
from `refresh` or `build`, publishes a copy to the `reports` subfolder of the
Drive publish folder (`config.drive_settings()`), overwriting the same file,
so Drive always holds what the server holds. `--no-publish` skips it. A fix-up
(`set-period`, `link`, `clear`, ...) edits the store only; run `build`
afterwards so the master and the Drive copy carry it.

**Scheduled**: `deploy/run-scheduled.sh balance-reports`, after the daily
chain: refreshes this report, the accruals report and the deposits report,
then `scripts/balance_reports_summary.py` posts one top-level message to the
agent's channel (`config.slack().channel_id`) with all three reports'
findings, the Checks lines that still need a person, with the recorded
decisions counted rather than repeated.

## The model it assumes

Bookkeeping a prepayment is (the group's deferral rule in `rules/EXPENSES.md`):
the bill is coded to the entity's Prepayments account with the covered period
in the line description, and the cost is released to the expense account by
monthly manual journals over that period. So on the Prepayments account:

- a **debit** is an addition: the bill, the spend money, the brought-forward
  journal. It becomes a **row**.
- a **credit** is a release: the monthly amortisation journal. It becomes a
  **cell** in that row's month column.

If `rules/GROUP.md` sets accuracy quarterly rather than monthly, a quarter's
releases landing in one month is right even though it looks lumpy. The report
shows the phasing; it does not judge it.

## Running it

On the server, always: it is the only machine that calls Xero.

```bash
.venv/bin/python scripts/prepayments_report.py refresh all       # the usual run
.venv/bin/python scripts/prepayments_report.py refresh opco-us   # one ledger
.venv/bin/python scripts/prepayments_report.py refresh all --full
.venv/bin/python scripts/prepayments_report.py checks            # no Xero call
.venv/bin/python scripts/prepayments_report.py rows opco-us      # the row keys
.venv/bin/python scripts/prepayments_report.py build             # no Xero call
```

`refresh` reads Xero from the last sync less 95 days, merges into
`data/reports/prepayments_store.json`, rebuilds `data/reports/Prepayments.xlsx`
in place and prints the checks. It exits 1 when any month disagrees with the
Balance Sheet.

**`--full` rebuilds from `[reports] first_build_from`.** Use it for exactly
three reasons: a month disagrees with Xero and the cause is not in the checks;
a journal or bill dated more than 95 days back has been edited; the store has
been lost. Never as a matter of routine: it is the expensive path.

Then deliver: `deploy/fetch-report.sh prepayments` on the admin's own machine,
landing in `~/Downloads`. In a Slack run you cannot run it for them; give them
that line.

## The workbook

**Prepayments**: the rows, Status in column B filtered to `active` when
opened, autofiltered on the entity column too, so "the OpCo US prepayments" or
"the inactive ones" is this same file filtered, never a different build. Then
a totals block per entity **in that entity's base currency**: additions,
released, unattributed, rows not shown, balance, balance in Xero, difference.
Summing different currencies down one column would be meaningless, which is
why the blocks are separate. The entity title sits in column A and the line
labels in column C, inside the frozen columns, so they stay in view across the
months.

**Month columns start at the first month of financial year `from_year`.** What a row did before that
is one cell, `Before Jan YY`, which the Released formula includes, so every
row still adds up; the totals block opens from Xero's own balance at the
December before. Anything before that January is inside the opening balance
and is ignored by the checks: an unattributed release dated earlier is not a
finding. **Balance in Xero** is the closing balance of the entity's
prepayments account(s) on the Balance Sheet at that month end, in base
currency, the accounts the discovery found added together.

Two of those lines are what make the balance the whole account rather than
just the sheet: **unattributed** carries movements no row could be found for,
and **rows not shown** carries items that opened and closed without ever being
open at a month end the sheet lists. Both are listed in Checks.

**The difference row is the report's own audit.** The schedule and the
Balance Sheet must agree at every month end. A non-zero difference means the
report is missing a movement or holding one Xero does not: say so in the
message, with the month and the amount, and do not paper over it.

**Checks**: everything a person has to look at. Each kind and what it means:

- `release missing`: a month inside the covered period with no release
  posted. The common one, and the reason for the report. Report it; posting
  the catch-up is a bookkeeping run, not this one.
- `expired with a balance`: the period has ended and money is still held.
  Either releases stopped early or the period is wrong.
- `released after the period`: a recurring journal outliving its
  subscription. It will keep going until someone stops it.
- `over-released` / a negative balance: more released than was ever deferred.
- `period not stated`: the line description carried no period, so there is no
  schedule for that row. Fix it with `set-period` (below), do not guess in the
  message. This one is `manual` in the summary.
- `unattributed release`: a credit the report could not tie to a row.
- `releases matched on balance`: see Attribution below.
- `does not agree with Xero`: as above.

## When the report cannot work something out

It asks rather than guesses, and the answer is stored so it is asked once.

```bash
.venv/bin/python scripts/prepayments_report.py set-period "OPCO_US|<doc>" "Jan 26" "Dec 26"
.venv/bin/python scripts/prepayments_report.py link "OPCO_US|<doc>|m1" "OPCO_US|<doc>"
.venv/bin/python scripts/prepayments_report.py set-field "OPCO_US|<doc>" type "Subscription"
.venv/bin/python scripts/prepayments_report.py combine "OPCO_US|<doc>|2" "OPCO_US|<doc>"
.venv/bin/python scripts/prepayments_report.py uncombine "OPCO_US|<doc>|2"
.venv/bin/python scripts/prepayments_report.py set-field "OPCO_US|<doc>" scheduled 5000
.venv/bin/python scripts/prepayments_report.py clear "OPCO_US|<doc>" "why it is settled"
.venv/bin/python scripts/prepayments_report.py unlink "OPCO_US|<doc>|m1"
.venv/bin/python scripts/prepayments_report.py accept "OPCO_US|<doc>" "release missing" "why"
```

`rows` lists the keys (entity key, then the Xero document id). A value set
this way is marked fixed and survives every later refresh, including `--full`.

**Where the period comes from.** Read it off the source document, not off the
amount: the invoice's own covered period, and only the part that was deferred
(the release is worked out on the period the deferred amount covers, never as
a fraction of the balance sitting in the account). One bill carrying several
invoices with different periods is several rows, and only the prepaid ones are
rows at all.

**One item posted twice is one row.** A prepayment raised in two lines of the
same journal, or a rounding top-up against a premium already deferred, reads
as two half-released rows that each look wrong and together clear to nil.
`combine` folds one row into the other: both postings, and every movement
attributed to either, show as one row, each posting keeping the month it was
posted in so the balance still ties. It is for the same item only: two
invoices from one supplier covering different periods are two rows, and a
release landing on the wrong one of them is a `link`, not a combine.

**Twelve months of cover is twelve calendar months from the month it starts.**
A policy or a rent running 10 Jun 25 to 9 Jun 26 is scheduled Jun 25 - May 26,
not across the thirteen months it touches: twelve is what the monthly journal
releases, and a thirteenth month in the period makes a release look missing.

**Only the deferred part is scheduled.** Where one invoice carries a one-off
cost and a period cost (a set-up fee beside a year of service) the monthly
release is worked out on the deferred part alone, not on the invoice.
`set-field <row> scheduled <amount>` records it and the Monthly release column
follows it; the one-off is simply released in its own month.

**A part month is pro-rated by days, not given a whole month.** Cover that
starts or ends mid-month is scheduled on the daily rate for the days it
covers (42 days at 300.00 a day is 14 days, 4,200.00, in the first month and
28 days, 8,400.00, in the second) so a stub month reads as a stub and the schedule still adds to the
invoice.

**`clear` marks a row inactive when a person has settled it**, with the
reason, which goes on the Checks sheet: an item the postings cannot show as
closed by themselves, a balance written back to another account, a reversal
posted outside the rebuild window. A row from `from_year` on stays on the
sheet as inactive; an older one leaves, and what it still holds is carried by
"rows not shown", so the balance is still the whole account. **`unlink` says a
movement belongs to no row at all** (a release whose own prepayment never
reached the account) and pins it there, where attribution would otherwise keep
finding it the least-bad row and putting that counterparty out by it.

**`accept` stops one check on one row being raised**, once an admin has
looked at it and accepted the position: releases caught up in a later month,
a period the invoice really does not state, a balance an admin is content to
carry. The row stays on the report with everything it holds and only the
check goes, replaced by a line naming what was accepted and why: a check that
quietly stopped being raised is a check nobody can audit. `clear` is the
different case, where the whole row is settled and leaves.

**Attribution.** A release is tied to a row by an explicit `link` first, then
by the invoice number in the narration, then by name: the row sharing the most
real words with the narration wins, bookkeeping filler and month names not
counting. A tie between rows of the same counterparty goes to the row that can
absorb the movement, largest balance first, and is reported as `matched on
balance` so a person can settle it with `link`.

Two consequences to know before reading a row:

- **a negative balance on one row of a counterparty that has several** usually
  means a movement landed on the wrong one of them. The total still agrees
  with Xero; `link` moves it.
- **a movement is never split across rows.** One reversal clearing two rows is
  attributed whole to one and shows as `over-released` there, with the other
  left looking untouched. Link it if the split matters.

## Account discovery

Every asset account whose name carries one of `[reports.prepayments]
account_words` (default "prepay", "prepaid"), per entity, read from the chart
of accounts and then cached in the store. Bank accounts never count. An
explicit list in `[reports.prepayments] accounts = { <ENTITY> = ["<code>"] }`
replaces discovery for that entity. An account the discovery misses is added
to that config list, or by hand under `accounts.<entity>.codes` in the store,
and is kept from then on: a cached list is never overwritten.

## What this report is not

It does not post, correct or catch up a single journal. Everything it finds is
a finding. The corrections are a bookkeeping run under `xero-bills`, with its
own confirmation, and they are a different run.
