---
name: query-investigation
description: Gather the evidence for a query before anyone is asked it. Every question a bookkeeping run is about to raise is looked up in Xero and the register, settled there in Python where the rule allows, otherwise goes with the run's other queries to one coordinator session that sends small-model researchers for the data, decides the answer and rates its certainty 1 to 5. The certainty maps to a grade (5 and 4 high, 3 medium, 1 and 2 low) and scripts/resolve_gate.py decides act, confirm or query. Every open break of an intercompany reconciliation goes the same way. Load it inside every xero-bills run, the moment the run is about to raise a query, and inside every xero-interco run; scripts/investigate_query.py runs it and a PreToolUse hook refuses to register a bookkeeping query without it.
---

# Query investigation: gather the evidence before asking

Most questions a bookkeeping run would put to an admin have an answer somewhere
the agent can read: an earlier ruling, the way the same transaction has always
been posted, the bank statement, a document on Drive. This skill makes the run
look for that answer properly before anyone is asked.

**The investigation is the evidence step and the gate is the decider.** The
investigation returns a verdict with a certainty. The run maps the certainty to
a grade, takes the grade and the amount to the gate (CLAUDE.md, "Queries:
answer them before asking them"), and does what the gate says: act, confirm or
query. Nothing in this skill lets the agent do more than `[auto_resolve]`
allows, and a verdict is never written back as a rule.

It is a support skill of the bookkeeping domain and loads inside a `xero-bills`
run alongside `xero` and `bill-payments`. It decides nothing about the books on
its own: it hands the run a verdict, and the run does every write.

### The parts and where each one lives

| Part | File | What it does |
|---|---|---|
| this protocol | `.claude/skills/query-investigation/SKILL.md` | the method, the certainty scale, the verdict shape, what the run does with it |
| the runner | `scripts/investigate_query.py` | `open` starts an investigation detached, `wait` starts the run's batch and collects it, `show` prints a verdict, `gate` takes it to the gate, `due` reopens deferred ones, `spend` shows the day against the cap. Runs the pre-check before any agent, settles or reuses where it can, batches the rest |
| the interco breaks | `scripts/interco_breaks.py` | `list` and `open` the open items of an intercompany reconciliation as investigations, one batch per run |
| the coordinator | `.claude/agents/query-investigator.md` | headless `claude -p --agent`, one session per batch. Reads the rules once, decides each query, rates each certainty |
| the researchers | `.claude/agents/query-researcher.md` | the small model, spawned by the coordinator. Pull data, return facts with sources, no judgement. Carry their own Xero read sheet in place of the `xero` skill |
| the guard | `.claude/hooks/guard-queries.py` | refuses `outstanding.py add --domain bookkeeping --kind queried` unless the verdict is graded low, failed, deferred, or a high or medium one the gate said to query |
| the register | `scripts/outstanding.py` | `add --investigation <result.json>` stores the certainty, grade and what is unsure on the queried line; `update --investigation` attaches a fresh verdict to a line already there; `check-queries` is the exit check |
| the report | `docs/COMMS.md` | `resolved`, `to confirm` and `queries`; the `queries` heading says whether the agent could answer |
| the verdicts | `data/investigations/<key>/<stamp>/` | brief, pre-check, verdict; on the server only, a source for later investigations |
| the batches | `data/investigations/_batches/<batch_id>/` | the manifest, the one raw session output, each verdict as the coordinator decided it |

Settings are in `[investigation]` in `config/group.toml` (cap, batch size,
reuse window, models); with `enabled = false` the run grades its own answer as
before and the guard and the exit check stand down.

### When it runs

**Every query a bookkeeping run is about to raise, before it is sent to anyone
or written into the register, and every open break an intercompany
reconciliation finds.** A query here is a question waiting on an admin's
decision, the `queried` kind in the register. Three things never go through it:

- **A chase for a missing document**: no amount of reading produces a document
  nobody has sent. It goes out as before and is registered as `documents`.
- **An admin's answered item**: the answer is the ruling; the run posts it and
  the runner refuses to open it (exit 3).
- **A read-only session**: it answers from what it reads, writes nothing and
  opens no investigation (exit 3).

**The run does not stop to wait, and it waits once.** It opens each
investigation the moment it hits the question, carries on with the intake, and
calls `wait` once, when the rest of its work is done and before it writes the
report. The run's queries form one batch that starts on that `wait`, or earlier
if the run goes quiet for a few minutes; a `wait` in the middle of the work only
starts the batch early and splits the run's queries over two sessions.

```bash
# the moment the question comes up
.venv/bin/python scripts/investigate_query.py open --domain bookkeeping \
    --key example-transfers-01-02oct \
    --entity "Example Holdings Limited" \
    --question "do the two USD 37,250 transfers post on the usual pattern, and where does the FX on the GBP-funded leg go" \
    --context "every prior transfer to the payee is spend money to account 400; nothing posted for these two" \
    --refs "Example Holdings Limited · 01-14 Oct · USD 80,000.00" \
    --txn "HOLDCO|2026-10-13|37250|USD|Example Payee" --txn "HOLDCO|2026-10-14|37250|USD|Example Payee"

# a query already in the register, carried over from an earlier run
.venv/bin/python scripts/investigate_query.py open --from-register OI-0901

# investigations the daily cap held back, whose day has come: at the start of the run
.venv/bin/python scripts/investigate_query.py due --open

# once, before the report: starts the run's batch, blocks up to 9 minutes, exits 75 while any are pending
.venv/bin/python scripts/investigate_query.py wait --all-open --since-hours 12
.venv/bin/python scripts/investigate_query.py show --key example-transfers-01-02oct
```

`wait --all-open` covers the queries this run opened (`--any-run` for every
run's). It exits 75 while anything is pending; call it again. A batch that runs
past its time limit, dies or returns something unreadable writes a `failed`
verdict for every query it did not answer, so `wait` always ends.

**A carried-over query is investigated once.** Once a verdict exists, the
runner reuses it rather than paying for a second one (below), unless something
new has arrived since: an answer, a document, a bank line. Where the run knows
of something new the pre-check cannot see, it says so in `--context`, or opens
with `--fresh`, and the coordinator reads the earlier verdict as a source.

**The register holds only what the agent could not settle.** A bookkeeping
query left in it carries a verdict graded low, a high or medium one the gate
said to query, a failed or deferred investigation, or an admin's answer;
nothing the run settled stays there. A run proves it before it reports, with
`.venv/bin/python scripts/outstanding.py check-queries`, which must print PASS.
A FAIL names each line: one `never investigated` is opened with
`--from-register` and dealt with like any other query; one graded high or
medium that never saw the gate goes through it now. A line marked
`investigation scheduled <date>` is waiting for a later investigation, held
back by the daily cap (below) or set with `outstanding.py update
--schedule-investigation`: `due --open` reopens it on its day, at the start of
the first run that day. The check passes it until the date and fails it from
the day after, so it cannot be parked for good.

### The pre-check

An agent paid to find transactions somebody has already posted is money
wasted. So before any agent starts, the runner looks the query's transactions
up itself, in plain Python and read-only:

- in Xero, every live bank transaction and invoice of the same amount within 4
  days, in the named entity or every entity, with its coding, its
  reconciliation and who posted it when; manual journals in the window with a
  line of that amount
- in Xero again, without trusting the amount (when `accounting_agent.xero.matching`
  is installed): every payment, bank transaction and document in the window
  matched on the payment's bank-side amount, the amount converted from another
  currency and the payee's name, written to `matches` with a `verdict_hint`
  where a payment for the exact bank amount to the named payee already accounts
  for the cash
- in the register, every line on the same key or the same amounts

It writes `precheck.json` beside the brief, with the name of every account on
the records it found. The coordinator reads it before anything else and, where
it settles the query, answers from it without a researcher. Name the
transactions with `--txn "<entity>|<YYYY-MM-DD>|<amount>|<CCY>|<payee>"`, one
per transaction; the entity is a config key, alias, short name or Xero name, and
the payee is optional and defaults to the first segment of `--refs`. A brief
built `--from-register` has them read out of the line's own references.

### Settled, reused or batched

Once the pre-check is in, the runner routes the query one of three ways:

- **Settled in Python, no session.** One rule, `posted since raised`, and
  every part of it must hold: the query's own words say nothing is in Xero
  for it; each transaction it names has exactly one live record of that
  amount in the window, a reconciled bank transaction or an approved or paid
  bill or invoice, in the currency named; each record was created after the
  query was raised; every account on it can be named; and no other register
  line on the same amounts asks for anything (a watch line is fine). The
  verdict says it is already posted, by whom and how coded, closes the query's
  own register line and posts nothing. Certainty 4 where a person created
  every record, their posting being their ruling; 3 where an agent run or a
  feed created one. It carries `decided_by: precheck`. Anything short of the
  whole rule goes to the coordinator: a question about coding, period or
  entity is a judgement, whatever Xero holds.
- **Reused, no session.** The last finished verdict for the same key stands
  when all of these hold: it is `done` (never failed or deferred), under
  `reuse_days` old, nobody has answered the register line since, the brief is
  the same (question, context, references, transactions, or for an
  intercompany break the lines themselves), the pre-check found the same
  records in the same state, and acting on it twice does no harm: graded low,
  or graded high or medium with only `none`, `close` and `match` actions. A
  verdict that posts or updates is never reused: the next look has to see
  whether its posting happened. It carries `reused_from`. `--fresh` forces a
  new investigation.
- **Batched.** Everything else waits for its run's batch.

### Batches and the daily cap

**One coordinator session per run, not per query.** The run's queries (its
`AGENT_RUN_ID`; a hand-run in one shell forms its own) go into one batch of up
to `batch_max`; more make a second batch, run after the first. A batch starts on
the run's `wait`, after `debounce_seconds` with no new query, or once it is
full. The coordinator gets every brief and pre-check, reads each rule once for
the whole batch and shares its researchers, and returns one verdict per key.
Each verdict is checked on its own: a missing or broken one fails that query
only. Each result carries the batch id and an equal share of the session's
cost; the session itself is counted once, in the run's cost bracket (an extra
cost file under `data/runs/<run id>/extra/`, read by `scripts/run_cost.py`) and
in the day's total.

