---
name: xero-bank-reconcile-check
description: Which bank lines are still open on Xero's reconcile screen, and what each one goes to. Takes every settled movement on every mapped bank account (statement CSVs, then the Revolut and Mercury APIs), pairs it one-to-one with the Xero record reconciled to it, gives each open line its record or document, flags lines matched to the wrong bill, and ties every account's balance out before anyone is asked to act. Keeps the not-yet-reconciled list in data/bankfeed/unreconciled/ and feeds the reconcile list the bookkeeper gets. Accepts a pasted Xero reconcile screen as the last word on an account. Use before any reconcile list is sent, when an admin asks what is still open on the bank feed or whether a line is reconciled, when an admin pastes a reconcile screen, and inside every bookkeeping run. Read-only against Xero and the banks.
---

# The bank reconcile check

Xero's API cannot read a statement line or whether it is reconciled, so the
agent infers it, and every inference is tied back to the money before anyone
acts on it. The check answers one question per bank line: is it still open on
Xero's reconcile screen, and if it is, what does it go to. **Nobody is asked to
reconcile a line unless this check has it open on a verified account.**

Load the `xero` skill first; its safety rules apply. The check never writes to
Xero and never touches a payment. It belongs to the bookkeeping domain and
loads nothing from the others.

## The two lists

They answer different questions and must not be confused.

| List | File | What is on it |
|---|---|---|
| The native bank feed | `data/bankfeed/<entity>.md` | lines with no document behind them yet: the chase list (`docs/BANKING.md`) |
| The not-yet-reconciled list | `data/bankfeed/unreconciled/<entity>.md` and `data/bankfeed/unreconciled.json` | every line open on Xero's reconcile screen as far as the check can tell, with its status, what it goes to and the account's tie-out |

A line leaves the native feed when its bill or spend money is posted. It stays
on the not-yet-reconciled list until a person matches it in Xero. Both live on
the server only (`data/` is gitignored).

## Commands

```bash
.venv/bin/python scripts/bank_reconcile_check.py run [entity|all] [--since YYYY-MM-DD] [--dry-run] [--sync-feed] [--no-agent]
.venv/bin/python scripts/bank_reconcile_check.py confirm <entity> "<account>" --paste <file> [--at ISO]
.venv/bin/python scripts/bank_reconcile_check.py show [entity]
```

`<entity>` is any key, slug, alias or short name from `config/group.toml`.
Which entities are read comes from each entity's `reconcile_check`: `full`
(the default with a bank account), `documents` (no bank account: its open
documents are still read for the cross-entity search) or `off` (never read).
`--cache DIR` keeps the pulls for development, `--out DIR` writes somewhere
other than `data/bankfeed/`, and `AGENT_ROOT` points the script at another
copy of the repo. Under `AGENT_READONLY` it writes nothing unless `--out`
names a folder. `scripts/reconcile_list.py` runs the check itself, fresh,
before it builds anything (below); other scripts read the last result with
`open_lines(entity)`.

A run costs about 15 Xero calls per entity and under a minute: the chart,
every payment and every live bank transaction (all dates, because a
foreign-currency account's Xero balance is their sum and Xero pages them a
thousand at a time), documents of every status from `docs_back_days` before
the window plus every open one, credit notes and the Bank Summary. The bank is
the part the window keeps small. The agent's call for tier 3 (below) adds a few
minutes when it has lines to ask about, and nothing when its cache answers.

Every number below is a default of the optional `[bank_reconcile]` section of
`config/group.toml` (`config.bank_reconcile()`); a group changes it there, never
in the script.

## The window

The bank is read from the oldest line the last run left open, less
`watermark_pad_days` (3), and never more than `window_cap_days` (30) back.
Nothing older than the oldest open line can still be open, so the window is
usually shorter than the cap. With no earlier run, the window is the cap.

- **Older open lines are carried, not re-read.** A line the last run left open,
  or one on the native feed or a paste, stays in the list however old it is.
  The window never reaches back for it.
- **A reconciled record dated before the window cannot take a line inside it.**
  Its own line is before the window too, so it may only explain a carried or
  native feed line near its own date.
- **The tie-out catches what the window cannot see.** A match somebody undoes
  after the window has moved past it reopens a line the run no longer reads,
  and the account stops tying. A run that finds an account not tying reads that
  entity again from the cap, once, before it reports.
- `--since` overrides the window for an admin who needs to look further back.

## What it reads

