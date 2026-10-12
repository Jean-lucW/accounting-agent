#!/usr/bin/env bash
# The scheduled runs, and the prompt each one is given.
#
#     deploy/run-scheduled.sh daily            # Mon-Sat 02:00 UTC
#     deploy/run-scheduled.sh bills            # Mon-Fri 02:00 UTC, runs once daily has finished
#     deploy/run-scheduled.sh balance-reports  # Mon-Sat 02:00 UTC, runs once daily has finished
#     deploy/run-scheduled.sh review           # Wed + Sun 10:00 UTC
#     deploy/run-scheduled.sh outstanding-check # Mon-Sat 02:00 UTC, runs once daily has finished
#     deploy/run-scheduled.sh outstanding      # not in the crontab by default: by hand on the server
#     deploy/run-scheduled.sh usage            # Mon 07:00 UTC, plain Python: the weekly Claude usage report
#
# Cron calls this, not run-agent.sh directly, so that the prompts live in the
# repo where they can be read and changed, and a schedule stays one crontab
# line. Every session still goes through run-agent.sh, which holds the lock,
# the env, the rate-limit deferral, the write-back commit and the
# phantom-payment check.
#
# `daily` is a CHAIN of three links, run one after another rather than as
# three cron entries, so two of them can never be in Xero at the same time.
# Each link is plain Python first (scripts/daily_chain.py) and a Claude session
# only when there is work that needs judgement: its gate exits 0 to start the
# session, 10 when it handled the link and posted it, 11 the same with a failed
# exit check (the chain stops, as after a failed session), anything else when
# the gate itself failed (the session runs as it always has).
# It holds data/daily-chain.lock for its whole length; `bills` and
# `balance-reports`, cron-fired at the same minute, wait on that lock so they
# read Xero after the day's bills are posted and never alongside a session.
# One domain per session: the bookkeeping run and the accounts payable check
# are the bookkeeping domain, the intercompany reconciliation loads its own
# support skill.
#
# Every one of these reports into the channel in config/group.toml [slack]. A
# cron run has no admin DM thread to reply in, and the channel is the standing
# record of what the books had done to them overnight.
#
# Nothing about the group is written here: the entity list, the channel, the
# accounting inbox and the timezone are read from config/group.toml at run
# time, so editing that file re-shapes every prompt on the next fire.
set -euo pipefail

HOME_DIR="${AGENT_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$HOME_DIR"
mkdir -p data/logs

PY=./.venv/bin/python

# A gate of the daily chain: plain Python, a session only when it says so.
chain() { "$PY" scripts/daily_chain.py "$@"; }

# One value from config/group.toml. The argument is a Python expression over
# the `config` module; the result is printed as-is.
cfg() {
    "$PY" -c "import sys; sys.path.insert(0, 'src')
from accounting_agent import config
print($1)"
}

COMPANY="$(cfg 'config.company_name()')"
ENTITIES="$(cfg '"; ".join(e.title for e in config.entities())')"
ENTITY_COUNT="$(cfg 'len(config.entities())')"
INBOX="$(cfg 'config.company("accounting_inbox", "") or "the accounting inbox"')"
TZ_NAME="$(cfg 'config.company("timezone", "UTC")')"
CHANNEL_ID="$(cfg 'config.slack().channel_id')"
CHANNEL_NAME="$(cfg 'config.slack().channel_name')"
COMPANY_UP="$(printf '%s' "$COMPANY" | tr '[:lower:]' '[:upper:]')"

