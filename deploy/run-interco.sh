#!/usr/bin/env bash
# Full intercompany reconciliation across every entity and every pair in
# config/group.toml, on demand.
#
# Runs ON THE SERVER (the only Xero client; the refresh token cannot live in
# two places). From a laptop with AGENT_SERVER set it ssh's to the server and
# runs itself there. This is the ad-hoc variant; a scheduled read-only recon
# can be chained into the `daily` job (deploy/run-scheduled.sh).
# Read-only against Xero: it reads documents and balances, writes files under
# data/interco/ and data/reports/ on the server and nothing else.
#
#     deploy/run-interco.sh                          # window = current financial quarter to today
#     deploy/run-interco.sh --from 2026-07-01        # window start (last month end the pairs agreed)
#     deploy/run-interco.sh --as-at 2026-08-31       # balance date (default today)
#     deploy/run-interco.sh --rate EUR=1.10          # passed through to interco_recon.py
#     deploy/run-interco.sh --local                  # run here even if AGENT_SERVER is set
#
# Outputs, dated by the as-at date, plus `latest.*` pointers:
#     data/interco/interco_recon_<as-at>.txt    the report interco_recon.py prints, then the
#                                               FX revaluation drafts from interco_fx.py
#     data/interco/interco_recon_<as-at>.json   full machine-readable detail
#     data/interco/intercompany_<as-at>.xlsx    dated copy of the workbook
#     data/reports/Intercompany Reconciliation.xlsx   the current workbook: summary
#                                               matrix + one tab per configured pair
# Fetch them with deploy/fetch-interco.sh. The workbook is also published to
# the Drive publish folder every run (kind "intercompany"), one copy
# overwritten each time. The Xero pull runs under AGENT_READONLY; the Drive
# upload is the one write and runs as its own step outside that lock.
#
# Settings (environment, else read from the local .env):
#   AGENT_SERVER      ssh target, e.g. agent@203.0.113.10 or an ~/.ssh/config alias
#                     (unset = run locally: this machine is the server)
#   AGENT_SERVER_DIR  repo path on the server (default /opt/accounting-agent)
#   AGENT_SSH_KEY     optional private key file for the server
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
envval() { [ -f "$HERE/.env" ] && sed -n "s/^$1=//p" "$HERE/.env" | tail -1 | sed -e 's/^"//' -e 's/"$//' || true; }
SERVER_HOST="${AGENT_SERVER:-$(envval AGENT_SERVER)}"
SERVER_DIR="${AGENT_SERVER_DIR:-$(envval AGENT_SERVER_DIR)}"
SERVER_DIR="${SERVER_DIR:-/opt/accounting-agent}"
KEY="${AGENT_SSH_KEY:-$(envval AGENT_SSH_KEY)}"

AS_AT="$(date -u +%F)"
FROM=""
LOCAL=0
EXTRA=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --as-at) AS_AT="$2"; shift 2 ;;
        --from)  FROM="$2"; shift 2 ;;
        --rate)  EXTRA+=("--rate" "$2"); shift 2 ;;
        --local) LOCAL=1; shift ;;
        -h|--help) sed -n '2,35p' "$0"; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done
# Laptop: hand the whole thing to the server. On the server itself either
# AGENT_SERVER is unset or this checkout IS the server directory.
if [[ "$LOCAL" == 0 && -n "$SERVER_HOST" && "$HERE" != "$SERVER_DIR" ]]; then
    args=(--local --as-at "$AS_AT")
    [[ -n "$FROM" ]] && args+=(--from "$FROM")
    for ((i = 0; i < ${#EXTRA[@]}; i += 2)); do args+=("${EXTRA[i]}" "${EXTRA[i+1]}"); done
    SSH_OPTS=(-o ConnectTimeout=20 -o BatchMode=yes)
    [[ -n "$KEY" ]] && SSH_OPTS=(-i "$KEY" "${SSH_OPTS[@]}")
    echo "running on the server ($SERVER_HOST): ${args[*]}"
    exec ssh "${SSH_OPTS[@]}" "$SERVER_HOST" \
        "cd $(printf '%q' "$SERVER_DIR") && ./deploy/run-interco.sh $(printf '%q ' "${args[@]}")"
fi

# Server.
cd "$HERE"
mkdir -p data/interco data/reports
exec 9> data/interco-run.lock
if ! flock -n 9; then
    echo "an intercompany reconciliation is already running" >&2
    exit 1
fi
if [[ -f .env ]]; then set -a; . ./.env; set +a; fi

# Financial quarter and year from [company] financial_year_end (default 12-31,
# so the calendar quarter and 1 January), computed here on the server where
# config/group.toml lives.
fy_date() {
    ./.venv/bin/python -c "import sys, datetime; sys.path.insert(0, 'src')
from accounting_agent import config
d = datetime.date.fromisoformat(sys.argv[2])
print(getattr(config, sys.argv[1])(d).isoformat())" "$1" "$2"
}
# default window start: first day of the as-at financial quarter
[[ -z "$FROM" ]] && FROM="$(fy_date fy_quarter_start "$AS_AT")"
FY_FROM="$(fy_date fy_start_for "$AS_AT")"

TXT="data/interco/interco_recon_${AS_AT}.txt"
JSON="data/interco/interco_recon_${AS_AT}.json"
XLSX="data/reports/Intercompany Reconciliation.xlsx"
DATED="data/interco/intercompany_${AS_AT}.xlsx"

# Belt and braces on the Xero pull: under AGENT_READONLY the client refuses
# every non-GET. Scoped to the Xero steps so the Drive publish below can run.
echo "$(date -u +%FT%TZ) interco recon: transactions ${FROM}..${AS_AT}, balances as at ${AS_AT}"
AGENT_READONLY=1 ./.venv/bin/python scripts/interco_recon.py --from "$FROM" --to "$AS_AT" --as-at "$AS_AT" \
    --json "$JSON" "${EXTRA[@]}" 2>&1 | tee "$TXT"
echo "$(date -u +%FT%TZ) interco fx: drafting the revaluation journals (nothing posted)" | tee -a "$TXT"
AGENT_READONLY=1 ./.venv/bin/python scripts/interco_fx.py --json "$JSON" 2>&1 | tee -a "$TXT"
echo "$(date -u +%FT%TZ) interco matrix: as at ${AS_AT}, pair tabs from ${FY_FROM}"
AGENT_READONLY=1 ./.venv/bin/python scripts/interco_matrix.py --as-at "$AS_AT" -o "$XLSX" --no-publish
cp "$XLSX" "$DATED"

ln -sfn "$(basename "$TXT")"   data/interco/latest.txt
ln -sfn "$(basename "$JSON")"  data/interco/latest.json
ln -sfn "$(basename "$DATED")" data/interco/latest.xlsx

# The one write: the workbook to Drive, overwriting the copy of the previous run.
echo "$(date -u +%FT%TZ) interco workbook -> Drive"
./.venv/bin/python -c "import sys; sys.path.insert(0, 'src'); from accounting_agent.publish import publish_to_drive; print('Drive: ' + (publish_to_drive(sys.argv[1], kind='intercompany') or 'not published'))" "$XLSX"
echo "$(date -u +%FT%TZ) done: $TXT $JSON $XLSX"
