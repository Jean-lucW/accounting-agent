---
name: report-bills
description: The bills payable report. Every bill posted in every entity, paid or open, and for every open bill the reason it is still open, verified against the bank. Use when an admin asks which bills are paid or unpaid, what is outstanding, what needs paying, why a bill is still open, the accounts payable position, the bills workbook, or to refresh the bills report. Read-only; never creates a payment.
---

# Bills payable report

One workbook, one tab per entity, every bill Xero holds, **filtered to the
unreconciled ones**: the open bills first with a **status** that says why the
bill is still open and what to do about it, then any paid bill whose payment
is not reconciled to a statement line. Every bill paid and reconciled is in
the sheet but hidden by the Reconciled filter; clearing the filter shows it
with its payment date and account. A Summary tab counts every status per
entity and lists the manual actions.

Admins only, one report per run: the `reports` skill has the rules that hold
for every standing report. **Read-only against Xero and the banks: it never
creates a payment, never matches, never posts.** It tells the user what to
match.

## Why the bank decides

An AUTHORISED bill says nothing about cash (`docs/BANKING.md`). So the report
looks for every open bill on **every group bank account** in
`config/group.toml` (each account's saved statement CSV up to its
`csv_until`, the Revolut or Mercury API after it, a `csv` account from its CSV
only; never mixed for one movement) and it looks broadly, not for the amount
alone: the **invoice number** written on a bank line; the **amount**, in the
bill's currency or converted at the day's ECB rate when it was paid in another
currency; the **supplier's name** on a bank line at some other amount (a
batch, a tip, a different invoice), only when that amount is in the bill's
range; and one payment covering several bills of a supplier as their sum. A
converted amount with a stranger's name counts only for a card charge on the
entity's own account, and then as a question, never as a match. Then Xero
itself says whether the movement is already spoken for: a spend money of that
amount and date (an intercompany loan leg, or a duplicate), or the payment on
another bill. The reconstructed bank feed is the agent's own to-do list,
cleared when a bill is posted, so it never means Xero reconciled anything.
The answer is one of:

| Status | Meaning | What the reader does |
|---|---|---|
| `UNPAID` | no movement of that amount on any group bank account since the invoice date less `accounts_payable.lead_days` | pay it, or nothing if not yet due |
| `MATCH IN XERO` | the entity's own bank paid it and the statement line is still unreconciled | match the line to the bill in Xero |
| `INTERCOMPANY` | another entity's bank paid it; the line says whether the payer's spend money to the pair's intercompany loan is posted, reconciled, or still to come | reconcile the bill to intercompany by hand (`bill-payments`, `rules/INTERCOMPANY.md`) |
| `DUPLICATE?` | the bank movement is already taken by a spend money or by the payment on another bill, in this entity or the paying one | one document too many: void one, or recode the spend money to this bill, in a bookkeeping run |
| `VERIFY` | several bank lines fit, or a movement fits the amount in another entity but the payee does not read as the supplier, or the supplier appears on the bank at other amounts | look before matching or chasing |
| `NOT VIA BANK` | settled by journal, never by a bank payment: a bill matching `accounts_payable.not_via_bank` (a payroll control bill, say), or an intercompany recharge from another group entity | nothing at the bank |
| `UNVERIFIED` | dated before the bank records begin (`[company].records_from`) | check the statement archive by hand |
| `PAID` | Xero shows it paid | nothing, unless the payment state says `PHANTOM?`: a payment on a bank account not reconciled to a statement line (xero skill rule 7) |

An entity with no bank account of its own has its bills checked against every
group account (whoever funds it; `rules/GROUP.md`). A payroll bill is
`NOT VIA BANK` when it clears from the control accounts (`rules/PAYROLL.md`),
and the bank shows the individual salary transfers instead.

The settings live in `config/group.toml` `[accounts_payable]`: `lead_days`
(how far a card charge may precede its invoice), `leg_window_days` (posting
lag allowed on an intercompany funding leg), `not_via_bank` (pattern and
reason, matched on the contact or the reference) and `unpaid_notify`. Group
entity names (`config.group_name_pattern()`) mark an intercompany recharge;
intercompany accounts are recognised by
`[intercompany_settings].account_name_pattern`.

