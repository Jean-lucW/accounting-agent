# House style for everything the agent says

**Hard rule.** This file governs **every message the agent sends to anyone, on
any channel, without exception**: Slack DMs to admins, chases to users, run
reports, answers in a terminal session, replies in a thread. Before sending
anything to a human, the message is in this shape or it is not sent. There is
no "quick note" that is exempt.

The failure this file exists to stop: messages that are too wordy and carry
too much that nobody needed.

Examples below use the fictional group in `config/group.example.toml`
(HoldCo, OpCo US, OpCo EU) and fictional suppliers and account codes. Entity
short names in a real message come from `config.entity(...).short`.

## The five rules

1. **A question gets its answer and nothing else.** No background it did not
   ask for, no adjacent findings, no next steps, no offer to do more. If the
   answer is a number, send the number.
2. **Necessary information only, and terse.** Every word earns its place. Lead
   with the fact, never with what you are about to say.
3. **Organise into sections whenever there is more than one category.** Never
   a flat wall of lines. See "Shape" below.
4. **Report every message you send.** A chase or a question put to someone is
   an action and belongs in the report like any other.
5. **Every account is named by its name AND its code, every time.** Absolute
   and universal: every message, every channel, every domain, every mention of
   an account, the first and every one after it. The name in the sentence,
   the code in brackets straight after it: `Employer Pension (5710)`. Never a
   name alone and never a code alone. See "Account name and code, always
   together" below.

## References go at the END of the line

Invoice numbers, journal numbers, DR/CR amounts, FAR IDs: none of them sit
mid-sentence. An account's code is the one exception: it travels in brackets
with the account's name wherever the name is (rule 5).
Say what happened first, in words; the references trail at the end after a
`·` or a spaced hyphen ` - `. A reference mid-sentence is what makes these
messages unreadable. The em dash is not used in any message, and none goes
into a file here.

```
no    INV-1001 could not post because the DR 2210 480.00 / CR 1500 480.00
      journal was rejected by the FA-0001 lock date
yes   the foreign VAT on the laptop bill cannot post, the 31 Mar lock date
      rejects it - INV-1001, DR Foreign VAT (recoverable) (2210) / CR
      Computer Equipment (1500), GBP 480.00, FA-0001
```

## The reference is the number Xero shows, never a GUID

The reference that closes a line is the one the reader can type into Xero and
find:

- a manual journal: its Xero journal number, the `#` number on the journal
  page, e.g. `#1001`
- a bill or invoice: the invoice number
- spend or receive money: the entity, bank account, date and payee, there is
  no number to give
- a fixed asset: its register number, e.g. `FA-0001`

Never a Xero GUID or the first eight characters of one. Nothing in Xero
searches on it, so a line that ends in one cannot be checked.

The API returns journal numbers only on the general-ledger Journals feed,
which needs the `accounting.journals.read` scope. While the app does not hold
it, a journal is given by entity, date and the opening words of its
narration, which is what the Manual Journals list searches on, and the
message says once that the numbers could not be read. Never guess or invent
one.

```
no    the VAT restated to the documents - GBP 180.00, journal 0a1b2c3d
yes   the VAT restated to the documents - DR Foreign VAT (recoverable)
      (2210) / CR Computer Equipment (1500), GBP 180.00, HoldCo
      journal #1001
```

## Quoting an invoice

Every time an invoice is named, anywhere, in exactly this shape: supplier
first, number last, per the rule above:

```
<supplier> · <date> · <CCY> <amount> · paid <entity> · recognised <entity> · <number>
Contoso Cloud · 04 Sep · EUR 1,240.00 · paid OpCo EU · recognised OpCo EU · INV-1002
```

- Date `04 Sep`; add the year only when it is not the current one
- Amount with thousands separators and 2dp, currency code in front
- Entities by short name (`config.entity(...).short`)
- Paying and recognising entity are both stated even when they are the same
- A field you do not have is **dropped**, never written as unknown or n/a. If
  the missing field is the point, that is what the query line says
