#!/usr/bin/env bash
# Make laptop = git remote = server, losing nothing on any of the three.
# RUN ON THE LAPTOP.
#
#   ./deploy/sync-all.sh [--dry-run] [--no-restart]
#
# The three copies of this repo and who can reach whom:
#
#   laptop  --ssh-->  server (AGENT_SERVER, git remote `server`)
#   laptop  --ssh-->  your git host (git remote `origin`, a PRIVATE repository)
#   server  --?-->    git host (if your git host is unreachable from the server,
#                     behind a firewall or allowlist, or the server has no key)
#
# The laptop is the bridge either way, and this script is the whole bridging procedure
# in one place, in the only order that cannot lose work:
#
#   1. collect  - anything sitting uncommitted in the server's working tree is
#                 committed there (a run's write-back that commit-writeback.sh
#                 did not catch, or an edit made by hand over ssh). A dirty file
#                 that is byte-identical to the laptop's commit is a stale copy
#                 and is restored, not committed. Never collected: secrets,
#                 config/group.toml, data/, docs/bookkept/ and
#                 .claude/settings.local.json (server-only, gitignored).
#   2. merge    - the laptop merges server/main and origin/main into main. Git
#                 history says who is newest; nothing is decided by mtime.
#   3. push     - main goes to origin.
#   4. deliver  - main is pushed to the server as a plain ref and the server
#                 fast-forwards onto it. Tracked files only; data/, .env and the
#                 token dirs are gitignored and never cross the wire in either
#                 direction. Git tracks the exec bit, so nothing is stripped.
#   5. verify   - the three HEADs are printed and must be one hash.
#
# Why not rsync: rsync cannot see a write-back the server committed, `--delete`
# removes it, and it leaves the server's git index disagreeing with its working
# tree. Fast-forwarding over git cannot delete anything that was committed, and
# step 1 makes sure everything is.
#
# It refuses, rather than guesses, when: the laptop tree is dirty, either host is
# unreachable, a run is in flight on the server, a merge conflicts, or the server
# has deleted a file the laptop still has. Never force-pushes, never resets.
#
# Settings (environment, else read from the laptop's .env):
#   AGENT_SERVER      ssh target, e.g. agent@203.0.113.10 or an ~/.ssh/config alias
#   AGENT_SERVER_DIR  repo path on the server (default /opt/accounting-agent)
#   AGENT_SSH_KEY     optional private key file for the server
#   AGENT_GIT_NAME    author of the collect commit (default "Accounting agent")
#   AGENT_GIT_EMAIL   its email (default accounting-agent@localhost)
#   AGENT_IP_CHECK_URL  service that echoes this machine's public IP, printed
#                     to diagnose an allowlist (default https://api.ipify.org)
# One-time: git remote add server "$AGENT_SERVER:$AGENT_SERVER_DIR"
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

DRY=0
RESTART=1
for a in "$@"; do
    case "$a" in
        --dry-run)    DRY=1 ;;
        --no-restart) RESTART=0 ;;
        *) echo "usage: $0 [--dry-run] [--no-restart]" >&2; exit 2 ;;
    esac
done

# One KEY=value from .env without sourcing the whole file on the laptop.
envval() { [ -f .env ] && sed -n "s/^$1=//p" .env | tail -1 | sed -e 's/^"//' -e 's/"$//' || true; }
SERVER_HOST="${AGENT_SERVER:-$(envval AGENT_SERVER)}"
SERVER_DIR="${AGENT_SERVER_DIR:-$(envval AGENT_SERVER_DIR)}"
SERVER_DIR="${SERVER_DIR:-/opt/accounting-agent}"
KEY="${AGENT_SSH_KEY:-$(envval AGENT_SSH_KEY)}"
GIT_NAME="${AGENT_GIT_NAME:-$(envval AGENT_GIT_NAME)}"
GIT_NAME="${GIT_NAME:-Accounting agent}"
GIT_EMAIL="${AGENT_GIT_EMAIL:-$(envval AGENT_GIT_EMAIL)}"
GIT_EMAIL="${GIT_EMAIL:-accounting-agent@localhost}"
SERVICE=accounting-agent-slack

