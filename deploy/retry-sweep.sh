#!/usr/bin/env bash
# Re-run any job that deferred itself on the Xero daily rate limit.
#
# A scheduled run that meets the 5000-a-day cap cannot finish. Rather than lose
# the day's work, run-agent.sh writes data/retry/<job>.retry holding the epoch
# second the cap resets and the prompt it was running, and exits. This sweep
# runs every ten minutes, and the moment that second has passed it re-runs the
# job with the same prompt.
#
#     deploy/retry-sweep.sh          # cron, every 10 minutes
#     deploy/retry-sweep.sh --list   # what is deferred and until when
#
# The marker is removed before the re-run, so a re-run that meets the cap again
# simply writes a fresh marker with the new reset time and the job keeps waiting
# rather than looping.
set -euo pipefail

HOME_DIR="${AGENT_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$HOME_DIR"
export PATH="$HOME/.local/bin:$HOME/.npm-global/bin:/usr/local/bin:$PATH"

RETRY_DIR="data/retry"
LOG="data/logs/retry-sweep.log"
mkdir -p "$RETRY_DIR" data/logs

[ -n "$(ls -A "$RETRY_DIR" 2>/dev/null)" ] || exit 0

now=$(date -u +%s)

for marker in "$RETRY_DIR"/*.retry; do
    [ -e "$marker" ] || continue
    job=$(basename "$marker" .retry)
    at=$(cut -f1 "$marker")
    prompt=$(cut -f2- "$marker")

    if [ "${1:-}" = "--list" ]; then
        printf '%-16s deferred until %s\n' "$job" \
            "$(date -u -d "@$at" +%FT%TZ 2>/dev/null || echo "epoch $at")"
        continue
    fi

    [ "$now" -ge "$at" ] || continue

    echo "$(date -u +%FT%TZ) [$job] rate limit reset, re-running" >> "$LOG"
    rm -f "$marker"
    ./deploy/run-agent.sh "$job" "$prompt" >> "$LOG" 2>&1 \
        || echo "$(date -u +%FT%TZ) [$job] re-run failed" >> "$LOG"
done