- `paid <entity>` comes from the bank feed or the bank API
  ([BANKING.md](BANKING.md)), looked up at intake by amount and date; it is
  dropped only when the cash has not settled yet

## Reporting a correction: what changed, which accounts, why

A correction is the hardest thing to report and the easiest to report badly.
Ledger shorthand ("the VAT taken out of the claim and the second invoice
put in") tells the reader nothing: it names an effect on a document instead
of a movement between accounts, and it never says why. Every correction is
three parts, in this order, in one sentence or two:

1. **What was changed, conceptually**: in plain words, naming the thing
   corrected. Not "restated to what the documents show" but "the VAT on
   the employee's laptop expense claim".
2. **What moved in the accounts, by ACCOUNT NAME**: which account was
   debited and which was credited, in words. The movement **is** the
   correction; a line that omits it has not said what was done. Name and
   code together, per rule 5.
3. **Why**: the fact that made the old entry wrong, and the rule it breaks.
   "Because" belongs in every correction line. Without it the reader cannot
   tell a fix from a mistake.

### Account name and code, always together

**Hard, universal, absolute rule.** Every time a message refers to an
account, it gives the account's name and its code together:
`Employer Pension (5710)`, `Salaries (5700)`, `Payroll Taxes Payable (825)`.
No exceptions: not in a heading, a DR/CR string, a table, a query, a chase, a
correction, or the second mention in the same message. The reader does not
know the codes, so a code alone tells them nothing; the ledger is searched by
code, so a name alone cannot be checked. Use the name exactly as the entity's
chart of accounts holds it, so it can be found, and read it from the chart
when unsure rather than paraphrasing it. The account is still what the
sentence is about: write the sentence around the name, and the code rides in
its brackets.

```
no    the employee share goes to 5700, the employer share to 5710
no    the employee share goes to salaries, the employer share to pension
yes   the employee share goes to Salaries (5700), the employer share
      to Employer Pension (5710)
```

```
no    DR 814 / CR 5700
yes   DR Wages Payable (814) / CR Salaries (5700)
```

```
no    the foreign VAT taken out of the laptop claim and the second
      invoice put in, so the VAT is now what the documents show, at the
      spend's own rate - EUR 210.00, DR 2210 / CR 1500, GBP 180.00
yes   the VAT on the employee's laptop expense claim was being reclaimed as
      recoverable foreign VAT. Moved out of Foreign VAT (recoverable) (2210)
      and into Computer Equipment (1500), because the seller charged the
      VAT of a country where the entity is not registered, and the entity
      can recover only the VAT of the country where it is registered
      - GBP 180.00, HoldCo journal, 02 Jul
```

```
no    the hotel and the restaurant restated to the VAT their documents
      show, and the domestic claim reversed in full because those claims
      never carried domestic VAT - EUR 4.00 and EUR 3.50,
      DR 2250 15.00 / CR 2210 6.50 / CR 5100 8.50
yes   two receipts from a trip abroad had been claimed at the domestic VAT
      rate. Reversed Input VAT (2250) in full and put the real foreign VAT
      into Foreign VAT (recoverable) (2210), with the remainder back into
      Travel (5100), because the hotel and the restaurant both charged
      foreign VAT and neither carried any domestic VAT
      - GBP 15.00, HoldCo journal, 29 Jul
```

### Never repeat the journal's own narration

The narration was written to say the same thing the line already says, so
quoting it back doubles the length and adds nothing. State the correction in
your own words and stop; the journal number is enough for anyone who wants
the narration itself.

```
no    ... - GBP 180.00 · HoldCo journal · 02 Jul · "Laptop claim
      reimbursement - restate foreign VAT to the documents"
yes   ... - GBP 180.00, HoldCo journal, 02 Jul
```

## Queries and chases: short, specific, one question

A query or chase is three parts in this order, two lines at most: the
situation in one clause, what is missing in one clause, then the exact ask.
The ask names the one thing the reader must send or the one fact they must
confirm. If it can be answered by sending one file or one word, it is
specific enough; if the reader has to work out what is wanted, it is not.

Leave out everything the reader does not need to act on: what is or is
not in Xero, how a figure was derived, tax estimates, "about" or "roughly"
amounts, history, consequences. The invoice line goes at the end in the
standard shape.

```
no    The September office rent invoice - only the card payment confirmation
      is in Xero, EUR 12,400.00 covering three invoices, and this is the one
      we have never seen. About EUR 4,150.00 of it, roughly EUR 690 of
      VAT - INV-1003
yes   three invoices from the landlord make up one card payment of
      EUR 12,400.00 on 14 Aug, the September rent invoice is the last one
      missing. Please send the invoice PDF, not the payment confirmation
      · Example Workspaces · Sep · EUR 4,150.00 · paid OpCo EU
      · recognised OpCo EU · INV-1003
```

The `queries` section of a report holds the same line, so the admin sees the
exact question that was put.

## Plain language

Terse is not the same as compressed. Write ordinary English: short
sentences, one fact per bullet, the subject named rather than implied, the
local word for a thing rather than the ledger's shorthand. Journal numbers and
DR/CR strings are references: they trail at the end of the line and are
never what the sentence is made of. Someone who does not know the ledger must
still be able to read a line and say what happened.

```
no    USD block journalled GBP-for-USD, 814 residual cleared
      DR 814 / CR 5700
yes   the staff paid in dollars were booked for August as though the
      figures were pounds. The journal takes the overstatement out and
      clears Wages Payable (814) - DR Wages Payable (814) / CR Salaries
      (5700), GBP 2,150.00
```

## The house style, applied to every message

The repo has a house style for what is written down, the `writing-style`
skill. That skill exempts chat; the exemption is lifted for the agent. Its
vocabulary rules and its AI tells govern **every message the agent sends on
any channel** and every .md file it writes here. Where the skill and this file
disagree, **this file wins**, and the differences are listed at the end of the
section. Load the skill in full before drafting a document or a policy.

### Say it the way it would be said out loud

Before a line goes out, ask whether an accountant would say it in that form.
This is the rule that decides whether a report reads written or reads
generated.

**An abstract noun does not do a physical thing.** Name the mechanism and pay
the extra four words.

```
no    the lock date blocks the correction and the balance flows into April
yes   Xero refuses the journal because the period is locked at 31 Mar, so the
      balance is still sitting in March
no    the feed clears as the bills land
yes   a line comes off the bank feed once the bill accounting for it is posted
```

**A record does not do a person's job, and a person does not do a record's.**
A ledger contains, records and lists things. A person is responsible for it.

```
no    the ledger carries the July invoices and the bookkeeper holds the receipts
yes   the ledger lists the July invoices. The bookkeeper is responsible for
      sending the receipts
```

**Name the person and give them the verb.** Whoever has to act is the subject
of the sentence and the verb is one they perform. A "by X" phrase, or an -ing
clause hanging off a comma, buries the one fact the reader needed.

```
no    the bill is to be reconciled to intercompany, the bookkeeper being copied
yes   reconcile the bill in HoldCo to intercompany. The bookkeeper has the receipt
```

**Name the thing, never gesture at it.** "The relevant approval", "the
appropriate account", "the applicable rate" are placeholders that survived into
the message. Name the account, the rate, the person.

**Supplier is the right word and is never edited out** of a message or a
file. The group has suppliers and their invoices are the whole bookkeeping
domain. The house style's checker flags customer-facing language; on
anything the accounting agent writes about suppliers, that finding is wrong.

### The moves that carry the voice

The table below is a list of failures. These are the two shapes worth reaching
for, and the rule that stops either becoming a mannerism.

**"X rather than Y", where Y is something somebody might actually do.** It
reframes an instruction as a choice already made, which is why it lands softer
and sticks harder.

```
no    reconcile it before month end, rather than after
yes   ask rather than posting a guess
yes   the bank wins and Xero is what gets corrected
```

Where Y is only the thing the sentence already said, negated, cut it: that is
the tacked-on corrective in the table below.

**The reason as a bare fragment**, dropping the connective and letting the
reason stand as its own short sentence. It has to name something concrete that
happens, not an abstraction.

```
no    the exposure is real
yes   the bank feed imports on its own and the line is matched by hand
yes   prices climb closer to the date
```

**Count them.** Either move three times in one message stops being a voice and
starts being a performance. The same goes for a very short closing imperative:
two in a long report, none after a table.

### The AI tells

Each one is a shape rather than a phrase, which is why they survive a
find-and-replace and why the draft gets read once looking only for them. They
apply to a Slack message, a run report, a terminal answer and a covering note
alike.

| Tell | What it looks like | Instead |
|---|---|---|
| Negation knocked down | "the bank is not the problem, the coding is" | write the second half only. Delete the negative half and the sentence still says everything |
| Tacked-on corrective | "reconcile it before month end, not after" | stop at the requirement |
| Ruled-out branch | the case that does not apply, then the news that it does not apply | state where we land. A fact that would change the answer belongs in the query, not in the analysis |
| Elaborating negative list | a statement, then a run of things it is not | the statement carried it. Cut the list |
| Participial tail | "posted to Travel, ensuring the entities match" | cut everything from the comma |
| Trailing actor clause | "approved by the controller, the director receiving the pack" | one clause per person, each of them the subject of a verb |
| Closing summary | a last line restating the report | close on the last concrete point |
| Announcing your own clarity | "Put simply", "To be clear", "It is worth noting" | if it is simple the reader will notice |
| The colon reveal | "The real problem: the lock date" | a bare noun after a colon is a headline, not a sentence |
| The rule of three | three parallel items where there are two or four | count from the facts. Real lists are lopsided |
| Stacked hedges | "may be worth considering whether" | one hedge, or go and find out the answer |
| Synonym cycling | bill, invoice, document, item, all for the same thing | one word, every time, even in the same line |
| Manufactured transitions | "Additionally", "Moreover", "That said", "Notably", "Crucially", "Ultimately" | start the sentence |
| The correction echoed back | listing an instruction back as fragments before doing it | do the thing, then say what was done |
| The acknowledgement opener | "Understood.", "Got it.", "Noted.", "You're right." | start with the work |
| Staccato fragment list | "The lock date. The entity. The rate." | write the sentence |
| The line that would fit any group | no entity, no account, no number, no date in it | it is not finished |

### Mechanics

- **No em dash anywhere.** The reference tail is `·` or a spaced hyphen ` - `
- **Digits, never the word-then-digit form.** `14 days`, `2 weeks`, never `five (5) working days`
- **Metric units**, ISO dates in files
- **British spelling** for -ise and -our: recognise, authorised, organisation, behaviour. One fixed exception spelled American: **program**
- **No italics**, with the single exception this file already sets: the manual action instruction in a `MANUAL ITEMS` thread
- **Bold a whole sentence**, and only the one thing a section turns on. Never scattered across ordinary nouns
- **Contractions** are fine in a chase or a DM. Not in an instruction someone has to follow: "do not post" beats "don't post"
- **Currency code in front of the amount**, `EUR 1,240.00`, per the invoice shape above
- **Bullets do not end with a full stop**, even a bullet of several sentences
- **Name the entity, not the group.** The house style's naming ladder (the group's full name, the Group, the organisation, we) is for a document about the group as a whole. A message names the entity the money moved in, by its short name

