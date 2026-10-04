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
better than a misallocated bill.

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

## Step 0: the bank feed (READ BEFORE ANYTHING ELSE, EVERY RUN)

`data/bankfeed/<slug>.md`, server only. Every bank statement line that is not
yet reconciled, per entity (`config.entity(k).slug`), per account. It is the
standing answer to "what cash is unaccounted for", and a run that skips it is
working blind. Full rules: [docs/BANKING.md](../../../docs/BANKING.md).

```bash
.venv/bin/python scripts/bankfeed.py status
.venv/bin/python scripts/bankfeed.py list opco-us
```

In this order, before a single invoice is opened:

1. Read the feed for every entity in scope.
2. Read the `last checked` stamp on each account.
3. Refresh from the bank API: `.venv/bin/python scripts/bankfeed.py refresh all`.
   Settled movements after each stamp are appended (from each account's API
   provider in `config.bank_accounts()`, read-only, server only) and the
   stamps move to now. It never removes a line. CSV-only accounts are
   refreshed from the statement file dropped in `data/statements/`.
3a. **Refresh the bill feed**: `.venv/bin/python scripts/billfeed.py refresh all`,
   every AUTHORISED unpaid bill per entity, a snapshot from Xero
   (`data/billfeed/<slug>.md`, server only). Keep both feeds in hand: the
   bill-payments check at step 6b uses them without reading the bank again.
4. Then work the intake as below.
5. **Remove a line the moment its cash is accounted for**: the bill, spend
   money or transfer is posted. Immediately, not batched at the end:
   `bankfeed.py remove <entity> "<unique substring>" "<why>"`.
6. **Refresh again at the end of the run** (step 7 below) so the next run opens
   a current file. Bank credentials on the server for anything not in
   `config/group.toml` are out of scope, never refreshed or bookkept.

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
`data/bankfeed/`. The line therefore sits on the feed looking like
unaccounted cash for as long as nobody checks, and the run chases a receipt
for a cost that is already in the books and already matched. So before the
chases go out, pull each entity's AUTHORISED bank transactions over the
feed's own date range in one list call and match them to the feed on
currency, amount and date: every hit is a line to remove with the account it
was coded to, not a person to message. Payroll runs and month-end transfers
are where this bites hardest, because an admin posts them in a block.

A line only leaves for a posted transaction or an admin saying it was matched
by hand. Chasing someone does **not** clear it: the cash is still
unaccounted for; record the chase in the note column and leave the line.
A `staged` coding in the Xero UI is a hint, not a reconciliation.

**An admin saying a line is coded is a hint too.** Coding typed into the
reconcile screen and not completed creates no transaction and reaches no
API, so a run handed "I have coded X" still looks the transaction up in the
ledger before doing anything that depends on it. Where it is not there,
query it and leave the feed line: do not post the coding yourself. The
admin's own copy, finished later in the UI, would be the second one.

**The feed is the chase list.** A line with no invoice behind it is a person to
message, routed per "Missing invoices" below.

## Rolling ledger: the cross-agent completeness memory (READ FIRST, EVERY RUN)

`docs/bookkept/LEDGER.md` is the shared record of what has been bookkept, one
line per intake message (email or Slack message), kept for **7 days**
(anything older is stale and is deleted). It exists because intake items get
missed between runs; it is cheap to read and MUST be maintained by every
agent that runs this workflow. It lives on the server only, is gitignored and
never committed, and the scripts create it on first use.

Per run, in this order:

1. **Prune, then read the ledger**: `.venv/bin/python scripts/ledger_prune.py`
   deletes every entry and every run section older than 7 days; then read
   what is left, whole.
2. **Completeness sweep (metadata only, minimise tokens):** list the last
   7 days (the ledger's own window, one rolling window for both) of (a) Slack
   messages to the bot with attachment counts and (b) accounting inbox
   threads (any label, incl. the processed label) with attachment counts:
   senders, timestamps, subjects/first line only, do NOT download or re-read
   the underlying invoices. Every message with attachments must have a ledger
   line whose `docs seen/bookkept` counts match the message's attachment
   count. Any message absent from the ledger, or with fewer bookkept than seen,
   goes into this run's processing queue (verify against Xero before posting:
   the ledger is a completeness index, the duplicate guard in step 4 remains
   the authority).
