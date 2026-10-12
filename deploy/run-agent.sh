#!/usr/bin/env bash
# Run one headless Claude Code session against this repo.
#
#   ./deploy/run-agent.sh <job-name> "<prompt>"
#   ./deploy/run-agent.sh bookkeep "run the daily invoice pull"
#
# Everything cron triggers goes through here so that locking, logging, env
# loading and the permission mode are decided in exactly one place.
#
# PERMISSIONS: unattended runs use --permission-mode bypassPermissions, because
# there is no human to answer a prompt. The safety net is NOT the permission
# system, it is the PreToolUse hooks in .claude/settings.json, which still fire
# in this mode and still hard-deny every payment write. Verify that with
# ./deploy/preflight.sh after any Claude Code upgrade: if the guard ever stops
# firing, unattended runs must stop until it is fixed.
set -euo pipefail

# The repo root: AGENT_HOME if set, else the directory above this script.
HOME_DIR="${AGENT_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$HOME_DIR"

# cron's PATH is /usr/bin:/bin. The claude CLI lives in ~/.local/bin (native
# installer), ~/.npm-global/bin (npm with a user prefix) or /usr/local/bin.
export PATH="$HOME/.local/bin:$HOME/.npm-global/bin:/usr/local/bin:$PATH"

# Marks this as a real agent run: the UserPromptSubmit hook in
# .claude/settings.json injects the bookkeeping checklist only when it is set,
# so an ordinary development session that mentions an invoice is left alone.
export AGENT_RUN=1

if [ $# -lt 2 ]; then
    echo "usage: $0 <job-name> \"<prompt>\"" >&2
    exit 2
fi

JOB="$1"; shift
PROMPT="$*"

mkdir -p data/logs
LOG="data/logs/${JOB}-$(date -u +%Y-%m-%d).log"

# One run per job at a time. A second cron fire while the first is still
# working would double-post bills, so skip rather than queue.
exec 9> "data/${JOB}.lock"
if ! flock -n 9; then
    echo "$(date -u +%FT%TZ) [$JOB] previous run still holds the lock, skipping" >> "$LOG"
    exit 0
fi

# .env carries ANTHROPIC_API_KEY, the Xero client credentials, SLACK_BOT_TOKEN
# and the bank API settings. Cron gives us almost no environment, so this is
# not optional.
set -a
# shellcheck disable=SC1091
. ./.env
set +a

# Headless runs need a credential in the environment; there is no keychain and
# no interactive login on the server. Either works:
#   ANTHROPIC_API_KEY        - console.anthropic.com key, billed as API usage
#   CLAUDE_CODE_OAUTH_TOKEN  - from `claude setup-token`, uses the subscription
if [ -z "${ANTHROPIC_API_KEY:-}" ] && [ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
    echo "$(date -u +%FT%TZ) [$JOB] no Claude credential: set ANTHROPIC_API_KEY or" \
         "CLAUDE_CODE_OAUTH_TOKEN in .env (see deploy/README.md). Aborting." >> "$LOG"
    exit 1
fi

# The model: AGENT_MODEL, else [models] default in config/group.toml, else opus.
MODEL="${AGENT_MODEL:-$(PYTHONPATH=src ./.venv/bin/python -m accounting_agent.config model default 2>/dev/null || true)}"
MODEL="${MODEL:-opus}"

# --- effort, turn cap, prompt cache ----------------------------------------
# Read after .env, so .env can set any of them for every job.
#
# EFFORT: medium for every job. It also reaches the session's subagents.
# AGENT_EFFORT overrides every job; use low for a job that only runs a script
# and forwards its output.
#
# TURN CAP: a guard against a session that overruns, never a limit on a normal
# run. Measure the largest run of each job on your server (num_turns in
# data/runs/*.session.json) and set its cap to at least 2.5x that. The
# defaults below are deliberately roomy until you have measured: 500 for the
# jobs that bookkeep, review or reconcile, 300 for the short ones.
# AGENT_MAX_TURNS overrides every job.
case "$JOB" in
    ap-check|outstanding)  default_effort=medium; default_turns=300 ;;
    *)                     default_effort=medium; default_turns=500 ;;
esac
EFFORT="${AGENT_EFFORT:-$default_effort}"
MAX_TURNS="${AGENT_MAX_TURNS:-$default_turns}"

