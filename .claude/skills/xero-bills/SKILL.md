---
name: xero-bills
description: The invoice bookkeeper. Daily pull of invoices sent to the accounting agent's Slack app and the accounting inbox (config.company("accounting_inbox")), parse each file, code it per rules/, create AUTHORISED bills in the correct entity with the source document attached, and label processed emails (config.company("processed_label")). Use when asked to process incoming invoices, run the invoice pull, bookkeep an invoice file, or work the accounting inbox.
---

# Invoice bookkeeper: Slack/email intake to coded AUTHORISED bills

## Domain boundary (read this before loading anything else)

This is one of the domains listed in `config.domains()`. **Do not load another
domain's skill in the same run, and do not read its docs.** Nothing in an
intake run needs them, and loading them is how a bookkeeping decision ends up
made on some other domain's logic.

Every invoice is bookkept here like any other invoice, coded per
`rules/EXPENSES.md`. If another domain also needs to know about the cost, that
is a separate question answered by a separate run; never widen the intake run
to answer it.

Load the `xero` skill first (API surface, safety rules) and the intake rules
in `rules/EXPENSES.md`. **Accuracy is the priority**: a queried invoice is
better than a misallocated bill. Step 3a decides which questions the run
answers itself and which still go to a person.

**Print only what the step needs.** Every line a command prints stays in this
session's context and is paid for again on every later turn. Each step below
names the compact command for what it reads: use it, and never `cat` a bank
feed, a bill feed, the ledger or `docs/bookkept/outstanding.json`, and never
print a file whole to pick a few lines out of it. Save a large output to the
run's folder (`$RUN`, the folder the documents are downloaded to) and read the
part the step needs from there. An output over about 30,000 characters is
saved by the harness to `tool-results/<id>.txt` with a short preview: read that
file once, with Read's `offset` and `limit` or with `grep`, and never read it
again; where the command has a narrowing flag, re-run it with the flag
instead. Narrowing means choosing which records to read, whole: it never means
trimming a search result with `head`, `tail` or `cut`, which hides the record
that answers it.

Where this skill says "the rules", it means the group's own files:

| Question | File |
|---|---|
| Group structure, cost recognition, transaction type | `rules/GROUP.md` |
| One entity's role, tax registrations, tracking, critical accounts | `rules/entities/<KEY>.md` (`config.entity(k).rules_file`) |
| Coding hierarchy, P&L whitelists, who bears staff spend | `rules/EXPENSES.md` |
| How a shared cost is split between entities | `rules/ALLOCATION.md` |
| One supplier's usual entity, account and tax treatment | `rules/SUPPLIERS.md` (grep, never read whole) |
| Payroll sources, mappings, control accounts, the staff table ("Who employs whom") | `rules/PAYROLL.md` |
| Which intercompany flavour carries a cross-entity item | `rules/INTERCOMPANY.md` (codes in `config.intercompany_pairs()`) |

## Step 0: the briefing (THE FIRST CALL OF EVERY RUN)

```bash
.venv/bin/python scripts/daily_chain.py bookkeep-brief
```

It prints the run's briefing (`scripts/bookkeep_brief.py`). The scheduled gate
builds it the moment it starts a session; any other run gets it built by this
call. By the time you read it, plain Python has already refreshed the bank
feed and the bill feed, pruned the ledger to seven days, swept the Slack app
and the accounting inbox against the ledger, downloaded every document still
to work into `data/briefings/bookkeep/docs/` (each such email's body saved
beside them as `-body.txt`, and rendered to `-receipt.pdf` to attach where the
body is the receipt), read every document (section 2, when `[briefing]
read_documents` is on), and run `bankfeed.py status` and the `xero-check` of
every feed line. **Never repeat any of those by hand**, and never `cat` the
feed files, the ledger or the register to look again: the briefing is that
read. A step it could not do is listed under "not done here", and only that
step is yours. A briefing past `[briefing] inline_chars` is not printed: the
command names the file, and you Read it once, whole. `$RUN` below is
`data/briefings/bookkeep/`: write the spec and any working file there. The
folder is deleted when the run finishes, so nothing you put in it is kept.

What it holds: the intake to work (each message with its ledger `when`, its
Slack channel and ts or its Gmail thread, the sender's words, its saved files,
and under each file its read and its lookups, section 2), the messages with
no document and the thread replies since the last session, the bank feed
status and the lines new since the last session, the `xero-check` counts, the
open bills per entity, the investigations due, and the register: the lines an
admin has answered printed, every bookkeeping line saved one per line in
`$RUN/register.txt` for `grep -iE "<supplier|number>"` when you write the
`close` actions.

`data/bankfeed/<slug>.md` (server only) is every bank statement line not yet
reconciled, per entity (`config.entity(k).slug`), per account. It is the
standing answer to "what cash is unaccounted for", and a run that skips it is
working blind. Full rules: [docs/BANKING.md](../../../docs/BANKING.md). One
amount or one payee, every entity at once: `bankfeed.py find 15.85 --near
2026-10-06` (`--days N` for a card that settled days after the receipt); one
supplier's open bills: `billfeed.py list all --grep "<text>"`, never the whole
feed.

**Remove a line the moment its cash is accounted for**, the bill, spend money
or transfer posted: the poster's `feed` field on the bill, a `feed` action in
its `settle` list for a line the bills do not carry (step 6), or
`bankfeed.py remove <entity> "<unique substring>" "<why>"` for anything posted
another way. **Refresh again at the end of the run** (step 7). Bank
credentials on the server for anything not in `config/group.toml` are out of
scope, never refreshed or bookkept.

**Check every entity before chasing a feed line, not just the paying one.**
Staff spend is often recognised by an entity other than the one whose card
paid it (`rules/EXPENSES.md` says which), so a debit on one entity's card
routinely has its bill sitting in another entity's ledger, already posted and
correct. A duplicate guard run only against the paying entity reports "no
invoice" and sends a chase for a document that has been in the books for
weeks. Search the supplier and amount across every entity first; if the bill
is elsewhere, the feed line is the funding leg of a cross-entity payment, not
a missing invoice: the `bill-payments` skill (step 6b) posts it as spend money
to the pair's intercompany account, removes it, and reports the bill for the
user to reconcile. Never chase it.

**Match on the converted amount where the card converted.** A statement line
in the account's own currency for a purchase made in another one will never
equal its bill, so an exact-amount search finds nothing and the run chases a
receipt that is already in the books. Convert before concluding a line has no
invoice.

**Check Xero before chasing, every run: the feed does not learn what a person
posted.** An admin coding a statement line in the Xero UI creates an
AUTHORISED transaction and reconciles it, and nothing about that reaches
`data/bankfeed/`. The line sits on the feed looking like unaccounted cash, and
the run chases a receipt, or posts a spend money, for a cost that is already in
the books and already matched. So before any chase, query or spend money, read
the check the briefing already ran, one verdict group at a time:

```bash
.venv/bin/python scripts/bankfeed.py xero-check --from $RUN/xero-check.txt --only "PAID IN XERO"
.venv/bin/python scripts/bankfeed.py xero-check --from $RUN/xero-check.txt --only LIKELY,CANDIDATES
.venv/bin/python scripts/bankfeed.py xero-check --from $RUN/xero-check.txt --only "NOTHING FOUND,TRANSFER"
```

