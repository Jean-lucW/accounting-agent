#!/usr/bin/env bash
# Two Opus agents, one evidence pack, consensus before any write.
#
#   ./deploy/run-consensus.sh <job-name> "<task>"
#
# WHY. Two agents given the same context make the same mistake and agree with
# each other, which buys confidence and not accuracy. What does buy accuracy is
# a second INDEPENDENT reading of evidence that was gathered once. So the split
# here is not "two agents do the whole job twice": it is gather once, reason
# twice, reconcile, then act.
#
# THE SHAPE:
#
#   1 GATHER    agent A reads the sources (invoices, Xero, the bank) and
#               writes an evidence pack. It also records its own conclusion.
#               READ-ONLY. It actions nothing.
#   2 SECOND    agent B is handed the pack and reasons independently. When it
#               needs a fact it checks the pack FIRST and only fetches what is
#               genuinely missing: that is what stops both agents re-reading
#               forty invoices. READ-ONLY.
#   3 CONSENSUS a third pass compares the two conclusions item by item.
#               Agreed items are actioned. Disagreements are resolved from the
#               evidence where the evidence settles it, and escalated to the
#               admin by DM where it does not. This is the ONLY pass that may
#               write to Xero.
#
# COST. Roughly 2.5x a single run. Worth it for a batch with judgement in it:
# entity allocation, VAT or sales tax treatment, anything capitalised. Not
# worth it for a routine intake of unambiguous invoices; use run-agent.sh for
# those.
#
# The payment guard, the rules in rules/ and docs/COMMS.md apply to all three
# passes exactly as they do to a single run.
set -euo pipefail

HOME_DIR="${AGENT_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$HOME_DIR"
export PATH="$HOME/.local/bin:$HOME/.npm-global/bin:/usr/local/bin:$PATH"

# Marks this as a real agent run: the UserPromptSubmit hook in
# .claude/settings.json injects the bookkeeping checklist only when it is set,
# so an ordinary development session that mentions an invoice is left alone.
export AGENT_RUN=1