[ -n "$SERVER_HOST" ] || { echo "AGENT_SERVER is not set (environment or .env)" >&2; exit 2; }
git remote get-url server >/dev/null 2>&1 \
    || { echo "no git remote 'server': git remote add server $SERVER_HOST:$SERVER_DIR" >&2; exit 2; }

SSH_BASE=(-o ConnectTimeout=20 -o BatchMode=yes)
[ -n "$KEY" ] && SSH_BASE=(-i "$KEY" "${SSH_BASE[@]}")
SSH_OPTS=(-n "${SSH_BASE[@]}")  # -n: never read our stdin (the status loop below feeds a here-string)
export GIT_SSH_COMMAND="ssh ${KEY:+-i $KEY} -o ConnectTimeout=20 -o BatchMode=yes"

srv() { ssh "${SSH_OPTS[@]}" "$SERVER_HOST" "cd $SERVER_DIR && $*"; }
# srv() joins its arguments into one remote command line, so a path with a
# space in it ("Bills Payable.xlsx") must be shell-quoted first:
q() { printf '%q ' "$@"; }
# git status --porcelain wraps such a path in double quotes and backslash-
# escapes quotes and backslashes inside it; undo that to get the real path.
unquote() {
    local p="$1"
    if [ "${p:0:1}" = '"' ] && [ "${p: -1}" = '"' ]; then
        p="${p:1:${#p}-2}"
        p="${p//\\\"/\"}"
        p="${p//\\\\/\\}"
    fi
    printf '%s' "$p"
}
section() { printf '\n== %s ==\n' "$*"; }
fail() { printf '\nFAIL: %s\n' "$*" >&2; exit 1; }
would() { if [ "$DRY" = 1 ]; then echo "   (dry run) would: $*"; return 0; fi; return 1; }

# ---------------------------------------------------------------- 0. checks
section "preconditions"
BRANCH=$(git rev-parse --abbrev-ref HEAD)
[ "$BRANCH" = main ] || fail "laptop is on '$BRANCH', not main"
if [ -n "$(git status --porcelain)" ]; then
    git status --short
    fail "laptop working tree is dirty. commit or stash first, then re-run."
fi

echo "reminder: origin ($(git remote get-url origin 2>/dev/null || echo unset)) must be a PRIVATE repository; it holds your rules/."
IP_CHECK_URL="${AGENT_IP_CHECK_URL:-$(envval AGENT_IP_CHECK_URL)}"
IP=$(curl -s --max-time 5 "${IP_CHECK_URL:-https://api.ipify.org}" || echo unknown)
echo "laptop public ip  $IP"
if ! git ls-remote --exit-code origin HEAD >/dev/null 2>&1; then
    fail "origin ($(git remote get-url origin)) unreachable from $IP. Check your network and credentials; if your git host restricts access by IP address, this address may not be allowed (deploy/README.md, 'When the git host times out')."
fi
echo "origin            reachable"
srv true >/dev/null 2>&1 || fail "server $SERVER_HOST unreachable from $IP: check its firewall or security group allows this address (deploy/README.md, 'When SSH hangs')"
echo "server            reachable"

SERVER_BRANCH=$(srv git rev-parse --abbrev-ref HEAD)
[ "$SERVER_BRANCH" = main ] || fail "server is on '$SERVER_BRANCH', not main"

INFLIGHT=$(srv "pgrep -af 'deploy/run-(agent|consensus|thread|query)\.sh' | grep -v pgrep || true")
if [ -n "$INFLIGHT" ]; then
    echo "$INFLIGHT"
    fail "a run is in flight on the server; its write-back is not committed yet. re-run when it has reported."
fi

# ---------------------------------------------------------------- 1. fetch
section "fetch"
git fetch --quiet origin
git fetch --quiet server
for r in HEAD origin/main server/main; do
    printf '%-12s %s\n' "$r" "$(git log -1 --format='%h  %ci  %s' "$r")"
