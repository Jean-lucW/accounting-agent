#!/usr/bin/env bash
# Commit what a run wrote back, on the server, so a Slack-originated rule
# change is not stranded in an uncommitted working tree.
#
#   ./deploy/commit-writeback.sh <job-name>
#
# THE PROBLEM THIS SOLVES. An admin DMs a ruling; the run writes it into
# rules/, docs/ or a skill (the write-back step of the workflow skill). That
# edit lands in the server's working tree and stops there. If your git host is
# unreachable from the server (a firewall or IP allowlist, no deploy key), a
# later sync from the laptop could overwrite it. Committing makes it a real
# object the laptop can fetch over the `server` git remote and push on.
#
# Scope is deliberately narrow: rules/, .claude/skills/ and docs/ (except
# docs/bookkept/, the runtime registers). Never config/group.toml (the group's
# own structure, gitignored, lives only on the laptop and the server), never
# data/, never .env or tokens. Code changes on the server are not a thing that
# should be happening, so code is never committed from here either. Only the
# paths below are committed, even if something else was staged by hand.
#
# Nothing is pushed from here. The laptop runs deploy/sync-all.sh to collect
# it. Committing is safe and reversible; pushing from an unattended server is
# not.
set -euo pipefail

HOME_DIR="${AGENT_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$HOME_DIR"

JOB="${1:-run}"
GIT_NAME="${AGENT_GIT_NAME:-Accounting agent}"
GIT_EMAIL="${AGENT_GIT_EMAIL:-accounting-agent@localhost}"

# Only the trees a run is allowed to write back to, and only those that exist
# in this checkout (a pathspec that matches nothing is fatal to git add).
SPECS=()
for TREE in rules .claude/skills docs; do
    [ -e "$TREE" ] && SPECS+=("$TREE")
done
[ ${#SPECS[@]} -gt 0 ] || { echo "$(date -u +%FT%TZ) [$JOB] nothing to write back"; exit 0; }
# Never committed, whatever .gitignore says on this server.
EXCLUDE=(':(exclude)docs/bookkept' ':(exclude)config' ':(exclude)data'
         ':(exclude).claude/settings.local.json' ':(exclude)*.env' ':(exclude).env*')

git add -- "${SPECS[@]}" "${EXCLUDE[@]}" 2>/dev/null || true

if git diff --cached --quiet -- "${SPECS[@]}" "${EXCLUDE[@]}"; then
    echo "$(date -u +%FT%TZ) [$JOB] nothing written back"
    exit 0
fi

FILES=$(git diff --cached --name-only -- "${SPECS[@]}" "${EXCLUDE[@]}" | tr '\n' ' ')

# The pathspec on commit keeps anything else that happens to be staged out of
# this commit.
git -c user.name="$GIT_NAME" \
    -c user.email="$GIT_EMAIL" \
    commit -q -m "Write-back from the ${JOB} run, $(date -u +%Y-%m-%d)

Rules this run learned, written into the files that own them. Made on the
server by an unattended run; collect it with deploy/sync-all.sh from the
laptop.

Files: ${FILES}" -- "${SPECS[@]}" "${EXCLUDE[@]}"

echo "$(date -u +%FT%TZ) [$JOB] wrote back: ${FILES}"
echo "$(date -u +%FT%TZ) [$JOB] run deploy/sync-all.sh on the laptop to collect and push it"
