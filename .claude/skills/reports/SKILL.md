---
name: reports
description: The standing report catalogue. Match a request for a report ("give me a report for X", "send me the prepayments", "what deposits are open") to the one report skill that owns it, then load only that skill. Covers prepayments, accruals, deposits and bills payable (paid / unpaid, and why a bill is still open). Use whenever someone asks for a report by name or by description, or asks which reports exist.
---

# Reports: pick the one, load the one

A standing report is a workbook the agent keeps rather than writes: one copy on
the server, refreshed on request, fetched to the asker's Downloads. This skill
is the index. It does no work itself. **Read the table, load the one skill
that owns the request, and stop reading here.**

| Ask sounds like | Report | Skill | Runner |
|---|---|---|---|
| prepayments, prepaid, deferred cost, amortisation schedule, what is in the prepayments account, releases | Prepayments | `report-prepayments` | `scripts/prepayments_report.py` |
| accruals, accrued, what have we accrued, open accruals, month-end accruals | Accruals | `report-accruals` | `scripts/accruals_report.py` |
| deposits, deposits held, rent deposit, supplier deposit, what deposits are out | Deposits | `report-deposits` | `scripts/deposits_report.py` |
| bills, unpaid bills, what is outstanding, what needs paying, is X paid, why is this bill still open, accounts payable position, bills workbook | Bills payable | `report-bills` | `scripts/bills_report.py` |

Nothing else is a standing report. A question that fits none of the rows is
ordinary work for its own domain: a P&L or balance sheet question is a
`query`, a reconciliation is its own skill.

**One report per run.** Two reports asked for at once are two runs: do the
first, report it, say the second needs its own. The exception is the fetch:
`deploy/fetch-report.sh all` pulls whatever is already built and calls no API.

Every report also publishes itself to Drive after every build and refreshes
itself after the daily chain: the bills report (to the `bills` subfolder of
the Drive publish folder in `config.drive_settings()`) and the three balance
reports, prepayments, accruals and deposits (to the `reports` subfolder, on
the `balance-reports` schedule, the findings of all three posted to the
channel by `scripts/balance_reports_summary.py`). The copy on the server is
the master; Drive holds what the server holds. With no publish folder
configured, publishing is skipped with a one-line note. Each skill has the
detail.

## Rules that hold for every report

1. **ADMINS ONLY.** The admin tier in `config.slack().admins`. The user tier
   never triggers a report; a request from them is captured as input and goes
   in the report to the admin, like anything else they send. The read-only
   tier cannot build one either: a report writes a file, and
   `AGENT_READONLY=1` plus the guard hook deny it. A read-only `query` about
   prepayments, accruals or deposits is answered in prose from the store and
   the ledger, with no file and no refresh.
2. **Refresh, never rebuild.** Every runner is incremental: it reads Xero from
   the last sync less a lookback and merges. That is the whole point of the
   store: asking for the same report twice in a day costs a fraction of the
   first build. Use `--full` only for the reasons the report's own skill gives.
3. **One copy on the server.** `data/reports/<Workbook>.xlsx`, overwritten in
   place, never dated, never duplicated. The server is the only machine that
   calls Xero, so the report is only ever built there.
4. **Delivery is the fetch script.** The server cannot write to a laptop and
   the Slack app cannot upload a file, so an admin gets the workbook by
   running `deploy/fetch-report.sh <report>` on their own machine (with
   `AGENT_SERVER` set), which lands it in `~/Downloads`. In a laptop session,
   run the fetch yourself once the server has refreshed; in a Slack run, give
   the admin that one command in the report.
5. **An entity filter is a filter, not a run.** "the prepayments for OpCo US"
   is the same workbook: every report carries every entity in
   `config/group.toml`, with the entity in column A and an autofilter on it.
   Refresh all, deliver the one file, and answer the entity question from it.
   `refresh <entity>` exists for repairing one ledger, not for answering a
   narrower question. The same holds for status: the three balance workbooks
   open filtered to `active`, and "the inactive ones" is the same file with
   the filter cleared.
6. **Read-only against Xero.** No report posts, corrects or clears anything.
   What a report finds wrong goes in the Checks sheet and into the message;
   the correcting journal is a bookkeeping run, and a separate one.
