---
name: outstanding-items
description: The standing register of everything the agent has raised and nobody has closed: every manual item still waiting on a person in Xero, every query still waiting on an admin's decision, every blocked run, every chase still waiting on a document. Kept in docs/bookkept/OUTSTANDING.md by scripts/outstanding.py, one section per domain, written as each run sends its items and pruned as they are answered, reconciled against Slack and Xero read-only, and posted to the agent's Slack channel (config/group.toml) as one list. Use when an admin asks for the outstanding items, open items, open actions, pending or unanswered queries, what is still waiting, what has not been done or answered, or the manual actions list. Not the bills report (that is unpaid bills), and never a run that acts on anything.
---

# Outstanding items: one list of what still needs a person

Every run raises things only a person can close: a bill to reconcile to
intercompany, a duplicate to void, a question nobody has answered. Each lands
in one run's report and scrolls away, and the next run raises it again in
different words. This skill keeps the standing register of them, and posts it
whenever an admin asks.

It is not a domain. Load the `xero` skill for the read API and the entity
names, and nothing else: no `xero-bills`, no `xero-interco`, no report skill,
no domain docs. It decides nothing about the books; it reports what earlier
runs decided needed a person.

## The register: written when the message is sent, not rebuilt afterwards

The list is a file, `docs/bookkept/OUTSTANDING.md`, rendered from
`docs/bookkept/outstanding.json` by `scripts/outstanding.py`. One section per
domain (the domains in `config/group.toml`, plus `platform` for the agent's
own plumbing), and inside each domain the three subsections `manual items`,
`queried` and `blocked`, plus `to confirm`, `awaiting documents` and `watch`
where they have content.

**Every run in every domain writes its own items into the register as it sends
them, and deletes them as they are answered.** That is the rule the register
turns on, and it is not this skill's job alone:

```bash
# the moment a run sends a query, a manual action or a blocked line
.venv/bin/python scripts/outstanding.py add --domain bookkeeping --kind queried \
    --with admin --key INV-1007 \
    --text "which entity recognises the annual subscription, the invoice is addressed to both" \
    --refs "Contoso Cloud · 05 Aug · GBP 6,000.00 · paid HoldCo · INV-1007"

# the moment the answer arrives and the posting is done
.venv/bin/python scripts/outstanding.py close --key INV-1007 --reason "posted and recharged"

# an admin has ruled but nothing is posted yet, the agent's own backlog
.venv/bin/python scripts/outstanding.py answer --key INV-1007 --answer "allocate by headcount"
```

`add` deduplicates on the key first and the wording second, so the same thing
raised by a fourth run bumps one line rather than opening a second. A closed
item is deleted, not archived: the trail is the run report that closed it.
The register lives on the server only and is never committed to git; the
script creates it on first use.

**Every change republishes to Drive.** The Word copy people read,
`data/reports/Outstanding Items.docx`, is rebuilt from the store and published
into the Drive publish folder (`[drive]` in `config/group.toml`, kind
`outstanding`) after any `add`, `close`, `answer` or `update`, overwriting the
copy already there, the same way every workbook builder publishes its own
output. A run making many changes at once passes `--defer` and finishes with
`scripts/outstanding.py publish`, so Drive takes one upload rather than twenty.
Drive being unreachable, or no publish folder configured, prints a line and
changes nothing else: the register update is never lost to it, and a
read-only session cannot publish at all.

Never edit the markdown or the Word copy by hand; the next change overwrites
both.

Two styles meet in these files and they do not overlap. Each item line is a
message, so `docs/COMMS.md` governs it: what happened in words first,
references trailing, no em dash, currency code in front of the amount. The
page's own furniture, the title, the opening paragraph and the headings, is a
document, so the `writing-style` skill governs that. Anyone changing the
renderer or the Word template runs that skill's style check over
`docs/bookkept/OUTSTANDING.md` afterwards. Its findings that assume every
document is a policy (a missing review section, the `#` title read as a
missing opening paragraph, `supplier` read as client-facing language) are
standing overrides on this file, not defects.

## Read-only, and the two writes