if [ $# -lt 2 ]; then
    echo "usage: $0 <job-name> \"<task>\"" >&2
    exit 2
fi

JOB="$1"; shift
TASK="$*"
STAMP=$(date -u +%Y-%m-%dT%H%M%SZ)
PACK="data/consensus/${JOB}-${STAMP}"
LOG="data/logs/${JOB}-$(date -u +%Y-%m-%d).log"

mkdir -p "$PACK" data/logs

exec 9> "data/${JOB}.lock"
if ! flock -n 9; then
    echo "$(date -u +%FT%TZ) [$JOB] previous run still holds the lock, skipping" >> "$LOG"
    exit 0
fi

set -a
# shellcheck disable=SC1091
. ./.env
set +a

if [ -z "${ANTHROPIC_API_KEY:-}" ] && [ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
    echo "$(date -u +%FT%TZ) [$JOB] no Claude credential, aborting" >> "$LOG"
    exit 1
fi

# The model: AGENT_MODEL, else [models] default in config/group.toml, else opus.
MODEL="${AGENT_MODEL:-$(PYTHONPATH=src ./.venv/bin/python -m accounting_agent.config model default 2>/dev/null || true)}"
MODEL="${MODEL:-opus}"

# EFFORT, TURN CAP and PROMPT CACHE, as in deploy/run-agent.sh. The cap applies
# to each of the three passes. Measure your largest pass (num_turns in
# data/consensus/<job>-<stamp>/session-*.json) and set the cap to 2.5x that.
# AGENT_CONSENSUS_EFFORT, then AGENT_EFFORT; AGENT_CONSENSUS_MAX_TURNS, then
# AGENT_MAX_TURNS.
EFFORT="${AGENT_CONSENSUS_EFFORT:-${AGENT_EFFORT:-medium}}"
MAX_TURNS="${AGENT_CONSENSUS_MAX_TURNS:-${AGENT_MAX_TURNS:-500}}"
export CLAUDE_CODE_PROMPT_CACHE_TTL="${CLAUDE_CODE_PROMPT_CACHE_TTL:-1h}"

# The three passes are one run for its cost (scripts/run_cost.py): summed, and
# any channel post a pass queued goes out with it after the third.
RUN_ID="$(./.venv/bin/python scripts/run_cost.py begin --job "$JOB")"
export AGENT_RUN_ID="$RUN_ID"

run_phase() {   # $1 = phase name, $2 = prompt
    echo "$(date -u +%FT%TZ) [$JOB] phase $1 start (model $MODEL, effort $EFFORT, turn cap $MAX_TURNS)" >> "$LOG"
    set +e
    claude -p "$2" --model "$MODEL" --effort "$EFFORT" --max-turns "$MAX_TURNS" \
        --permission-mode bypassPermissions \
        --output-format json > "${PACK}/session-$1.json" 2>> "$LOG"
    local rc=$?
    set -e
    ./.venv/bin/python -c 'import json,sys
try: print((json.load(open(sys.argv[1])).get("result") or "").strip())
except Exception: print(open(sys.argv[1]).read())' "${PACK}/session-$1.json" >> "$LOG" 2>&1 || true
    if grep -q '"subtype": *"error_max_turns"' "${PACK}/session-$1.json" 2>/dev/null; then
        echo "$(date -u +%FT%TZ) [$JOB] phase $1 STOPPED AT THE TURN CAP ($MAX_TURNS): it did not finish" >> "$LOG"
    fi
    echo "$(date -u +%FT%TZ) [$JOB] phase $1 rc=$rc" >> "$LOG"
    return $rc
}

COMMON="
docs/COMMS.md is a HARD RULE for every message you send to anyone. References
at the END of a line, never mid-sentence. Bold lowercase section headings with
bullets under them.

NEVER create a payment, allocate one, or mark any bill paid or reconciled.
The bank is the source of truth on cash (docs/BANKING.md), so read the
statement before concluding anything about what was paid, by whom, or twice.

Every company-specific judgement (which entity recognises a cost, coding,
allocation, suppliers, payroll, intercompany routing) comes from rules/; the
group's structure and account codes come from config/group.toml.
"

# ---------------------------------------------------------------- 1. GATHER
run_phase gather "
TASK: ${TASK}

You are AGENT A of two. This pass is GATHER and it is STRICTLY READ-ONLY. You
create nothing, edit nothing, void nothing, send no Slack message. A second
agent reasons from what you write here, so the pack is the deliverable.

Write ${PACK}/evidence.json. One object per item in scope:

  { \"id\": \"<stable id, e.g. the invoice number>\",
    \"source\": \"<where you read it: slack ts, gmail id, xero id, bank row>\",
    \"facts\": { supplier, date, currency, gross, net, tax, description,
                 addressed_to, delivered_to, payment_evidence },
    \"xero\": { \"duplicate_check\": \"...\", \"existing\": \"...\" },
    \"bank\": \"<the matching statement line, or why there is none>\",
    \"unread\": [\"anything you could NOT read and why\"] }

Facts only. No coding decisions, no conclusions, no opinions in this file.
Quote figures exactly as the document states them.

Then write ${PACK}/conclusion-a.json, your own answer, separately:

  { \"<id>\": { \"action\": \"post|skip|query|block\",
                \"entity\": \"...\", \"account\": \"...\", \"tax\": \"...\",
                \"reasoning\": \"one sentence\",
                \"confidence\": \"high|medium|low\" } }

Be complete about what you could not determine. An honest gap is worth more to
agent B than a confident guess.
${COMMON}
" || true

# ---------------------------------------------------------------- 2. SECOND
run_phase second "
TASK: ${TASK}

You are AGENT B of two. Agent A has already read the sources. Its evidence is
at ${PACK}/evidence.json.

HOW TO USE THE PACK. Read it first, always. When you need a fact, look in the
pack BEFORE going to the source. Re-read a source ONLY when the pack does not
have the fact, or when the pack's own \"unread\" list says A could not read it,
or when a figure in the pack is internally inconsistent and you must check it.
Re-reading what A already read is the waste this design exists to remove.

Do NOT read ${PACK}/conclusion-a.json. Your value is that you did not see it.

Reason independently and write ${PACK}/conclusion-b.json, same schema as A's:
action, entity, account, tax, reasoning, confidence, per id. Where you had to
go back to a source, add \"refetched\": \"<why>\".

READ-ONLY. Action nothing. Send nothing.
${COMMON}
" || true

# ------------------------------------------------------------- 3. CONSENSUS
run_phase consensus "
TASK: ${TASK}

You are the CONSENSUS pass. Two agents reasoned independently from the same
evidence:
  ${PACK}/evidence.json
  ${PACK}/conclusion-a.json
  ${PACK}/conclusion-b.json

Compare them item by item and write ${PACK}/consensus.json with, per id, one
of: \"agreed\", \"resolved\", \"escalated\".

- AGREED: A and B match on action, entity, account and tax. Action it.
- DISAGREED: resolve it ONLY where the evidence settles it outright: one of
  them misread a figure, or missed a rule in rules/ or docs/REVIEW_METHOD.md
  that plainly applies. Say which one was right and why, then action it.
  Record it as \"resolved\".
- Everything else is ESCALATED. A genuine judgement difference is not yours to
  break by picking a side. DM the admin who started the run: what the item is,
  what each agent concluded, and the one question that would settle it.
  Action NOTHING on an escalated item.
- Either agent saying \"low\" confidence escalates the item regardless of
  whether they agree. Two agents can be confidently wrong together.

THEN, and only then, do the actual work for the agreed and resolved items:
create the bills, post the journals, label the emails, append the ledger lines.
This is the only pass permitted to write.

Report per docs/COMMS.md, with a *consensus* section: how many agreed, how many
resolved and which way, how many escalated. Escalated items go under
*queries*.
${COMMON}
" || true

./.venv/bin/python scripts/run_cost.py stamp --run-id "$RUN_ID" \
    --session-json "${PACK}/session-gather.json" --session-json "${PACK}/session-second.json" \
    --session-json "${PACK}/session-consensus.json" >> "$LOG" 2>&1 \
    || echo "$(date -u +%FT%TZ) [$JOB] cost stamp failed" >> "$LOG"

./deploy/commit-writeback.sh "$JOB" >> "$LOG" 2>&1 || true

if ./.venv/bin/python scripts/check_no_phantom_payments.py >> "$LOG" 2>&1; then
    echo "$(date -u +%FT%TZ) [$JOB] phantom-payment check PASS" >> "$LOG"
    rc=0
else
    echo "$(date -u +%FT%TZ) [$JOB] PHANTOM-PAYMENT CHECK FAILED: investigate" >> "$LOG"
    rc=1
fi

echo "$(date -u +%FT%TZ) [$JOB] consensus run finished, pack at ${PACK}" >> "$LOG"
exit "$rc"