3. **Process intake as below.** After each item's outcome is settled (bill
   posted + attached, already-in-Xero, queried, or routed as not-a-bill),
   append its ledger line immediately, not in a batch at the end.
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
  or its channel). Pull everything since the last run (default: today).
- **Mechanics:** use the app's own bot token, `SLACK_BOT_TOKEN` in the repo's
  `.env`. A hosted Slack connector may be able to search but not download
  files. Call the Slack Web API with `slack_sdk` or `curl` (if the venv
  python's `urllib` fails SSL verification, give it `certifi`'s context):
  `conversations.list?types=im` then per-IM `conversations.history?oldest=<ts>`
  then `users.info` for sender names, then download each file's
  `url_private_download` with the `Authorization: Bearer` header.
- For each message with attachments record: **sender** (Slack user to real
  name), timestamp, message text (may carry instructions like "this is for
  the EU office": the sender's instruction overrides the mapping, subject to
  the users-are-evidence rule above), and every file (PDF, JPEG, PNG, HEIC,
  anything).
- Download files to `data/intake/`, named `<date>-<sender>-<original name>`.
- **Mark the message done in Slack.** Once every file in a message is
  bookkept, already in Xero or ruled not-a-bill, add the bot's reaction to
  that message: `reactions.add` with the bot token, `name=books`, `channel`
  and `timestamp` of the message. That reaction is the Slack equivalent of the
  email processed label; the completeness sweep treats a message with
  attachments and no `books` reaction as unprocessed, and the ledger line
  stays the record of what was done. A receipt-acknowledgement reaction the
  listener adds is not a done marker. If `reactions.add` returns
  `missing_scope`, the app needs `reactions:write`: say so under blocked and
  fall back to the ledger line. The duplicate guard (step 4) is the backstop.

## 2. Parse each invoice

**Fan the reading out, keep the thinking.** Reading a document has an
objective right answer and needs none of the rulebook, so it does not belong
in this session: spawn the `invoice-extract` subagent (a small model,
transcribes only, no judgement, `.claude/agents/invoice-extract.md`) once per
downloaded file, all of them in one message so they run at once, each given
one file path. Each returns the document as JSON: what it literally says,
nothing decided. You do everything from step 3 on: entity, account, tax
treatment, the duplicate guard, the create. Never delegate a coding decision,
and never let a subagent write.