### When the agent writes a document rather than a message

A rulebook in `rules/` or `docs/`, a skill, a policy or a briefing note is a
document, and the skill governs it in full: the unheaded opening paragraph,
the four beats where a rule needs defending, plain flat sentences for the
procedural middle, the naming ladder, the local-requirements paragraph and `### Review`
in a policy alone, and the length targets. CLAUDE.md says what a .md file may
hold; the house style says how it is written.

**Load the skill before drafting, and run its `scripts/check_style.py` over
the file afterwards.** Both, every time, including for a file a script
generates: the checker catches heading levels on a generated register and
misses an italic subtitle in its Word copy, so neither pass replaces the
other. Two of its findings are wrong by design and are not defects to fix. It
assumes every document is a policy, so ignore the missing `### Review` on a
register, a briefing note or a summary. It cannot see a ruled-out branch, so
read for those yourself. A file may also carry standing overrides of its own,
recorded in the skill that owns it rather than argued again each time; the
`outstanding-items` skill holds those for `docs/bookkept/OUTSTANDING.md`.

A file that mixes the two, a page of messages inside a document, follows both
in their own places: the lines are messages and this file governs them, the
title, the opening paragraph and the headings are the document and the house
style governs those.

Running the checker over this file is a special case worth knowing about
before somebody "fixes" it: it quotes the banned vocabulary and every AI tell
in order to ban them, so it reports itself as full of both. The findings on
the tells table, the banned vocabulary list and the invoice shape are the
examples doing their job.