CHANNEL="Report into #${CHANNEL_NAME} (${CHANNEL_ID:-channel id not set in config/group.toml}) following
docs/COMMS.md in full: sections, the invoice shape, references at the end of the line.
Anything the run leaves with a person goes under one of three sections, queries for a
question waiting on an answer, to confirm for a posting waiting on an admin's yes, and
manual for an action waiting on a pair of hands, and under no other heading. Before a
question goes out, answer it yourself and run scripts/resolve_gate.py (CLAUDE.md,
\"Queries: answer them before asking them\"); a read-only run puts its answer in the query. This is a scheduled run with no admin thread behind it, so the
channel is the only place it reports. Queue the report, never post it yourself:
.venv/bin/python scripts/slack_outbox.py channel --headline \"${COMPANY_UP} <run name>
(<date, time it started in ${TZ_NAME}>)\" --thread <report file>. The runner posts it when
the session ends, the headline at top level with the run's cost in brackets after it, the
report as the first reply in its thread; write the headline without any cost. Then, if the
run leaves anything for a person to do in Xero by hand, queue a second one: headline
${COMPANY_UP} MANUAL ITEMS (date, time ${TZ_NAME}), the items in its thread, opening with
the run they came from. Every query, to confirm and manual item also goes into
scripts/outstanding.py in the same step that sends it."

NO_PAYMENTS='Never create or allocate a payment, never mark a bill paid and never
reconcile anything: bills stay AUTHORISED and unpaid and the user matches the bank
feed to them by hand.'

RATE_LIMIT='If Xero returns a daily rate limit (429, X-Rate-Limit-Problem: daily),
stop and say so in the report rather than working around it: the job re-runs itself
when the cap resets.'

# Wait for the daily chain to release its lock (fired at the same minute, so
# sleep first to let the chain take it). Six hours, then give up.
wait_for_chain() {   # $1 = job name, $2 = seconds to let the chain start
    sleep "$2"
    exec 8> data/daily-chain.lock
    if ! flock -w 21600 8; then
        echo "$(date -u +%FT%TZ) $1: daily chain still running after six hours, giving up" >&2
        exit 1
    fi
    set -a; . ./.env; set +a
}

case "${1:-}" in

  daily)
    exec 8> data/daily-chain.lock
    flock 8
    # The gates run inside this lock: they must not flock it a second time.
    export AGENT_CHAIN_LOCK_HELD=1
    set -a; . ./.env; set +a

    # 1. Bookkeeping. Skipped only when the gate proves nothing is new since the
    # last session that finished: no message to the Slack app or the inbox,
    # every message of the last days in the ledger, no new bank line after the
    # refresh, nothing due on the register. A skip still runs the exit checks
    # and posts the run to the channel.
    set +e; chain bookkeep; gate=$?; set -e
    if [ "$gate" -eq 11 ]; then exit 1; fi
    if [ "$gate" -ne 10 ]; then
    set +e
    ./deploy/run-agent.sh bookkeep "Scheduled daily bookkeeping run, all ${ENTITY_COUNT}
${COMPANY} entities (${ENTITIES}). Load the xero-bills skill and follow it in full. Your
first command is .venv/bin/python scripts/daily_chain.py bookkeep-brief: it prints the
briefing the gate has just built, with the bank feed and the bill feed refreshed, the
ledger pruned, the Slack app and the ${INBOX} inbox swept against the ledger, every
document still to work downloaded and read, each with its Xero, rules/SUPPLIERS.md and
coding-history lookups; never redo any of that. Settle the open to confirm items. Send any
further lookup with a long output to query-researcher, code each document per
rules/EXPENSES.md, rules/SUPPLIERS.md and the entity's own file under rules/entities/, and
post every AUTHORISED bill with its source document in one post_bills.py --live call whose
settle list writes the ledger lines, closes the register lines and labels the emails.
Reopen the investigations the daily cap deferred when the briefing lists any. A question
the rules do not answer goes through the skill's step 3a and the query-investigation
skill: open an investigation, grade its verdict, and post or query as the gate
(scripts/resolve_gate.py) says.
Do NOT run the bill-payments check: the accounts payable check that follows this run runs
it, once for the night. Refresh the bank feed again at the end so it opens current for
that check. Write the report with scripts/bookkeep_report.py. Finish by running
scripts/check_no_phantom_payments.py and scripts/outstanding.py check-queries (the
register keeps only the queries you could not
answer) and report whether each printed PASS. Load the bookkeeping domain only. $NO_PAYMENTS
$RATE_LIMIT $CHANNEL"
    rc=$?
    set -e
    # A session that ran to the end becomes what tomorrow's gate compares against.
    chain bookkeep-commit --rc "$rc" || true
    [ "$rc" -eq 0 ] || exit "$rc"
    fi

    # 2. Intercompany. The recon, the FX drafts and both matrices run here in
    # plain Python (deploy/run-interco.sh); the session starts only for an open
    # break, a verdict or an admin answer waiting, or a revaluation ready that
    # nobody has been told about, and then reads what the gate wrote instead of
    # re-running the recon.
    set +e; chain interco; gate=$?; set -e
    if [ "$gate" -eq 11 ]; then exit 1; fi
    if [ "$gate" -ne 10 ]; then
    TODAY="$(date -u +%F)"
    if [ "$gate" -eq 0 ]; then
        RECON_STEP="The reconciliation already ran this morning in plain Python
