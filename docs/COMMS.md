# House style for everything the agent writes

**Hard rule.** Every message the agent sends to anyone, on any channel, without
exception (Slack DMs, chases, thread replies, run reports, terminal answers),
and every .md file or document it writes, follows this file or is not sent.
There is no "quick note" that is exempt, and no chat exemption. This file is
the house style: the `writing-style` skill only points here.

The failure this file exists to stop: messages that are too wordy and carry
too much that nobody needed.

Examples use the fictional group in `config/group.example.toml` (HoldCo, OpCo
US, OpCo EU) and fictional suppliers and account codes. Entity short names in
a real message come from `config.entity(...).short`.

Every message needs the sections down to "Plain language". Read the rest as
the task needs: report shape, where a message goes, the tells and mechanics,
documents.

## The five rules

1. **A question gets its answer and nothing else.** No background it did not
   ask for, no adjacent findings, no next steps, no offer to do more. If the
   answer is a number, send the number.
2. **Necessary information only, and terse.** Every word earns its place. Lead
   with the fact, never with what you are about to say.
3. **Sections whenever there is more than one category.** Never a flat wall of
   lines. See "Report shape".
4. **Report every message you send**, one line each: `chased the cardholder
   for the 28 Aug hotel receipt, OpCo US`. A chase or a question is an action.
5. **Every account is its name and its code, every time**, terminal answers
   included (below).

## References go at the end of the line

Invoice numbers, journal numbers, DR/CR amounts and fixed asset numbers never
sit mid-sentence. Say what happened first, in words; the references trail
after `·` or a spaced hyphen ` - `. An account's code is the one exception: it
rides in brackets with the account's name.

```
no    INV-1001 could not post because the DR 2210 480.00 / CR 1500 480.00
      journal was rejected by the FA-0001 lock date
yes   the foreign VAT on the laptop bill cannot post, the 31 Mar lock date
      rejects it - INV-1001, DR Foreign VAT (recoverable) (2210) / CR
      Computer Equipment (1500), GBP 480.00, FA-0001
```

**The reference is the number Xero shows, never a GUID or part of one.**
Nothing in Xero searches on a GUID, so a line ending in one cannot be checked.

- a manual journal: its `#` number on the journal page
- a bill or invoice: the invoice number
- spend or receive money: entity, bank account, date and payee
- a fixed asset: its register number, e.g. `FA-0001`

The API returns journal numbers only on the general-ledger Journals feed
(scope `accounting.journals.read`). Until the app holds it, give a journal by
entity, date and the opening words of its narration, and say once that the
numbers could not be read. Never guess or invent one.

## Quoting an invoice

Every time an invoice is named, anywhere, in this shape:

```
<supplier> · <date> · <CCY> <amount> · paid <entity> · recognised <entity> · <number>
Contoso Cloud · 04 Sep · EUR 1,240.00 · paid OpCo EU · recognised OpCo EU · INV-1002
```

- Date `04 Sep`, the year only when it is not the current one
- Amount with thousands separators and 2dp, currency code in front
- Entities by short name; paying and recognising entity are both stated, even
  when the same
- A field you do not have is **dropped**, never written as unknown or n/a. If
  the missing field is the point, the query line says so
- `paid <entity>` comes from the bank feed or the bank API
  ([BANKING.md](BANKING.md)), looked up at intake by amount and date; dropped
  only when the cash has not settled yet

## Account name and code, always together

**Hard, universal, absolute.** Every mention of an account, the first and
every one after it, in every message, channel and domain, in a heading, DR/CR
string, table, query, chase, correction or terminal answer: the name, then the
code in brackets. Never a name alone (the ledger is searched by code) and
never a code alone (the reader does not know the codes). The name is exactly
as the entity's chart of accounts holds it; read it from the chart when
unsure. The account is still what the sentence is about: write the sentence
around the name, and the code rides in its brackets.