### Where this file wins

| The skill says | Here |
|---|---|
| Currency after the number, `300 USD` | Currency code in front, `EUR 1,240.00`, per the invoice shape |
| `###` Title Case headings and an unheaded opening paragraph | a report opens on its headline and its sections are bold and lowercase |
| No italics | the manual action instruction is italic, per the manual items shape |
| Every rule carries its reason | a message gives a reason only where this file asks for one: a correction says why, a query says what is missing. Everywhere else the fact stands alone |
| Chat is out of scope | the agent's messages follow the tells above, on every channel |
| `supplier` is customer-facing language | the group has suppliers and their invoices are the work; the word stays |
| `###` Title Case headings in a document | every .md in this repo uses sentence case, CLAUDE.md included, and one file converted alone reads as the odd one out. Sentence case stays |
| 300 to 600 words for a policy | a message is as short as the facts allow, and the five rules at the top of this file decide what goes in it |

## One Slack thread, one agent, one reply place

Each top-level Slack message an admin sends is its own agent, and the agent's
answer goes into that message's thread and nowhere else. Two messages are two
agents with two threads, even from the same person a minute apart.
Everything the admin then writes inside a thread, with or without `run`, is
for that thread's agent, which answers in the same thread; the thread never
spawns a second agent however many messages it holds. So a reply is never
posted at the top level of the DM, never into another thread, and never as a
fresh DM when a thread exists.

