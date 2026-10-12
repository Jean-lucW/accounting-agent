---
name: report-bills
description: The bills payable report. Every bill posted in every entity, paid, open or not yet approved, and for every open bill the reason it is still open, verified against the bank. Use when an admin asks which bills are paid or unpaid, what is outstanding, what needs paying, why a bill is still open, the accounts payable position, the bills workbook, or to refresh the bills report. Read-only; never creates a payment.
---

# Bills payable report

One workbook, one tab per entity, every bill Xero holds (DRAFT and SUBMITTED
bills included), **filtered to the unreconciled ones**: the open bills first
with a **status** that says why the bill is still open and what to do about
it, then any paid bill whose payment is not reconciled to a statement line. Every bill paid and reconciled is in
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
range; and one payment covering several bills of a supplier as their sum (it
must name the supplier and come after the earliest bill less `lead_days`). The
bank settles what it can: another merchant's movement of the amount, or the
supplier's other charges, leave the bill `UNPAID`, never a question. A card
charge under a different merchant name is still the bill only when everything
else agrees: within `stranger_fx_tolerance` at the day's rate, within
`stranger_days` of the bill, on the bill's own entity, held by nothing in Xero,
on the card of someone the bill names, and no other open bill fits it better.
A line naming the supplier at the exact amount may precede a bill of
`named_lead_min` or more (reporting currency) by up to `named_lead_days`, if
nothing in Xero holds it (rent paid ahead); a named transfer within
`named_amount_tolerance` of the bill is the bill (an expense claim reimbursed
at a slightly different rate); a transfer to a person whose reference shares
words with the bill's description counts as a weak match and is never claimed,
since it is also that person's own claim; a booking agent's merchants come
from `accounts_payable.agent_payees`. A line is used once, and the same card
line twice in an export is one payment. Then Xero
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
| `DUPLICATE?` | the bank movement is already taken by a spend money or by the payment on another bill, in this entity or the paying one (not for a supplier in `accounts_payable.never_duplicate`, whose repeat orders at one price are normal) | one document too many: void one, or recode the spend money to this bill, in a bookkeeping run |
| `MONTHLY DEBIT` | the supplier is on `[[accounts_payable.periodic_billing]]` for this entity from a date: one monthly debit settles its bills early the next month | nothing: never in the payer's list, never overdue |
| `VERIFY` | several free bank lines fit the amount and nothing tells them apart, or a movement fits the amount in another entity but the payee does not read as the supplier | look before matching or chasing |
| `NOT APPROVED` | DRAFT or SUBMITTED in Xero. The reason says "do not pay, void" when an approved bill carries the same number, or the same supplier, currency and amount within 7 days; otherwise "approve it before paying" | approve it, or void the copy, in a bookkeeping run |
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
reason, matched on the contact or the reference), `periodic_billing`
(entity, supplier pattern, from date, reason), `never_duplicate` (supplier
patterns), `agent_payees`, the tunables `named_lead_days`, `named_lead_min`,
`named_amount_tolerance`, `stranger_fx_tolerance`, `stranger_days`, and
`unpaid_notify`. Group
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
.venv/bin/python scripts/bills_report.py notify [--dry-run]      # the sectioned DM to accounts_payable.unpaid_notify
.venv/bin/python scripts/bills_report.py daily [--dry-run]       # the scheduled job: refresh, build, publish, DM, channel post
```

`refresh` pulls, per entity, every AUTHORISED unpaid bill of any date, every
DRAFT and SUBMITTED bill, and every PAID bill dated from `--from` (default `[company].records_from`, the start of
the bank records; the value is kept in the store), the payments on them and the
entity's spend money since the oldest open bill; reads the statement CSVs, the
bank APIs and `data/bankfeed/`; writes `data/reports/bills_store.json` and
`data/reports/Bills Payable.xlsx`; publishes the workbook to the Drive publish
folder's bills subfolder (`[drive]` in config), one copy overwritten every
build like every other workbook there. `--no-publish` skips Drive; a blank
`publish_folder_id` means nothing is published.

**It refreshes itself and tells the payer.** The scheduled `bills` job runs
after the daily bookkeeping chain (it waits on the chain's lock so it reads
Xero once the day's bills are posted), and `daily` refreshes, builds and
publishes the workbook, sends the person in `accounts_payable.unpaid_notify`
one DM (chunked to Slack's size limit) and posts the run to the channel in
`[slack]`: a headline and the detail in its thread. The DM has four sections,
each entity by entity, each line supplier, bill date, due date, due status,
amount with currency, invoice number last:

- `to pay`: the UNPAID bills, with the total per currency. A `MONTHLY DEBIT`
  bill is never here.
- `not approved in Xero`: the drafts and what to do about each.
- `check: may already be paid`: the VERIFY bills.
- `to match in Xero`: `data/reports/reconcile_list.json`, built earlier in the
  chain by `scripts/reconcile_list.py`; one line says so when it is missing or
  not from today.

When the Xero pull fails (a daily rate limit, say), `daily` rebuilds from the
last pull with today's bank, says so at the top of the DM and in the channel,
and still sends both. A blank `unpaid_notify` sends no DM. An admin asking for
the report ad hoc gets a fresh refresh; whether to DM again is the admin's
call, not a default (`notify <member ID or name>` sends to someone else).

```
accounts payable, 21 Sep 2026

*to pay*
total USD 270.00

_opco us_
• Contoso Cloud · 31 Jul · due 30 Aug · overdue 22d · USD 270.00 · INV-1011

*not approved in Xero*

_opco us_
• Example Supplier · 12 Sep · USD 55.00 · draft in Xero: approve it before paying, or void it · INV-1015
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
- unpaid and verified against the bank 71, match in Xero 9, verify 6, not approved 3, monthly debit 2, not via bank 2

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
