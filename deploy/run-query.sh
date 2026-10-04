#!/usr/bin/env bash
# Run one READ-ONLY headless Claude session and post its answer to Slack.
#
#   ./deploy/run-query.sh <job> <slack-user-id> <channel> <thread-ts> "<prompt>"
#   ./deploy/run-query.sh query-UXXXXXXXXXX UXXXXXXXXXX DXXXXXXXXXX 1757900000.000100 "..."
#
# The `query` command in scripts/slack_agent.py lands here. It is run-agent.sh
# with the writes taken out and the reply put in:
#
#   - AGENT_READONLY=1 is exported, which makes XeroClient refuse every
#     non-GET (src/accounting_agent/readonly.py), and arms the PreToolUse
#     guard .claude/hooks/guard-readonly.py against every other write.
#   - the CLI starts with the Write/Edit tools disallowed outright.
#   - the session's final message is posted into the asker's thread by
#     scripts/slack_query.py, so the session never addresses anyone itself.
#     Files it wants to send go the same way (`slack_query.py upload`).
#   - no write-back commit, no ledger touch. A dirty tree after the session is
#     logged as a violation, never committed.
#
# One query per job at a time (flock), own log under data/logs/<job>-<date>.log,
# scratch under data/query/<job>-<stamp>/ for extracts the session builds.
set -euo pipefail

HOME_DIR="${AGENT_HOME:-$(cd "$(dirname "$0")/.." && pwd)}"
cd "$HOME_DIR"
export PATH="$HOME/.local/bin:$PATH"

if [ $# -lt 5 ]; then
    echo "usage: $0 <job> <slack-user-id> <channel> <thread-ts> \"<prompt>\"" >&2
    exit 2
fi

JOB="$1"; USER_ID="$2"; CHANNEL="$3"; THREAD_TS="$4"; shift 4
PROMPT="$*"
MODEL="${AGENT_QUERY_MODEL:-${AGENT_MODEL:-opus}}"
STAMP=$(date -u +%Y%m%dT%H%M%SZ)

mkdir -p data/logs data/query
LOG="data/logs/${JOB}-$(date -u +%Y-%m-%d).log"
QUERY_DIR="data/query/${JOB}-${STAMP}"
mkdir -p "$QUERY_DIR"

exec 9> "data/${JOB}.lock"
if ! flock -n 9; then
    echo "$(date -u +%FT%TZ) [$JOB] previous query still holds the lock, skipping" >> "$LOG"
    exit 0
fi

set -a
# shellcheck disable=SC1091
. ./.env
set +a

# Everything below this line is read-only. The exports are what the
# transport lock, the guard hook and the reply helper key on.
export AGENT_READONLY=1
export QUERY_JOB="$JOB" QUERY_USER="$USER_ID" QUERY_CHANNEL="$CHANNEL" \
       QUERY_THREAD_TS="$THREAD_TS" QUERY_DIR="$QUERY_DIR"

reply() {  # $1 = file with the text
    ./.venv/bin/python scripts/slack_query.py reply "$1" >> "$LOG" 2>&1 \
        || echo "$(date -u +%FT%TZ) [$JOB] reply to Slack failed" >> "$LOG"
}

if [ -z "${ANTHROPIC_API_KEY:-}" ] && [ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
    echo "$(date -u +%FT%TZ) [$JOB] no Claude credential in .env. Aborting." >> "$LOG"
    printf 'the query could not start: no Claude credential on the server.\n' > "$QUERY_DIR/answer.md"
    reply "$QUERY_DIR/answer.md"
    exit 1
fi

DENIALS="data/logs/readonly-denials.log"
denials_before=$( { grep -c "\[$JOB\]" "$DENIALS" 2>/dev/null || true; } | tail -1 )
denials_before=${denials_before:-0}
tree_before=$(git status --porcelain --untracked-files=no 2>/dev/null || true)

{
    echo
    echo "==================================================================="
    echo "$(date -u +%FT%TZ) [$JOB] start (read-only query for $USER_ID)"
    echo "model: $MODEL  scratch: $QUERY_DIR"
    echo "prompt: $PROMPT"
    echo "==================================================================="
} >> "$LOG"

set +e
claude -p "$PROMPT" \
    --model "$MODEL" \
    --permission-mode bypassPermissions \
    --output-format text \
    --disallowedTools Write Edit NotebookEdit \
    > "$QUERY_DIR/answer.md" 2>> "$LOG"
rc=$?
set -e

echo "$(date -u +%FT%TZ) [$JOB] session finished rc=$rc" >> "$LOG"
{
    echo "--- answer ---"
    cat "$QUERY_DIR/answer.md"
    echo "--- end answer ---"
} >> "$LOG"

if [ "$rc" -ne 0 ] || [ ! -s "$QUERY_DIR/answer.md" ]; then
    printf 'the query failed on my side. log on the server: %s\n' "$LOG" > "$QUERY_DIR/answer.md"
    rc=1
fi
reply "$QUERY_DIR/answer.md"

# Independent backstops. Neither can undo anything; both leave a trail.
denials_after=$( { grep -c "\[$JOB\]" "$DENIALS" 2>/dev/null || true; } | tail -1 )
denials_after=${denials_after:-0}
if [ "$denials_after" -gt "$denials_before" ]; then
    echo "$(date -u +%FT%TZ) [$JOB] read-only guard denied $((denials_after - denials_before)) call(s) this session" >> "$LOG"
fi
tree_after=$(git status --porcelain --untracked-files=no 2>/dev/null || true)
if [ "$tree_after" != "$tree_before" ]; then
    {
        echo "$(date -u +%FT%TZ) [$JOB] READ-ONLY VIOLATION: tracked files changed during a query session (not committed):"
        diff <(printf '%s\n' "$tree_before") <(printf '%s\n' "$tree_after") || true
    } >> "$LOG"
    rc=1
fi

exit "$rc"