- **Every settled movement** on every mapped account: the saved statement CSV
  up to the account's `csv_until`, the read-only Revolut and Mercury APIs
  after (a `csv` account is read from its file only). Pending, failed,
  declined, cancelled and reversed rows are not cash and are dropped. A
  Revolut fee is a statement line of its own in Xero, on the leg it was taken
  from, so the check reads it as one. `pad_days` (10) before the window are
  read as context, so a record near the edge finds its own line, and any older
  line on the native feed, on a paste or left open by the last run is kept
  whatever its age (above). A native feed line the bank data does not hold
  (Xero's feed can show a line before the bank's API does) is kept as a
  statement line.
- **Every bank-side record in Xero** on the matching Xero bank account:
  payments at their `BankAmount`, spend and receive money, transfer,
  prepayment and overpayment legs at their `Total`, each with `IsReconciled`
  and the time it was last saved.
- **Every open document in every entity read**: bills, sales invoices, expense
  claims and credit notes, AUTHORISED with something still due, with their
  currency, `CurrencyRate`, contact and line descriptions.

## Bank accounts are mapped by config, never guessed

Each `[[entities.bank_accounts]]` block in `config/group.toml` is one bank
account the check reads, and says which Xero bank account it feeds:
`xero_account_code` (the chart's code) or `xero_account` (its name). `currency`
limits a block to one sub-account, and `statement_name` (as the statement or
the API names the account) tells two blocks of one bank login apart, such as
several accounts under one Mercury login. Every run checks the Xero account
exists in that entity's chart and has the movement's currency. A movement on an
account no block claims, or two blocks claim, is refused and listed; it is
never put on the nearest-looking account. A block with no Xero account is a
problem on the report.

```
no    a new bank pocket's lines filed under the GBP account because the currency matched
yes   refused, account not mapped: HoldCo · GBP · 3 lines. Add a block for the pocket once its Xero account exists
```

## How a line is called reconciled

**Matching is narrow on purpose: a wrong match is worse than a line left
open.** A line left open is held back and shown to the admin; a wrong match
tells the bookkeeper to reconcile something that is not there. So the check
expects exact agreement and treats anything else as not a match:

- **One currency agrees to the cent.** The bank line and the record's bank-side
  amount (a payment's `BankAmount`, never the bill's own currency amount) are
  the same money, so there is no tolerance at all.
- **Across currencies, `fx_tolerance` (2%) at most** between the line and the
  document converted at its own rate or the window's median, and the name must
  agree.
- **A part payment only where the line says so.** A line for less than a bill
  is that bill's part payment only when it names the payee and carries the
  bill's number (code, high). A word like deposit, balance or instalment
  alone makes it a candidate for the agent (tier 3, below), never a code
  match.

Each line is paired with at most one thing Xero has reconciled, and each
reconciled record explains at most one line:

1. One record, one batch payment, or one day's records to one contact, to the
   cent, within `reconciled_gap_days` (5); within `reconciled_gap_named_days`
   (10) where the payee's name agrees or the cardholder is the person the
   record's bill names. Pairs are taken nearest date first, then the name
2. Two or three records to the cent within 5 days, only as a payment plus the
   bank fees on it, or as records to one contact the line names
3. A collection split over several records, to the cent within 2 days, only
   where the records share one reference or one contact and the line names it

Nothing else. A record a few per cent off the line, or the same amount weeks
apart, is not a match, however close it looks: the line stays open, the
account does not tie, and the admin sees the difference.

```
no    two USD 312.00 airline debits on one day, one reconciled payment found, both called reconciled
yes   each debit takes its own payment; with only one payment, one debit is reconciled and the other open
no    a GBP card line matched to a payment because the payment's Amount, in EUR, looked close
yes   the payment's BankAmount in GBP equals the line to the cent
```

## Repeated amounts: "one of these"

When two lines of the same amount sit equally close to one reconciled record,
the dates cannot say which one Xero matched. The check marks the leftover
AMBIGUOUS and lists its siblings, and every message about it says one of these.
Where the dates do settle it, the nearer line takes the record.

```
no    a cafe line of GBP 9.70 on 11 May called reconciled because the 10 May line of the same amount is
yes   the 10 May line takes the 10 May payment; the 11 May line is open
no    reconcile the 19 May USD 38.00 gym line, when three lines of that amount could each be the open one
yes   one of these is still open: the 4, 11 and 18 Mar USD 45.00 payments on the card account. Reconcile the open one to its bill
```

## Who paid

The bank names the cardholder on a card line: Revolut gives the card's first
and last name, Mercury the card's id or its user's email, which the check
turns into the name on the card. A bill names the person in its line
description, in full, and optionally by the initials in an agent-made bill
number. Two optional settings turn names into people: `staff_register` (a
JSON file of staff names; absent means the signal is skipped) and
`agent_bill_number_initials` (a pattern with one group for the initials, read
against the register and only where they belong to one person). With neither,
the cardholder's first and last name appearing on the record is still read.
Who paid counts wherever names are compared:

- **Reconciled**, step 1 above: a record within the named gap where the
  cardholder is the person its bill names, as for the payee's name
- **Tier 1**: who paid agreeing stands in for the payee's name. A bill that
  names somebody else is never code-certain, however exact the amount
- **Tier 2**: the first tie-breaker
- **Tier 3**: shown to the agent, and a verified pick needs it, the payee's
  name or a reference

A bank's name may carry a middle name: first and last agreeing is enough.

## What an open line goes to: three tiers

| Status | Meaning | Who acts |
|---|---|---|
| TO MATCH | an unreconciled record on the same account, or an open document in the same entity, found by code or by a verified agent pick; always high | the bookkeeper, through the reconcile list |
| INTERCO | the document is in another entity | the `bill-payments` skill: the payer's spend money to the pair's intercompany loan, the bill reconciled to intercompany by hand |
| AWAITING INFORMATION | a NO DOCUMENT line with a possible bill the agent could not settle; the candidates and the reason are on the line's note | the native bank feed, as NO DOCUMENT |
| NO DOCUMENT | nothing in Xero comes near it | the native bank feed: a chase, or spend money where the line has no document by nature |
| AMBIGUOUS | open or reconciled cannot be told apart | the reconcile list, phrased as one of these |
| SUSPECT WRONG MATCH | its bill is marked paid by a payment reconciled to another line | the reconcile list, under its own heading |

A candidate is an unreconciled record on the line's account, or an open
document of the entity dated from `doc_before_days` (45) before the line to
`doc_after_days` (7) after. It **fits** in its own currency to the cent, as the
card's own charge where the bank gives the merchant's currency and amount, or
converted at its own `CurrencyRate` or the window's median within
`fx_tolerance` with the payee's or the person's name. One line may settle
several documents of one supplier. Every candidate goes to one line at most.

**Tier 1, code, certain.** One open line and one fitting candidate between
them, and the payee's name, who paid or the bill's number on the line agrees.
A transfer leg names no payee: exact, alone and within 2 days is enough.

**Tier 2, code, tie-break.** Where one amount gives several lines or several
candidates, code separates them in this order, and each rule must point to
exactly one: who paid against the person on the bill; a reference or
invoice number on the line that is on the bill; branch or merchant detail
(one candidate shares strictly more of the line's words than every other);
the date, only when one candidate is within 1 day and every other 3 days or
more away. A candidate two lines point to goes to neither. When one line and
one candidate are left and they agree by name, they pair. What is still tied
goes to tier 3.

**Tier 3, the `bank-line-match` agent.** Lines still open, with candidates
gathered narrowly: the same entity and direction, dated within the document
window, and either the amount within `narrow_amount` (5%, same currency or
converted) or the name or who paid agreeing within `narrow_named_days` (7).
Tied and part payment candidates first, then nearest; `line_match_candidates`
(6) at most. All of them go in **one call per run**.

```
no    two GBP 25.00 cafe lines on one day, two bills of GBP 25.00, each line given the first bill
yes   the line on one cardholder's card takes the bill naming that person, by code; with no cardholder on either line, both go to the agent as a tie
no    TO MATCH on a GBP 74.00 bill from a different supplier because the amount is exact
yes   the agent sees it; unsure, or refused by code, so AWAITING INFORMATION
```

### The agent and its limits

The agent (a small model, `.claude/agents/bank-line-match.md`; the model,
effort, timeout and spending cap are `line_match_*` in `[bank_reconcile]`)
starts from "no match": most of these lines have no bill yet. It picks only a
candidate that reasonably clearly is the payment, says "certain" only when
nothing else is reasonable, and gives one sentence of evidence: the name, who
paid, a reference, the date, and what explains an amount difference (a tip,
an estimate against the charge, card FX, a fee). It never picks a different
supplier or on the amount alone.

Code verifies every answer. The pick must be one of that line's candidates,
still free, of the same entity and direction, and picked for no other line.
Code must itself see the payee's name, who paid or a reference agreeing. Two
candidates nothing on the line tells apart, or another candidate of the
supplier nearer the amount without a clear date, refuse the pick. The amount
must have a reason the rules allow: in the line's own currency or the card's,
up to `tip_max` (25%) over the bill (a tip, a fee) or down to `estimate_min`
(90%) of an estimate; across currencies within `fx_band` (5%), plus a tip. A
verified "certain" pick is TO MATCH, high, by `agent`, and reaches the
bookkeeper like any other.

```
no    TO MATCH on an agent's pick of a EUR 4,350.00 office invoice for a card charge of EUR 4,180.00, "explained by FX"
yes   refused: in the card's own currency no exchange rate applies, and nothing calls the invoice an estimate
```

### AWAITING INFORMATION, not a separate list

An unsure answer, a refused pick, a failed call, a line over the cap or the
budget, and a line on an account that does not tie all leave the line on the
NO DOCUMENT path: the native feed and the chase, shown as AWAITING INFORMATION
wherever a candidate exists. The candidates and the reason go on the line's
note in the not-yet-reconciled list and, with `--sync-feed`, at the head of
the native feed line's note, `awaiting information, possible: <contact>
<number> <amount> <date>, not certain; <reason>`. The bookkeeping run's
duplicate guard reads it there before posting a receipt that arrives: the bill
already in Xero may be this one.

### Cost and safety

- The call loads nothing of the repo: the agent's body is its whole system
  prompt, no tools, no settings, no MCP, from an empty folder. Per line it is
  sent the date, amount, payee, who paid and reference; per candidate the
  contact, number, date, amount due and the start of the description
- At most `line_match_cap` (40) lines a run, tied lines and then the oldest
  first. The payload stays under `line_match_budget_tokens` (about 12,000,
  characters / 4): the furthest candidates go first, down to 3 a line, then
  whole lines, which are AWAITING INFORMATION, not assessed
- Only lines on accounts that tie or have a paste, or on the native feed, are
  asked
- Answers are cached in `data/bankfeed/line_match_cache.json`, keyed by the
  line and its candidates, so one question is asked once
- A failed or timed-out call leaves its lines AWAITING INFORMATION and the
  check completes; it never fails a line to a TO MATCH
- Each call is logged to `data/bankfeed/line_match/` with its cost, tokens
  and run id. Expect a few US cents and two or three minutes for a dozen lines
- Under `AGENT_READONLY` the call is allowed, since it writes nothing; the
  cache and the log are written only where the outputs are
- `--no-agent` skips tier 3: its lines are AWAITING INFORMATION, not assessed

## Lines matched to the wrong bill

Two shapes, both reported with the bank line and the bills on each side.

- **A reconciled payment that does not fit its bill.** In one currency the bank
  amount differs from the payment; across currencies the rate the payment
  implies is more than `wrong_fx` (2%) from the window's median, whether or
  not the line names the payee
- **An open bill that fits a reconciled line better than the bill it was
  matched to**: the right currency and amount, the right person, while the
  matched bill does not fit the line or is dated more than a week after it

Only the second shape reaches the bookkeeper, because only it names the bill
the line belongs to and so gives them something to do. The first shape, a
match that does not fit with no better bill to point at, goes to the admin
under "for the admin, not sent to the bookkeeper" in the manual items, where
most turn out to be an expense claim paid at a typed rate rather than a wrong
match.

```
no    a GBP 29.65 taxi line called reconciled and left there
yes   reconciled, and flagged: it is matched to a EUR bill dated 2 weeks after the ride, while an open GBP 29.65 bill of the day before fits it
```

## A pasted reconcile screen is the last word

An admin can paste Xero's reconcile screen for an account. It overrides the
inference for every line up to the moment of the paste.

1. Save the paste to a file in the run's scratch folder, exactly as pasted
2. Run `confirm <entity> "<account>" --paste <file> --at <time of the paste>`,
   once per account, with the time the admin sent it (the Slack message time),
   never the time the run got to it. `<account>` is any name the block goes by:
   its label and currency, statement name, Xero account name or code
3. `confirm` reads both date formats Xero uses (`24 Jul 2026`, `Sep 19, 2026`),
   Spent or Received with the amount after it, the staged Who and What, and the
   Statement Balance and Balance in Xero header. It refuses a paste whose count
   does not match the screen's `Reconcile (n)`: page through and paste the lot

Up to the paste, a line on it is OPEN, and a line not on it is RECONCILED. Two
exceptions keep the paste honest: a line the inference matched to records all
saved after the paste was reconciled since and stays reconciled, and a line
settled within `feed_lag_days` (2) before the paste may not have reached Xero
yet, so the inference decides it. After the paste, the inference decides.
**Every disagreement between a paste and the inference is reported**: that is
how the matcher's mistakes get found and fixed in the script, never by editing
the list.

## The tie-out

For every account, every run:

```
(bank balance now) - (Xero balance) = (open lines) - (unreconciled Xero records)
```

The bank balance is the ledger balance from the API: Revolut's, with pending
holds added back, Mercury's `currentBalance`; for an account read from a saved
statement only, the file's last `Balance` where it carries one. The Xero
balance is the Bank Summary's closing balance for an account in the entity's
base currency, and the sum of every live record for any other currency, because
the Bank Summary reports in base. An account TIES when the two sides agree to
the cent.

**An account that does not tie and has no paste is UNVERIFIED. Its lines are
never put to the bookkeeper as reconcile this**: they go to the admin's report
with the difference. A difference means a line the check could not see, a
record it paired wrongly, or a line older than the window still open in Xero.
Find it, or ask for the account's reconcile screen.

```
no    reconcile list built from an account OFF BY GBP 87.35
yes   the account's lines held back: Revolut GBP Main, HoldCo, OFF BY GBP 87.35, paste the reconcile screen to confirm
```

## Timing

A scheduled run builds the list hours before the bookkeeper reads it. Every
message states the moment the check ran, `as at 7 Oct 2026, 03:14 UTC` (in
`[company] timezone`). A line reconciled after that moment is still on the list
they have, and the next build drops it. The reconcile list is never built from
a stale file: it runs the check fresh, and when the check fails, nothing is
written and no DM is sent.

## The reconcile list keeps only what the check holds open

`scripts/reconcile_list.py` runs the check first, then builds its own pairs,
then keeps an item only where the check has its statement line OPEN and TO
MATCH (or AMBIGUOUS, or a wrong match) on a verified account. It drops the rest
with the reason, adds the check's own TO MATCH lines at high confidence that it
did not find, phrases AMBIGUOUS lines as one of these, and adds a `matched to
the wrong bill` section. Its output files, the register write and the
once-a-day DM rule are in `docs/BANKING.md`, "The reconcile list".

## The native feed, kept in step

Every run reports two kinds of drift: NO DOCUMENT lines missing from
`data/bankfeed/<entity>.md`, and native feed lines the check finds reconciled.
`--sync-feed` acts on both through `scripts/bankfeed.py`: it adds the missing
lines, from verified accounts only, and removes a feed line only where a paste
or a one-to-one exact match with the payee's name shows it reconciled, giving
the reason. It also writes an AWAITING INFORMATION line's candidates at the
head of its feed line's note. A line reconciled by an approximate pass, or
ambiguous, stays on the feed and is reported.

## Where it goes wrong

| Failure | What the check does |
|---|---|
| Repeated amounts | one-to-one pairing, nearest date first; a tie it cannot break is AMBIGUOUS |
| Repeated amounts on open lines | tier 2's discriminators, each pointing to exactly one; what stays tied goes to the agent, and twins it cannot tell apart stay AWAITING INFORMATION |
| The agent picks wrongly | code verifies every pick: its own candidate, free, named, the amount explainable, nothing as good beside it; else AWAITING INFORMATION |
| The agent fails or times out | its lines are AWAITING INFORMATION, the check completes |
| One record explaining two identical lines | impossible by construction: a record is used once |
| A person matched a line to the wrong bill | rate and fit checks on every reconciled payment; an open bill that fits better |
| Currency conversion | the document's own rate and the window's median, `fx_tolerance` for a match, `wrong_fx` for a suspect; one currency to the cent |
| Bank currency against bill currency | the payment's `BankAmount`, never its `Amount` |
| Reconciled since the build | the as-at time on every message; the next build drops it |
| One line paying two bills | a batch payment or one day's payments to one contact; several open bills of one supplier together |
| A bill in another entity | INTERCO, the `bill-payments` skill's work, never TO MATCH |
| An unmapped bank account | refused and listed, never guessed |
| The tie-out fails | UNVERIFIED: held back from the bookkeeper, reported to the admin with the difference |
| Xero's line differs from the bank's | no approximate pass: the line stays open and the tie-out shows the gap |
| A line Xero has and the bank API does not | read from the native feed or the paste, and the tie-out says whether it belongs |
