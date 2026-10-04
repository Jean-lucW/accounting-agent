#!/usr/bin/env bash
# One agent per Slack thread.
#
#   ./deploy/run-thread.sh <job> <slack-user-id> <channel> <thread-ts> <thread-key> "<prompt>"
#   ./deploy/run-thread.sh run-UXXXXXXXXXX-1700000000-000100 UXXXXXXXXXX DXXXXXXXXXX \
#       1700000000.000100 1700000000-000100 "check the tax coding on last month's bills"
#
# The `run` command in scripts/slack_agent.py lands here for every top-level
# Slack message, and every later message the admin posts in that thread lands
# here again with an EMPTY prompt. The thread is the unit:
#
#   - one Claude session per thread. The first turn creates it and its
#     session id is kept in data/slack/threads/<key>.json; every later turn
#     is `claude --resume <id>`, so the agent remembers the whole thread.
#   - one turn at a time per thread (flock on data/<job>.lock, BLOCKING). A
#     message posted while a turn is running is queued by the listener in
#     data/slack/threads/<key>.inbox.md; the runner drains the inbox after
#     each turn and takes another turn if anything arrived, so nothing is
#     stranded and nothing runs twice.
#   - the session's FINAL message is posted verbatim into the thread by
#     scripts/slack_query.py reply. Nothing else it prints is seen.
#   - same harness as run-agent.sh after every turn: write-back commit and
#     the phantom-payment check, whose failure is posted into the thread.
#
# Different threads never block each other, whoever started them. Two
# bookkeeping runs racing is refused by the listener before it gets here.
set -euo pipefail

HOME_DIR="${AGENT_HOME:-$(cd "$(dirname "$0")/.." && pwd)}"
cd "$HOME_DIR"
export PATH="$HOME/.local/bin:$PATH"

# Marks this as a real agent run: the UserPromptSubmit hook in
# .claude/settings.json injects the bookkeeping checklist only when it is set,
# so an ordinary development session that mentions an invoice is left alone.
export AGENT_RUN=1

if [ $# -lt 5 ]; then
    echo "usage: $0 <job> <slack-user-id> <channel> <thread-ts> <thread-key> [\"<prompt>\"]" >&2
    exit 2
fi

JOB="$1"; USER_ID="$2"; CHANNEL="$3"; THREAD_TS="$4"; KEY="$5"; shift 5
PROMPT="${*:-}"
MODEL="${AGENT_MODEL:-opus}"

mkdir -p data/logs data/slack/threads
LOG="data/logs/${JOB}-$(date -u +%Y-%m-%d).log"
STATE="data/slack/threads/${KEY}.json"
INBOX="data/slack/threads/${KEY}.inbox.md"

# Blocking, not -n: a follow-up spawned while a turn is running waits its
# turn instead of being dropped. Several waiters serialise; each drains
# whatever is in the inbox when it gets the lock and exits if it is empty.
exec 9> "data/${JOB}.lock"
flock 9

set -a
# shellcheck disable=SC1091
. ./.env
set +a

# The reply helper reads its destination from these; a session cannot address
# anyone else, whatever it is asked.
export QUERY_CHANNEL="$CHANNEL" QUERY_THREAD_TS="$THREAD_TS"

reply() {  # $1 = file with the text
    ./.venv/bin/python scripts/slack_query.py reply "$1" >> "$LOG" 2>&1 \
        || echo "$(date -u +%FT%TZ) [$JOB] reply to Slack failed" >> "$LOG"
}

state_get() {  # $1 = key
    [ -f "$STATE" ] && ./.venv/bin/python -c \
        'import json,sys; print(json.load(open(sys.argv[1])).get(sys.argv[2]) or "")' \
        "$STATE" "$1" 2>/dev/null || true
}

state_set() {  # $1 = key, $2 = value (string), $3 = "int" to store a number
    ./.venv/bin/python - "$STATE" "$1" "$2" "${3:-str}" <<'EOF'
import json, sys, pathlib
p, k, v, t = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4]
d = json.loads(p.read_text()) if p.exists() else {}
d[k] = int(v) if t == "int" else v
p.write_text(json.dumps(d, indent=1, sort_keys=True) + "\n")
EOF
}