# PROMPT CACHE: one hour for the main session whatever the credential. The
# subscription token already gets an hour; an API key defaults to five
# minutes, and a run sits idle longer than that while a long script or a Xero
# pull runs. Past the TTL the next call writes the whole context again, at a
# premium over a cache read. Subagents keep the default five minutes.
#
# The bookkeeping job is the exception, set by [briefing] session_cache_ttl
# (five minutes by default): its briefing (scripts/bookkeep_brief.py) does
# the long scripts before the session starts, so its waits are short, and a
# five-minute cache write costs less than an hour's on every write. The
# investigations it opens set the hour back for themselves.
default_ttl=1h
if [ "$JOB" = "bookkeep" ]; then
    default_ttl="$(PYTHONPATH=src ./.venv/bin/python -c 'from accounting_agent import config; print(config.briefing()["session_cache_ttl"])' 2>/dev/null || echo 1h)"
fi
export CLAUDE_CODE_PROMPT_CACHE_TTL="${CLAUDE_CODE_PROMPT_CACHE_TTL:-$default_ttl}"

# --- Xero daily rate limit -------------------------------------------------
# 5000 calls a day per tenant. A run that meets that cap cannot finish, and the
# client's own 429 retry sleeps at most 65 seconds, so waiting inside the
# session is not an option. Instead the job defers itself: it writes the epoch
# second the cap resets into data/retry/<job>.retry alongside its own prompt,
# and deploy/retry-sweep.sh re-runs it the moment that second passes. The minute
# limit is not deferred: the client already waits that one out.
mkdir -p data/retry
RETRY_MARKER="data/retry/${JOB}.retry"

defer_for_rate_limit() {
    local at
    at="$(./.venv/bin/python scripts/xero_rate_limit.py --retry-at 2>>"$LOG")" || true
    if [ -z "$at" ]; then
        # Capped but no reset time readable: come back in an hour rather than
        # dropping the run.
        at=$(( $(date -u +%s) + 3600 ))
    fi
    # One line per marker: tabs and newlines in the prompt are flattened to
    # spaces so the sweep can read it back with cut.
    printf '%s\t%s\n' "$at" "$(printf '%s' "$PROMPT" | tr '\t\n' '  ')" > "$RETRY_MARKER"
    echo "$(date -u +%FT%TZ) [$JOB] Xero daily rate limit reached, deferred to" \
         "$(date -u -d "@$at" +%FT%TZ 2>/dev/null || echo "epoch $at")" >> "$LOG"
}

# Before spending a session: if Xero is already capped, defer without starting.
set +e
./.venv/bin/python scripts/xero_rate_limit.py --quiet >> "$LOG" 2>&1
limit_rc=$?
set -e
if [ "$limit_rc" -eq 3 ]; then
    defer_for_rate_limit
    exit 0
fi

# The run's cost goes in brackets after its Slack headlines (scripts/run_cost.py):
# the id tags every channel post the session queues (scripts/slack_outbox.py)
# and any extra cost file it drops under data/runs/<id>/extra/; the JSON output
# carries the session's own cost.
RUN_ID="$(./.venv/bin/python scripts/run_cost.py begin --job "$JOB")"
export AGENT_RUN_ID="$RUN_ID"
SESSION_JSON="data/runs/${RUN_ID}.session.json"

{
    echo
    echo "==================================================================="
    echo "$(date -u +%FT%TZ) [$JOB] start"
    echo "model: $MODEL  effort: $EFFORT  turn cap: $MAX_TURNS"
    echo "prompt: $PROMPT"
    echo "==================================================================="
} >> "$LOG"

set +e
claude -p "$PROMPT" \
    --model "$MODEL" \
    --effort "$EFFORT" \
    --max-turns "$MAX_TURNS" \
    --permission-mode bypassPermissions \
    --output-format json \
    > "$SESSION_JSON" 2>> "$LOG"
rc=$?
set -e

# The session's final message, as the text output used to put it in the log.
./.venv/bin/python -c 'import json,sys
try: print((json.load(open(sys.argv[1])).get("result") or "").strip())
except Exception: print(open(sys.argv[1]).read())' "$SESSION_JSON" >> "$LOG" 2>&1 || true

echo "$(date -u +%FT%TZ) [$JOB] finished rc=$rc" >> "$LOG"