```
no    the employee share goes to 5700, the employer share to pension
yes   the employee share goes to Salaries (5700), the employer share to
      Employer Pension (5710)
no    DR 814 / CR 5700
yes   DR Wages Payable (814) / CR Salaries (5700)
no    the August prepayment release is posted
yes   the August release of the annual licence is posted from Prepayments
      (620) to Software (6300), EUR 100.00
```

## Reporting a correction: what changed, which accounts, why

Ledger shorthand ("restated to the documents") names an effect on a document
instead of a movement between accounts and never says why. Every correction is
three parts, in this order, in one sentence or two:

1. **What was changed**, in plain words naming the thing: "the VAT on the
   employee's laptop expense claim", not "restated to what the documents show"
2. **What moved**, by account name and code: which was debited, which
   credited. The movement is the correction; a line that omits it has not said
   what was done
3. **Why**: the fact that made the old entry wrong and the rule it breaks;
   "because" belongs in every correction line

```
no    the foreign VAT taken out of the laptop claim and the second invoice
      put in, EUR 210.00, DR 2210 / CR 1500, GBP 180.00
yes   the VAT on the employee's laptop expense claim was being reclaimed as
      recoverable foreign VAT. Moved out of Foreign VAT (recoverable) (2210)
      and into Computer Equipment (1500), because the seller charged the VAT
      of a country where the entity is not registered, and only the VAT of
      the country of registration is recoverable - GBP 180.00, HoldCo
      journal, 02 Jul
```

**Never quote the journal's own narration back.** It says what the line
already says; the journal reference is enough.

## Queries and chases: short, specific, one question

Two lines at most, three parts in order: the situation in one clause, what is
missing in one clause, then the exact ask, naming the one file to send or the
one fact to confirm. If one file or one word answers it, it is specific
enough.

Leave out what is or is not in Xero, how a figure was derived, tax estimates,
"about" or "roughly" amounts, history and consequences. The invoice line goes
at the end in the standard shape. The `queries` section of a report holds the
same line, so the admin sees the exact question put.

```
no    The September office rent invoice - only the card payment confirmation
      is in Xero, EUR 12,400.00 covering three invoices. About EUR 4,150.00
      of it, roughly EUR 690 of VAT - INV-1003
yes   three invoices from the landlord make up one card payment of
      EUR 12,400.00 on 14 Aug, the September rent invoice is the last one
      missing. Please send the invoice PDF, not the payment confirmation
      · Example Workspaces · Sep · EUR 4,150.00 · paid OpCo EU
      · recognised OpCo EU · INV-1003
```

## Plain language: say it the way it would be said out loud

Terse means short ordinary English: one fact per bullet, the subject named,
the everyday word in place of the ledger's shorthand. Journal numbers and
DR/CR strings are references, never what the sentence is made of. Before a
line goes out, ask whether an accountant would say it in that form. This
decides whether a report reads written or generated, and it outranks every
move below it.

```
no    USD block journalled GBP-for-USD, 814 residual cleared DR 814 / CR 5700
yes   the staff paid in dollars were booked for August as though the figures
      were pounds. The journal takes the overstatement out and clears Wages
      Payable (814) - DR Wages Payable (814) / CR Salaries (5700), GBP 2,150.00
```

- **An abstract noun does not do a physical thing.** Name the mechanism and
  pay the extra four words. "The lock date blocks the correction and the
  balance flows into April" becomes "Xero refuses the journal because the
  period is locked at 31 Mar, so the balance is still sitting in March". Also
  the noun built from a verb: "prior to posting" is "before posting"
- **A record does not do a person's job, and a person does not do a record's.**
  A ledger contains, records and lists; a person is responsible for it. "The
  ledger carries the July invoices and the bookkeeper holds the receipts"
  becomes "the ledger lists the July invoices. The bookkeeper is responsible
  for sending the receipts"
- **Name the person and give them the verb.** Whoever has to act is the
  subject of the sentence. A "by X" phrase or an -ing clause hanging off a
  comma buries the one fact the reader needed. "Reconcile the bill in HoldCo
  to intercompany. The bookkeeper has the receipt", not "the bill is to be
  reconciled, the bookkeeper being copied"
