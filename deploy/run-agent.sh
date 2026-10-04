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
MODEL="${AGENT_MODEL:-opus}"

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

{
    echo
    echo "==================================================================="
    echo "$(date -u +%FT%TZ) [$JOB] start"
    echo "model: $MODEL"
    echo "prompt: $PROMPT"
    echo "==================================================================="
} >> "$LOG"

set +e
claude -p "$PROMPT" \
    --model "$MODEL" \
    --permission-mode bypassPermissions \
    --output-format text \
    >> "$LOG" 2>&1
rc=$?
set -e

echo "$(date -u +%FT%TZ) [$JOB] finished rc=$rc" >> "$LOG"

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

exit "$rc"