if [ -z "${ANTHROPIC_API_KEY:-}" ] && [ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
    echo "$(date -u +%FT%TZ) [$JOB] no Claude credential in .env. Aborting." >> "$LOG"
    printf 'the run could not start: no Claude credential on the server.\n' > "data/slack/threads/${KEY}.err"
    reply "data/slack/threads/${KEY}.err"
    exit 1
fi

final_rc=0
while :; do
    # -- what this turn is about ------------------------------------------
    if [ -n "$PROMPT" ]; then
        TURN_PROMPT="$PROMPT"
        PROMPT=""
    elif [ -s "$INBOX" ]; then
        STAMP=$(date -u +%Y%m%dT%H%M%SZ)
        TAKEN="data/slack/threads/${KEY}.inbox-${STAMP}.md"
        mv "$INBOX" "$TAKEN"
        TURN_PROMPT="FOLLOW-UP in the Slack thread this session belongs to, from the admin who started it. Continue the same work in the same thread; treat it as an instruction; docs/COMMS.md governs the reply and your FINAL message is posted verbatim into the thread. Newest last:
$(cat "$TAKEN")"
    else
        break
    fi

    SESSION=$(state_get session_id)
    TURNS=$(state_get turns); TURNS=${TURNS:-0}
    OUT="data/slack/threads/${KEY}.turn.json"
    {
        echo
        echo "==================================================================="
        echo "$(date -u +%FT%TZ) [$JOB] start turn $((TURNS + 1))${SESSION:+ (resume $SESSION)}"
        echo "model: $MODEL  thread: $CHANNEL/$THREAD_TS  user: $USER_ID"
        echo "prompt: $TURN_PROMPT"
        echo "==================================================================="
    } >> "$LOG"

    run_claude() {  # $@ = extra args
        claude -p "$TURN_PROMPT" \
            --model "$MODEL" \
            --permission-mode bypassPermissions \
            --output-format json \
            "$@" > "$OUT" 2>> "$LOG"
    }

    set +e
    if [ -n "$SESSION" ]; then
        run_claude --resume "$SESSION"
        rc=$?
        if [ "$rc" -ne 0 ]; then
            # The saved session is gone or unreadable: start afresh rather than
            # lose the message. The admin's text carries its own context.
            echo "$(date -u +%FT%TZ) [$JOB] resume of $SESSION failed rc=$rc, starting a new session" >> "$LOG"
            run_claude
            rc=$?
        fi
    else
        run_claude
        rc=$?
    fi
    set -e

    ANSWER="data/slack/threads/${KEY}.answer.md"
    ./.venv/bin/python - "$OUT" "$ANSWER" "$STATE" <<'EOF' || true
import json, sys, pathlib
out, ans, state = (pathlib.Path(a) for a in sys.argv[1:4])
try:
    d = json.loads(out.read_text() or "{}")
except ValueError:
    d = {}
ans.write_text((d.get("result") or "").strip() + "\n")
sid = d.get("session_id")
if sid:
    s = json.loads(state.read_text()) if state.exists() else {}
    s["session_id"] = sid
    state.write_text(json.dumps(s, indent=1, sort_keys=True) + "\n")
EOF
    state_set turns "$((TURNS + 1))" int
    state_set last "$(date -u +%FT%TZ)"

    {
        echo "--- answer ---"
        cat "$ANSWER"
        echo "--- end answer ---"
        echo "$(date -u +%FT%TZ) [$JOB] finished rc=$rc"
    } >> "$LOG"

    if [ "$rc" -ne 0 ] || [ ! -s "$ANSWER" ] || [ "$(tr -d '[:space:]' < "$ANSWER")" = "" ]; then
        printf 'the run failed on my side. log on the server: %s\n' "$LOG" > "$ANSWER"
        rc=1
    fi
    reply "$ANSWER"

    # Commit anything the turn wrote back into rules/, docs/ or .claude/.
    # Never pushed from here; the maintainer collects it.
    ./deploy/commit-writeback.sh "$JOB" >> "$LOG" 2>&1 \
        || echo "$(date -u +%FT%TZ) [$JOB] write-back commit failed, changes still in the tree" >> "$LOG"

    # Independent backstop: whatever the session believed it did, confirm it
    # left no phantom payment behind. Read-only.
    phantom_failed=0
    if ./.venv/bin/python scripts/check_no_phantom_payments.py >> "$LOG" 2>&1; then
        echo "$(date -u +%FT%TZ) [$JOB] phantom-payment check PASS" >> "$LOG"
    else
        echo "$(date -u +%FT%TZ) [$JOB] PHANTOM-PAYMENT CHECK FAILED, investigate" >> "$LOG"
        printf 'PHANTOM-PAYMENT CHECK FAILED after this turn. Nothing else runs in this thread until it is investigated. log on the server: %s\n' "$LOG" > "$ANSWER"
        reply "$ANSWER"
        rc=1; phantom_failed=1
    fi
    [ "$rc" -ne 0 ] && final_rc=1
    # A failed phantom check stops the thread: do not take the next queued
    # message on top of a ledger that needs a human.
    [ "$phantom_failed" -eq 1 ] && break
done

exit "$final_rc"