7. **The Checks sheet is the report, and it holds only what is outstanding**:
   a query waiting on an answer or an issue a person has to settle, such as
   what does not agree with Xero, what could not be attributed, or what has no
   period on the line or on the invoice. Nothing for information: no accepted
   check, no row cleared by hand, no account cleared to nil, no movement placed
   on a row by balance. Those stay in the store (`rows`, `notes`), not on the
   sheet. An empty Checks sheet is worth one line saying so. **Every Checks
   line is also a line on the outstanding-items register** (domain `reports`,
   key `rpt-check:...`, its id in the sheet's Register column), added the
   build it is raised and deleted the build it is not; a line some other run
   already holds as a manual item for the same invoice is used instead of a
   second one.
7a. **A quarter-end copy beside the live one**, where `[reports]
   quarter_end_from` is set: `data/reports/quarter-end/<Company>_<Report>_Q<n>_<year>.xlsx`,
   published to the quarter-end Drive folder. It is the store cut at the
   quarter end (quarters follow `financial_year_end`): no row raised, no
   movement posted, no Xero balance and no note dated after it, and every
   check judged as at that day, so what was missing then is on it and what
   arose later is not. Every build rewrites it until the next quarter end
   passes, so a posting dated back into the quarter clears its check there
   too; then it is left as it stands.
8. **A decision an admin has made is recorded, not re-raised.** An item
   accepted as it stands, or an open point waiting on something outside Xero
   (a board ruling, a refund not yet received) is a `note`: an entity, what
   kind of line it is, and what it says. It survives `--full`, so the decision
   is auditable and the run stops proposing a correction the admin has already
   refused. An open point sits on the Checks sheet and on the register (its own
   `rpt-note:` line, or an existing line tied to it with `set-note <note>
   register_key <key>`), and the build after that register line is closed, the
   note goes by itself. `set-note <note> as_of <month>` says which quarter-end
   copies carry it. A note of kind `accepted` is a decision, not a point, and
   stays off both. `notes` lists them, `unnote` removes one. A note never replaces a correction the admin asked
   for, and it is never used to silence a difference against the Balance Sheet.

```bash
.venv/bin/python scripts/deposits_report.py note holdco "open point" "<what it says>"
```

## Answering

`docs/COMMS.md` governs the message, as always. What the Checks sheet holds is
not one undifferentiated list: a check waiting on an admin's answer goes under
`queries`, a check whose fix is a step only a person can take goes under
`manual`, and they are two sections, never one. A report run is read-only,
so it never has a `to confirm` section: where it can answer a check itself,
the answer goes into the query as the proposed one (CLAUDE.md, "Queries:
answer them before asking them").

```
prepayments · 34 active · 2 queries · 1 manual · refreshed every entity

report
- Prepayments.xlsx refreshed on the server and on Drive (reports folder), 34 prepayments active
- fetch it with deploy/fetch-report.sh prepayments

queries
- release missing in Jul 26 and Aug 26, Contoso Cloud, USD 36,000.00, INV-1010
- does not agree with Xero in Aug 26 by USD 500.00, OpCo US

manual
- no period on the invoice line, so no schedule, set-period, Litware Software, USD 3,600.00, OpCo US
```

References trail the line, every time. Never paste the workbook's rows into
Slack: the file is the answer, the checks are the message.

**Scheduled, the summary posts as two messages.** The `balance-reports` job
runs `scripts/balance_reports_summary.py`, which posts a top-level
`<COMPANY> REPORTS RUN (date, time)` and the three reports as the first reply
in its thread, like every other run. The script builds the sections itself
from the stores; a session does not post that summary again.

## Adding a report

An adopter adds a standing report in five steps. It is not a new `.md`
document: the rules live in the skill, and the report's own findings live in
the workbook.

1. **The runner.** For another balance-sheet account that behaves like the
   three here (rows created on one side, released or reversed on the other,
   checked against the Balance Sheet), add a `Spec` beside `PREPAYMENTS`,
   `ACCRUALS` and `DEPOSITS` in `scripts/balance_report.py` (key, title,
   workbook name, account class, default discovery words, row side, labels,
   `grid`, `stale_months`, `schedule`) and a runner of about forty lines
   copied from `scripts/deposits_report.py` that calls
   `main(<YOUR_SPEC>, sys.argv[1:], __doc__)`. It gets refresh, build, checks,
   notes, link, combine, clear and accept for free, and `refresh all` covers
   every entity in config. For anything else (a report that is not one
   account's ledger) write a new script under `scripts/` that reads entities
   from `accounting_agent.config`, writes one workbook to `data/reports/`,
   publishes with `accounting_agent.publish.publish_to_drive(path, kind="reports")`,
   and never writes to Xero.
2. **Config.** Add a `[reports.<key>]` table to `config/group.toml` with its
   `account_words`, optional `account_not_words`, and optional explicit
   `accounts = { <ENTITY> = ["<code>"] }`.
3. **The skill.** `.claude/skills/report-<key>/SKILL.md`, shaped like
   `report-deposits`: the model it assumes, how to run it, the workbook, each
   check and what it means, how to fix what it cannot work out.
4. **The catalogue.** A row in the table above, so a request finds it.
5. **Delivery and schedule.** A line in `book_for` in
   `deploy/fetch-report.sh`, and, if it should refresh itself, a step in the
   `balance-reports` job in `deploy/run-scheduled.sh` (and in
   `scripts/balance_reports_summary.py` if its findings belong in that post).