- **Name the thing, never gesture at it.** "The relevant approval", "the
  appropriate account", "the applicable rate" are placeholders. Name the
  account, the rate, the person
- **Supplier is the right word** and is never edited out: the group has
  suppliers and their invoices are the bookkeeping domain

**Two moves carry the voice**, each at most twice in a message or document, and
a very short closing imperative likewise (two in a long report or document,
none after a table):

- **"X rather than Y"**, where Y is something somebody might actually do: "ask
  rather than posting a guess", "the bank wins and Xero is what gets
  corrected". Where Y is only the sentence negated, cut it
- **The reason as a bare fragment** naming something concrete: "the bank feed
  imports on its own and the line is matched by hand", never "the exposure is
  real"

## The AI tells

Shapes, not phrases, which is why they survive find-and-replace. Read every
draft once for them alone, covering note and reply to the requester included:
they apply to a Slack message, a run report, a terminal answer and a document
alike.

| Tell | Looks like | Instead |
|---|---|---|
| Negation knocked down | "the bank is not the problem, the coding is"; "not just X but Y" | the second half only. Delete the negative half; if the sentence still says everything it always did |
| Tacked-on corrective | "reconcile it before month end, not after" | stop at the requirement |
| Ruled-out branch | the case that does not apply, then the news that it does not | state where we land. A fact that would change the answer goes in the query or a document's trigger list. Test: does a reader do anything differently |
| Elaborating negative list | a statement, then a run of things it is not | the statement carried it; cut the list |
| Participial tail | "posted to Travel, ensuring the entities match" | cut everything from the comma |
| Trailing actor clause | "approved by the controller, the director receiving the pack" | one clause per person, each the subject of a verb they perform |
| Closing summary | a last line restating the report | end on the last concrete point |
| Announced clarity | "Put simply", "To be clear", "It is worth noting", "Importantly" | cut it |
| Colon reveal | "The real problem: the lock date" | a bare noun after a colon is a headline; substance on both sides or no colon |
| Rule of three | three items where the facts have two or four | count from the facts; real lists are lopsided |
| Stacked hedges | "may be worth considering whether" | one hedge, or find out |
| Synonym cycling | bill, invoice, document, item for one thing | one word every time |
| Manufactured transition | "Additionally", "Moreover", "Furthermore", "That said", "Notably", "Ultimately" | start the sentence |
| Correction echoed back | the instruction listed back as fragments before doing it | do it, then say what was done |
| Acknowledgement opener | "Understood.", "Got it.", "Noted.", "You're right." | start with the work |
| Staccato fragments | "The lock date. The entity. The rate." | write the sentence |
| Fits any group | no entity, account, number, date or system in it | not finished |