# --- turn cap --------------------------------------------------------------
# A session that meets --max-turns ends with subtype error_max_turns (and
# terminal_reason max_turns, is_error true, no result, rc 1); its cost is still
# in the JSON, so the stamp below posts whatever it queued, as usual. The run
# did not finish, so its report gets one blocked line saying so, the register
# gets the same line, and the next run of the job that finishes inside its cap
# closes it.
TURN_CAP_MARK="data/runs/turn-cap-${JOB}.open"
if ./.venv/bin/python -c 'import json,sys
try: d=json.load(open(sys.argv[1]))
except Exception: sys.exit(1)
sys.exit(0 if d.get("subtype")=="error_max_turns" or d.get("terminal_reason")=="max_turns" else 1)' \
        "$SESSION_JSON" 2>/dev/null; then
    echo "$(date -u +%FT%TZ) [$JOB] STOPPED AT THE TURN CAP: the session used its $MAX_TURNS turns and did not finish. Whatever it had not reached did not run." >> "$LOG"
    CAP_LINE="the session stopped at its turn cap before it finished, so whatever it had not reached did not run. read the log, then start the rest from Slack, or raise AGENT_MAX_TURNS if the work really needs more · ${MAX_TURNS} turns · ${LOG}"
    ./.venv/bin/python scripts/run_cost.py turn-cap --run-id "$RUN_ID" --job "$JOB" \
        --line "$CAP_LINE" >> "$LOG" 2>&1 \
        || echo "$(date -u +%FT%TZ) [$JOB] could not add the turn cap line to the report" >> "$LOG"
    ./.venv/bin/python scripts/outstanding.py add --domain platform --kind blocked \
        --with admin --key "turn-cap-${JOB}" \
        --text "the ${JOB} session stopped at its turn cap before it finished; read the log and start the rest from Slack" \
        --refs "${MAX_TURNS} turns · ${LOG}" >> "$LOG" 2>&1 \
        && touch "$TURN_CAP_MARK" \
        || echo "$(date -u +%FT%TZ) [$JOB] could not register the turn cap line" >> "$LOG"
elif [ "$rc" -eq 0 ] && [ -f "$TURN_CAP_MARK" ]; then
    ./.venv/bin/python scripts/outstanding.py close --key "turn-cap-${JOB}" \
        --reason "the next ${JOB} run finished inside its turn cap" >> "$LOG" 2>&1 \
        && rm -f "$TURN_CAP_MARK" || true
fi

# Now the cost is known: post the queued channel posts with it in brackets.
./.venv/bin/python scripts/run_cost.py stamp --run-id "$RUN_ID" \
    --session-json "$SESSION_JSON" >> "$LOG" 2>&1 \
    || echo "$(date -u +%FT%TZ) [$JOB] cost stamp failed" >> "$LOG"

# The session may have met the cap part-way through. Probe again: if Xero is
# capped now, the run is incomplete whatever it reported, so queue a re-run.
if [ "$rc" -ne 0 ] || grep -qiE 'rate.?limit|429' "$LOG"; then
    set +e
    ./.venv/bin/python scripts/xero_rate_limit.py --quiet >> "$LOG" 2>&1
    limit_rc=$?
    set -e
    if [ "$limit_rc" -eq 3 ]; then
        defer_for_rate_limit
    fi
fi

# Commit anything the run wrote back into rules/, docs/ or .claude/, so a rule
# an admin sent over Slack becomes a real commit instead of an uncommitted edit
# a later sync could lose. Never pushed from here; the laptop collects it.
./deploy/commit-writeback.sh "$JOB" >> "$LOG" 2>&1 ||
    echo "$(date -u +%FT%TZ) [$JOB] write-back commit failed, changes still in the tree" >> "$LOG"

# Independent backstop: whatever the session believed it did, confirm it left
# no phantom payment behind. Read-only.
if ./.venv/bin/python scripts/check_no_phantom_payments.py >> "$LOG" 2>&1; then
    echo "$(date -u +%FT%TZ) [$JOB] phantom-payment check PASS" >> "$LOG"
else
    echo "$(date -u +%FT%TZ) [$JOB] PHANTOM-PAYMENT CHECK FAILED: investigate" >> "$LOG"
    rc=1
fi

# The second exit check of a bookkeeping run: the register keeps only the
# queries the run could not answer (scripts/outstanding.py check-queries).
# Read-only; logged, never fails the job.
if [ "$JOB" = bookkeep ]; then
    if ./.venv/bin/python scripts/outstanding.py check-queries >> "$LOG" 2>&1; then
        echo "$(date -u +%FT%TZ) [$JOB] register query check PASS" >> "$LOG"
    else
        echo "$(date -u +%FT%TZ) [$JOB] REGISTER QUERY CHECK FAILED: a solved or uninvestigated query is in the register" >> "$LOG"
    fi
fi

exit "$rc"