**A daily cap** (`daily_cap_usd`, UTC day, the figure the CLI reports) on
investigation sessions. Before a batch starts the runner adds up the day; at or
over the cap it starts nothing and writes every waiting query as `status:
deferred`, with the day it will run and the register command for it. A batch
whose estimate would take the day more than 10% over the cap is cut down to
what fits, and the rest waits for the next look at the cap. Deferred is never
dropped. A deferred query is not put to anyone yet: the run registers it with
the command `wait` prints (`outstanding.py add ... --investigation
<result.json>` for a new query, `outstanding.py update --key <key>
--schedule-investigation <date>` for one the register already holds) and
reports it under `not attempted`, with the day it will be investigated.

### The brief

The brief is everything the run knew when it stopped, so the investigation does
not start from nothing. `--question` is the query as it would have been put to
the admin. `--context` is what the run had already checked and what it found, in
plain words: the bank line, the precedent it saw, the rule it could not apply,
the reason it stopped. `--entity` is the entity in full. The coordinator treats
the context as a lead, never as a finding, and checks it.

### How the coordinator works

The coordinator reads the rules itself, because the decision is made on them,
but only the ones the question touches. It greps for the section and reads the
section, never the whole rulebook:

| The question is about | Where the rule is |
|---|---|
| which account, how a supplier is coded | `rules/EXPENSES.md`, the supplier's row in `rules/SUPPLIERS.md` |
| which entity bears a cost, how it is shared | `rules/GROUP.md`, `rules/ALLOCATION.md`, the entity's own file in `rules/entities/` |
| cash: paid or not, by whom, the bank feed | `docs/BANKING.md` |
| a transfer between group entities, its routing | `rules/INTERCOMPANY.md`, `docs/INTERCOMPANY_RECON.md` |
| payroll and the control accounts | `rules/PAYROLL.md` |
| tax treatment | the entity's own file, `rules/EXPENSES.md` |
| what the run itself may do | the `xero-bills` skill, the section on that step only |

