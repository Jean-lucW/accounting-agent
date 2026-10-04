#!/usr/bin/env bash
# Wait for a run to finish, then Slack a summary of it to whoever started it:
# what it did, and what still needs them. Exists because unattended runs have
# nobody watching at the time, so the account of the run, and the questions it
# could not answer, have to find the user afterwards instead.
#
#   ./deploy/notify-queried.sh <job> <slack-user-id> [thread-ts]
#
# The job name is per-operator (`run-<member id>-<thread key>` from Slack),
# not a scheduled job name, because each operator gets their own session,
# lock and log. `bookkeep` is still the scheduled bookkeeping job name.
#
# The report it writes follows docs/COMMS.md, which is the single place the
# house style lives. Change the style there, not here. The company name, the
# timezone and the reporting channel come from config/group.toml.
#
# Launch it detached, alongside (or just after) the run it watches:
#
#   setsid nohup ./deploy/notify-queried.sh run-UXXXXXXXXXX-1-0 UXXXXXXXXXX \
#     > /dev/null 2>&1 < /dev/null &
set -euo pipefail

HOME_DIR="${AGENT_HOME:-$(cd "$(dirname "$0")/.." && pwd)}"
cd "$HOME_DIR"
export PATH="$HOME/.local/bin:$PATH"

JOB="${1:-bookkeep}"                    # per-operator: run-<slack-user-id>-<thread key>
SLACK_USER="${2:-}"                  # who started the run; their DM gets the report
THREAD_TS="${3:-}"                   # set when triggered from a Slack thread
DATE=$(date -u +%Y-%m-%d)
# RUN_LOG_OVERRIDE lets a past run be replayed through the notifier to check
# the message it produces, without waiting for the next real run.
RUN_LOG="${RUN_LOG_OVERRIDE:-data/logs/${JOB}-${DATE}.log}"
LOG="data/logs/notify-${JOB}-${DATE}.log"

mkdir -p data/logs

if [ -z "$SLACK_USER" ]; then
    echo "usage: $0 <job> <slack-user-id> [thread-ts]" >&2
    exit 2
fi

set -a
# shellcheck disable=SC1091
. ./.env
set +a