(deploy/run-interco.sh: the recon, the FX drafts and both matrices, the matrices on Drive):
do not run it again before settling. Read the compact summary
data/interco/interco_recon_${TODAY}.summary.txt, the open items with .venv/bin/python
scripts/interco_breaks.py list --json data/interco/interco_recon_${TODAY}.json, and why this
session was started in data/gates/interco-${TODAY}.json; read the full
data/interco/interco_recon_${TODAY}.txt only for a pair you are settling."
    else
        RECON_STEP="Run it read-only under the lock:
mkdir -p data/interco, then AGENT_READONLY=1 .venv/bin/python scripts/interco_recon.py
--json data/interco/interco_recon_\$(date -u +%F).json, then rebuild the matrix workbook
with .venv/bin/python scripts/interco_matrix.py, whose publish to the Drive folder is the
only write this run makes."
    fi
    ./deploy/run-agent.sh interco "Scheduled daily intercompany reconciliation. Load
the xero-interco skill and reconcile every intercompany pair in config/group.toml across
the ${ENTITY_COUNT} entities, both sides, transaction by transaction, per
docs/INTERCOMPANY_RECON.md and rules/INTERCOMPANY.md. $RECON_STEP Classify every difference and set out the correcting journal
each one needs, but post nothing to Xero: posting is a separate instruction from an
admin. List the open items with scripts/interco_breaks.py list. Never post an FX
revaluation: after a month end, say which pairs are ready and that an admin posts them
with run post the fx interco journals. Report only the breaks and what they need; a pair
that agrees is one line.
$RATE_LIMIT $CHANNEL"
    fi

    # 3. Accounts payable. The bill feed refresh, the cross-entity matching, the
    # reconcile list and both posts are plain Python; a session starts only to
    # post the funding legs for new pairs of a bill and another entity's bank
    # line, and ends by handing back to ap-finish. The payables DM stays the
    # `bills` job's, so the chain never sends it. If the gate fails, the whole
    # check runs as a session, as before.
    set +e; chain ap-prepare; gate=$?; set -e
    AP_DIR="data/ap_check/$(date -u +%F)"
    if [ "$gate" -eq 10 ]; then
        chain ap-finish --dir "$AP_DIR"
    elif [ "$gate" -eq 0 ]; then
        set +e
        ./deploy/run-agent.sh ap-check "Scheduled daily accounts payable check, the funding
