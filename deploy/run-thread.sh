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
#   - one Claude session per thread at a time. The first turn creates it and
#     its session id is kept in data/slack/threads/<key>.json; a later turn
#     is `claude --resume <id>`, so the agent remembers the whole thread,
#     until resuming would cost more than starting over: then
#     scripts/thread_session.py builds a handover note from the thread's own
#     record (<key>.history.jsonl, <key>.brief.json) and the turn starts a new
#     session from it, whose id the state keeps for the turns after.
#   - one turn at a time per thread (flock on data/<job>.lock, BLOCKING). A
#     message posted while a turn is running is queued by the listener in
#     data/slack/threads/<key>.inbox.md; the runner drains the inbox after
#     each turn and takes another turn if anything arrived, so nothing is
#     stranded and nothing runs twice.
#   - the session's FINAL message is posted verbatim into the thread by
#     scripts/slack_query.py reply, with the turn's cost in brackets on its
#     first line (scripts/run_cost.py). Nothing else it prints is seen.
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

# The model: AGENT_MODEL, else [models] default in config/group.toml, else opus.
MODEL="${AGENT_MODEL:-$(PYTHONPATH=src ./.venv/bin/python -m accounting_agent.config model default 2>/dev/null || true)}"
MODEL="${MODEL:-opus}"

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

# --- effort, turn cap, prompt cache ----------------------------------------
# Read after .env, so .env can set them.
#
# EFFORT: medium. A thread turn is anything an admin asks, a full bookkeeping
# run included, so nothing here is simple enough for low. It also reaches the
# session's subagents. AGENT_THREAD_EFFORT, then AGENT_EFFORT, override it.
#
# TURN CAP, per turn (--max-turns counts each `claude -p` call, a resumed one
# included, not the thread's total): a guard against a turn that overruns,
# never a limit on a normal one. Measure the largest turn you see (num_turns in
# data/slack/threads/<key>.turn.json, from a full bookkeeping run asked for in
# a thread) and set the cap to 2.5x that. AGENT_THREAD_MAX_TURNS overrides it.
# The thread-wide limit on how many turns a thread may take is a different
# thing, SLACK_AGENT_MAX_TURNS in scripts/slack_agent.py.
EFFORT="${AGENT_THREAD_EFFORT:-${AGENT_EFFORT:-medium}}"
MAX_TURNS="${AGENT_THREAD_MAX_TURNS:-500}"

# PROMPT CACHE: one hour for the main session whatever the credential. A turn
# that waits more than five minutes on a script would otherwise write its whole
# context again, at a premium over a cache read. Subagents keep five minutes.
export CLAUDE_CODE_PROMPT_CACHE_TTL="${CLAUDE_CODE_PROMPT_CACHE_TTL:-1h}"

# A turn that met the cap ends with subtype error_max_turns (rc 1, no result).
turn_cap_hit() {  # $1 = the turn's JSON
    ./.venv/bin/python -c 'import json,sys
try: d=json.load(open(sys.argv[1]))
except Exception: sys.exit(1)
sys.exit(0 if d.get("subtype")=="error_max_turns" or d.get("terminal_reason")=="max_turns" else 1)' \
        "$1" 2>/dev/null
}
TURN_CAP_MARK="data/slack/threads/${KEY}.turn-cap"

# --- resume, or a new session from the thread's record --------------------
# scripts/thread_session.py decides before each turn (its docstring has the
# thresholds):
#   resume   context under 300k tokens, and under 100k or the prompt cache
#            still warm: `claude --resume <id>`.
#   rotate   past either line: a new session whose prompt is a handover note
#            built in Python from the thread itself (the rules the first turn
#            was given, the original request, each turn's message and reply,
#            the latest whole, the earlier transcript's path for grep) and the
#            new message whole. Later turns resume the new session.
#   fresh    the thread's first turn: the listener's prompt as it is.
# A resumed turn sends only the admin's words and one line: the rules are in
# the session's history already. AGENT_THREAD_RESUMED tells the
# UserPromptSubmit bookkeeping reminder (.claude/settings.json) that it has
# already fired in this session; a first or rotated turn gets it as before.
SESSIONS=scripts/thread_session.py
HANDOVER="data/slack/threads/${KEY}.handover.md"
ERR="data/slack/threads/${KEY}.turn.err"

build_handover() {  # $1 = reason; sets TURN_PROMPT, returns 1 if it could not
    ./.venv/bin/python "$SESSIONS" handover --state "$STATE" --message-file "$MSG_FILE" \
        --reason "$1" --out "$HANDOVER" >> "$LOG" 2>&1 && [ -s "$HANDOVER" ] || return 1
    TURN_PROMPT="$(cat "$HANDOVER")"
}