Nothing is written to Xero, Drive (beyond the register's own Word copy) or
Gmail. Nothing is chased, nobody is DM'd, no query is re-sent. The two writes
are the register itself and the Slack post in "The message" below.

## What counts

Six kinds, and each line belongs to exactly one. Manual items, queried and
blocked are the subsections every domain has; the other three appear only when
they have content.

| Kind | `--kind` | What it is | Who closes it |
|---|---|---|---|
| **manual items** | `manual` | a step only a person can take in Xero: reconcile a bill to intercompany, clear a control account, recode a reconciled line, set depreciation, retype an account, void behind a lock date | an admin, in the Xero UI |
| **queried** | `queried` | a decision the agent asked for and has not received: which entity, which record stays, post this journal or not, which account | the admin the question was put to |
| **to confirm** | `decided` | an answer the agent reached itself at medium confidence and has already posted (`scripts/resolve_gate.py` said `confirm`) | an admin's yes or no; or, unchallenged for `[auto_resolve] confirm_days`, `scripts/outstanding.py accept` at the start of the next bookkeeping run |
| **blocked** | `blocked` | a run tried and could not: a missing scope, a locked period, an API that refuses the write, a statement nobody has sent | whoever owns the obstacle |
| **awaiting documents** | `documents` | a chase to a person for an invoice or receipt that has not arrived by the last run that mentioned it | the person chased |
| **watch** | `watch` | nothing to do now, check later: a control account expected to clear when a journal posts, a refund expected the month it lands | nobody, until it moves |

`blocked` and `not attempted` are different and are never merged: `blocked`
means a run tried and could not, and something never started does not belong on
this list at all.

An item an admin has ruled on but no run has yet posted carries `answered:` at
the end of its line and sorts to the top of its own subsection. It is the
agent's own backlog and it is the most important thing on the list.

What does not count: anything the agent did itself; a bill that is merely
unpaid (the bills report owns that); a chase already answered; a manual action
a later run withdrew.

## Sources, in this order

0. **The register**, `docs/bookkept/outstanding.json`. It is the list. Read
   it first and treat the rest of this section as a reconciliation against
   it, not as the way the list is built: sources 1 to 4 exist to catch an item
   a run forgot to register and an answer nobody recorded. Anything they turn
   up that is not in the register is added to it, and anything the register
   holds that the closing evidence below shows closed is deleted from it,
   before the list is posted.

1. **The channel**, `config.slack().channel_id` (`channel_name` for the
   human-readable name), the last fourteen days unless the admin names a
   longer window. `conversations.history` with `SLACK_BOT_TOKEN` from `.env`,
   then `conversations.replies` on every run headline for the report in its
   thread. Take:
   - every `<COMPANY> MANUAL ITEMS (date)` headline (the company name from config, upper case): the first reply in its thread holds
     the items, line by line;
   - the `bill payments`, `blocked`, `queries`, `manual` and `chased`
     sections of every report, scheduled or not;
   - every human reply in those threads: it is the closing evidence below.
   If the read answers `missing_scope`, say so under **blocked** and work
   from sources 2 to 4.
2. **The admins' own run threads on the server**,
   `data/slack/threads/<key>.json` (`task`, `user_id`, `last`) with
   `<key>.answer.md`, the report posted into that admin's DM thread, and
   `<key>.inbox-*.md`, the admin's follow-ups. A focused run's `queries`,
   `manual`, `blocked` and `chased` never reach the channel, so this is the
   only place they exist. Read every thread whose `last` falls in the window.
   Never read `data/slack/notes/`: notes are private to each admin and are
   instructions for a run, not items a run raised.
3. **The intake ledger**, `docs/bookkept/LEDGER.md`: every outcome that opens
   `queried:` with no later `posted` line for the same reference.
4. **The run logs**, `data/logs/<job>-<date>.log`, only for a run whose
   report is missing from the channel (a failed post): the report is in the
   log between `--- answer ---` and `--- end answer ---`.

## Closing evidence: when a line leaves the list

A line leaves the list only when one of these shows it closed. Nothing else
does, and in particular the passage of time does not.

- **`done` in the thread** (`docs/COMMS.md`). An admin replying `done` in a
  `MANUAL ITEMS` thread (any company prefix) closes every item in it; `done <reference>` closes the
  one whose reference it names.
- **Xero shows it closed**, checked read-only, one filtered read per item:
  the bill to reconcile to intercompany is `PAID`; the duplicate is `VOIDED`
  and one copy remains; the draft journals are gone; the funding leg is
  matched; the depreciation is set; the account is retyped. The API cannot
  read a bank line's reconciled state through a payment it did not make, so
  a control-account clearing is checked on the bill's `AmountDue`.
- **A later run reports it actioned** in its `bookkept` section, or withdraws
  it in words.
- **An admin answered** the question in the thread, in a later `run`
  instruction (the thread digest and the inbox files) or in a note a run
  reports under `from users` or in its headline. An answer alone does not
  close a query: it moves to **answered, not yet actioned** until a run
  reports the posting.

A line that cannot be checked (the item is a UI setting the API cannot see,
the source is a screenshot) stays, and its line ends `unverified`.

## One item, however many runs raised it

The same reference raised by four runs is one line. Key on the invoice number,
journal description or account; failing those, on supplier plus date plus
amount. The line carries the date it was first raised and how many runs have
repeated it, oldest first in each section. Where two runs proposed different
fixes for the same thing, it is one line that says so and asks the admin to
pick, with both fixes in a clause each. Never choose between them here.

## Xero budget

About one call per item, forty in a normal week. Filter every read
(`InvoiceNumber=="..."`, `Status=="DRAFT"`); never page a whole ledger to
check one line. If the check would take more than sixty calls, check the
manual actions and queries and mark the documents `unverified`.

## The message

`scripts/outstanding.py slack` renders it from the register, so the message and
the file never disagree. It prints the message; it does not send it, and it
names the target channel from `config/group.toml` on stderr. `docs/COMMS.md`
governs the shape completely: plain words first, references at the end of the
line, the invoice shape wherever an invoice is named, bold lowercase section
headings, no em dash, no preamble and no sign-off. Each line: what has to
happen, in words; who it is with; when it was first raised and how many runs
have repeated it; the references; and `answered:` where an admin has ruled and
the posting is still ours to do.

```
OUTSTANDING ITEMS (25 Sep 2026, 14:32) · 2 manual items · 3 queried · 1 awaiting documents · 1 answered and waiting on us

*Bookkeeping - manual items*
• set the depreciation on two registered fixed assets in HoldCo, still nil today - admin, raised 19 Sep · FA-0010, FA-0011 · answered: updated

*Bookkeeping - queried*
• which record stays, the bill or the spend money coded to travel four days later - admin, raised 20 Sep, 2 runs · Fabrikam Travel · 25 Jun · EUR 375.00 · paid OpCo EU · recognised OpCo EU · INV-1008

*Bookkeeping - awaiting documents*
• what the payment was for; the confirmation says only miscellaneous payment - bookkeeper, raised 13 Sep, 5 runs · Northwind Office Supplies · 01 Sep · CHF 500.00 · paid HoldCo · INV-1009
```

A domain with nothing open has no section, and a subsection with nothing in it
is dropped; an empty register is the one line
`OUTSTANDING ITEMS (date) · nothing open`. The headline counts exclude
**watch**.

### Where it goes: the channel, always

The list is posted to the channel in `config.slack().channel_id` at **top
level**, whoever asked and wherever they asked, a DM included: it is the
standing list the team scrolls, and a list in a thread is one nobody sees.
`chat.postMessage` with `SLACK_BOT_TOKEN`, `channel` the channel ID, no
`thread_ts`; split under 3,500 characters at a section boundary, the headline
on the first chunk and `(2/3)` after the headline on the rest. The thread the
request came from gets the headline line and `full list in
#<channel_name>`, nothing else. If the channel answers `channel_not_found`, or
no `channel_id` is configured, the thread gets the full list and one line
saying why.

## How it is started

- Slack, any admin, DM or channel: `run give me all outstanding items`, or
  `run pull me the outstanding items list`, in any wording the listener's gate
  recognises (`scripts/slack_agent.py`). Routed through `deploy/run-thread.sh`
  like any run.
- On the server: `deploy/run-scheduled.sh outstanding`. Not in the crontab.
- In a terminal session, for the file rather than the Slack post:
  `.venv/bin/python scripts/outstanding.py list`.

A window: `run outstanding items since 1 Sep` widens the fourteen days the
reconciliation reaches back over. It never narrows the register itself, which
holds an item until it is closed however old it is.

## The register is only as good as the runs that write it

The reconciliation in "Sources" is the backstop, not the mechanism. A run that
sends a query and does not register it leaves an item that only a Slack sweep
will ever find, and a run that posts an answer and does not close the line
leaves a list that cries wolf. Both failures are silent. So:

- register the item in the same step that sends the message, never at the end
  of the run;
- close it in the same step that posts the fix;
- when an admin rules and the posting is somebody else's or a later run's,
  `answer` it rather than closing it, so it stays visible as ours to finish.