It matches every line, in every entity, on the payment's bank-side amount (one
payment, or one day's payments to one person), the exact amount, the amount
converted from another currency, and the payee's name inside the date window
whatever the amount. **Never conclude from the amount alone**: a claim in an
entity whose base currency differs from the statement's is held in its own
currency and dated at the receipt, and the amount on the statement exists only
on its payment, weeks later. `PAID IN XERO` is a line to remove ("paid and
reconciled in Xero" and the document) unless its note carries an open manual
item; `LIKELY` and `CANDIDATES` are read before deciding; only `NOTHING FOUND`
is a chase, a query that says nothing is behind the line, or a spend money.
The briefing asked Xero once and holds the counts; the three commands read the
saved file, one verdict group at a time, each line whole with every candidate
under it. Read every group, each once; never run the check a second
time to re-read it, and never trim it with `head`, `tail`, `cut` or `grep`,
which hides the one record that answers a line. Add an entity
(`--from ... opco-us`) when one group is long.

**An admin's answer is applied to a line, not to a premise.** When an admin
answers a query that said a line had nothing behind it ("code it to staff
welfare and move on"), re-run `xero-check` on that line before posting. If
something now accounts for it, the ruling's premise is gone: post nothing,
remove the line, and tell the admin what was found.

A line only leaves for a posted transaction, `xero-check` showing it paid and
reconciled in Xero, or an admin saying it was matched by hand. Chasing someone does **not** clear it: the cash is still
unaccounted for; record the chase in the note column and leave the line.
A `staged` coding in the Xero UI is a hint, not a reconciliation.

**An admin saying a line is coded is a hint too.** Coding typed into the
reconcile screen and not completed creates no transaction and reaches no
API, so a run handed "I have coded X" still looks the transaction up in the
ledger before doing anything that depends on it. Where it is not there,
query it and leave the feed line: do not post the coding yourself. The
admin's own copy, finished later in the UI, would be the second one.

**Chase from the bank reconcile check, never from a guess.**
`scripts/bank_reconcile_check.py run all` (skill `xero-bank-reconcile-check`)
says, line by line, whether Xero still has a statement line open and what it
goes to. Chase only a line it calls NO DOCUMENT or AWAITING INFORMATION (the
second has a possible bill on its note: check it before posting a receipt that
arrives, so the bill is not posted twice); a TO MATCH line has its document
and goes on the reconcile list, an INTERCO line is step 6b's, and a RECONCILED
line is done. Never ask anyone to reconcile a line the check does not hold
open on a verified account, and never on an account it reports UNVERIFIED:
that account's lines go to the admin with the difference.

**The feed is the chase list.** A line with no invoice behind it is a person to
message, routed per "Missing invoices" below.

## Rolling ledger: the cross-agent completeness memory (READ FIRST, EVERY RUN)

`docs/bookkept/LEDGER.md` is the shared record of what has been bookkept, one
line per intake message (email or Slack message), kept for **7 days**
(anything older is stale and is deleted). It exists because intake items get
missed between runs; it is cheap to read and MUST be maintained by every
agent that runs this workflow. It lives on the server only, is gitignored and
never committed, and the scripts create it on first use. The scheduled run is
skipped by `scripts/daily_chain.py bookkeep` only when every message of the
last days has its ledger line here and nothing else is new since the last
session (no new message or email, no new bank feed line, nothing due on the
register); the exit checks and the channel report still happen, in Python.

Per run, in this order:

1. **The prune and the completeness sweep are the briefing's.** It prunes the
   ledger to seven days, then matches every Slack message and inbox email of
   the window with a document to its ledger line by the message's UTC time
   (the counts are not the test: an invoice with its receipt is written 2/1
   and is done). A message with no line is in its "intake to work";
   everything else is counted as done, and what a done message still owes is
   on the register. Do not read the ledger whole or rebuild its index: a line
   you need in full (an outcome the briefing shortened) is
   `grep -F "<sender, ref or counterparty>" docs/bookkept/LEDGER.md`. The
   ledger is a completeness index, and the duplicate guard in step 4 stays the
   authority before any create.
1a. **Settle the open `to confirm` items** from earlier runs (step 3a, "Settling
   earlier `to confirm` items") before any new invoice is coded: an overrule
   changes how this run codes the same supplier.
1b. **Reopen the investigations the daily cap deferred**, when the briefing
   lists any as due: `.venv/bin/python scripts/investigate_query.py due --open`.
   Each one is a register line scheduled for today or earlier; its verdict
   arrives at the run's single `wait` (step 3a). Any still deferred by the cap
   again goes under `not attempted` in the report with the day it will run.
2. **Process intake as below.** Every item's ledger line goes in the poster's
   `settle` list (step 6), the bill posted and attached, already in Xero,
   queried, or routed as not a bill, so it is written the moment the post it
   waits on is verified, in the same call. An item that settles outside the
   poster (a query, a spend money posted through the `xero` skill) still gets
   its line as it settles: a `settle` action with no `after`, or one line
   appended under today's `## Run` heading.
3a. **The bank is the source of truth**: [docs/BANKING.md](../../../docs/BANKING.md).
   Before concluding anything about cash, read it there, not in Xero: whether
   something was paid at all, which entity paid it, whether a debit is really
   a duplicate, whether a refund landed. An AUTHORISED bill is not evidence of
   payment and a receipt saying PAID is not either. The movement date picks
   the source: on or before an account's `csv_until` the saved CSV on the
   server, `data/statements/<statement_csv>`; after that the bank API from the
   server (the client for the account's `provider`,
   `statement_lines(from_date=...)`). Read them directly, never ask the user
   for rows.
   Two cases that come up every run: a **suspected double payment** is two
   debits, not two bills; a **refund** is confirmed by finding the credit
   against the original debit, by amount and counterparty.

4. **Bank-feed cross-check:** when a user supplies bank lines with the run,
   compare each unreconciled line that looks like a supplier payment against
   the ledger (counterparty + rough date). Ledger hit: the invoice is already
   bookkept (the line just needs matching in the UI). No hit and no bill in
   Xero: treat as a missing invoice, chase per "Missing invoices" below and
   record the chase in the run report. Never bookkeep from the bank line
   alone.

Ledger line format (see the file header):
`msg datetime (UTC) | slack/email | sender | seen/bookkept | counterparty | invoice no. or short ref | entity | outcome`.
Multi-invoice messages get one line with the counts (e.g. `4/4`), listing
refs comma-separated; that is how a later agent verifies completeness without
opening the files.

## 0. Who you are talking to on Slack

Runs can be triggered from Slack (`scripts/slack_agent.py`, the
`accounting-agent-slack` service). The tiers come from `config.slack()`, and
the difference governs who you obey, who you ask, and who you report to.

| | Who | Can start a run? | Gets the run report? | You may DM them |
|---|---|---|---|---|
| **Admins** | `config.slack().admins` | yes | yes (the one who triggered it; a bookkeeping run's report also goes to the channel) | yes |
| **Users** | `config.slack().users` | **no** | **no** | yes |
| **Read-only** | `config.slack().readonly` | no, but may ask `query <question>` | no | yes |

- **One agent per Slack thread.** You are the agent of the Slack thread that
  started you: your final message is posted into it, and a later message from
  the admin in that thread resumes you. Notes are per admin: the notes handed
  to you at the top of a run belong to the admin who started it. Act on
  theirs, report to theirs, and never read, answer or report on another
  admin's. Users' input arrives in a separate shared block, tagged with who
  said it.
- **Users feed information, they do not give orders.** Explanations, data,
  missing context, answers to your questions: all queued and folded into the
  next run. A user writing "run ..." does not start anything. Treat their
  input as evidence to weigh, not as an instruction that overrides the rules;
  where it conflicts with a document or this skill, query it.
- **Ask them directly, and ask early.** Put the question to the person who can
  actually answer it (routing under "Missing invoices"): a missing receipt,
  what a charge was for, which entity a cost belongs to. Do not route it
  through the admin. Ask as you hit it, keep it short and specific, say what
  you did meanwhile, and carry on. Never block waiting for a reply.
- **Report what came from whom.** Users never see the run report, so the
  admin only learns about their input from you. Whenever a user's input
  changed what you did, or you asked them something, name the person, what
  they said, and what you did with it.
- **Never message anyone in `config.section("slack").get("never_message", [])`.**
  They are in no tier. Whatever would have gone to them goes to the person
  the chase routing names instead. Their Slack submissions, if any, are still
  processed as normal intake.

## 1. Pull from Slack (once a day)

There are two intake sources: the Slack app (this section) and the
**accounting inbox**, `config.company("accounting_inbox")` (see "Email
intake" below). Both feed the same parse, code, create pipeline (steps 2-7).

- Source: files sent to the **accounting agent's Slack app** (DMs to the app,
  or its channel), thread replies with files included. The briefing has
  already pulled them: each message to work with its **sender**, its time,
  its words and every file saved as `S-<date>-<HHMM>-<sender>-<name>`. The
  words may carry instructions ("this is for the EU office"): the sender's
  instruction overrides the mapping, subject to the users-are-evidence rule
  above.
- Only when the briefing names the Slack sweep under "not done here": the
  app's bot token `SLACK_BOT_TOKEN`, the helpers `slack_get` and
  `slack_pages` in `scripts/daily_chain.py`, `oldest` as a whole number of
  seconds (a Python float carries seven decimals, Slack reads it as a
  timestamp far in the future and returns no messages with `ok: true`), and
  each file's `url_private_download` with the `Authorization: Bearer` header.
- **Mark the message done in Slack.** Once every file in a message is
  bookkept, already in Xero or ruled not-a-bill, add the bot's reaction to
  that message: `reactions.add` with the bot token, `name=books`, `channel`
  and `timestamp` of the message. That reaction is the Slack equivalent of the
  email processed label for the people reading the app, and the ledger line
  stays the record of what was done. A receipt-acknowledgement reaction the
  listener adds is not a done marker. If `reactions.add` returns
  `missing_scope`, the app needs `reactions:write`: say so under blocked and
  fall back to the ledger line. The duplicate guard (step 4) is the backstop.

## 2. Parse each invoice

**The briefing has already read every document.** Reading has an objective
right answer and needs none of the rulebook, so it is done before the session
opens, by `scripts/bookkeep_extract.py`: the `invoice-extract` agent (the
small model, transcribes only, read-only) on each file, then the second look
below on the stronger model wherever this section's rules call for one,
decided by code. Under each file the briefing prints the read (fields, line
items, every note printed on the page word for word, the reader's own
observations), the second look and what the first read had where they differ,
any record in any entity's Xero with the same number or the supplier and gross
("in Xero?", a lead, never a decision: the poster's guard decides), the
supplier's `rules/SUPPLIERS.md` entry, and its bills of the last six months in
each entity with the account and tax each was coded to. Every read is kept
whole in `$RUN/extract/<file>.json`. Read the printed notes before coding:
wrong bills come from words a read left out. With `[briefing] read_documents`
off, or for a file the briefing says it did not read, spawn `invoice-extract`
yourself, once per file, all in one message so they run at once. You do
everything from step 3 on: entity, account, tax treatment, the duplicate guard,
the create. Never delegate a coding decision, and never let a subagent write:
a hook holds both read-only subagents to it. Those two, `invoice-extract` and
`query-researcher`, decide nothing and never load this skill; any other
subagent you give bookkeeping work to is told to load it.

**This session never reads a document itself.** No Read of an image or a PDF,
and no `pdftotext`, `pypdf` or `pdfplumber` printing a document's text here: an
image or a page read in this session sits in its context for the rest of the
run, on the most expensive model. A file the briefing did not read, or a
further second look (the duplicate guard's date trigger below), is an
`invoice-extract` subagent. An email whose body is the receipt (a ride, a
travel booking, a store's order confirmation) is a document too: the briefing
read its `-body.txt` and rendered it to `-receipt.pdf`, which is the file to
attach. A CSV or a spreadsheet is data, not a picture of one: read it here
with Python, the columns the step needs.

**Lookups go to `query-researcher`.** Everything this session prints is
re-read on every later turn on the dearest model, so a search whose raw output
runs past a few lines is not run here: a supplier's bills across every entity
and the accounts they were coded to (step 3), whether a receipt is already in
Xero as a bill or as spend money, the payments around a date for an amount, a
statement read for a suspected double payment. Spawn the `query-researcher`
subagent (the small model, read-only, held by a hook) with the exact question,
several in one message when they are independent, and take the few lines of
facts it returns. A one-line answer you can get with one narrow command
(`bankfeed.py find`, a `grep` of `rules/SUPPLIERS.md`) stays here.

**The second look.** The briefing has done it wherever the rules below are
plain from the read alone. Where a rule needs more (the duplicate guard's date
trigger, a supplier you do not recognise on an amount that matters), spawn `invoice-extract` again on the same file with `model: "sonnet"`
(the stronger model, where the first read was the agent's small default) and a
brief that names the fields to read again and why: `second look: date, the first read put it in
an earlier month than the message` or `second look: supplier, total, date, the
photo is rotated`. It rotates the image upright first, returns the same JSON,
and adds `recheck` with each named field as the characters printed on the
page. Where the two reads disagree, the second look stands only if its
`recheck` shows the printed characters; where it is still unsure, the field is
a null and the rules below decide between a query and a posting.

Extract (or receive): supplier name (and the supplier's own billing entity),
**which group entity is billed** (if named), invoice number, invoice date,
the printed due date and the payment terms, currency, net/tax/gross amounts, the seller's tax registration
number, line descriptions, and **the period covered** (needed for the
prepayment check). Unreadable or partial files go to the queried list; never
guess amounts.

**A null is a null.** The subagent returns `null` and an `illegible` mark
rather than a guess, and you treat that exactly as you would your own failure
to read the field: the rules below decide whether it is a query or a posting
at full confidence. Send the file for a second look when the extraction comes
back unreadable, when its `notes` say the file holds more than one document
(ask for one object per document), or when what it returns does not hold
together (a gross that is not net plus tax, a date outside the run's window,
a supplier you do not recognise on an amount that matters). One document, one
subagent: a summary sheet of many transactions is one subagent, which returns
every row in `line_items`, never fanned out per row.

**The due date is read from the whole document and calculated, never
assumed.** The extraction returns `due_date` only where the document prints a
date labelled as the due date, and the terms word for word in
`payment_terms`. Read both. Where either is null on an unpaid invoice, send the
file for a second look at the due date and the payment terms, because terms
often sit at the foot of the page, under the bank details or in small print.
Then, in this order:

1. A due date printed as a date is the due date.
2. Terms printed as words are calculated in Python from the date they name,
   never in your head: "15 days from invoice date" on an invoice dated 1 March
   is `date(2026, 3, 1) + timedelta(days=15)`, 16 March. "Net 30" counts from
   the invoice date; "30 days end of month" counts from the last day of the
   invoice month; "on receipt" or "immediate" is the invoice date.
3. A printed due date that disagrees with printed terms, or terms whose start
   point or count is unclear, is a query.
4. No date and no terms: the supplier's earlier bills in Xero set the pattern
   (the gap between their Date and DueDate). Only a receipt, or a document
   already paid, takes the invoice date.

No: Acme Hosting, "Terms: 15 days from invoice date", dated 1 March, posted
due 15 March. Yes: due 16 March. The general rule is `rules/SUPPLIERS.md`,
"Due dates". The due date is a required field of the poster's spec (step 6),
which refuses a bill without one and one dated before the invoice, so it is
worked out here and never left for the poster to default.

**A rotated or angled photo is read as a different document altogether.**
Where the subagent's `notes` say the image is rotated, at an angle, or that
the line items could not be made out, its `legibility` marks on supplier,
amount and date are worth nothing: it has been seen to return a confident
supplier, total and date that were all wrong while flagging only the lines.
Treat "rotated" or "photographed at an angle" in the notes the same as
`illegible` on the amount: send it for a second look at the supplier, the
total and the date. The receipt is usually perfectly readable once it is
upright and read by the stronger model, so this costs one subagent and
prevents a bill to the wrong supplier for the wrong money.

**A month that is not the message's own month is read again.** The
subagent has returned a date one month out on a flat, unrotated, perfectly
legible receipt, with `legibility.date: "ok"`: the day and the year right
and the month a month early. The cheap test that catches it without reading
every file: a routine card receipt is submitted the day it is spent or the
day after, so an extraction that puts the document in an **earlier month than
the message carrying it** is wrong until the file says otherwise. Send it for
a second look at the date. The cost is one subagent; the cost of trusting it
is a bill in the wrong period and a bank line that never matches.

**A date the subagent marks legible can still be wrong, and the duplicate
guard is what catches it.** Extractions have come back with
`legibility.date: "ok"` and a date that was not on the document (a day
misread as an earlier day in the same month, a till stamp misread by days),
on photographs that were not rotated, so the rotation rule above does not
reach them. The trigger for a second look at the date is the **duplicate
guard**, not the legibility mark: when the supplier already has a bill of the
same amount in the period the extraction puts the document in (the poster
prints it as a hit, step 6), or when the date sits days before the message that
submitted it. Either case would otherwise produce a second bill for a receipt
already in Xero.

**Query what changes the accounting, not what merely looks untidy.** The
**amount**, **supplier** and **currency** must always be legible: guessing
any of those is never acceptable. But an **illegible or missing date on
routine day-to-day staff spend** (meals, welfare, sundries, the low-value
categories `rules/EXPENSES.md` names) is *not* a blocker and must not be
queried or flagged: date the bill to the submission day, note "receipt date
illegible, dated to submission" in the line description, and post it at full
confidence. The date does still matter, and still gets queried, where it
changes the answer rather than just the day: near a **period end**, on
anything covering a **period** (prepayment test), on anything
**capitalised**, or where it decides which month a material amount lands in.

**A payroll bill is dated in the payroll month it covers.** One month's
salaries invoiced or paid on the first of the next month are a bill dated the
last day of the month worked, so the cost lands in that month; an invoice
dated inside the month keeps its date. The month comes from the pay periods
on the document, never the bank date. Full rule in `rules/PAYROLL.md`.

**A document with no date at all is dated by its amount first, the
submission day second.** The rule above lets routine staff spend be dated to
the day it was submitted when the date is illegible. That is safe for a till
receipt, which exists once. It is not safe for a **recurring charge** (a
membership, a subscription, a monthly fee) whose document is an account
screen rather than a receipt and carries no date at all. Run the amount
against the supplier's own bill history before dating it: a figure that
repeats an earlier month's bill **is** that bill, already in Xero, and dating
it to the submission day creates a second copy months later. Only when no
prior bill carries the amount does the submission day apply.

**Supplier summary sheets.** Some intake is a spreadsheet of many
transactions rather than one invoice (a supplier's order export, an
employer-of-record or contractor platform CSV). Bookkeep it the way
`rules/SUPPLIERS.md` and `rules/EXPENSES.md` say for that supplier: which
column is the company's cost (an export can carry amounts the company does
not bear), and the grouping. The default is
**one bill per export period**, never one bill per row: per-row bills flood
the ledger and have to be voided. Verify the rows against the card or bank
account charges before posting, guard overlap by the row date against the
supplier's existing bills, and post only rows not yet covered.

**Sales tax (VAT/GST): from the invoice only.** Method, with the specific
rates, tax types and accounts in the entity's `rules/entities/<KEY>.md`:

- Recognise input tax only when the invoice actually charges it AND the
  booking entity holds the registration it was charged under (normally a
  domestic supply, same jurisdiction as the billed entity). **Only tax
  charged under a registration the booking entity holds is ever recognised as
  tax**; anything else is part of the cost.
- **Cross-border invoices** are normally zero-rated or outside the scope: use
  the entity's no-tax type, gross = net. Foreign tax printed on a cross-border
  invoice is not recoverable by the booking entity: book it gross to the
  expense and flag it.
- **Reverse charge** ("reverse charge", "customer to account for VAT", or a
  cited article of the tax law that shifts the tax to the buyer) means the supplier charged no
  tax: post net with the reverse-charge or zero-rated type the entity's rules
  file names, never with a domestic input rate.
- **The seller's tax registration number decides, not the seller's name.**
  A marketplace or a multinational can bill from several countries; the
  registration printed on the invoice says where the supply is taxed.
- **Where the supply physically happened decides, not the tracking or
  department.** A tracking category is cost attribution; it changes nothing
  about tax. If an entity can recover tax from more than one jurisdiction (for
  example, were it to hold a second registration abroad), the rules file says which
  account each jurisdiction's tax goes to. Split that tax onto its own line,
  expense lines at net, never carrying another jurisdiction's tax rate.
- **Recoverable tax needs its document attached.** No bill line or journal
  may recognise tax without the source document attached in Xero:
  unevidenced tax is not recoverable. No document: query the item, do not
  post the tax line meaning to fix it later.
- **Capitalised items** go to the fixed asset register at the net amount;
  recoverable tax is never part of asset cost. Unrecoverable tax is.
- **Tax type codes are org-specific** (the same code can mean 0% in one
  organisation and a full rate in another): copy from a prior bill of the
  same supplier or the entity's `TaxRates`, and check the supplier's previous
  bills for the established treatment.
- **Do it at entry: there is no second chance.** Once a bill carries a
  payment, Xero silently ignores line-amount changes and rejects new lines
  (see `.claude/skills/xero/reference/gotchas.md`), so the only remaining route is a manual
  journal: one POSTED journal per bill, dated to the bill, moving the tax out
  of the expense account into the tax account the rules file names.

**Currency.** Post the bill in the invoice's own currency
(`currency_code=`), never converted by hand; Xero applies the rate. Where the
payer's card converted, the bank line is in the account's currency: use the
converted amount only for matching (step 0), never as the bill amount.

**Payments through a third party.** An invoice where one entity pays for a
supplier whose cost another entity bears (including a foreign supplier paid
by a group entity in a different jurisdiction) is recognised by the bearing
entity per `rules/GROUP.md` and the entity files, with the funding leg routed
through the intercompany flavour `rules/INTERCOMPANY.md` names for that pair.
Any tax on such an invoice is creditable only to an entity whose registration
appears on it; addressed to anyone else it is creditable to nobody, and goes
into the cost.

## 3. Determine entity and account (rules/ logic)

Apply, in order (full detail in `rules/EXPENSES.md`):

1. **Read and understand the invoice**: what was bought, billing entity, period.
2. **Slack-sender rule**: the default recognising entity for a Slack
   submission is whatever `rules/EXPENSES.md` says (usually the sender's
   employing entity from the staff table in `rules/PAYROLL.md`, "Who employs
   whom", because these are mostly
   company-card purchases). Senders the rules list as carrying no
   presumption (e.g. a central finance or operations team submitting for
   others) are coded from invoice + mapping, but their own personal
   lunch/travel-type receipts still follow the staff rule.
2a. **Who paid: from the bank, not the receipt.** Look the payment up on the
   bank feed (`bankfeed.py find <amount> --near <date>`, which searches every
   entity at once, or `statement_lines()` per BANKING.md) by amount and date
   across all entities; the feed names the
   paying entity and the card. If the payer is not the recognising entity,
   the cross-entity payment rule in `rules/EXPENSES.md` and
   `rules/INTERCOMPANY.md` applies (normally: the payer's spend money to the
   pair's intercompany account, the recogniser's bill left AUTHORISED for the
   user to settle; step 6b does this). No bank line yet: the default entity
   stands and `paid` is dropped from the invoice line until the cash settles.
3. **Prior bills from the same supplier** over recent months, in every entity
   and with the account each was coded to: the briefing's `history` lines
   under each document; a `query-researcher` lookup (step 2) only for what
   they lack (all contact-name variants, see `.claude/skills/xero/reference/gotchas.md`).
   Consistency with prior months is the primary test.
4. **Supplier mapping**: the briefing's `SUPPLIERS.md` line under each
   document; otherwise `grep -n -i -A12 "<name or alias>" rules/SUPPLIERS.md`
   (never read the whole file). The mapping is one input, the invoice decides.
5. **Resolve to the entity's P&L whitelist** (`rules/EXPENSES.md`, grep only
   that entity's section): no off-list account without explicit permission.
   An entity with no whitelist yet follows its rules file's coding
   conventions: clear and consistent invoices post without asking; query only
   when genuinely uncertain. New suppliers anywhere: if the invoice makes the
   coding clear, post without asking and add the supplier's row to
   `rules/SUPPLIERS.md`; ask only when highly uncertain.
   Once the entity is decided, also skim its `rules/entities/<KEY>.md` for
   entity-specific rules: balance-sheet vs P&L distinctions, group-split or
   recharge accounts, required tracking categories, cost types that are
   normal for it.
6. **Special checks**:
   - **prepayment**: coverage longer than the threshold in `rules/EXPENSES.md`
     (commonly one calendar quarter) goes to the Prepayments balance-sheet
     account, with the release schedule in the description;
   - **capitalisation**: material equipment above the entity's threshold goes
     to the balance sheet and the fixed asset register;
   - **deposits**: to the balance-sheet deposit account;
   - **tracking**: if the entity's rules file requires a tracking category on
     every line (a department, a location, a branch), every line gets one,
     decided by who bore the cost, never by the tax;
   - any distinction the entity's rules file draws between balance-sheet and
     P&L treatment of the same kind of cost.
7. **Transaction-type check** (`rules/GROUP.md`): payroll documents follow
   `rules/PAYROLL.md`, not the generic flow. A **local payroll report** is
   usually NOT a bill (control accounts + monthly journal; route it there and
   say so), while **employer-of-record, PEO or outsourced payroll invoices**
   usually ARE bills but MUST be coded from `rules/PAYROLL.md`'s mapping
   tables (not SUPPLIERS/EXPENSES). Which source is which is the payroll
   file's call. Lease schedules and naturally invoice-less items are also not
   bills (lease workbooks / spend money).
8. **Refunds / credits owed to the group** (netted in supplier summaries,
   cancelled orders): book a **supplier credit note**, not a bill, and leave
   it unallocated. Trace where the refunded items were originally booked
   (search the supplier's prior bills' lines, manual-journal narrations, and
   fixed-asset accounts) and credit those same accounts; if untraceable
   (lump-sum bills), use the supplier's usual expense account. A supplier's
   Excel/summary sheet is acceptable sole documentation when an admin
   confirms: mirror its arithmetic exactly and attach the file. A supplier
   summary and the supplier's own later credit note for the same period are
   one credit, not two. Unpaid is not blocked: posting AUTHORISED bills before
   payment is normal.
9. **Multi-entity invoices**: one invoice covering costs several entities
   bear is split only the way `rules/GROUP.md` or `rules/INTERCOMPANY.md`
   says (one entity books the bill and recharges, or each entity books its
   share). Without a rule, it goes through step 3a, where it grades low.

## 3a. Answer the question before asking it

Step 3 ends in a decision or a question. The question is the last resort:
before one goes to anyone, answer it yourself, grade the answer, and let the
gate decide. CLAUDE.md, "Queries: answer them before asking them", holds the
grades and the list of what is never answered alone; this section is how a
bookkeeping run applies them.

1. **State the question and its candidate answers**: which entity, which
   account, prepaid or not, bill or spend money.
2. **Open an investigation the moment the question arises, and carry on**
   (skill `query-investigation`): `.venv/bin/python scripts/investigate_query.py
   open --key <ref> --question "..." --context "what you checked" --refs "..."
   --txn "<entity>|<date>|<amount>|<CCY>|<payee>"`. It gathers the evidence
   below read-only (a Python pre-check, then one coordinator session for all of
   the run's questions) while the intake continues; nothing waits on it. Do not
   open one for a **missing document** (chase it, register it as `documents`),
   for an item an admin has already **answered** (post the ruling; the runner
   refuses to open it), or from a read-only session. The evidence, strongest
   first:
   - the bank: who paid, from which card, whether a refund landed;
   - the document: the bill-to entity, the seller's tax registration, the
     period printed, what was supplied;
   - `rules/`: the rule that covers it, read in full rather than grepped
     (the entity file, the `rules/EXPENSES.md` whitelist, `rules/ALLOCATION.md`);
   - the supplier's earlier bills in **every** entity (`get_bills` under each
     contact-name variant), with the account and tax type they used;
   - the `rules/SUPPLIERS.md` row;
   - the nearest analogous supplier or rule: the same kind of service in the
     same entity;
   - the email thread or Slack message the document came in, and what the
     sender said.
3. **Collect the verdicts once, before the report**:
   `.venv/bin/python scripts/investigate_query.py wait --all-open` (exit 75
   means call it again). **Map the certainty to the grade**: 5 and 4 high, 3
   medium, 1 and 2 low; the runner stores it as `grade` on the verdict and
   already caps it at low where it touches anything on the "still always
   queried or chased" list below. Never raise a grade yourself. Conflicting signals (the sender's entity is not the bill-to
   entity, the mapping disagrees with the invoice) are no longer a query on
   sight: the order above settles which signal wins. The grade is medium at
   best when a weaker signal disagrees, and low when two of equal strength do.
4. **Run the gate** with the gross amount in the reporting currency,
   converted at the document date's rate (the verdict's `amount_reporting`
   is the investigation's own figure):
   `.venv/bin/python scripts/investigate_query.py gate --key <ref> --amount 1240`,
   which is `scripts/resolve_gate.py` on the verdict's grade and records the
   gate on it. A question you graded yourself, with no investigation, goes to
   `.venv/bin/python scripts/resolve_gate.py --grade medium --amount 1240`.
   A verdict is a plan, never a ruling: do not write it back as a rule.
5. **Follow its first word**, and never argue with it:
   - `act`: carry on at step 4 like any other invoice. Report it under
     `resolved`: the answer, the evidence in one clause, the invoice line.
     Ledger outcome `posted (resolved)`.
   - `confirm`: the same posting, then report it under `to confirm`, ending
     with what to reply ("reply yes, or the right entity"), and register it:
     `scripts/outstanding.py add --domain bookkeeping --kind decided --with admin --key <invoice number> --text "<answer>, <evidence>" --refs "<invoice line>"`.
     Ledger outcome `posted (to confirm)`.
   - `query`: post nothing: the queried list, the email left unlabelled, the
     ledger outcome `queried:`. The question carries the proposed answer and
     its grade (`docs/COMMS.md`, "`queries`, `to confirm` and `manual`").
     Register it with the verdict: `outstanding.py add --domain bookkeeping
     --kind queried --with <person> --key <ref> --investigation
     <path to result.json> --text "<question and proposed answer (grade)>"
     --refs "<invoice line>"`. A failed investigation is queried the same
     way, saying it failed. The guard refuses a bookkeeping query without a
     verdict that allows it.

**Still always queried or chased, whatever the grade**: an amount, supplier or
currency the document does not show; a missing invoice (chase it); a possible
duplicate (step 4: treat it as one and query); a payroll line that is not in
`rules/PAYROLL.md`'s mapping tables; tax with no document behind it.

### Settling earlier `to confirm` items

At the start of every run (step 1a of the ledger routine):

1. `.venv/bin/python scripts/outstanding.py list --domain bookkeeping --kind decided --json`.
2. Read the replies to each: the admin notes handed to this run, and the
   threads of the channel's `BOOKKEEPING RUN` headlines since the oldest item
   was raised (`conversations.replies`, as the `outstanding-items` skill reads
   `done`). Only an admin's reply settles one; a user's is evidence.
3. **Confirmed** (yes, ok, correct): `close --key <ref> --reason "confirmed by
   <admin>"`, and write the answer back as a rule (step 8).
4. **Overruled**: correct the posting in this run, close the item, and write
   the admin's rule back. Wrong account or tracking: update the line in place
   with the amounts unchanged, which Xero allows even on a paid bill. Wrong
   entity: void the bill and create it again in the right one, but only while
   it carries no payment and is not reconciled; the admin's overrule is the
   explicit instruction the `xero` skill's safety rule 3 asks for, and the
   report states the correction per `docs/COMMS.md`, "Reporting a
   correction". One that does is not
   touched: it becomes a manual item for the user to unmatch first
   (`outstanding.py update --key <ref> --kind manual --text "..."`).
5. Then `.venv/bin/python scripts/outstanding.py accept`: whatever is left and
   has waited `confirm_days` closes as accepted. Report those as one count
   under `resolved`: `2 to-confirm items accepted, unchallenged for 7 days`.

## 4. Duplicate guard (VERY IMPORTANT, never skip this step)

Before creating anything: search the target entity for an existing bill with
the same supplier + `InvoiceNumber`, and same supplier + amount around the
same date, across **all statuses including DRAFT** (a receipt-capture tool
may have published it already, or it may sit unapproved; VOIDED/DELETED
copies don't count). Any hit: report as "already in Xero" and **skip; never
create a second bill for the same invoice**. If unsure whether an existing
bill is the same invoice, treat it as a duplicate and query rather than
create. (Also safety rule 6 in the `xero` skill: it applies to every create
path, not just this workflow.)

The poster (`scripts/post_bills.py`, step 6) runs this guard itself on every
document, straight before each create: the number
across all live bills in the target entity, the supplier and amount within
four days as a bill or as spend money, `matching.candidates` (bank-side
amounts, other currencies, named), and the number or the supplier and amount
in every other entity. It skips a document on any hit and prints the record.
Read every hit it prints. A hit that turns out not to be this document (a
different day's lunch for the same amount, the copy a move is replacing) is
acknowledged in the spec's `ack` with the reason, and the reason goes in the
ledger line; never acknowledge a hit you have not read. The poster only finds
the records; the rules below decide what they mean.

**Search on more than the amount.** Supplier and amount is the first search,
not the last: a document in another currency, or dated at the receipt weeks
before it was paid, matches neither. Search the window by date, payee or
person and description too, and read the payments' bank-side amount.
`xero-check` does all of it for a bank line; `src/accounting_agent/xero/matching.py`
(`load_window`, `candidates`) does it for anything else.

**Search bank transactions too, not just bills.** The same receipt can already
be in Xero as a **SPEND** bank transaction rather than a bill, booked by an
earlier run, by a receipt-capture tool, or by a person coding the feed line
directly. A guard that only looks at `Invoices` will not see it and will
create a second record of the same purchase. So also query `BankTransactions`
for the same supplier and amount around the same date (filter
`Status=="AUTHORISED"`, see `.claude/skills/xero/reference/gotchas.md` on deleted rows),
and if a SPEND already covers the receipt, skip and report it rather than
creating a bill.

**Corollary: prefer the bill, and do not create spend money for something that
has a document.** Per `rules/GROUP.md`'s transaction-type rule an invoice- or
receipt-backed cost is a **bill**. Spend money is for genuinely document-less
card spend only. If you find you have created a SPEND for a documented item,
say so in the report rather than leaving it for someone to find on the feed.

**Is it a duplicate at all, and which copy goes?** Whenever the poster prints
a hit, two debits look like one purchase, or a duplicate is to be cleared, read
`reference/duplicates.md` before deciding: one settled debit means no
duplicate whatever the records are called, a processor's percentage makes
amounts differ, and a reconciled record or the copy carrying the document and
the tax is never the one removed.

## 5. Contacts

`find_contacts(c, name)` in the target entity; match loosely (the family
variants `rules/SUPPLIERS.md` lists). No match: create the contact (supplier
family name) as part of the confirmed batch: the spec names it exactly with
`new_contact: true`, and the poster refuses a name it cannot find without that
flag. Where several contacts carry the name, the spec pins one with
`contact_id`.

## 6. Create the bills

For each allocation, per the `xero` skill safety rules: present the batch
table (file, sender, supplier, **full entity name**, account, net/tax/gross,
currency, date, invoice number, prepayment/capitalisation flags) and get
confirmation unless the standing instruction for the run is already
unambiguous.

**Post through `scripts/post_bills.py`, never a script of your own, in ONE
call.** Write the batch as one JSON spec at `$RUN/bills.json`: every bill and
supplier credit note the run has decided, and in `settle` everything those
postings answer, in the shape the poster's docstring sets out. Then:

```bash
.venv/bin/python scripts/post_bills.py $RUN/bills.json --live                   # the whole step
.venv/bin/python scripts/post_bills.py $RUN/bills.json --live --only INV-10135  # one document after a fix
```

```json
{"docs": "data/briefings/bookkeep/docs", "bills": [
 {"entity": "OPCO_US", "type": "bill", "contact": "Acme Hosting", "number": "INV-10135",
  "date": "2026-10-01", "due": "2026-10-16", "currency": "USD", "total": 248.60,
  "lines": [{"description": "Hosting, October 2026", "amount": 248.60, "account": "4900",
             "tax": "NONE", "tracking": {"Region": "North"}}],
  "files": ["E-2026-10-06-0912-Acme-INV-10135.pdf"],
  "feed": {"entity": "OPCO_US", "match": "rev:6abe6b09"}, "paid": "OPCO_US"}],
 "settle": [
  {"ledger": {"when": "2026-10-06 08:12", "source": "email", "sender": "Acme Hosting", "seen": 1,
              "counterparty": "Acme Hosting", "ref": "INV-10135 USD 248.60", "entity": "OpCo US",
              "outcome": "posted, Hosting (4900)"},
   "after": ["INV-10135"]},
  {"close": "acme-inv-10135", "reason": "the invoice arrived and is posted", "after": ["INV-10135"]},
  {"email": "18f2a3b4c5d6e7f8", "after": ["INV-10135"]},
  {"ledger": {"when": "2026-10-06 14:02", "source": "email", "sender": "Fabrikam Workspace", "seen": 0,
              "bookkept": 0, "counterparty": "Fabrikam Workspace", "ref": "portal notice", "entity": "-",
              "outcome": "not-a-bill: the invoice is in the portal, chased"}}]}
```

The spec carries the entity as a key, alias or short name from
`config/group.toml`, the due date (required on a bill), the tax type on every
line (never left to the account's default), `tracking` as
`{"<Category>": "<Option>"}` where the entity's `required_tracking` or its
rules file asks for it, and `paid` (the entity whose bank paid it, from the
bank feed) where the cash has settled, for the report. That one call refuses
the whole batch, before anything is read, on a spec that does not hold: no due
date on a bill, a due date before the invoice, a line missing a tracking
category the entity requires, a tax type `[posting] restricted_tax_types`
keeps to other entities, lines that do not come to the total, a file that is
missing, empty or over Xero's attachment limits, a `settle` action naming a
document the spec does not hold. It refuses one document, and goes on with
the rest, where the entity's own settings rule it out: an account missing,
archived or a bank account, a tax type Xero does not hold, a total Xero would
compute differently from the document's, a date inside the end-of-year lock.
Then it runs the duplicate guard (step 4) on each document and skips any with
a hit, posts the rest AUTHORISED with a deterministic idempotency key, checks
Xero's reply (no element errors, a real ID, AUTHORISED, the total and the
amount due equal to the document's), attaches every file and removes the
`feed` line. Last it works the `settle` list: each ledger line written under
today's `## Run` heading with the posted ids, the register lines closed in one
go, each email given the processed label and taken out of the inbox, any
further feed line removed, each the moment the documents it names in `after`
are posted. One whose document was skipped or refused prints `HELD` and waits.
It refuses to run in a read-only session.

A document that posted cleanly prints one `POSTED` line, which the report is
written from; every payload sent is in `$RUN/bills.results.jsonl`. A document
that did not prints whole: its entity in full, each line's account by name and
code, the guard's records and the reason. Read it, fix the spec (acknowledge a
hit shown not to be this document in `ack`, with the reason), and run the same
command with `--only` and the number: the re-run skips what the results file
shows posted or settled and does what was held. A `FLAG` line (a total that
differs, a file that did not attach) goes in the report as it stands, and a
batch stopped by the Xero rate limit (exit 5) is finished by running the same
command again once the limit resets. Run the poster without `--live` (the dry
run: checks, guard and every payload, nothing sent), or with `--offline` (the
spec alone, no network), only where an admin asked to see the batch before it
posts. The poster never makes a payment, an allocation or a reconciliation,
and it has no way to.

A posting the poster cannot make (spend money, a manual journal, an update or
a void) is not a bill and goes through the `xero` skill's helpers as before,
with the entity named and the payload shown.

**What the postings answer is in the same spec.** Grep `$RUN/register.txt`
for the suppliers and numbers the batch posts: every line a posting settles is
a `close` action after that document, never a separate `outstanding.py close`
per line (each one republishes the register). Only a line that needs an
`update`, an `answer` or a new `add` goes through `outstanding.py` directly.

- **Create as AUTHORISED** (no draft or manual-approval step). The safety
  valve is upstream: anything the step 3a gate does not clear goes to the
  queried list, not to Xero.
- Always attach the source file to the bill.
- Deterministic idempotency key (the poster derives it from the document, or
  takes the spec's `key`) so re-runs can't double-create.
- **NEVER create a payment, and never mark the bill paid** (`xero` skill
  safety rule 7). Your output is an AUTHORISED bill with the document
  attached and `AmountDue` still equal to the total. The user matches the
  imported statement line to it by hand in the UI. A payment you create
  becomes a phantom unreconciled bank entry competing with the real feed
  line, which is the duplicate this whole workflow exists to prevent. This
  holds **even when the receipt says PAID, even when you can see the matching
  line in the bank feed, and even when the bill is a card receipt that has
  obviously already been settled.** "Already paid" is information for the
  user's matching step, never a reason for you to post a payment. The same ban
  covers credit-note refunds: create the credit note, leave it unallocated.

## 6b. Bill payments: which open bill has been paid from another entity's bank

**Once a night, never twice.** The scheduled bookkeeping run does not run it:
the accounts payable check that follows it in the daily chain does, after
this run's bills are posted, so the scheduled prompt says so and this step is
skipped there. Every other bookkeeping run, one started from a Slack thread
above all, has nothing after it and runs it itself.

**After the bank lines are read and the intake is posted.** Load
the `bill-payments` skill and follow it: refresh the bill feed again so this
run's bills are in, run `scripts/billfeed.py match` against the bank feed
lines already in hand (never re-read the bank), and for every open bill paid
from a **different** entity's bank post the payer's spend money to the
pair's intercompany account (`config.intercompany_pairs()`, flavour per
`rules/INTERCOMPANY.md`), remove that feed line, and report the bill under
**bill payments** with the instruction to reconcile it to intercompany by
hand. Same-entity matches and unmatched bills need nothing. The bill itself
is never touched: no payment, no allocation.

## Email intake: the accounting inbox, labelled when done

The shared accounting inbox (`config.company("accounting_inbox")`) receives
invoices and correspondence directly from suppliers and staff. When a run
processes an item that arrived by email (or when asked to work the inbox),
the same steps 2-6 apply, plus this labelling rule. Its purpose is that
anyone filtering the inbox can see at a glance which emails' invoices are
already in Xero and which still need action.

**Access**: the repo holds a `gmail.modify` token for the inbox
(`.gmail/tokens.json`; bootstrap via `python -m accounting_agent.gmail_auth`).
The briefing sweeps the inbox (`format=full`, the only format that carries the
attachments: `format=metadata` omits `payload.parts`, so every message counts
as having none) and downloads what is to work, leaving out a signature image,
a logo or a certificate, which it names. Some suppliers send
**password-protected PDFs with the password in a separate email in the same
thread**. Before querying an unreadable PDF, read the rest of the thread, then
decrypt it with the `pypdf` reader's `decrypt(pw)` and attach the *decrypted*
copy to the bill so the next person can read it.

**The rule: MOVE the email into the processed label the moment its document
is in Xero.**

1. After the invoice from an email is bookkept (**the bill, or spend money /
   credit note / whatever the correct treatment was, is created and the
   attachment attached**), **move** the email's **thread** into the label
   named `config.company("processed_label")`, label added and `INBOX`
   removed in the same call: an `email` action in the poster's `settle` list
   with the thread id from the briefing, `after` the document, which the
   poster does once the post is verified (it resolves the label by name,
   never a hard-coded ID). A bookkept invoice must not stay in the inbox: the
   inbox is the outstanding-work queue.
2. Also apply the label when the duplicate guard (step 4) finds the invoice
   is **already in Xero** (via another intake route or Slack): the goal is
   "this email needs no further bookkeeping", not "I created it".
3. **Never label queried or unclear items**: an unlabelled invoice email
   means outstanding work. Non-invoice correspondence is out of scope: leave
   it alone unless asked.
4. Label AFTER the Xero write is verified (`HasErrors` check / real GUID),
   never before. If the Xero create fails, the email stays unlabelled.
5. Removing `INBOX` is REQUIRED (rule 1), and it is the only label you may
   remove. Don't mark read, don't trash, and don't touch anything else. If the
   mailbox has the team's own per-entity labels, you may add the matching
   entity label alongside the processed label but never remove one.
   Non-invoice correspondence and queried items keep their place in the inbox.

When reporting (step 7), add a **labelled** count so the run states how many
emails were moved into the processed label.

## Missing invoices: chase on Slack, no permission needed

A cost with a bank line but **no invoice** is never bookkept from the bank line
alone: an invoice-less bill has nothing to attach and will be duplicated the
moment the real invoice arrives through another intake route. Genuinely
invoice-less card spend is **spend money** per `rules/GROUP.md`'s
transaction-type rule, not a bill.

Instead, **message the owner on Slack from the bot** (`SLACK_BOT_TOKEN` in
`.env`, `chat.postMessage` straight to the user ID; `conversations.open` may
fail with `missing_scope`, but posting to a bare user ID works whether or not
a DM already exists). **Send these automatically, every time, without asking
for permission first.**

Routing comes from `config.section("slack").get("chase_routing", {})`, a map
from a selector to a Slack member ID. Check in this order:

1. **A cardholder key** that matches the payee text or the card on the bank
   line: that person, about their own card.
2. **A category key** the group defines (e.g. `travel`), where the cost is of
   that category, whatever the entity. A category beats an entity: a travel
   cost on any entity goes to the travel owner.
3. **The entity key** of the paying entity (`config.entity(...).key`).
4. **`default`**: everything else.

No match and no `default`: report the gap under `queries` for the admin
instead of messaging anyone. Never message anyone listed in
`config.section("slack").get("never_message", [])`; route what would have
gone to them by the next rule in the list.

Word it per `docs/COMMS.md` "Queries and chases": the situation in one clause,
what is missing in one clause, then the one thing you need them to send or
confirm, invoice line at the end. Group several gaps for one person into a
single message rather than one message per line. Every chase is also
registered as a document waiting on that person (not investigated, never a query):
`scripts/outstanding.py add --domain bookkeeping --kind documents --with <person> ...`
in the same step that sends it.

## 7. Report

**Rebuild the bank feed first**, so the next run opens a current file, then the
two exit checks, both of which must print PASS:

```bash
.venv/bin/python scripts/bankfeed.py refresh all --brief
.venv/bin/python scripts/check_no_phantom_payments.py
.venv/bin/python scripts/outstanding.py check-queries
```

The closing refresh is `--brief`: the lines it adds are the next run's to
read. Every bookkeeping query left in the register must be one the run could
not answer: a verdict the gate sent to `query`, a failed investigation, or an
admin's answer still to post. **If either check prints FAIL, read
`reference/report.md`, "When an exit check fails", before touching
anything**: a FAIL is the ledger's state, not proof the run did it, and a
reconciled payment is never removed.

**Then the report:**

```bash
.venv/bin/python scripts/bookkeep_report.py
```

It writes `$RUN/report.md` with the two sections code can write without
judgement, `bookkept` (every document posted, in the invoice shape, `paid`
from the spec's `paid` field) and `bank feed` (the lines and the open bills
per entity as they now stand), and prints every document it did not post and
every flag for you to place. Add with Edit the headline above them and every
other section the run has, in `docs/COMMS.md`'s order and words (read it once,
with Read), add a **labelled** count for the emails moved into the processed
label, then queue it:

```bash
.venv/bin/python scripts/slack_outbox.py channel \
  --headline "BOOKKEEPING RUN (18 Sep 2026, 14:32 UTC)" --thread $RUN/report.md
```

Should `bookkeep_report.py` fail, write the whole report by hand. The example
report, the register rule, the channel and thread rules and the MANUAL ITEMS
post are in `reference/report.md`, "The report" and "Where the report goes":
read it when writing the report.

## 8. Write back what you learned, every run

Before you finish, read `reference/write-back.md` and write the durable part of
what the run learned back into the file that owns it, in the same run, and list
it in the report under `wrote back`. A question you had to ask once should
never have to be asked again; a safety rule is never weakened this way.