final_rc=0
while :; do
    # -- what this turn is about ------------------------------------------
    FIRST=0
    if [ -n "$PROMPT" ]; then
        TURN_PROMPT="$PROMPT"
        PROMPT=""
        FIRST=1
        # The first prompt whole, for a handover when the listener left no
        # brief (scripts/slack_agent.py writes <key>.brief.json).
        MSG_FILE="data/slack/threads/${KEY}.prompt.md"
        printf '%s\n' "$TURN_PROMPT" > "$MSG_FILE"
    elif [ -s "$INBOX" ]; then
        STAMP=$(date -u +%Y%m%dT%H%M%SZ)
        TAKEN="data/slack/threads/${KEY}.inbox-${STAMP}.md"
        mv "$INBOX" "$TAKEN"
        MSG_FILE="$TAKEN"
        TURN_PROMPT="FOLLOW-UP in this thread from the admin who started it: an instruction for the same work, under the same rules. docs/COMMS.md governs the reply; your FINAL message is posted verbatim into the thread. Newest last:
$(cat "$TAKEN")"
    else
        break
    fi

    SESSION=$(state_get session_id)
    TURNS=$(state_get turns); TURNS=${TURNS:-0}
    OUT="data/slack/threads/${KEY}.turn.json"

    FOLLOWUP_FLAG=""
    [ "$FIRST" -eq 0 ] && FOLLOWUP_FLAG="--followup"
    # "<resume|rotate|fresh> <reason> <context tokens> <idle seconds>"
    PLAN=$(./.venv/bin/python "$SESSIONS" plan --state "$STATE" $FOLLOWUP_FLAG 2>> "$LOG") \
        || PLAN="resume - - -"
    read -r ACTION REASON PLAN_CTX PLAN_IDLE <<< "$PLAN"
    MODE=fresh
    [ -n "$SESSION" ] && MODE=resume
    if [ "$ACTION" = rotate ]; then
        if build_handover "$REASON"; then
            MODE=rotate
        else
            echo "$(date -u +%FT%TZ) [$JOB] handover could not be built; turn runs as $MODE" >> "$LOG"
        fi
    fi

    # Each turn is a run for its cost (scripts/run_cost.py): the id tags the
    # channel posts it queues (scripts/slack_outbox.py) and any extra cost file
    # it drops under data/runs/<id>/extra/; the stamp below posts the queue
    # with the bracket and puts it on the reply.
    RUN_ID="$(./.venv/bin/python scripts/run_cost.py begin --job "$JOB")"
    export AGENT_RUN_ID="$RUN_ID"
    case "$MODE" in
        resume) HOW=" (resume $SESSION, context ${PLAN_CTX:--} tokens, idle ${PLAN_IDLE:--}s)" ;;
        rotate) HOW=" (new session, ${REASON}, context ${PLAN_CTX:--} tokens, idle ${PLAN_IDLE:--}s${SESSION:+, was $SESSION})" ;;
        *)      HOW="" ;;
    esac
    {
        echo
        echo "==================================================================="
        echo "$(date -u +%FT%TZ) [$JOB] start turn $((TURNS + 1))${HOW}"
        echo "model: $MODEL  effort: $EFFORT  turn cap: $MAX_TURNS  thread: $CHANNEL/$THREAD_TS  user: $USER_ID"
        echo "prompt: $TURN_PROMPT"
        echo "==================================================================="
    } >> "$LOG"

    run_claude() {  # $@ = extra args
        claude -p "$TURN_PROMPT" \
            --model "$MODEL" \
            --effort "$EFFORT" \
            --max-turns "$MAX_TURNS" \
            --permission-mode bypassPermissions \
            --output-format json \
            "$@" > "$OUT" 2> "$ERR"
        local r=$?
        cat "$ERR" >> "$LOG" 2>/dev/null || true
        return "$r"
    }

    RESUME_FAILED=0
    set +e
    if [ "$MODE" = resume ]; then
        export AGENT_THREAD_RESUMED="$SESSION"
        run_claude --resume "$SESSION"
        rc=$?
        unset AGENT_THREAD_RESUMED
        # A resumed turn that met the turn cap did run: starting a fresh
        # session on top of it would repeat its work and write a new cache.
        if [ "$rc" -ne 0 ] && ! turn_cap_hit "$OUT"; then
            if ! ./.venv/bin/python "$SESSIONS" ran --turn-json "$OUT" --err "$ERR"; then
                # Refused as unknown, or never took a turn: nothing ran, so a
                # new session repeats nothing. It gets the thread's record.
                echo "$(date -u +%FT%TZ) [$JOB] resume of $SESSION never started rc=$rc, continuing in a new session" >> "$LOG"
                REASON="resume-refused"
                if build_handover "$REASON"; then MODE=rotate; else MODE=fresh; fi
                run_claude
                rc=$?
            else
                # It ran and stopped part way. Starting again would repeat
                # what it did; the session stays for `continue`.
                RESUME_FAILED=1
                echo "$(date -u +%FT%TZ) [$JOB] resumed turn failed part way rc=$rc, session $SESSION kept" >> "$LOG"
            fi
        fi
    else
        unset AGENT_THREAD_RESUMED
        run_claude
        rc=$?
    fi
    set -e

    ANSWER="data/slack/threads/${KEY}.answer.md"
    ./.venv/bin/python "$SESSIONS" finish --state "$STATE" --turn-json "$OUT" \
        --answer "$ANSWER" --mode "$MODE" >> "$LOG" 2>&1 \
        || echo "$(date -u +%FT%TZ) [$JOB] thread state not updated after the turn" >> "$LOG"
    state_set turns "$((TURNS + 1))" int
    state_set last "$(date -u +%FT%TZ)"

    {
        echo "--- answer ---"
        cat "$ANSWER"
        echo "--- end answer ---"
        echo "$(date -u +%FT%TZ) [$JOB] finished rc=$rc"
    } >> "$LOG"

    if turn_cap_hit "$OUT"; then
        # The session id is kept above, so `continue` resumes where it stopped.
        echo "$(date -u +%FT%TZ) [$JOB] STOPPED AT THE TURN CAP: the turn used its $MAX_TURNS turns and did not finish. Whatever it had not reached did not run." >> "$LOG"
        printf '*blocked*\n• the session stopped at its turn cap before it finished, so whatever it had not reached did not run. reply continue in this thread to pick it up where it stopped · %s turns · %s\n' \
            "$MAX_TURNS" "$LOG" > "$ANSWER"
        ./.venv/bin/python scripts/outstanding.py add --domain platform --kind blocked \
            --with admin --key "turn-cap-${KEY}" \
            --text "a Slack run stopped at its turn cap before it finished; reply continue in its thread to pick it up" \
            --refs "${MAX_TURNS} turns · thread ${CHANNEL}/${THREAD_TS} · ${LOG}" >> "$LOG" 2>&1 \
            && touch "$TURN_CAP_MARK" \
            || echo "$(date -u +%FT%TZ) [$JOB] could not register the turn cap line" >> "$LOG"
        rc=1
    elif [ "$RESUME_FAILED" -eq 1 ]; then
        printf '*blocked*\n• this turn stopped part way on my side, so whatever it had not reached did not run. reply continue in this thread to pick it up where it stopped · %s\n' \
            "$LOG" > "$ANSWER"
        rc=1
    elif [ "$rc" -ne 0 ] || [ ! -s "$ANSWER" ] || [ "$(tr -d '[:space:]' < "$ANSWER")" = "" ]; then
        printf 'the run failed on my side. log on the server: %s\n' "$LOG" > "$ANSWER"
        rc=1
    elif [ -f "$TURN_CAP_MARK" ]; then
        # A later turn in the same thread finished inside the cap.
        ./.venv/bin/python scripts/outstanding.py close --key "turn-cap-${KEY}" \
            --reason "a later turn in the thread finished inside its turn cap" >> "$LOG" 2>&1 \
            && rm -f "$TURN_CAP_MARK" || true
    fi
    # The thread's own record, what a later handover is built from: the
    # admin's words and the reply as posted, before the cost goes on it.
    LOG_FIRST=""
    [ "$FIRST" -eq 1 ] && LOG_FIRST="--first"
    ./.venv/bin/python "$SESSIONS" log-turn --state "$STATE" --turn "$((TURNS + 1))" \
        --message-file "$MSG_FILE" $LOG_FIRST --answer "$ANSWER" --turn-json "$OUT" \
        --run-id "$RUN_ID" --mode "$MODE" >> "$LOG" 2>&1 \
        || echo "$(date -u +%FT%TZ) [$JOB] thread record not written" >> "$LOG"
    # A resumed session reports its cumulative cost; the thread state holds
    # the last figure, so the bracket is this turn's alone.
    ./.venv/bin/python scripts/run_cost.py stamp --run-id "$RUN_ID" \
        --session-json "$OUT" --thread-state "$STATE" --reply "$ANSWER" >> "$LOG" 2>&1 \
        || echo "$(date -u +%FT%TZ) [$JOB] cost stamp failed" >> "$LOG"
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
