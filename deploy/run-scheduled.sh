#!/usr/bin/env bash
# The scheduled runs, and the prompt each one is given.
#
#     deploy/run-scheduled.sh daily            # Mon-Sat 02:00 UTC
#     deploy/run-scheduled.sh bills            # Mon-Fri 02:00 UTC, runs once daily has finished
#     deploy/run-scheduled.sh balance-reports  # Mon-Sat 02:00 UTC, runs once daily has finished
#     deploy/run-scheduled.sh review           # Wed + Sun 10:00 UTC
#     deploy/run-scheduled.sh outstanding      # not in the crontab by default: by hand on the server
#
# Cron calls this, not run-agent.sh directly, so that the prompts live in the
# repo where they can be read and changed, and a schedule stays one crontab
# line. Every session still goes through run-agent.sh, which holds the lock,
# the env, the rate-limit deferral, the write-back commit and the
# phantom-payment check.
#
# `daily` is a CHAIN of three sessions, run one after another rather than as
# three cron entries, so two of them can never be in Xero at the same time.
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

CHANNEL="Report into #${CHANNEL_NAME} (${CHANNEL_ID:-channel id not set in config/group.toml}) following
docs/COMMS.md in full: sections, the invoice shape, references at the end of the line.
Anything the run leaves with a person goes under one of two sections, queries for a
question waiting on an answer and manual for an action waiting on a pair of hands, and
under no other heading. This is a scheduled run with no admin thread behind it, so the
channel is the only place it reports. Post the headline as a top-level message, the
report itself as the first reply in that message thread. Head the top-level message with
the run name and the date and time it started in ${TZ_NAME}. Then, if the run leaves
anything for a person to do in Xero by hand, the same two calls again: a top-level message
that is only MANUAL ITEMS (date, time ${TZ_NAME}) and the items as the first reply in its
thread, opening with the run they came from. Every query and manual item also goes into
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
    ./deploy/run-agent.sh bookkeep "Scheduled daily bookkeeping run, all ${ENTITY_COUNT}
${COMPANY} entities (${ENTITIES}). Load the xero-bills skill and follow it in full:
read the reconstructed bank feed for every entity, refresh the bank feed and the bill
feed, prune and read docs/bookkept/LEDGER.md, sweep the last seven days of the Slack app
and the ${INBOX} inbox for anything not yet bookkept, parse each new document, code it
per rules/EXPENSES.md, rules/SUPPLIERS.md and the entity's own file under rules/entities/,
and create the AUTHORISED bill in the right entity with the source document attached.
Close the run with the bill-payments check against the bank lines already read, then
refresh the bank feed again. Finish by running scripts/check_no_phantom_payments.py and
report whether it printed PASS. Load the bookkeeping domain only. $NO_PAYMENTS
$RATE_LIMIT $CHANNEL"

    ./deploy/run-agent.sh interco "Scheduled daily intercompany reconciliation. Load
the xero-interco skill and reconcile every intercompany pair in config/group.toml across
the ${ENTITY_COUNT} entities, both sides, transaction by transaction, per
docs/INTERCOMPANY_RECON.md and rules/INTERCOMPANY.md. Run it read-only under the lock:
mkdir -p data/interco, then AGENT_READONLY=1 .venv/bin/python scripts/interco_recon.py
--json data/interco/interco_recon_\$(date -u +%F).json, then rebuild the matrix workbook
with .venv/bin/python scripts/interco_matrix.py, whose publish to the Drive folder is the
only write this run makes. Classify every difference and set out the correcting journal
each one needs, but post nothing to Xero: posting is a separate instruction from an
admin. Report only the breaks and what they need; a pair that agrees is one line.
$RATE_LIMIT $CHANNEL"

    ./deploy/run-agent.sh ap-check "Scheduled daily accounts payable check. Refresh the
bill feed (scripts/billfeed.py refresh all) and read data/billfeed/<slug>.md for every
entity: every AUTHORISED unpaid bill as Xero holds it. Report the open payables position
per entity, oldest first, and flag anything past its due date. Then load the bill-payments
skill and check the open bills against the bank feed lines: a bill in one entity paid from
another entity's bank gets the payer's spend money to the pair's intercompany account from
config/group.toml and a bill payments line telling the user to reconcile it to
intercompany by hand. $NO_PAYMENTS $CHANNEL"
    ;;

  bills)
    # The bills payable report (scripts/bills_report.py, plain Python, no
    # Claude session): every bill paid or open per entity, every open bill
    # searched for on the bank, one workbook published to the Drive folder
    # (subfolder `bills`), then the UNPAID list DM'd to the member in
    # [accounts_payable] unpaid_notify so they know what is to be paid. Waits
    # up to six hours for the daily chain to release its lock; read-only
    # against Xero.
    wait_for_chain bills 90
    echo "$(date -u +%FT%TZ) bills report: refresh all"
    "$PY" scripts/bills_report.py refresh all
    if [ -n "$(cfg 'config.accounts_payable()["unpaid_notify"]')" ]; then
        echo "$(date -u +%FT%TZ) bills report: notify the unpaid list"
        "$PY" scripts/bills_report.py notify
    else
        echo "$(date -u +%FT%TZ) bills report: [accounts_payable] unpaid_notify is blank, no DM"
    fi
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
intercompany reconciliation as the standard check the skill requires. Post only the
corrections the review's own rules settle without a judgement call; anything that needs a
decision goes in the report under queries, with the question stated. $NO_PAYMENTS
$RATE_LIMIT $CHANNEL"
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
domain, and post the list to #${CHANNEL_NAME} (${CHANNEL_ID:-channel id not set}) at TOP
LEVEL with no thread_ts, split under 3,500 characters with the headline on the first
chunk. This is not a domain run: load xero for the read API and no domain skill. Nothing
is written to Xero, Drive, Gmail or the docs, nobody is chased or messaged, nothing is
marked done; the register and the Slack post are the only writes. docs/COMMS.md governs
the shape in full. $RATE_LIMIT"
    ;;

  *)
    echo "usage: $0 daily|bills|balance-reports|review|outstanding" >&2
    exit 2
    ;;
esac