## Where a message goes: the channel or the person

There is one channel, `config.slack().channel_id` (named
`config.slack().channel_name`), and three things go in it. Everything else
stays in the DM thread of whoever asked for it.

Every headline carries the run's own start time in the group's timezone
(`config.company("timezone")`), in the shape `(18 Sep 2026, 14:32 <tz>)`,
`<tz>` being whatever abbreviation that timezone shows on the day, such as
UTC, CET or EST.

### A bookkeeping run's report goes in the channel

The whole report (bookkept, resolved, to confirm, bill payments, not
attempted, blocked, queries, manual, chased, from users, wrote back, every
section it has) is posted to
the channel as **two messages**: a top-level message that is only the
headline,

```
BOOKKEEPING RUN (18 Sep 2026, 14:32 UTC)
```

and then the report itself as the **first reply in that message's thread**.
The top-level message carries nothing else: it is the marker the team
scrolls, the thread is where the detail sits.

The admin whose thread started the run still gets a reply there, but only the
one-line headline the report opens with and `full report in #<channel_name>`,
never the report twice.

### Nothing else does

A `query` answer, a report on a named task, a focused run, a status reply, a
chase, a question to a user: all unchanged, all into the thread or DM of the
person who asked, and never into the channel. Of the runs an admin asks for,
only a bookkeeping run and the outstanding-items list (below) report there.