Extract (or receive): supplier name (and the supplier's own billing entity),
**which group entity is billed** (if named), invoice number, invoice date,
due date, currency, net/tax/gross amounts, the seller's tax registration
number, line descriptions, and **the period covered** (needed for the
prepayment check). Unreadable or partial files go to the queried list; never
guess amounts.

**A null is a null.** The subagent returns `null` and an `illegible` mark
rather than a guess, and you treat that exactly as you would your own failure
to read the field: the rules below decide whether it is a query or a posting
at full confidence. Read the file yourself when the extraction comes back
unreadable, when its `notes` say the file holds more than one document, or
when what it returns does not hold together (a gross that is not net plus
tax, a date outside the run's window, a supplier you do not recognise on an
amount that matters). One document, one subagent: a summary sheet of many
transactions is read by you, not fanned out per row.

**A rotated or angled photo is read as a different document altogether.**
Where the subagent's `notes` say the image is rotated, at an angle, or that
the line items could not be made out, its `legibility` marks on supplier,
amount and date are worth nothing: it has been seen to return a confident
supplier, total and date that were all wrong while flagging only the lines.
Treat "rotated" or "photographed at an angle" in the notes the same as
`illegible` on the amount: open the file yourself. The receipt is usually
perfectly readable once a human eye is on it, so this costs one read and
prevents a bill to the wrong supplier for the wrong money.

**A month that is not the message's own month is read directly.** The
subagent has returned a date one month out on a flat, unrotated, perfectly
legible receipt, with `legibility.date: "ok"`: the day and the year right
and the month a month early. The cheap test that catches it without reading
every file: a routine card receipt is submitted the day it is spent or the
day after, so an extraction that puts the document in an **earlier month than
the message carrying it** is wrong until the file says otherwise. Open it.
The cost is one read; the cost of trusting it is a bill in the wrong period
and a bank line that never matches.

**A date the subagent marks legible can still be wrong, and the duplicate
guard is what catches it.** Extractions have come back with
`legibility.date: "ok"` and a date that was not on the document (a day
misread as an earlier day in the same month, a till stamp misread by days),
on photographs that were not rotated, so the rotation rule above does not
reach them. The trigger to open the file yourself is the **duplicate guard**,
not the legibility mark: when the supplier already has a bill of the same
amount in the period the extraction puts the document in, or when the date
sits days before the message that submitted it, read the date directly.
Either case would otherwise produce a second bill for a receipt already in
Xero.

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
  (see the `xero` skill's gotchas), so the only remaining route is a manual
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
   bank feed (`bankfeed.py list <entity>`, or `statement_lines()` per
   BANKING.md) by amount and date across all entities; the feed names the
   paying entity and the card. If the payer is not the recognising entity,
   the cross-entity payment rule in `rules/EXPENSES.md` and
   `rules/INTERCOMPANY.md` applies (normally: the payer's spend money to the
   pair's intercompany account, the recogniser's bill left AUTHORISED for the
   user to settle; step 6b does this). No bank line yet: the default entity
   stands and `paid` is dropped from the invoice line until the cash settles.
3. **Prior bills from the same supplier**: `get_bills(c, contact_name=...)`
   over recent months in the candidate entity (all contact-name variants,
   see the `xero` skill gotcha); consistency with prior months is the primary
   test.
4. **Supplier mapping**: `grep -i "^| <name>" rules/SUPPLIERS.md`
   (never read the whole file); the mapping is one input, the invoice decides.
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
   share). Without a rule, it is a query.

Confidence: every allocation gets **high / medium / low**. Low-confidence or
conflicting signals (sender entity differs from invoice billing entity,
mapping differs from invoice content) go to the queried list with a stated
question, not a bill.

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

**Search bank transactions too, not just bills.** The same receipt can already
be in Xero as a **SPEND** bank transaction rather than a bill, booked by an
earlier run, by a receipt-capture tool, or by a person coding the feed line
directly. A guard that only looks at `Invoices` will not see it and will
create a second record of the same purchase. So also query `BankTransactions`
for the same supplier and amount around the same date (filter
`Status=="AUTHORISED"`, see the `xero` skill's gotcha about deleted rows),
and if a SPEND already covers the receipt, skip and report it rather than
creating a bill.

**Corollary: prefer the bill, and do not create spend money for something that
has a document.** Per `rules/GROUP.md`'s transaction-type rule an invoice- or
receipt-backed cost is a **bill**. Spend money is for genuinely document-less
card spend only. If you find you have created a SPEND for a documented item,
say so in the report rather than leaving it for someone to find on the feed.

**Is it a duplicate at all? Two questions, in this order.** A supplier name
that looks like another one is not evidence of anything; the bank is.

1. **Are there two identical payments?** Search the paying entity's statement
   for a second debit of the same amount within a few days. One debit means
   there is no duplicate, whatever the two records are called, and the answer
   is simply which record the line belongs to.
2. **Only if there are two: is there an invoice behind each?** Two debits with
   two invoices are two purchases. Two debits with one invoice between them is
   a duplicate payment and is flagged, never quietly cleared.

**The amounts do not have to be equal.** A supplier collected through a card
processor is charged the invoice plus that processor's percentage, so the debit
sits a few per cent above the invoice total and an exact-amount guard sees two
unrelated costs. Work the ratio before concluding: a debit that is the invoice
total times a flat percentage is that invoice, and a second invoice for the base
amount is the duplicate. The tell is a bill already in the books for the odd
figure, often under a number the processor's own confirmation supplied and
therefore stale. Query the pair and post neither.

The common false positive is an acquirer or processor name against the
merchant's: *no* - two records, same amount, a day apart, so a duplicate;
*yes* - one debit on the statement, so the processor line and the merchant
bill are one payment described twice.

**Clearing a duplicate: which copy goes, and check the other one is alive.**
Removing the wrong half of a pair is as damaging as the duplicate. Three
tests, in order, before any void:

- **The counterpart must be LIVE.** A DELETED or VOIDED sibling is not a
  record of anything, so voiding against it leaves the cost in no ledger at
  all. Re-read the counterpart's `Status`; never conclude "already recorded"
  from a list row or an earlier run's report.
- **Keep the copy that carries the document and the tax.** Where the pair is a
  documented bill against a gross card spend, the bill is the record: it holds
  the attachments and any tax split, and voiding it turns recoverable tax into
  cost. The reconciled spend is the one to remove, and the agent cannot, so
  the item is reported for the user to unmatch in the UI, not voided to tidy
  the list.
- **Reconciled means untouchable.** Never void or unreconcile a
  statement-matched transaction to resolve a duplicate; that is the user's own
  matching work (`xero` skill safety rule 7).

A duplicate that fails any of these tests is reported and held, even where the
void was already approved: the approval was given on the premise the run has
just disproved.

## 5. Contacts

`find_contacts(c, name)` in the target entity; match loosely (the family
variants `rules/SUPPLIERS.md` lists). No match: create the contact (supplier
family name) as part of the confirmed batch.

## 6. Create the bills

For each allocation, per the `xero` skill safety rules: present the batch
table (file, sender, supplier, **full entity name**, account, net/tax/gross,
currency, date, invoice number, prepayment/capitalisation flags) and get
confirmation unless the standing instruction for the run is already
unambiguous. Then:

```python
import uuid
from accounting_agent import config
from accounting_agent.xero import client_for
from accounting_agent.xero.purchases import create_bill, make_line
from accounting_agent.xero.documents import attach_file

e = config.entity(entity_key)           # decided in step 3, never a literal
c = client_for(e)
bill = create_bill(
    c, contact_id,
    [make_line(desc, 1, net_amount, account_code, tax_type=tax_type,
               tracking=tracking)],     # tracking only where the entity's rules file requires it
    date=inv_date, due_date=due_date or inv_date,  # DueDate REQUIRED for AUTHORISED; receipts: due = invoice date
    invoice_number=inv_number,
    status="AUTHORISED", currency_code=currency,
    idempotency_key=f"slackbill-{sender}-{inv_number or file_hash}",
)
assert not bill.get("HasErrors"), bill.get("ValidationErrors")  # element errors return 200, not an exception
attach_file(c, "Invoices", bill["InvoiceID"], file_path)
# STOP HERE. The bill stays AUTHORISED and unpaid. No payment, ever.
```

- **Create as AUTHORISED** (no draft or manual-approval step). The safety
  valve is upstream: anything uncertain goes to the queried list, not to Xero.
- Always attach the source file to the bill.
- Deterministic idempotency key so re-runs can't double-create.
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

**Every run, after the bank lines are read and the intake is posted.** Load
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

> **Use `format=full`, never `format=metadata`, when counting attachments.**
> `GET messages/{id}?format=metadata` returns the headers but **omits
> `payload.parts`**, so an attachment counter walking the payload reports
> `att=0` for every message: the completeness sweep then silently concludes
> nothing has attachments and finds no gaps at all. There is no error; the
> sweep just comes back clean. Always `format=full`, and sanity-check the
> count against a message you know has a PDF.
>
> Fetching ~140 full messages also trips Gmail's per-user rate limit with a
> bare `403`. Wrap every call in a retry that backs off on 403/429/5xx and
> sleep ~0.12 s between messages.
>
> Some suppliers send **password-protected PDFs with the password in a separate
> email in the same thread**. Before querying an unreadable PDF, read the rest
> of the thread, then decrypt it with the `pypdf` reader's `decrypt(pw)` and
> attach the *decrypted* copy to the bill so the next person can read it.

```python
import sys; sys.path.insert(0, "src")
from accounting_agent.gmail_auth import get_access_token
# Gmail REST API with Authorization: Bearer <token>, base
# https://gmail.googleapis.com/gmail/v1/users/me/
# (use urllib with certifi's SSL context, see gmail_auth.py, or curl)
```

**The rule: MOVE the email into the processed label the moment its document
is in Xero.**

1. After the invoice from an email is bookkept (**the bill, or spend money /
   credit note / whatever the correct treatment was, is created and the
   attachment attached**), **move** the email's **thread** into the label
   named `config.company("processed_label")`: add the label AND remove
   `INBOX` in the same call. Resolve the label ID by name via `GET labels`
   (never hard-code it), then
   `POST threads/{threadId}/modify` with
   `{"addLabelIds": ["<label id>"], "removeLabelIds": ["INBOX"]}`.
   A bookkept invoice must not stay in the inbox: the inbox is the
   outstanding-work queue.
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
registered as a query waiting on that person:
`scripts/outstanding.py add --domain bookkeeping --kind queried --with <person> ...`
in the same step that sends it.

## 7. Report

**Rebuild the bank feed first**: every bookkeeping run ends by refreshing it,
so the next run opens a current file:

```bash
.venv/bin/python scripts/bankfeed.py refresh all
.venv/bin/python scripts/bankfeed.py status
```

The per-entity outstanding count goes in the report under **bank feed**; the
bill feed's open count per entity (`billfeed.py status`) goes beside it.

**MANDATORY exit check: run this before reporting the run as done:**

```bash
.venv/bin/python scripts/check_no_phantom_payments.py
```

It scans every entity for unreconciled payments sitting on **bank** accounts,
the phantom entries that duplicate real statement lines. It must print
**PASS**. A FAIL means a payment was created during the run: remove the ones
the agent created, but **never remove a payment that is already reconciled**
(that is the user's own matching work, see `xero` skill safety rule 7).
Payroll control accounts (`config.payroll_controls()`) and any codes in
`config.phantom_exempt_accounts()` are reported separately as an allowed
exception, not a failure.

**A FAIL is not proof the run did it.** The check reports the state of the
ledger, not the authorship of it, so a person paying staff expense claims in
the Xero Expenses UI produces exactly the same FAIL: a batch of unreconciled
payments on a bank account, sitting there until the bank line arrives and
someone matches it. The tell is the invoice number **Expense Claims** and an
`UpdatedDateUTC` before the run started. Those are the user's own work, they
are API-immutable anyway, and deleting them would destroy a real
reimbursement. So before touching anything: read each flagged payment's
timestamp and invoice, and delete only what this run can be shown to have
created. Where none of it is the run's, **report the FAIL in the headline
exactly as it printed** and say in one line who made the payments and why they
are there. Never soften it to a pass, and never quietly delete to make the
check go green.

Then report in the house style, `docs/COMMS.md`. It is a **hard rule** and it
governs every message this run sends to anyone, not just this report: every
chase, every query, every thread reply. Headline line, then only the sections
that have content: a bold lowercase heading, bullets underneath. The example
uses the fictional group in `config/group.example.toml`:

```
bookkeeping · 14 bookkept · 2 blocked · 3 queries · 1 manual · check PASS

*bookkept*
• Contoso Cloud · 04 Sep · EUR 1,240.00 · paid OpCo EU · recognised OpCo EU · INV-1002

*bill payments*
• Adventure Works Hotels · 28 Aug · GBP 240.00 · booked to Travel (5100) · INV-1004 - paid by OpCo US from Mercury on 30 Aug, recognised as a bill in HoldCo, spend money posted in OpCo US to the HoldCo loan account. *Manually reconcile the bill in HoldCo to intercompany.*

*not attempted*
• the July Contoso Cloud invoices for OpCo US, deferred to the August review

*blocked*
• the tax split on the laptop bill cannot post, the 31 Mar lock date rejects it - INV-1001, DR Input VAT (2250) / CR Computer Equipment (1500), GBP 480.00

*queries*
• Litware Software, which entity - 05 Sep, USD 3,400.00, INV-1006

*manual*
• reconcile the HoldCo Adventure Works Hotels bill to intercompany - INV-1004

*chased*
• the travel owner for the 28 Aug Adventure Works Hotels receipt, HoldCo

*from users*
• bookkeeper: the card charge is OpCo US · coded Software (6300) in OpCo US

*wrote back*
• rules/SUPPLIERS.md, Contoso Cloud to Hosting (5320) in the bill-to entity
```

Every rule for these lines lives in `docs/COMMS.md` and nowhere else: read it
before writing the report, do not work from memory.

**Register what the report leaves with a person.** Every `queries`,
`manual` and `blocked` line is added to the outstanding register in the same
step that writes it (`scripts/outstanding.py add --domain bookkeeping --kind
queried|manual|blocked --with <person> --key <ref> --text "..." --refs
"..."`), and every item this run resolved is closed
(`scripts/outstanding.py close --key <ref>`). The report and the register
must agree.

**The report is posted to the channel**, because a bookkeeping run belongs to
the team, not to the admin who happened to start it. Two calls,
`SLACK_BOT_TOKEN` as always, the channel from `config.slack().channel_id`:

```bash
CH=$(.venv/bin/python -c 'import sys; sys.path.insert(0,"src"); from accounting_agent import config; print(config.slack().channel_id)')
TS=$(curl -sS -H "Authorization: Bearer $SLACK_BOT_TOKEN" -H 'Content-type: application/json' \
  -d "{\"channel\":\"$CH\",\"text\":\"BOOKKEEPING RUN (18 Sep 2026, 14:32 UTC)\"}" \
  https://slack.com/api/chat.postMessage | python3 -c 'import sys,json; d=json.load(sys.stdin); assert d["ok"], d; print(d["ts"])')
# then the same call again with the full report as "text" and "thread_ts": "$TS"
```

The top-level message is the headline and nothing else; the whole report
(every section, `queries`, `manual` and `bill payments` included) is the first
reply in its thread. The date and time are the run's own start, in the
group's timezone (`config.company("timezone")`), in that shape. The admin
whose thread started the run gets the report's one-line headline there plus
`full report in #<channel_name>`, never the report twice.
**Then the manual items, the same two calls again**: a top-level
`MANUAL ITEMS (18 Sep 2026, 14:32 UTC)` and, as the first reply in its
thread, `from the bookkeeping run of ...` followed by every `bill payments`
bullet and every other line telling the user to do something in Xero by hand.
Skipped only when the run has none.
Chases, queries put to a person and answers to anything else asked in a thread
are unaffected: they stay with the person. `channel_not_found` means the bot is
not in the channel: say so under `blocked` and send the full report to the
admin's thread instead. Rules in `docs/COMMS.md`, "Where a message goes".

## 8. Write back what you learned (every run)

The books get easier to keep only if each run leaves the rules better than it
found them. **A question you had to ask once should never have to be asked
again.** Before you finish, write the durable part of what you learned back
into the repo, in the same run, and list what you wrote in the report.

Where each kind of learning belongs:

| You learned... | Write it to |
|---|---|
| A new supplier, or a refinement to how an existing one is coded | `rules/SUPPLIERS.md` (one row; the run that meets a supplier owns its row) |
| A general coding or judgement rule an admin ruled on | `rules/EXPENSES.md` as a new numbered rule, or by tightening the existing one |
| Something entity-specific | that entity's `rules/entities/<KEY>.md` |
| A group-level recognition rule | `rules/GROUP.md` |
| A payroll treatment | `rules/PAYROLL.md` |
| An intercompany routing rule | `rules/INTERCOMPANY.md` |
| A Xero API quirk, error shape, or workaround | the `xero` skill's gotchas section |
| A change to this workflow itself | this file |

How to write it so it is actually useful later:

- **Write it as a rule.** State the rule and, where it matters, the reason in
  a clause. Add an example only where it illustrates the rule (the no / yes
  shape), never as a record of what happened; no journal IDs, bill GUIDs or
  run amounts.
- **Write the general rule, not the instance.** "Illegible dates on routine
  staff spend are not a blocker" is reusable. "The supermarket receipt was
  fine" is not. State the boundary too: when the rule stops applying. Open
  items and the state of a correction belong in the ledger, the run report or
  the outstanding register, never in a rules file.
- **Answers to queried items are the richest source.** Every question an
  admin answers is a rule you did not have. When a run starts with answers
  attached to previously queried items, resolving the item is only half the
  job: the other half is writing down the rule so the same question is never
  asked twice.
- **Refine, don't duplicate.** Grep for an existing rule covering the ground
  and tighten it rather than adding a near-identical one. The files are read
  in full every run, so redundancy costs tokens and invites contradiction.
- **Only durable rules.** Facts about one invoice belong in the ledger line,
  not here. If it will not apply again, do not write it. **A one-off about one
  supplier's one document is never a write-back**: write back the mechanism,
  not the incident. *No* - "this supplier's formal credit note arrives after
  the summary and is that credit note's evidence". *Yes* - "a supplier
  summary and the supplier's own later document for the same quarter are one
  credit, not two". If the rule cannot be stated without naming the document,
  it belongs in the ledger or the run report.
- **Never weaken a safety rule this way.** The payment ban, the duplicate
  guard and the whitelist are not "learnings" to be relaxed; changing those
  needs an explicit, separate instruction from an admin.
