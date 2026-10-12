#!/usr/bin/env bash
# Copy a report workbook from the server to this machine's Downloads folder.
#
# The server holds ONE copy of each report, at
# $AGENT_SERVER_DIR/data/reports/, overwritten every time the report is
# refreshed. This pulls the current copy down. Read-only: nothing is sent to
# the server, and nothing on the server is changed.
#
#     deploy/fetch-report.sh prepayments          # -> ~/Downloads; also in the Drive publish folder (reports)
#     deploy/fetch-report.sh accruals ~/Desktop   # also on Drive, same folder; so is deposits
#     deploy/fetch-report.sh bills                # also on Drive (bills)
#     deploy/fetch-report.sh all
#
# Where the server is, from the environment (put them in your shell profile):
#     AGENT_SERVER       ssh destination, user@host (required)
#     AGENT_SERVER_DIR   the checkout on the server (default /opt/accounting-agent)
#     AGENT_SSH_KEY      private key for ssh (optional; else your ssh config)
#
# Refresh first, then fetch: this script builds nothing.
#     ssh "$AGENT_SERVER" 'cd /opt/accounting-agent && .venv/bin/python scripts/prepayments_report.py refresh all'
set -euo pipefail
SERVER="${AGENT_SERVER:-}"
SERVER_DIR="${AGENT_SERVER_DIR:-/opt/accounting-agent}"
REMOTE="${SERVER_DIR%/}/data/reports"
if [[ -z "$SERVER" ]]; then
    echo "set AGENT_SERVER to the server's ssh destination (user@host)" >&2
    exit 2
fi
SSH_OPTS=(-o ConnectTimeout=20 -o BatchMode=yes)
if [[ -n "${AGENT_SSH_KEY:-}" ]]; then
    SSH_OPTS+=(-i "$AGENT_SSH_KEY")
fi

# A case, not an associative array: macOS still ships bash 3.2, where
# `declare -A` is a syntax error and every lookup is an unbound variable.
book_for() {
    case "$1" in
        prepayments) echo Prepayments.xlsx ;;
        accruals)    echo Accruals.xlsx ;;
        deposits)    echo Deposits.xlsx ;;
        bills)       echo "Bills Payable.xlsx" ;;
        *)           echo "" ;;
    esac
}

WHICH="${1:-}"
DEST="${2:-$HOME/Downloads}"
if [[ -z "$WHICH" ]]; then
    echo "usage: deploy/fetch-report.sh <prepayments|accruals|deposits|bills|all> [destination]" >&2
    exit 2
fi
if [[ "$WHICH" == "all" ]]; then
    TARGETS="prepayments accruals deposits bills"
elif [[ -n "$(book_for "$WHICH")" ]]; then
    TARGETS="$WHICH"
else
    echo "unknown report ${WHICH}; one of: prepayments accruals deposits bills all" >&2
    exit 2
fi

mkdir -p "$DEST"
for name in $TARGETS; do
    book="$(book_for "$name")"
    remote="$(printf '%q' "$REMOTE/$book")"     # a name with spaces survives the remote shell
    if ! ssh "${SSH_OPTS[@]}" "$SERVER" "test -f $remote"; then
        echo "$name: not on the server yet, refresh it there first" >&2
        exit 1
    fi
    scp -q "${SSH_OPTS[@]}" "$SERVER:$remote" "$DEST/$book"
    ls -la "$DEST/$book"
done