legs only. The bill feed was refreshed a minute ago and every open bill was matched against
the bank feed in plain Python: the pairs of an open bill and another entity's bank line are
under \"new\" in $AP_DIR/candidates.json, each with the bill, the line and what they matched on
(same currency and amount, or the Revolut leg's own bill amount). Load the bill-payments skill
and settle each new pair per its table and \"The cross-entity posting\": the payer's spend
money to the pair's intercompany account through the duplicate guard and the feed line removed,
or a query (answered from the evidence first, per CLAUDE.md) or a blocked line where the skill
says not to post. Do not match any other bill, do not build the reconcile list and do not queue
anything for the channel yourself. Write your report lines to $AP_DIR/report.md, the bill
payments, queries, to confirm and blocked sections in the docs/COMMS.md shape, and every line
a person has to act on, the reconcile-to-intercompany instruction above all, as bullets to
$AP_DIR/manual.md, registering each per the register rule. Last, run
.venv/bin/python scripts/daily_chain.py ap-finish --session --dir $AP_DIR, which builds the
reconcile list and queues the ${COMPANY_UP} ACCOUNTS PAYABLE CHECK report and the
${COMPANY_UP} MANUAL ITEMS thread with your lines in them. $NO_PAYMENTS $RATE_LIMIT"
        rc=$?
        set -e
        # Backstop: a session that died, or was deferred for the Xero rate limit,
        # before its last step still leaves the reconcile list and the report.
        [ -e "$AP_DIR/finished" ] || chain ap-finish --session --dir "$AP_DIR" || true
        [ "$rc" -eq 0 ] || exit "$rc"
    else
        ./deploy/run-agent.sh ap-check "Scheduled daily accounts payable check. Refresh the
bill feed (scripts/billfeed.py refresh all) and read data/billfeed/<slug>.md for every
entity: every AUTHORISED unpaid bill as Xero holds it. Report the open payables position
per entity, oldest first, and flag anything past its due date. Then load the bill-payments
skill and check the open bills against the bank feed lines: a bill in one entity paid from
another entity's bank gets the payer's spend money to the pair's intercompany account from
config/group.toml and a bill payments line telling the user to reconcile it to
intercompany by hand. $NO_PAYMENTS $CHANNEL"
    fi
    ;;

  bills)
    # The bills payable report (scripts/bills_report.py, plain Python, no
    # Claude session): every bill paid or open per entity, every open bill
    # searched for on the bank, one workbook published to the Drive folder
    # (subfolder `bills`); then the reconcile list (scripts/reconcile_list.py:
    # the bank reconcile check, fresh, then which statement line goes to which
    # posted bill), so the payables DM can carry its "to match" section; then
    # the sectioned payables list DM'd to the member in [accounts_payable]
    # unpaid_notify and a run post in the channel. The bills are pulled once.
    # On a Xero rate limit `daily` rebuilds from the last pull and still sends.
    # A failed reconcile check writes nothing, sends no DM and does not fail
    # this job. Waits up to six hours for the daily chain to release its lock;
    # read-only against Xero and the banks.
    wait_for_chain bills 90
    echo "$(date -u +%FT%TZ) bills report: refresh all"
    "$PY" scripts/bills_report.py refresh all \
        || echo "$(date -u +%FT%TZ) bills report: pull failed, daily rebuilds from the last pull"
    # One DM when one person gets both lists: the payables DM already carries
    # the reconcile list under "to match in Xero".
    RECON_DM=""
    if [ "$(cfg 'config.reconcile_notify()')" = "$(cfg 'config.accounts_payable()["unpaid_notify"]')" ]; then
        RECON_DM="--no-dm"
    fi
    # The daily chain's ap-finish already built today's list (without a DM).
    # Rebuild only when it is missing or stale, or when its own DM still has
    # to go to a different person.
    BUILT_TODAY=$("$PY" -c 'import json,sys,datetime
try:
    b = json.load(open("data/reports/reconcile_list.json")).get("built", "")
except Exception:
    b = ""
print("yes" if b[:10] == datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d") else "")')
    if [ -n "$BUILT_TODAY" ] && [ -n "$RECON_DM" ]; then
        echo "$(date -u +%FT%TZ) reconcile list: built today by the daily chain, reused"
    else
        echo "$(date -u +%FT%TZ) reconcile list: build"
        "$PY" scripts/reconcile_list.py run --no-refresh $RECON_DM \
            || echo "$(date -u +%FT%TZ) reconcile list: not built, see above"
    fi
    echo "$(date -u +%FT%TZ) bills report: daily"
    "$PY" scripts/bills_report.py daily --no-refresh
    ;;

  balance-reports)
    # The prepayments, accruals and deposits reports, then the bank and FX fees
    # refresh: plain Python, no Claude session. Fired the same minute as the
    # daily chain Mon-Sat, the days the intercompany reconciliation runs, and
    # waits on the chain's lock so it reads Xero after the day's bills and
    # never alongside a session; the bills job waits on the same lock and
    # starts first, so the two never overlap either. Each refresh rebuilds the
    # master workbook under data/reports/ and publishes the copy to the Drive
    # folder (subfolders `reports` and `bank_fees`); exit 1 from a refresh means
    # a month disagrees with the Balance Sheet, which the summary carries, so
    # it does not stop the job. Then one top-level message in the channel with
    # the findings of all of them.
    wait_for_chain balance-reports 120
    echo "$(date -u +%FT%TZ) prepayments report: refresh all"
    "$PY" scripts/prepayments_report.py refresh all || true
    echo "$(date -u +%FT%TZ) accruals report: refresh all"
    "$PY" scripts/accruals_report.py refresh all || true
    echo "$(date -u +%FT%TZ) deposits report: refresh all"
    "$PY" scripts/deposits_report.py refresh all || true
    echo "$(date -u +%FT%TZ) bank and fx fees: refresh"
    "$PY" scripts/bank_fees.py refresh || true
    echo "$(date -u +%FT%TZ) reports: summary to the channel"
    "$PY" scripts/balance_reports_summary.py
    ;;

  review)
    ./deploy/run-agent.sh review "Scheduled full bookkeeping review. Load the xero-review
