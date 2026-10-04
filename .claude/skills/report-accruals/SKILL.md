---
name: report-accruals
description: The accruals report. Every accrual still open across every entity in config/group.toml, in the month it was posted, checked against the Balance Sheet. Use when an admin asks for the accruals report, what is accrued, open or outstanding accruals, the month-end accrual position, or an accruals workbook for one entity.
---

# Accruals report

One workbook, every entity, one row per accrual, one column per month. The
month cell holds what posted journals did: the accrual in the month it went
on, any partial reversal as a negative in its own month. So the row sums to
what is still open.

Admins only, and one report per run: `reports` has the rules that hold across
reports. Read-only against Xero: this report never posts a journal.

## Active and inactive, and the from_year floor

Every row carries a **Status**: `active` while a balance is still owed **as at
this month** and nobody has settled it, `inactive` once it is reversed in
full, cleared by hand, or raised before a month end at which the account was
nil. A reversal posted with a future date does not make the row inactive yet;
the `Open at <month>` column shows the position as at this month beside
`Open`, which nets every posted movement. Movements before the first month of
financial year `from_year` are inside the opening balance and raise no check. An inactive
accrual stays on the report instead of leaving it, so the year's history is on
one sheet, but the workbook **opens filtered to `active`**: the Status filter
is set and the inactive rows are hidden, and clearing the filter shows the
rest. What is still owed and not yet invoiced is therefore what you see first;
the totals sum every row, hidden or not.

**Every row from `from_year` on is on the report; an inactive row from before
it is not.** `from_year` is `[reports] from_year` in `config/group.toml`. A row
counts when it was posted or reversed in that year or later. An active older
accrual is on the report because it is active.

Leaving older rows out does not cost the report its tie to Xero. The totals
block carries a **rows not shown** line holding the net movement of everything
the sheet omits, so the balance is the whole account and agrees with the
Balance Sheet at **every** month end. A difference in any month is a real
break and is reported as one.

## An account cleared to nil settles everything before it

Where the Balance Sheet says the account held nothing at a month end, nothing
raised up to that month is still open: the balance is the truth, and a row
still reading as open is a reversal the report could not tie back, not a
liability anyone owes. Those rows are inactive, and every check they raised
goes with them: open a long time, over-reversed, the unattributed reversals
and the reversals matched on balance dated up to that month. One line in
Checks says the account was cleared and how many older rows left the sheet, so
nobody hunts for a history that is settled.

The rule is the shared engine's, so it holds across the balance reports. The
tie to Xero is unaffected: what leaves is carried by **rows not shown**, and
every month end is still checked for every entity, including one with no rows
left.

## The master and the Drive copy

`data/reports/Accruals.xlsx` on the server is the **master**. Every build,
from `refresh` or `build`, publishes a copy to the `reports` subfolder of the
Drive publish folder (`config.drive_settings()`), overwriting the same file,
so Drive always holds what the server holds. `--no-publish` skips it. A fix-up
(`link`, `set-field`, `clear`, ...) edits the store only; run `build`
afterwards so the master and the Drive copy carry it.

**Scheduled**: `deploy/run-scheduled.sh balance-reports`, after the daily
chain: refreshes the prepayments report, this one and the deposits report,
then `scripts/balance_reports_summary.py` posts one top-level message to the
agent's channel (`config.slack().channel_id`) with all three reports'
findings.

## The model it assumes

