#!/usr/bin/env bash
# Collect what the server wrote back and push it to origin. RUN ON THE LAPTOP.
#
#   ./deploy/sync-back.sh [--dry-run]
#
# Superseded by deploy/sync-all.sh, which does this and the delivery the other
# way, plus the collect step for uncommitted server edits. Kept for the case
# where only the collect-and-push half is wanted.
#
# The server writes rule changes into rules/, docs/ and .claude/skills/ during
# a run and commits them (deploy/commit-writeback.sh), but it may not be able
# to push: if your git host is unreachable from the server (a firewall or
# allowlist, or no key for it), the laptop is the machine that reaches both,
# so it is the bridge. origin must be a PRIVATE repository.
#
# Run this BEFORE anything that overwrites the server's tree. Pushing the
# laptop's tree over an uncollected write-back destroys it silently. That is
# the whole reason this script exists.
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"
DRY=""
[ "${1:-}" = "--dry-run" ] && DRY=1

if ! git diff --quiet || ! git diff --cached --quiet; then
    echo "working tree is dirty. commit or stash first, then re-run." >&2
    exit 1
fi

envval() { [ -f .env ] && sed -n "s/^$1=//p" .env | tail -1 | sed -e 's/^"//' -e 's/"$//' || true; }
KEY="${AGENT_SSH_KEY:-$(envval AGENT_SSH_KEY)}"
export GIT_SSH_COMMAND="ssh ${KEY:+-i $KEY} -o ConnectTimeout=20"

git remote get-url server >/dev/null 2>&1 \
    || { echo "no git remote 'server' (docs/setup/SERVER.md)" >&2; exit 2; }

echo "== fetching the server =="
git fetch server

AHEAD=$(git rev-list --count HEAD..server/main)
if [ "$AHEAD" -eq 0 ]; then
    echo "server has nothing new."
else
    echo "== $AHEAD commit(s) on the server, not here =="
    git log --oneline HEAD..server/main
    echo
    git diff --stat HEAD..server/main
fi

if [ -n "$DRY" ]; then
    echo
    echo "dry run, nothing merged or pushed."
    exit 0
fi

if [ "$AHEAD" -gt 0 ]; then
    echo "== merging =="
    git merge --no-edit server/main
fi

echo "== pushing to origin =="
git push origin main

echo
echo "done. deliver to the server with deploy/sync-all.sh."
