#!/usr/bin/env bash
# Copy the intercompany reconciliation outputs from the server to this machine.
#
# deploy/run-interco.sh builds them on the server under data/interco/ (the
# recon report, its JSON detail and a dated copy of the workbook). This pulls
# one set down to data/interco/ here (gitignored). Read-only: nothing is sent
# to the server.
#
#     deploy/fetch-interco.sh                 # the latest run
#     deploy/fetch-interco.sh 2026-08-31      # the run as at that date
#     deploy/fetch-interco.sh latest ~/Desktop
#
# Settings (environment, else read from the local .env):
#   AGENT_SERVER      ssh target, e.g. agent@203.0.113.10 or an ~/.ssh/config alias
#   AGENT_SERVER_DIR  repo path on the server (default /opt/accounting-agent)
#   AGENT_SSH_KEY     optional private key file for the server
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
envval() { [ -f "$HERE/.env" ] && sed -n "s/^$1=//p" "$HERE/.env" | tail -1 | sed -e 's/^"//' -e 's/"$//' || true; }
SERVER_HOST="${AGENT_SERVER:-$(envval AGENT_SERVER)}"
SERVER_DIR="${AGENT_SERVER_DIR:-$(envval AGENT_SERVER_DIR)}"
SERVER_DIR="${SERVER_DIR:-/opt/accounting-agent}"
KEY="${AGENT_SSH_KEY:-$(envval AGENT_SSH_KEY)}"
[[ -n "$SERVER_HOST" ]] || { echo "AGENT_SERVER is not set (environment or .env)" >&2; exit 2; }
SSH_OPTS=(-o ConnectTimeout=20 -o BatchMode=yes)
[[ -n "$KEY" ]] && SSH_OPTS=(-i "$KEY" "${SSH_OPTS[@]}")

REMOTE="$SERVER_DIR/data/interco"
WHICH="${1:-latest}"
DEST="${2:-$HERE/data/interco}"
mkdir -p "$DEST"
if [[ "$WHICH" == "latest" ]]; then
    # resolve the pointers so the local copies carry the real as-at date
    AS_AT=$(ssh "${SSH_OPTS[@]}" "$SERVER_HOST" "readlink $REMOTE/latest.xlsx" \
        | sed -E 's/^intercompany_(.*)\.xlsx$/\1/')
    [[ -n "$AS_AT" ]] || { echo "no run on the server yet" >&2; exit 1; }
else
    AS_AT="$WHICH"
fi
scp -q "${SSH_OPTS[@]}" "$SERVER_HOST:$REMOTE/interco_recon_${AS_AT}.txt" \
                        "$SERVER_HOST:$REMOTE/interco_recon_${AS_AT}.json" \
                        "$SERVER_HOST:$REMOTE/intercompany_${AS_AT}.xlsx" "$DEST/"
ls -la "$DEST"/*"${AS_AT}"*