done

# ---------------------------------------------------------------- 2. collect
section "server working tree"
# A secret is never collected, whatever .gitignore says. An untracked
# .env.bak-<stamp> on the server would otherwise be committed and pushed
# before an ignore rule for it reached the server. Stop instead: the user
# renames or removes the file by hand.
is_secret() {
    case "$1" in
        .env.example) return 1 ;;
        .env|.env.*|*/.env|*/.env.*|*.pem|*.key|*.p12|*tokens.json|\
        .xero/*|.gmail/*|.gdrive/*|.revolut/*) return 0 ;;
        *) return 1 ;;
    esac
}
# Server-only runtime and configuration: never committed from the server.
never_collect() {
    case "$1" in
        config/group.toml|data|data/*|docs/bookkept|docs/bookkept/*|.claude/settings.local.json) return 0 ;;
        *) return 1 ;;
    esac
}

SERVER_STATUS=$(srv git status --porcelain)
if [ -z "$SERVER_STATUS" ]; then
    echo "clean"
else
    TO_ADD=()
    TO_RESTORE=()
    OUTSIDE=()
    while IFS= read -r line; do
        [ -n "$line" ] || continue
        code="${line:0:2}"
        path="$(unquote "${line:3}")"
        if never_collect "$path"; then
            case "$code" in
                '??') echo "server only $path   (gitignored runtime or config, left alone)"; continue ;;
                *) fail "'$path' is tracked on the server but must never be committed. On the server: git rm --cached -r -- '$path', commit, and make sure .gitignore lists it; then re-run." ;;
            esac
        fi
        case "$code" in
            '??')
                is_secret "$path" && fail "'$path' on the server looks like a secret and is not gitignored. rename it to a .keep name or delete it by hand; never commit it."
                echo "untracked   $path"
                TO_ADD+=("$path")
                case "$path" in rules/*|docs/*|.claude/*) ;; *) OUTSIDE+=("$path") ;; esac ;;
            ' M'|'M '|'MM'|'AM'|'A ')
                srvhash=$(srv git hash-object -- "$(q "$path")")
                lhash=$(git rev-parse -q --verify "HEAD:$path" 2>/dev/null || echo none)
                if [ "$srvhash" = "$lhash" ]; then
                    echo "stale copy  $path   (identical to laptop HEAD, restoring; the merge delivers it)"
                    TO_RESTORE+=("$path")
                else
                    is_secret "$path" && fail "'$path' on the server looks like a secret. sort it out by hand; never commit it."
                    echo "server edit $path   (not on the laptop, will be committed)"
                    TO_ADD+=("$path")
                    case "$path" in rules/*|docs/*|.claude/*) ;; *) OUTSIDE+=("$path") ;; esac
                fi ;;
            ' D'|'D ')
                if git cat-file -e "HEAD:$path" 2>/dev/null; then
                    fail "'$path' is deleted on the server but the laptop still has it. decide by hand."
                fi
                echo "stale del   $path   (laptop dropped it too, restoring; the merge removes it)"
                TO_RESTORE+=("$path") ;;
            *)
                fail "unhandled server status '$line'. sort it out by hand on the server." ;;
        esac
    done <<< "$SERVER_STATUS"

    if [ ${#TO_RESTORE[@]} -gt 0 ]; then
        would "git checkout -- ${TO_RESTORE[*]}" || srv git checkout -- "$(q "${TO_RESTORE[@]}")"
    fi
    if [ ${#TO_ADD[@]} -gt 0 ]; then
        if ! would "commit on the server: ${TO_ADD[*]}"; then
            srv git add -A -- "$(q "${TO_ADD[@]}")"
            srv git -c user.name="$(q "$GIT_NAME")" -c user.email="$(q "$GIT_EMAIL")" \
                commit -q -m "\"Collected from the server working tree, $(date -u +%Y-%m-%d)

Uncommitted changes found on the server by deploy/sync-all.sh and committed so
the laptop can merge them. Files: ${TO_ADD[*]}\""
            echo "committed on the server: $(srv git log -1 --format=%h)"
            git fetch --quiet server
        fi
    fi
    if [ ${#OUTSIDE[@]} -gt 0 ]; then
        printf 'NOTE: server-only edits outside rules/, docs/ and .claude/ (code is not supposed to change on the server):\n'
        printf '      %s\n' "${OUTSIDE[@]}"
    fi
fi

# ---------------------------------------------------------------- 3. merge
section "merge into laptop main"
MERGED=0
for ref in server/main origin/main; do
    n=$(git rev-list --count "HEAD..$ref")
    if [ "$n" -eq 0 ]; then
        echo "$ref: nothing new"
        continue
    fi
    echo "$ref: $n commit(s) not on the laptop"
    git log --format='   %h  %ci  %s' "HEAD..$ref"
    git diff --stat "HEAD...$ref" | sed 's/^/   /'
    if ! would "git merge --no-edit $ref"; then
        if ! git merge --no-edit "$ref"; then
            git diff --name-only --diff-filter=U | sed 's/^/   CONFLICT /'
            git merge --abort
            fail "merging $ref conflicts. resolve by hand (git merge $ref), commit, re-run."
        fi
        MERGED=1
    fi
done

# ---------------------------------------------------------------- 4. push
section "push to origin"
if [ "$(git rev-list --count origin/main..HEAD)" -eq 0 ] && [ "$MERGED" = 0 ]; then
    echo "origin/main already at HEAD"
else
    git log --format='   %h  %s' origin/main..HEAD
    would "git push origin main" || git push --quiet origin main
fi

# ---------------------------------------------------------------- 5. deliver
section "deliver to the server"
SERVER_OLD=$(srv git rev-parse HEAD)
if [ "$SERVER_OLD" = "$(git rev-parse HEAD)" ]; then
    echo "server already at HEAD"
    SERVER_NEW=$SERVER_OLD
else
    git log --format='   %h  %s' "$SERVER_OLD..HEAD"
    if would "git push server main:refs/remotes/laptop/main && server git merge --ff-only laptop/main"; then
        SERVER_NEW=$SERVER_OLD
    else
        git push --quiet server main:refs/remotes/laptop/main
        srv git merge --ff-only --quiet laptop/main
        SERVER_NEW=$(srv git rev-parse HEAD)
        if [ -n "$(git diff --name-only "$SERVER_OLD" "$SERVER_NEW" -- requirements.txt pyproject.toml setup.py setup.cfg)" ]; then
            echo "dependencies changed, running deploy/install.sh on the server"
            srv ./deploy/install.sh | sed 's/^/   /'
        fi
        if [ -n "$(git diff --name-only "$SERVER_OLD" "$SERVER_NEW" -- scripts/slack_agent.py deploy/$SERVICE.service)" ]; then
            if [ "$RESTART" = 1 ] && srv sudo -n true 2>/dev/null; then
                srv sudo -n systemctl restart "$SERVICE"
                echo "listener code changed: restarted ($(srv systemctl is-active "$SERVICE")). KillMode=process, so no run was touched."
            else
                echo "listener code changed: RESTART THE LISTENER by hand: ssh $SERVER_HOST, sudo systemctl restart $SERVICE"
            fi
        fi
    fi
fi

# ---------------------------------------------------------------- 6. verify
section "verify"
L=$(git rev-parse HEAD)
O=$(git rev-parse origin/main)
S=$SERVER_NEW
SS=$(srv git status --porcelain)
printf 'laptop  %s\norigin  %s\nserver  %s\n' "$L" "$O" "$S"
if [ "$DRY" = 1 ]; then
    echo; echo "dry run: nothing collected, merged, pushed or delivered."
    exit 0
fi
[ -z "$SS" ] || { echo "$SS"; fail "server working tree not clean after delivery"; }
[ "$L" = "$O" ] && [ "$L" = "$S" ] || fail "the three are not equal"
echo
echo "laptop = origin = server at ${L:0:7}"