It does not load the `xero` skill or read `docs/COMMS.md`: the verdict's
`report_line` takes the invoice shape given under "The verdict" below.

It sends `query-researcher` agents for the data, a few at a time and for a few
rounds a query at most (`researchers_parallel`, `max_rounds`), each with
specific questions; one researcher serves every query in the batch that needs
the same entity and period. These are the sources, in the order they win a
disagreement:

1. the bank: the bank feed, the saved statements, the Revolut and Mercury clients
2. the document itself: the bill-to entity, the period, the tax registration
3. an explicit rule in `rules/`
4. Xero: how the same supplier's earlier bills were treated in every entity,
   and how the other entity in a pair posted its side
5. the supplier mapping, then the nearest analogous rule
6. what a sender said, and what an admin has already said: the register's
   answered lines, the ledger, the run logs
7. earlier verdicts under `data/investigations/`

It then decides what the admin would most likely rule, sets out the actions that
carry the answer out and rates how sure it is. **Every register line on the same
transactions is part of the answer**, whatever its kind: a manual item the
answer changes is an `update` of that line by its id, and a second query the
answer settles is a `close`. A new line for something the register already lists
is a duplicate.

### Intercompany breaks

The `xero-interco` run turns each open item of its reconciliation into an
investigation with `scripts/interco_breaks.py open`: a one-sided line, a
probable match, or a matched pair booked at genuinely different amounts, with
related lines in one pair grouped into one item. The brief names the pair, the
flavour, both loan accounts and every open line; the pre-check looks for each
line where it sits and for the same amount in the other entity over 60 days.
The key is `interco-<pair>-<flavour>-<date>-<amount in the reporting
currency>`, and an item whose lines (by doc_id) an earlier investigation
already had keeps that earlier key when the rate moves the amount. A later run
reuses the verdict without a pre-check while the item's lines are unchanged and
the verdict is safe to act on twice (above); one that posted a correction is
looked at again, and the look sees the correction. All of one run's breaks go
into one batch.