skill and review all ${ENTITY_COUNT} entities (${ENTITIES}) against the method and
checklist in docs/REVIEW_METHOD.md and the group's own rules in rules/ (rules/GROUP.md,
rules/EXPENSES.md, rules/SUPPLIERS.md, rules/PAYROLL.md, rules/INTERCOMPANY.md and each
entity's file under rules/entities/), plus the standard hygiene checks, and run the
intercompany reconciliation as the standard check the skill requires. Post the
corrections the review's own rules settle without a judgement call; a correction that
needs a decision is answered from the evidence and posted or queried as
scripts/resolve_gate.py says, the question and your proposed answer stated. $NO_PAYMENTS
$RATE_LIMIT $CHANNEL"
    ;;

  outstanding-check)
    # The daily outstanding-items check (scripts/outstanding_check.py): Python
    # closes what Xero shows done and deletes every bookkeeping line under the
    # trivial amount on its wording, a read-only coordinator session with
    # checker agents (light_checker_model for documents and watches,
    # checker_model for the rest) judges what is left, and every line
    # definitely done, not needed or trivial is deleted from the register; the
    # run goes to the channel with its cost. Models, the trivial amount and the
    # Xero-calls floor come from [models] and [outstanding_check] in
    # config/group.toml. Waits on the chain's lock like bills and
    # balance-reports and starts after them, so it reads Xero once the day's
    # posting is over. Not scheduled when [outstanding_check] enabled is false.
    if [ "$(cfg 'config.outstanding_check()["enabled"]')" != "True" ]; then
        echo "$(date -u +%FT%TZ) outstanding-check: disabled in [outstanding_check], nothing to do"
        exit 0
    fi
    wait_for_chain outstanding-check 150
    echo "$(date -u +%FT%TZ) outstanding check: run"
    "$PY" scripts/outstanding_check.py run
    ;;

  outstanding)
    # The outstanding-items list. Not scheduled by default: an admin asks for
    # it in Slack, which comes through the listener with the same brief, or
    # someone on the server fires this by hand (an optional crontab line is in
    # deploy/crontab.example). Read-only against Xero; the register and the
    # Slack post are the only things written, and the list goes to the channel
    # at top level, not into a thread, because a manual action nobody can see
    # is not a standing list.
    ./deploy/run-agent.sh outstanding "Outstanding items, asked for on the server. Load
the outstanding-items skill and follow it in full. The register docs/bookkept/OUTSTANDING.md
is the list: read it with scripts/outstanding.py list, then reconcile it against what the
runs of the last fourteen days raised (the channel's MANUAL ITEMS threads and run threads,
the admins' own run threads under data/slack/threads, the intake ledger), adding anything
a run forgot to register and closing anything the skill's closing evidence shows done,
checked in Xero read-only. One line per item however many runs raised it, one section per
domain, and queue the list for #${CHANNEL_NAME} (${CHANNEL_ID:-channel id not set}) at TOP
LEVEL with .venv/bin/python scripts/slack_outbox.py channel --top-level <file>, which the
runner posts when the session ends, split under 3,500 characters, the headline first.
This is not a domain run: load xero for the read API and no domain skill. Nothing
is written to Xero, Drive, Gmail or the docs, nobody is chased or messaged, nothing is
marked done; the register and the Slack post are the only writes. docs/COMMS.md governs
the shape in full. $RATE_LIMIT"
    ;;

  usage)
    # The weekly Claude usage report (scripts/usage_report.py, plain Python, no
    # Claude session, so it costs nothing): what every session cost by job
    # against the week before, read from the session JSON the runners keep
    # under data/runs/. A headline at top level in the channel, the detail in
    # its thread.
    echo "$(date -u +%FT%TZ) usage report: last seven days"
    "$PY" scripts/usage_report.py --post
    ;;

  *)
    echo "usage: $0 daily|bills|balance-reports|review|outstanding-check|outstanding|usage" >&2
    exit 2
    ;;
esac