if [ -z "${ANTHROPIC_API_KEY:-}" ] && [ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
    echo "$(date -u +%FT%TZ) [notify] no Claude credential, aborting" >> "$LOG"
    exit 1
fi

# The group's own names, from config/group.toml (tab separated).
IFS=$'\t' read -r COMPANY TZ_NAME CHANNEL_ID CHANNEL_NAME < <(./.venv/bin/python - <<'EOF'
import sys
sys.path.insert(0, "src")
from accounting_agent import config
s = config.slack()
print("\t".join([config.company_name().upper(), config.company("timezone", "UTC"),
                 s.channel_id or "-", s.channel_name]))
EOF
)

echo "$(date -u +%FT%TZ) [notify] waiting for '${JOB}' to finish" >> "$LOG"

# Wait on the process rather than the lock: taking the lock ourselves would
# race with a run that has not acquired it yet, and would then block the next
# scheduled run while we work.
waited=0
while pgrep -f "run-agent.sh ${JOB}" > /dev/null 2>&1; do
    sleep 20
    waited=$((waited + 20))
    if [ "$waited" -gt 21600 ]; then          # 6 hours
        echo "$(date -u +%FT%TZ) [notify] gave up waiting after 6h" >> "$LOG"
        exit 1
    fi
done

echo "$(date -u +%FT%TZ) [notify] '${JOB}' finished after ${waited}s, composing" >> "$LOG"

if [ ! -f "$RUN_LOG" ]; then
    echo "$(date -u +%FT%TZ) [notify] no run log at ${RUN_LOG}, nothing to send" >> "$LOG"
    exit 1
fi

if [ "$CHANNEL_ID" = "-" ]; then
    CHANNEL_RULE="No reporting channel is configured (config/group.toml [slack] channel_id is empty): everything below that would go to the channel goes to ${SLACK_USER} by DM instead, and say so in one line."
else
    CHANNEL_RULE="The reporting channel is ${CHANNEL_ID} (#${CHANNEL_NAME})."
fi

read -r -d '' PROMPT <<EOF || true
The unattended '${JOB}' run on this server has just finished. Its report is at
the end of ${RUN_LOG}. Read that file.

Then read docs/COMMS.md and write the message in that style. It is the house
style and it is not optional. The person who started the run was not watching,
so this DM is the only account of it they get, but terse: facts, one per line,
no preamble, no sign-off, no emoji, no restating the task.

${CHANNEL_RULE}

WHERE IT GOES. Read the run log and decide first whether this was a BOOKKEEPING
run (the invoice intake, the bank and bill feeds, the bill-payments check, the
phantom-payment check) or something else: a focused task, a review, a report
refresh, a question answered.

A BOOKKEEPING run reports in the channel ${CHANNEL_ID} (#${CHANNEL_NAME}), in
two calls:

  1. top level, no thread_ts, the headline and nothing else:
     ${COMPANY} BOOKKEEPING RUN (18 Sep 2026, 14:32 <zone>)
     with the run's own start date and time in ${TZ_NAME}, in that shape.
  2. the same endpoint again with "thread_ts" set to the "ts" the first call
     returned, carrying the WHOLE report: every section it has, queried and
     bill payments included.

  Then DM ${SLACK_USER} the report's one-line headline and the words
  full report in #${CHANNEL_NAME}. Nothing more: never the report twice.

ANYTHING ELSE stays with the person: ONE Slack DM to ${SLACK_USER} carrying the
whole report, and nothing posted in the channel, unless the report has a
*bill payments* section or another line telling the user to do something in
Xero by hand. Those bullets then also go to the channel, from any run, in the
same two-call shape as a run report:
  1. top level, no thread_ts, the headline and nothing else:
     ${COMPANY} MANUAL ITEMS (18 Sep 2026, 14:32 <zone>)
     with the run's own start date and time in ${TZ_NAME}, in that shape.
  2. the same endpoint again with "thread_ts" set to the "ts" the first call
     returned: one line naming the run it came from, then the bullets. No
     chases, no queries, nothing the agent did itself.

Both use the bot token SLACK_BOT_TOKEN from .env: curl POST to
https://slack.com/api/chat.postMessage, "channel" set to the channel ID or to
${SLACK_USER}. Posting to a bare user ID works; do not call conversations.open,
the app does not have the scope it needs.
${THREAD_TS:+Include "thread_ts": "${THREAD_TS}" on the DM so it lands in the
thread the run was triggered from.}
If the channel answers channel_not_found the bot is not in it: DM the full
report instead, say so in one line, and do not retry.
docs/COMMS.md, "Where a message goes", is the rule for all of this.

SHAPE. docs/COMMS.md is a hard rule and governs this message completely.
First line, the headline:

  bookkeeping · 14 bookkept · 2 blocked · 3 queried · check PASS

For a focused run, replace the word bookkeeping with the task in a handful of
words, and drop the counts that do not apply. If the run failed, or the
phantom-payment check did not print PASS, that goes in the headline and the
reason is the next line. Never report a clean run the log does not show.

Then only the sections that have content, in this order. Bold lowercase
heading, bullets underneath (the entity names below are placeholders: use
each entity's short name from config/group.toml):

  *bookkept*
  • Contoso Cloud Ltd · 04 Sep · EUR 980.00 · paid <entity> · recognised <entity> · INV-1002

  *bill payments*
  • a hotel supplier · 28 Aug · EUR 240.00 · booked to Travel · INV-1003, paid by <entity A>, recognised as a bill in <entity B>, spend money posted in <entity A>. *Manually reconcile the bill in <entity B> to intercompany.*

  *not attempted*
  • last month's invoices from a SaaS supplier, deferred to the month-end review

  *blocked*
  • a tax reclaim cannot post, the period lock date rejects it · DR <code> / CR <code>, EUR 480.00

  *queries*
  • a cloud hosting bill, which entity · 05 Sep, USD 1,250.00, INV-1001

  *manual*
  • reconcile the <entity B> hotel bill to intercompany · INV-1003

  *chased*
  • <user> for the 28 Aug hotel receipt, <entity>

  *from users*
  • <user>: the card charge belongs to <entity> · coded <code> in <entity>

  *wrote back*
  • new supplier row for Contoso Cloud Ltd in rules/SUPPLIERS.md

RULES. docs/COMMS.md is the only copy of the rules for these lines: read it
before writing and do not work from memory. It covers references at the end of
the line, the invoice shape, not attempted vs blocked, one line per message
sent, from users (the only place the admin learns what the user tier said),
counts only for labels and ledger lines, dropping empty sections except
queried, the bill payments section, the 15-bill cut and the closing line when
bills were created.

Check the API response has "ok":true. If false, log the error field and retry
once.

Read-and-notify only. Do not touch Xero, do not create or modify any bill, and
change no file other than by appending to logs.
EOF

set +e
claude -p "$PROMPT" \
    --model "${AGENT_MODEL:-opus}" \
    --permission-mode bypassPermissions \
    --output-format text \
    >> "$LOG" 2>&1
rc=$?
set -e

echo "$(date -u +%FT%TZ) [notify] finished rc=${rc}" >> "$LOG"
exit "$rc"