The question for the coordinator is what corrects the break, by the class in
`docs/INTERCOMPANY_RECON.md`: the missing side, a reclass, a reversal, or
nothing because it is a match, timing, or already dealt with. The actions are
manual journals in the entity missing the entry, never in the one that got it
right; a bank-side recode or anything reconciled is a `manual` action. Where the
open lines are each other's counterpart, the verdict says so with a `match`
action naming their doc_ids, and the run records it with `interco_breaks.py
confirm --doc-id`, so the matcher pairs them from then on. FX revaluations are
not opened as breaks: an admin posts them from Slack once the matrix is right
(`xero-interco-fx`).

### The certainty scale and the grade

| Certainty | What it means | Grade |
|---|---|---|
| 5 | a written rule or an admin's ruling settles it, and the data confirms every figure | high |
| 4 | no rule settles it in words, but the entity has posted the same thing the same way consistently and the data confirms every figure | high |
| 3 | the rules and the precedent point one way, a careful accountant would do the same, and if it proved wrong a later journal would put it right without touching anything reconciled | medium |
| 2 | two treatments are plausible and the evidence leans without deciding, or the answer turns on one fact only a person has | low |
| 1 | the answer depends on a decision or a document nobody has written down | low |

**A verdict is capped at grade low, whatever the certainty, where it touches
something the agent never answers alone** (CLAUDE.md, "Queries"). The
coordinator lists what applies in `touches`, from this set:

| `touches` | The answer would |
|---|---|
| `not_shown` | rest on an amount, supplier or currency the document does not show |
| `missing_document` | need a document nobody has sent (a chase action sets this too) |
| `same_purchase` | decide whether two records are the same purchase |
| `safety_rule` | involve anything a safety rule governs |
| `locked_or_reconciled` | write to a locked period or a reconciled transaction (a post or update against a record the pre-check found reconciled sets this too) |
| `rules_change` | change `rules/` beyond a supplier row |

Certainty is also capped at 2 where the answer would contradict a rule or an
admin's ruling, depend on what somebody intended, or move cost between entities
with no precedent for that pair and that kind of cost. The cap is about what the
answer rests on, not its size: a large amount that follows a long, unbroken
precedent can be a 4; a small one with no precedent cannot. The runner stores
the certainty, the grade, any `touches` and the grade the certainty alone would
have given.

### The verdict

The coordinator's final message is one JSON array, one object per query in the
batch, each in the shape below with its `"key"` added. The runner checks each on
its own and writes it to that query's `result.json`, adding `status`, `grade`,
`decided_by`, the batch id, the query's share of the cost and the timings. A
verdict settled or reused in Python takes the same shape.

```json
{
  "certainty": 4,
  "touches": [],
  "question": "the query in one sentence",
  "answer": "what the admin would rule, in words, with the reason",
  "evidence": ["one fact per line, its source at the end"],
  "unsure": "what is still not known; required at 1 and 2",
  "question_for_admin": "the query as it should now be put, narrowed to what is unsure; required at 1 and 2",
  "amount_reporting": 37250,
  "actions": [
    {"type": "post", "entity": "Example Holdings Limited", "what": "spend money to the payee, account 400",
     "endpoint": "BankTransactions", "payload": {}, "idempotency_key": "inv-holdco-payee-20261001-1",
     "bank_feed_line": "the line in data/bankfeed/<entity-slug>.md it clears"},
    {"type": "update", "entity": "...", "what": "...", "endpoint": "...", "payload": {}},
    {"type": "manual", "with": "admin", "what": "the step only a person can take in Xero", "refs": "..."},
    {"type": "chase", "with": "the cardholder", "what": "the one document still needed"},
    {"type": "close", "what": "the register line this answers"},
    {"type": "match", "doc_ids": ["...", "..."], "what": "intercompany only: these lines are one transaction"},
    {"type": "none", "what": "nothing to post, and why"}
  ],
  "report_line": "the bullet for the report, in the docs/COMMS.md shape"
}
```