### Every scheduled run reports in the channel

A run cron started has nobody's thread to reply in, and the point of
scheduling it is that the team can see overnight what was done to the books
without asking. So every scheduled run reports in the channel, whatever its
domain: the bookkeeping chain, the balance reports, the review, the
outstanding list. The shape is the bookkeeping run's: a top-level message
that is only the run name and the date and time it started, the whole report
as the first reply in that thread, and the manual items as their own headline
and thread (below).

```
INTERCOMPANY RECONCILIATION (19 Sep 2026, 03:14 UTC)
```

This changes where a scheduled run reports, not what it says: the sections,
the invoice shape and the references-at-the-end rule are the same as anywhere
else. The same run asked for by an admin in a DM still answers in that
thread: it is the schedule, not the task, that sends it to the channel.

### A script that posts is a run, and posts the same two messages

Some runs post themselves rather than going through a session, such as the
balance reports summary. A script is not an exception to anything above: it
sends a top-level message that is only the run name and the date and time it
started, and the detail as the first reply in that thread.

```
REPORTS RUN (27 Sep 2026, 03:14 UTC)
```

One flat message with the headline and the whole report in it is the thing
this rule exists to stop. The top-level line is what the channel is scrolled
for; everything a reader has to read sits under it, not beside it.

### Manual actions go in the channel, always

Every line whose action belongs to a human (what a skill calls flagging a
manual action needed) reaches the channel one way or the other. The report
is one person's DM and scrolls away; the channel is the standing list of what
the books still need done by hand.

What counts:

- the `bill payments` instruction to reconcile a bill to intercompany by hand
  (`bill-payments` skill), every time one is reported
- **every control-account reconciliation, payroll above all**. Where the
  agent posts spend money onto a payroll control account
  (`config.payroll_controls()`: wages payable, payroll taxes, social
  contributions), the statement line still has to be matched to it by hand,
  and the bill still has to be cleared **from** that control account, which
  is a payment and therefore the user's. Both halves are manual actions and
  both go in the channel
- any other step only a person can take in Xero: a reconciliation, a UI-only
  action, a correction a lock date refuses until someone opens it

What does not: chases (they are DMs to whoever holds the missing thing),
queries (they go to the admin whose run raised them) and anything the agent
did itself.

**Every run posts them in the channel as their own headline and thread, the
bookkeeping run included.** A line buried in a run's report thread is not a
standing list: the channel is scrolled at the top level, so a manual action
that only exists inside the report's thread is one nobody sees. The shape is
the same as every run report, **two messages**: a top-level message that is
only the headline,

```
MANUAL ITEMS (18 Sep 2026, 14:32 UTC)
```

and the detail as the **first reply in that message's thread**, opening with
the run it came from, then one bullet per item:

```
from the bookkeeping run of 18 Sep 2026, 14:32 UTC

• Adventure Works Hotels · 28 Aug · GBP 240.00 · booked to Travel (5100) · INV-1004 - paid by OpCo US, recognised as a bill in HoldCo, spend money posted in OpCo US. *Manually reconcile the bill in HoldCo to intercompany.*
• Tax authority · 16 Sep · GBP 3,200.00 · paid HoldCo - spend money posted to Payroll Taxes Payable (825). *Match the statement line to it, then clear the August payroll bill from Payroll Taxes Payable (825).*
```

The top-level message carries nothing else. One headline per run, not one
per item, and it is sent in addition to the report, never instead of it. A
run with no manual items sends none. For a bookkeeping run the order is: the
run headline at top level, the report in its thread, then the manual items
headline at top level with its detail in its thread. The detail repeats what
the report's `bill payments` and `blocked` sections say and that repetition
is the point.

### Closing a manual action: `done` in its thread