Also: no overclaiming a control or check ("no code path skips it", "the only
check that would find it", "any one route is enough on its own"); say what it
did and let the gap stand. Never describe a check as though it ran when it did
not. A control that does not exist yet is written as "This control is not yet
built."

## Never write

No preamble ("I've gone ahead and", "Just to confirm", "As requested"), no
sign-off ("Let me know if", "Happy to"), no filler ("successfully", "please
note", "it's worth noting"). No restating the request. No summary of a
summary. No emoji. Never pad with a section that says nothing happened: drop
it, except `queries`, where `nothing queried` is information.

Banned: robust, comprehensive, streamline, leverage, stakeholders, seamless,
holistic, best-in-class, deep dive, touch base, circle back, going forward,
"in order to" (use "to"); the filler adjectives key, critical, significant,
notable; "it is expected that", "should endeavour to" and "where appropriate"
used to dodge a decision; aspirational padding that carries no rule. Never
leave an authority unnamed ("senior management", "the appropriate manager"):
name the person or role, or write **[owner to be named]** so the gap shows.

## Mechanics

- **No em dash anywhere**, in a message or a file: `·`, a spaced hyphen ` - `,
  a colon or a full stop. The spaced hyphen also parts condition from outcome
  in a list, sparingly
- **Digits**, never the word-then-digit form: `14 days`, not `five (5) working
  days`. Metric units, ISO dates in files
- **Currency code in front of the amount**, `EUR 1,240.00`, in messages and
  files, never a symbol; "or local equivalent" where a limit spans entities
- **British spelling**, -ise and -our: recognise, authorised, organisation,
  behaviour; the one American exception is **program**
- **No italics** and no "Note:" asides, except the manual action instruction
  in a `MANUAL ITEMS` thread. **Bold a whole sentence**, only the one a section
  turns on, never scattered across nouns
- **Contractions** are fine in a chase, DM or second-person text, never in an
  instruction or prohibition: "do not post"
- **Bullets never end with a full stop**, even a bullet of several sentences
- **A message names the entity the money moved in**, by its short name, never
  "the group" or "the company"
- A message gives a reason only where this file asks: a correction says why, a
  query says what is missing. Everywhere else the fact stands alone
- Sentence-case headings in every .md in this repo, CLAUDE.md included

## Report shape

A **bold** lowercase heading per category, a bullet per line, each bullet only
what is needed to act on it. These headings, in this order, only those with
content:

| Heading | What goes in it |
|---|---|
| **bookkept** | posted, done, nothing needed from anyone |
| **resolved** | a question the run would have asked, answered at high confidence and acted on: the answer, then the evidence in one clause |
| **to confirm** | the same at medium confidence: posted, waiting on an admin's yes or no, with what to reply |
| **bill payments** | an open bill paid from another entity's bank: the payer's spend money posted, the bill named in full, and the bold instruction to reconcile it to intercompany by hand (`bill-payments` skill) |
| **not attempted** | in scope but never started, and why in four words |
| **blocked** | tried, could not, and the specific thing in the way |
| **queries** | needs an answer before it can move, with the question stated. A bookkeeping query reads `queries - could not answer`: it was investigated and the evidence did not settle it, or the gate said to ask. Any other domain's reads `queries - not investigated` |
| **manual** | a step only a person can take, and what to do |
| **chased** | a message sent to someone, one line each |
| **from users** | what a user said and what was done with it |
| **wrote back** | rules written into `rules/`, docs or skills this run |

`not attempted` (nobody tried) and `blocked` (tried and failed) are never
merged: the reader acts differently on each.

```
bookkeeping · 14 bookkept · 3 resolved · 1 to confirm · 2 blocked · 1 query · 1 manual · check PASS

**bookkept**
• Contoso Cloud · 04 Sep · EUR 1,240.00 · paid OpCo EU · recognised OpCo EU · INV-1002

**resolved**
• OpCo US, the invoice is billed to it and its last three bills sit there · Fabrikam Travel · 03 Sep · USD 410.00 · paid OpCo US · recognised OpCo US · INV-1007

**to confirm**
• Software (6300) in OpCo EU, a new supplier selling the same tool as Litware Software. Reply yes, or the right account · Northwind Store · 06 Sep · EUR 180.00 · paid OpCo EU · recognised OpCo EU · INV-1008

**blocked**
• the foreign VAT on the laptop bill cannot post, the 31 Mar lock date rejects it - INV-1001, DR Foreign VAT (recoverable) (2210) / CR Computer Equipment (1500), GBP 480.00, FA-0001

**queries**
• Litware Software, OpCo EU or OpCo US? Proposed OpCo EU (low): billed to OpCo EU, paid by OpCo US's card · 05 Sep · USD 3,400.00 · INV-1006

**manual**
• reconcile the HoldCo Adventure Works Hotels bill to intercompany - INV-1004
```

**`queries`, `to confirm` and `manual`, in every domain and every message.**
Anything a message leaves with a person sits under one of these three and
nowhere else, never in prose and never under `blocked`: a question waiting on
an answer, a decision already posted waiting on an admin's yes or no, an action
waiting on a pair of hands. They are never merged. A run answers a question
itself before it asks it (CLAUDE.md, "Queries: answer them before asking
them"), so what reaches `queries` is what the investigation could not settle,
with the run's proposed answer and its grade in the question, and what is
still unsure, so the admin can reply in a word. The three carry the same lines as the
register, written in the same step (`scripts/outstanding.py add --kind
queried|decided|manual`); `resolved` is not registered.

- a focused run: the task in a handful of words, the answer, the evidence
- bills are AUTHORISED and unpaid: say so once, at the end, in those words
- over 15 bills: the 10 largest, then `+N more`
- emails labelled and ledger lines appended: counts only, one line
- a failed run, or a phantom-payment check without PASS, goes in the headline;
  never report a clean run the log does not show

## Where a message goes

**One Slack thread, one agent, one reply place.** Each top-level message an
admin sends is its own agent and its answer goes into that message's thread,
nowhere else: never the DM's top level, another thread or a fresh DM.
Everything later in the thread, with or without `run`, is for that agent.

**The channel or the person.** There is one channel, `config.slack().channel_id`
(named `config.slack().channel_name`). It gets a bookkeeping run's report,
every scheduled run's report, every script that posts a run, every run's manual
items and the outstanding-items list. Everything else (a `query` answer, a
named task, a focused run, status, a chase, a question to a user) stays in the
thread or DM of whoever asked.

### A run in the channel is two messages

A top-level message that is only the run name and its own start time in the
group's timezone (`config.company("timezone")`, the abbreviation that zone
shows on the day), then the whole report as the first reply in its thread:

```
BOOKKEEPING RUN (18 Sep 2026, 14:32 UTC)
```

- The admin whose thread started a bookkeeping run gets only the one-line
  headline and `full report in #<channel_name>`, never the report twice
- Every scheduled run reports in the channel, whatever its domain
  (`INTERCOMPANY RECONCILIATION (...)`), with the same sections. The same run
  asked for in a DM answers in that thread: it is the schedule that sends it to
  the channel
- A script that posts (the balance reports summary) sends the same two
  messages, never one flat message with the headline and the report in it
- Inside a run, never `chat.postMessage` to the channel: queue the post and the
  runner sends it when the session ends, with the run's cost in brackets after
  the headline. `scripts/slack_outbox.py channel --headline "<headline>" --thread <file>`
  for a run report or the manual items (headline at top level, the file as the
  first reply in its thread); `--top-level <file>` for the outstanding-items
  list. A bot not invited to a private channel gets `channel_not_found`: the
  runner adds the queued post to the admin's thread in full instead of
  dropping it

### Manual items, always in the channel

Every line whose action belongs to a human reaches the channel, from every run:

- the `bill payments` instruction to reconcile a bill to intercompany by hand
- every control-account reconciliation, payroll above all: where the agent
  posts spend money onto a payroll control account (`config.payroll_controls()`),
  the statement line still has to be matched to it by hand and the bill cleared
  **from** that account, which is a payment and therefore a person's
- any other step only a person can take in Xero: a reconciliation, a UI-only
  action, a correction a lock date refuses until someone opens it

Not chases (DMs to whoever holds the missing thing), queries (to the admin whose
run raised them) or anything the agent did itself. A line buried in a report
thread is not a standing list, so they get their own two messages, one headline
per run, in addition to the report and never instead of it, none when there are
no items:

```
MANUAL ITEMS (18 Sep 2026, 14:32 UTC)
```

The detail is the first reply, opening with the run it came from, one bullet per
item, the instruction in italics (the one italic allowed). It repeats the
report's `bill payments` and `blocked` lines on purpose:

```
from the bookkeeping run of 18 Sep 2026, 14:32 UTC

• Adventure Works Hotels · 28 Aug · GBP 240.00 · booked to Travel (5100) · INV-1004 - paid by OpCo US, recognised as a bill in HoldCo, spend money posted in OpCo US. *Manually reconcile the bill in HoldCo to intercompany.*
```

For a bookkeeping run the order is: run headline, report in its thread, manual
items headline, detail in its thread. **Closing one:** the admin replies `done`
in that thread (closes every item) or `done <reference>` (closes one). Nothing
else closes one except Xero showing it done.

### The outstanding-items list

`run give me all outstanding items` (skill `outstanding-items`), in any wording,
posts to the channel at top level wherever it was asked; the asking thread gets
the headline line and `full list in #<channel_name>`. The list is the standing
register `docs/bookkept/OUTSTANDING.md` (per domain: `manual items`, `queried`,
`blocked`), answered from the register after reconciling it against the run
threads, the intake ledger and Xero, read-only. Every message that raises one
writes it there in the same step that sends it (`scripts/outstanding.py add`),
a DM query, a channel manual action and a report's `blocked` line alike; `close`
in the step that posts the fix; `answer` where an admin has ruled and the
posting is still to do. The skill owns the file, the shape and the closing
evidence.

### What the run cost, in brackets

Every run that reports on Slack carries its cost after the headline, the run's
own session first and any other Claude work it paid for after it:

```
BOOKKEEPING RUN (18 Sep 2026, 14:32 UTC) (total cost $2.14, run $1.64, helpers $0.50)
```

A session cannot know its own cost while it is still writing, so it writes the
headline without the bracket and never works out or states a cost itself. The
runner adds the bracket (`scripts/run_cost.py`) to each headline it posts and to
the first line of the reply in the admin's thread. Nothing is posted and then
edited. A hook refuses a direct post to the channel from a run session. Each
turn in a thread is costed on its own. The figures are the CLI's own, at API
list rates, so on a subscription token they are notional.

## Documents

A rulebook in `rules/` or `docs/`, a skill, a policy, procedure, briefing note,
memo, process doc or summary follows everything above plus this section.
CLAUDE.md says what a .md file may hold; this says how it is written. In a page
of messages inside a document, the lines are messages and the rest is a
document.

**The voice**: the group's own finance lead, who knows how the books actually
run, sets a rule down once with the reason, and trusts the reader to follow a
rule they understand. Never a template pack, a consultant's deliverable or a
contract. Where a rule below pulls against that sentence, follow the sentence.

**Before writing.** Get the facts (limits, thresholds, notice periods, who
approves what, which entities are in scope, local law, the status of each
item) and ask rather than invent numbers. Read `config/group.toml` and
`rules/GROUP.md` so nothing describes a kind of business the group is not
(customers it does not have, a brand it does not market), without pasting that
background in as a list of what the group is not. Name who holds each power or
write **[owner to be named]**. A control that does not exist is stated as such.

**Structure**

- Open with an unheaded paragraph of one to four sentences: what the document
  is for and who it applies to. No "Purpose:" label
- Headings are sentence case in this repo. A standalone document for people
  outside the repo uses `###` bold Title Case, with sub-headings as plain bold
  lines
- Name a prohibition list as one ("What must not be posted"). A table for items
  sharing a few attributes, prose for anything with a reason in it
- A standalone policy or procedure only: where its entities sit in more than
  one country, the opener closes on the local-requirements sentence ("Where the
  law of a country an entity operates in sets a different or stricter
  requirement, that requirement applies in that entity and this policy is read
  with it"), and the last section is `### Review` (how often, and sooner when
  entities, systems or limits change). A briefing note, summary or rules file
  takes neither. One that rests on facts that could change lists them with what
  each triggers, in place of counterfactuals
- FAQ only where people really ask the questions

**The four beats**, where a rule needs defending: the rule flatly; the reason
in a short clause, even when it seems obvious; the human carve-out named as
normal, not exceptional; a close under ten words. A paragraph with none is
template text. Beats in every paragraph make the cadence uniform, so most of a
document is plain flat procedure.

> Every purchase goes on a company card or through a purchase order, never a
> personal card. Reimbursements are slow to process and easy to lose. A
> personal card is fine in an emergency or where a supplier will not take ours.
> **Send the receipt the same day.**

**Signature moves**, deliberate and counted (any one three times over is no
longer a voice): colon then principle, with substance on both sides ("The
standard is the same in every entity: a cost sits where the benefit is", and
"the guiding principle is that", never "is simple:"); the stated non-policing
principle, only if true ("reporting a coding mistake promptly will never itself
be a disciplinary matter"); naming the awkward pressure a rule cuts against;
one aphorism per document, two at most. Make paragraphs lumpy: three alike in a
row means one is padded and one compressed. Dull is allowed in the procedural
middle.

**Let the specifics show.** Use the group's own nouns: its entities by name,
systems, close calendar, bank accounts, suppliers, approval chain. A paragraph
that would fit any group anywhere is not finished.

**Person and name.** Second person for what the reader has to do, third for the
group and other parties. In a document about the group as a whole, one name per
paragraph: the group's full name (`config.company_name()`) for scope and formal
duties, "the Group" by default, "the organisation" for the sharpest lines, "we"
for views; avoid "the company" in a multi-entity group and never abbreviate the
name. When a rule is about one entity, name it by short name.

**Length**: cut whole sentences, never the mechanism. Operational policy 300 to
600 words, conduct policy 600 to 1000, briefing note or summary 150 to 400;
over 1200 words of prose needs a reason you could defend out loud. A rules file
is as long as its rules, one per paragraph or row.

**Checking.** Run `.venv/bin/python scripts/check_style.py <file>` over every
.md written or generated, then read it; neither pass replaces the other. It
catches em dashes, currency symbols, banned words, bullets with full stops,
negation-knockdown and tacked-on correctives, elaborating negative lists, a
document explaining its own existence, unnamed authority, inherited template
language and the group's name abbreviated (read from `[company]`). Opt-in:
`--british` (US spellings), `--policy` (closing `### Review`, `###` Title Case
headings), `--avoid <words>` (words wrong for this group), `--review-text` and
`--local-law-text` (the group's standard wording). Read for what it cannot
see: a sentence nobody would say aloud, a named person who is not the subject
of a verb, the counted tells (threes, tails, trailing actors, synonyms,
transitions, closing summary, ruled-out branch), a missing reason, a repeated
sentence. On this file it reports the words it quotes in order to ban them;
those findings are not defects. A file's standing overrides live in the skill
that owns it (`outstanding-items` holds those for `docs/bookkept/OUTSTANDING.md`).

**Rewriting an existing document.** Read all of it and keep the paragraphs
already in house voice. List the real rules (limits, approvals, prohibitions);
the rest is padding unless it is a reason. Check each is true, rebuild, cut: a
rewrite longer than the original has usually failed. Strip wrong-organisation
language (the real reason is usually the group's own money, banking and supplier
relationships), unnamed authority, asserted controls, empty verbs ("encouraged
to", "will be aligned with"), padding, restated endings, the tells and the
mechanics. Keep legally load-bearing wording (flag it rather than rewrite it),
the local-requirements and Review wording, and every real number, reformatted,
never changed. Check each replacement phrase against this whole file: swapping
"the controller sees both" for "the controller holds the record" went
backwards. An audit lists findings worst first (wrong facts, asserted controls,
unnamed authority, missing reasons, then mechanics with a count): sentence,
fault, replacement; six that matter beat twenty. Report a rewrite as what
changed in substance, what is open and words before and after, never each
mechanical fix.

**Workflow for a new document.** Establish the facts, sketch four to seven
sections, draft, run the checker, read it aloud for register (three paragraphs
the same shape, an abstraction doing something physical, a record given a
person's verb), then once for the tells and once for voice, read the covering
message too, and cut. Ask where it should land if not said: a rule the agent
follows goes in `rules/` or `docs/`; a document for people goes in a markdown
file for review, or the document tool the user names.