## Running it

On the server, always: it is the only Xero client.

```bash
.venv/bin/python scripts/bills_report.py refresh all             # the usual run: Xero, banks, workbook, Drive
.venv/bin/python scripts/bills_report.py refresh all --from 2026-01-01
.venv/bin/python scripts/bills_report.py build                   # re-assess and rebuild from the store, no Xero
.venv/bin/python scripts/bills_report.py open opco-us            # the open bills and their status, in the terminal
.venv/bin/python scripts/bills_report.py summary                 # counts per entity and status
.venv/bin/python scripts/bills_report.py notify [--dry-run]      # DM the UNPAID list to accounts_payable.unpaid_notify
```

`refresh` pulls, per entity, every AUTHORISED unpaid bill of any date and every
PAID bill dated from `--from` (default `[company].records_from`, the start of
the bank records; the value is kept in the store), the payments on them and the
entity's spend money since the oldest open bill; reads the statement CSVs, the
bank APIs and `data/bankfeed/`; writes `data/reports/bills_store.json` and
`data/reports/Bills Payable.xlsx`; publishes the workbook to the Drive publish
folder's bills subfolder (`[drive]` in config), one copy overwritten every
build like every other workbook there. `--no-publish` skips Drive; a blank
`publish_folder_id` means nothing is published.

**It refreshes itself and tells the payer.** The scheduled `bills` job runs
after the daily bookkeeping chain (it waits on the chain's lock so it reads
Xero once the day's bills are posted), refreshes, publishes, then `notify`
sends the person in `accounts_payable.unpaid_notify` one DM: the **UNPAID
bills only**, entity by entity, each as supplier, bill date, due date, due
status, amount with currency and invoice number, nothing else. No other status
is in that message: a bill the bank shows paid is not theirs to pay. A blank
`unpaid_notify` sends nothing. An admin asking for the report ad hoc gets a
fresh refresh; whether to DM again is the admin's call, not a default
(`notify <member ID or name>` sends to someone else).

```
unpaid bills, 21 Sep 2026

*opco us*
• Contoso Cloud · 31 Jul · due 30 Aug · overdue 22d · USD 270.00 · INV-1011
```

## The workbook

**Summary**: per entity, open bills, a count per status, overdue, paid bills
whose payment is unreconciled, open amount by currency; how each bank account
was read; the status legend; the **manual actions** list, every open bill paid
from another entity's bank and every duplicate, with the instruction.

**One tab per entity**, in config order: the Reconciled column filtered to
`not reconciled` as the file opens, open bills oldest first then paid bills
newest first. Per bill: status, why and what to do (terse: the reader knows
every bank account was checked), reconciled or not, bill date, due date, due
status (overdue by n days), supplier, invoice number, reference, currency,
total, paid, credited, amount due, the account(s) it is coded to, the line
description, the **bank evidence** (entity, account, date, payee, amount,
source), paid on, paid from, payment state, whether a document is attached,
and a link that opens the bill in Xero.

## Answering

`docs/COMMS.md` governs the message. The workbook is the answer; the message
carries the counts and, under `queries` and `manual`, everything it leaves
with a person. References trailing.

```
bills · 99 open · 4 intercompany · 2 duplicates · refreshed every entity

report
- Bills Payable.xlsx refreshed on Drive (Bills Payable folder)
- unpaid and verified against the bank 71, match in Xero 9, verify 6, not via bank 2

manual
- Tailspin Telecom · 03 Sep · EUR 45.00 · paid HoldCo · recognised OpCo EU · INV-1012: reconcile the bill in OpCo EU to intercompany
```

Never paste the tabs into Slack. A `PHANTOM?` payment state goes in the
message under `queries`: it is a payment the agent must never have created,
and the user decides what it is. Every manual item goes into
`scripts/outstanding.py` in the same step that sends it.

## What this report is not

It posts nothing, matches nothing, chases nobody, and **never creates a
payment**. `DUPLICATE?` and `INTERCOMPANY` are findings for a bookkeeping run
(`xero-bills`, `bill-payments`) afterwards; a `MATCH IN XERO` line is the
user's own work in the Xero reconcile screen. It never reads a bank account
that is not in `config/group.toml`.