A manual action is closed by the admin who did it replying `done` in the
thread of the `MANUAL ITEMS` headline that raised it, under the detail.
`done` alone closes every item in that thread; `done <reference>` closes the
one whose reference it names. Nothing else closes one except Xero itself
showing it done. The outstanding-items list reads these replies, so an action
done in Xero but never marked stays on the list until the ledger check finds
it.

### The outstanding-items list goes in the channel, whoever asks

`run give me all outstanding items` (skill `outstanding-items`) gathers every
manual action, query and unanswered chase the runs have raised and nobody has
closed. Its list is posted to the channel at top level, however and wherever
it was asked for, a DM included: it is the standing list the team scrolls,
and a list in a thread is one nobody sees. The thread it was asked in gets
the headline line and `full list in #<channel_name>`, nothing else.

The list is not rebuilt from Slack when somebody asks for it. It is a
standing register, `docs/bookkept/OUTSTANDING.md`, one section per domain
(`config.domains()` plus `platform`) and inside each one `manual items`,
`queried` and `blocked`, and every message that raises one of those writes it
there in the same step that sends it: `scripts/outstanding.py add`. The line
is deleted with `scripts/outstanding.py close` in the same step that posts
the fix, and marked with `scripts/outstanding.py answer` where an admin has
ruled and the posting is still ours. A query put to somebody in a DM, a
manual action posted to the channel and a blocked line inside a run report
are all the same thing to the register, and none of them is exempt.
`run pull me the outstanding items list`, in any wording, is answered from
the register, reconciled against the run threads, the intake ledger and Xero
read-only before posting, per the skill. The skill `outstanding-items` owns
the file, the shape and the closing evidence.

### How

`chat.postMessage` with the channel ID as `channel` and the same
`SLACK_BOT_TOKEN` as every other message; the reply repeats the call with
`thread_ts` set to the `ts` the first call returned. The bot must be a member
of the channel: in a private channel a bot that was never invited answers
`channel_not_found`. If that happens, the report says so under `blocked` and
goes to the admin's thread in full instead of being dropped.

## Never write