`amount_reporting` is the money the answer moves, in the reporting currency
(0 where it posts nothing). A `post` or `update` carries the exact payload the
run will send, with every account by its code and the contact by name, so the
run sends what was decided rather than redrafting it. The coordinator never
writes a payment, an allocation or a reconciliation into an action: where the
answer needs one, it is a `manual` action.

### What the run does with the verdict

1. **Take the grade to the gate**: `.venv/bin/python scripts/investigate_query.py
   gate --key <key>` (or `--amount <reporting currency>` to override the
   verdict's own). It is the same decision as `scripts/resolve_gate.py`, from
   `[auto_resolve]`, and it records the answer on the verdict so the register and
   the guard can see the gate was asked. Follow its first word and never argue
   with it:
   - **`act`**: carry out the actions, report it under `resolved`.
   - **`confirm`**: carry out the actions, report it under `to confirm` and
     register it `--kind decided` (xero-bills step 3a).
   - **`query`**: post nothing. Put `question_for_admin` to the admin with the
     proposed answer and its grade, and register it with the verdict attached.
2. **Carrying out the actions** (act and confirm), without asking, for exactly
   the actions in the verdict and nothing wider:
   - each `post` and `update` goes through the run's own normal path: the
     duplicate guard first, the idempotency key on every create, the bank feed
     line removed the moment it posts
   - each `manual` action goes under `manual` in the report and in the manual
     items thread, and is registered with `outstanding.py add --kind manual`, or
     `update` where the register already has a line on the same transactions
   - each `chase` is sent as any other chase and registered as `documents`
   - the register line is closed if the query was already in it:
     `outstanding.py close --key <key> --reason "investigated, certainty 4: <what was done>"`
3. **For a `query`**, register it:

```bash
.venv/bin/python scripts/outstanding.py add --domain bookkeeping --kind queried \
    --with admin --key example-transfers-01-02oct \
    --investigation data/investigations/example-transfers-01-02oct/20261005T101500Z/result.json \
    --text "where the FX on the 14 Oct GBP-funded leg lands, proposed: the FX gain account (medium)" \
    --refs "Example Holdings Limited · 14 Oct · USD 37,250.00"
```

If a write fails, a lock date refuses it or the duplicate guard finds the
transaction already there, the item is `blocked` like any other, with the
investigation named in the line. The run never edits a payload to make it fit: a
payload that does not post is a verdict that was wrong.

**A failed investigation** is reported under `queries` saying the investigation
failed and why, and registered with its `result.json` like any other query. The
query is still put.

### The guard

`.claude/hooks/guard-queries.py` runs before every Bash call. It refuses
`outstanding.py add --domain bookkeeping --kind queried` unless `--investigation`
names a `result.json` that exists and is failed, graded low, graded high or
medium with a gate that said `query`, or deferred by the daily cap to a day not
yet passed (the line is then registered as scheduled). A verdict graded high or
medium that has not been through the gate is refused with "run it through
resolve_gate". A re-raise of a line the register already holds with a
certainty passes without the flag, because that line was investigated when it
was first raised. `scripts/outstanding.py` makes the same checks itself, so a
call the hook does not see is refused there. The hook stands down in a read-only
session and when `[investigation] enabled` is false.

Test the guard after touching either file: `.venv/bin/python scripts/test_query_guard.py`.

### What an investigation cannot do

It cannot lift a safety rule. The payment ban, the duplicate guard, the
read-only locks and the Slack whitelist stand whatever the certainty.

**A verdict is not a ruling.** The write-back step of `xero-bills` writes an
admin's answer into the rules; it never writes back a verdict, however certain.
The verdict stays on disk under `data/investigations/`, where the next
investigation of the same kind of question finds it. If the admin later rules
the same way, that ruling is the rule.

It reads only. Both agents run read-only, the CLI starts with Write and Edit
disallowed and `.claude/hooks/guard-readonly.py` denies every other write; the
bookkeeping prompt hook stands down while `AGENT_INVESTIGATION` is set.

### Cost and limits

A query settled or reused in Python costs Xero reads and nothing else. A batch
costs one coordinator session, whose reading of the skill and the rules is
shared by every query in it, plus the researchers it sends.
`investigate_query.py spend` shows the day against the cap. Three batches run at
once at most and two pre-checks; the rest queue for a slot. They share the Xero
rate limits with the run, so researchers read the bank feed, the bill feed and
the saved statements before they call Xero.
