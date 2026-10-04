---
name: report-deposits
description: The deposits report. Every deposit the group still has out, across every deposit account in every entity in config/group.toml, in the month it was paid, checked against the Balance Sheet. Use when an admin asks for the deposits report, what deposits are held or outstanding, a rent or supplier deposit, or a deposits workbook for one entity.
---

# Deposits report

One workbook, every entity, every deposit account, one row per deposit, one
column per month. The month cell holds what posted documents did: the deposit
in the month it was paid, any partial refund or application as a negative in
its own month. So the row sums to what is still held.

Admins only, and one report per run: `reports` has the rules that hold across
reports. Read-only against Xero: this report never posts a journal.

## Active and inactive, and the from_year floor

Every row carries a **Status**: `active` while money is still out **as at this
month** and nobody has settled it, `inactive` once it is refunded, returned or
applied in full, cleared by hand, or raised before a month end at which the
account was nil. The `Open at <month>` column shows the position as at this
month beside `Open`, which nets every posted movement. Movements before
the first month of financial year `from_year` are inside the opening balance and raise no check. An
inactive deposit stays on the report instead of leaving it, but the workbook
**opens filtered to `active`**: the Status filter is set and the inactive rows
are hidden, and clearing the filter shows the rest. So what you see first is
the money currently out with landlords, suppliers and other counterparties;
the totals sum every row, hidden or not. A deposit partly returned is active,
showing the refund as a negative in its month and the remainder open.

**Every row from `from_year` on is on the report; an inactive row from before
it is not.** `from_year` is `[reports] from_year` in `config/group.toml`. A row
counts when it was paid or refunded in that year or later. An active older
deposit (a rent deposit paid years ago) is on the report because it is active.

Leaving older rows out does not cost the report its tie to Xero. The totals
block carries a **rows not shown** line holding the net movement of everything
the sheet omits, so the balance is the whole account and agrees with the
Balance Sheet at **every** month end. A difference in any month is a real
break.

**An account cleared to nil settles everything before it.** Where the Balance
Sheet says the account held nothing at a month end, every row raised up to
that month is inactive, with the checks it raised; Checks carries one line
saying so. `report-accruals` has the rule in full: it is the shared engine's,
so it holds here too.

## The master and the Drive copy

`data/reports/Deposits.xlsx` on the server is the **master**. Every build,
from `refresh` or `build`, publishes a copy to the `reports` subfolder of the
Drive publish folder (`config.drive_settings()`), overwriting the same file,
so Drive always holds what the server holds. `--no-publish` skips it. A fix-up
(`link`, `set-field`, `clear`, ...) edits the store only; run `build`
afterwards so the master and the Drive copy carry it.

**Scheduled**: `deploy/run-scheduled.sh balance-reports`, after the daily
chain: refreshes the prepayments and accruals reports and this one, then
`scripts/balance_reports_summary.py` posts one top-level message to the
agent's channel (`config.slack().channel_id`) with all three reports'
findings.

## The model it assumes

A deposit is a debit to a deposit account: usually a bill coded there,
sometimes spend money straight from the bank. It comes back as a receipt or a
credit note, or is applied against what is owed. So:

- a **debit** creates the deposit. It becomes a **row**.
- a **credit** returns or applies it. It becomes a negative **cell** in its
  own month, and when it clears the row entirely the row turns inactive.

Every asset account whose name says deposit is in scope, however many an
entity has: the deposit account each row sits in is a column, so several
accounts read as one list. Bank accounts are not deposit accounts and are
excluded, and so is anything reading as a customer or client deposit, which is
a liability.

## Running it

On the server, always: it is the only machine that calls Xero.

```bash
.venv/bin/python scripts/deposits_report.py refresh all        # the usual run
.venv/bin/python scripts/deposits_report.py refresh opco-eu    # one ledger
.venv/bin/python scripts/deposits_report.py refresh all --full
.venv/bin/python scripts/deposits_report.py checks             # no Xero call
.venv/bin/python scripts/deposits_report.py rows opco-eu       # the row keys
```

`refresh` reads Xero from the last sync less 95 days, merges into
`data/reports/deposits_store.json`, rebuilds `data/reports/Deposits.xlsx` in
place and prints the checks. It exits 1 when any month end disagrees with the
Balance Sheet.

`--full` rebuilds from `[reports] first_build_from`. Three reasons only: a
month disagrees with Xero and the cause is not in the checks; a document dated
more than 95 days back has been edited; the store has been lost.

Then deliver: `deploy/fetch-report.sh deposits` on the admin's own machine,
landing in `~/Downloads`. In a Slack run you cannot run it for them; give them
that line.

## The workbook

**Deposits**: the rows, Status in column B filtered to `active` when opened,
autofiltered on the entity column too, so "the OpCo EU deposits" or "the
inactive ones" is this same file filtered. Each row carries the entity, the
status, the counterparty, the description, the deposit account it sits in, any
invoice number, the invoice date, the month posted, the currency, the invoice
amount, the deposit amount, what has been returned and what is open. Then a
totals block per entity **in that entity's base currency**: movement,
unattributed, rows not shown, balance, balance in Xero, difference, the title
in column A and the labels in column C so they stay in view. Month columns
start at the first month of financial year `from_year`: a row's earlier postings are one
`Before Jan YY` cell that the Open formula includes, and the block opens from
Xero's balance at the December before. Balance in Xero is the deposit
accounts' closing balance on the Balance Sheet at that month end, added
together.

**Checks**: what a person has to look at:

- `over-returned`: more came back than went out, so the row is holding
  something that is not this deposit.
- `unattributed refund`: a credit on a deposit account the report could not
  tie to a deposit.
- `refunds matched on balance`: a refund put on one of several rows for the
  same counterparty by balance; `link` settles it.
- `does not agree with Xero`: at any month end. The balance is the whole
  account, so this is always a real break.

There is deliberately **no staleness check**. A rent or supplier deposit is
meant to sit there for years; age is not a finding, and flagging it would bury
the checks that matter.

## When the report cannot work something out

```bash
.venv/bin/python scripts/deposits_report.py rows opco-eu
.venv/bin/python scripts/deposits_report.py link "OPCO_EU|<doc>|m1" "OPCO_EU|<doc>"
.venv/bin/python scripts/deposits_report.py set-field "OPCO_EU|<doc>" type "Rent deposit"
.venv/bin/python scripts/deposits_report.py note opco-eu "accepted" "<what it says>"
```

A value set this way is marked fixed and survives every later refresh,
including `--full`. `combine`, `unlink`, `clear` and `accept` work as in
`report-prepayments`. Attribution ties a refund to a row by an explicit
`link`, then the invoice number in the narration, then the counterparty with
one open deposit on that account. Anything ambiguous is left unattributed and
reported.

## Account discovery

Every asset account whose name carries one of `[reports.deposits]
account_words` (default "deposit"), less `account_not_words` (default
"customer", "client"), per entity, read from the chart of accounts and cached
in the store. An explicit list in
`[reports.deposits] accounts = { <ENTITY> = ["<code>"] }` replaces discovery
for that entity. An account the discovery misses is added to that list, or by
hand under `accounts.<entity>.codes` in the store, and is kept from then on.

## What this report is not

It does not post, refund, release or write off anything. A deposit that
should have come back is a finding here and a chase or a bookkeeping run
afterwards.