An accrual is a credit to the entity's accruals account with the expense
debited; the reversal is the opposite when the invoice lands (and **an accrual
is cleared against the account it was raised to, never against the
prepayment**; the group's own accrual rule is in `rules/EXPENSES.md`). So on
the accruals account:

- a **credit** creates the accrual. It becomes a **row**.
- a **debit** reverses or settles it. It becomes a negative **cell** in its
  own month, and when it clears the row entirely the row turns inactive.

Accrued income is not in scope: it is an asset, and the discovery excludes it.

## Running it

On the server, always: it is the only machine that calls Xero.

```bash
.venv/bin/python scripts/accruals_report.py refresh all        # the usual run
.venv/bin/python scripts/accruals_report.py refresh holdco     # one ledger
.venv/bin/python scripts/accruals_report.py refresh all --full
.venv/bin/python scripts/accruals_report.py checks             # no Xero call
.venv/bin/python scripts/accruals_report.py rows holdco        # the row keys
```

`refresh` reads Xero from the last sync less 95 days, merges into
`data/reports/accruals_store.json`, rebuilds `data/reports/Accruals.xlsx` in
place and prints the checks. It exits 1 when any month end disagrees with the
Balance Sheet.

`--full` rebuilds from `[reports] first_build_from`. Three reasons only: a
month disagrees with Xero and the cause is not in the checks; a journal dated
more than 95 days back has been edited; the store has been lost.

Then deliver: `deploy/fetch-report.sh accruals` on the admin's own machine,
landing in `~/Downloads`. In a Slack run you cannot run it for them; give them
that line.

## The workbook

**Accruals**: the rows, Status in column B filtered to `active` when opened,
autofiltered on the entity column too, so "the HoldCo accruals" or "the
inactive ones" is this same file filtered. Each row carries the entity, the
status, the supplier or the journal narration that raised it, the
description, the accruals account, the expense account it was raised to, any
invoice number, the date, the month posted, the currency, the amount accrued,
what has been reversed and what is open. Then a totals block per entity **in
that entity's base currency**: movement, unattributed, rows not shown,
balance, balance in Xero, difference, the title in column A and the labels in
column C so they stay in view. Month columns start at the first month of financial year `from_year`:
a row's earlier postings are one `Before Jan YY` cell that the Open formula
includes, and the block opens from Xero's balance at the December before.
Balance in Xero is the accruals account's closing balance on the Balance Sheet
at that month end.

**Checks**: what a person has to look at:

- `open a long time`: posted four months ago or more and still open. The one
  that matters: an accrual nobody has reversed is usually an invoice that
  arrived and was coded straight to the expense, so the cost is in twice.
- `over-reversed`: more reversed than was ever accrued.
- `unattributed reversal`: a debit the report could not tie to an accrual.
- `reversals matched on balance`: a reversal put on one of several rows for
  the same counterparty by balance rather than by anything the narration said;
  `link` settles it.
- `does not agree with Xero`: at any month end. The balance is the whole
  account, so this is always a real break.

## When the report cannot work something out

```bash
.venv/bin/python scripts/accruals_report.py rows holdco
.venv/bin/python scripts/accruals_report.py link "HOLDCO|<doc>|m1" "HOLDCO|<doc>"
.venv/bin/python scripts/accruals_report.py set-field "HOLDCO|<doc>" supplier "Contoso Cloud"
.venv/bin/python scripts/accruals_report.py accept "HOLDCO|<doc>" "open a long time" "why"
.venv/bin/python scripts/accruals_report.py clear "HOLDCO|<doc>" "why it is settled"
```

A journal-raised accrual has no contact, so its supplier column is the
narration. Where that is unreadable, `set-field ... supplier` names it
properly and the value is kept through every later refresh, including
`--full`. `combine`, `unlink`, `clear` and `accept` work as in
`report-prepayments`.

Attribution ties a reversal to a row by an explicit `link`, then the invoice
number in the narration, then the supplier with one open accrual on that
account. Anything ambiguous is left unattributed and reported, never guessed.

## Account discovery

Every liability account whose name carries one of `[reports.accruals]
account_words` (default "accrual", "accrued"), per entity, read from the chart
of accounts and cached in the store; anything carrying one of
`account_not_words` (default "income", "receivable", "revenue") is excluded.
An explicit list in `[reports.accruals] accounts = { <ENTITY> = ["<code>"] }`
replaces discovery for that entity. An account the discovery misses is added
to that list, or by hand under `accounts.<entity>.codes` in the store, and is
kept from then on.

## What this report is not

It does not reverse, post or correct anything. A stale accrual is a finding
here and a bookkeeping run under `xero-bills` afterwards.