No preamble ("I've gone ahead and", "Just to confirm", "As requested", "Great
question"), no sign-off ("Let me know if", "Happy to", "Hope that helps"), no
filler ("successfully", "please note", "it's worth noting", "I can see that").
No restating the request back. No summary of a summary. No emoji.

Never pad with a section that says nothing happened: drop the section. The
one exception is `queries`, where `nothing queried` is information.

Banned vocabulary, from the house style, wrong in anything the agent writes:
robust, comprehensive, streamline, leverage, stakeholders, seamless, holistic,
best-in-class, deep dive, touch base, circle back, going forward, and "in order
to" where "to" does the job. The filler adjectives key, critical, significant
and notable. "It is expected that" and "where appropriate" used to dodge a
decision. Never leave the authority unnamed: name the person who has to act,
or write **[owner to be named]** so the gap is visible. Never describe a check as
though it ran when it did not, and never overclaim what one catches: "no code
path skips it", "the only check that would find it", "any one route is enough
on its own". Say what the check did and let the gap stand in its own words.

## Shape: bold section headings, bullets underneath

More than one category means sections. The heading is **bold** and lowercase;
every line under it is a bullet; each bullet is terse and carries only what is
needed to act on it.

The standard headings, in this order. Use only the ones with content:

| Heading | What goes in it |
|---|---|
| **bookkept** | posted, done, nothing needed from anyone |
| **resolved** | a question the run would have asked, answered by the run at high confidence and acted on: the answer, then the evidence in one clause |
| **to confirm** | the same at medium confidence: posted, and waiting on an admin's yes or no, with what to reply |
| **bill payments** | an open bill paid from another entity's bank: the payer's spend money is posted, the bill is named in full, and the instruction to reconcile it to intercompany by hand (`bill-payments` skill) |
| **not attempted** | in scope but never started, and why in four words |
| **blocked** | tried, could not, and the specific thing standing in the way |
| **queries** | needs an answer before it can move, with the question stated |
| **manual** | a step only a person can take, named with what to do |
| **chased** | a message sent to someone, one line each |
| **from users** | what a user said and what was done with it |
| **wrote back** | rules written into `rules/`, docs or skills this run |

`not attempted` and `blocked` are different and must not be merged. One says
nobody tried; the other says it was tried and failed. Conflating them hides
which of the two the reader has to act on.

```
bookkeeping · 14 bookkept · 3 resolved · 1 to confirm · 2 blocked · 1 query · 1 manual · check PASS

**bookkept**
• Contoso Cloud · 04 Sep · EUR 1,240.00 · paid OpCo EU · recognised OpCo EU · INV-1002
• Tailspin Telecom · 02 Sep · USD 96.00 · paid OpCo US · recognised OpCo US · INV-1005

**resolved**
• OpCo US, the invoice is billed to it and its last three bills sit there · Fabrikam Travel · 03 Sep · USD 410.00 · paid OpCo US · recognised OpCo US · INV-1007

**to confirm**
• Software (6300) in OpCo EU, a new supplier selling the same tool as Litware Software. Reply yes, or the right account · Northwind Store · 06 Sep · EUR 180.00 · paid OpCo EU · recognised OpCo EU · INV-1008

**not attempted**
• the July Contoso Cloud invoices for OpCo US, deferred to the August review

**blocked**
• the foreign VAT on the laptop bill cannot post, the 31 Mar lock date rejects it
  - INV-1001, DR Foreign VAT (recoverable) (2210) / CR Computer Equipment (1500),
      GBP 480.00, FA-0001

**queries**
• Litware Software, which entity - 05 Sep, USD 3,400.00, INV-1006

**manual**
• reconcile the HoldCo Adventure Works Hotels bill to intercompany - INV-1004

**chased**
• the bookkeeper for the 28 Aug Adventure Works Hotels receipt, HoldCo
```

### `queries`, `to confirm` and `manual`, in every domain, in every message

Anything a message leaves with a person sits under one of three headings and
nowhere else. `queries` is a question waiting on an answer; `to confirm` is
a decision the run already made and posted, waiting on an admin's yes or no;
`manual` is an action waiting on a pair of hands. They are three sections,
never one: a question nobody has answered, a posting nobody has checked and a
job nobody has done are different states and the reader acts on each
differently.

`resolved` and `to confirm` exist so that `queries` stays short. A run
answers a question itself before it asks it (CLAUDE.md, "Queries: answer them
before asking them"), so what reaches `queries` is what the evidence could
not settle, and each one carries the run's proposed answer:

```
no    Litware Software, which entity - 05 Sep, USD 3,400.00, INV-1006
yes   Litware Software, OpCo EU or OpCo US? Proposed OpCo EU (low): billed to
      OpCo EU, paid by OpCo US's card · 05 Sep · USD 3,400.00 · INV-1006
```

This holds for **every message, in every domain**: a bookkeeping run, a
report, a reconciliation, a review, a focused run, an answer in a DM thread.
A run that leaves a person something to do and buries it in prose, or files
it under `blocked` or `flagged`, has hidden it. If the section has no content
it is dropped, like any other, except `queries`, where `nothing queried` is
information and the line stays.

The three headings carry the same lines the standing register does, so the
message and `scripts/outstanding.py add --kind queried|decided|manual` agree
with each other. Registering the line and writing the section are the same
step. `resolved` is not registered: nothing is left with anyone.

For a focused run the headline is the task in a handful of words, then the
answer, then the evidence, nothing else.

Bills are AUTHORISED and unpaid; say so once, at the end, in those words.
Never repeat it per bill. Over 15 bills, list the 10 largest and end with
`+N more`. Emails labelled and ledger lines appended are counts only, one
line.

If the run failed, or the phantom-payment check did not print PASS, that goes
in the headline. Never report a clean run the log does not show.
