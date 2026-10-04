#!/usr/bin/env bash
# Set the accounting agent up on the server. Safe to re-run.
#
#   ./deploy/install.sh            # venv, deps, exec bits, secrets check,
#                                  # systemd unit installed (not started)
#   ./deploy/install.sh --enable   # all of that, then the crontab block is
#                                  # installed and the Slack listener enabled
#                                  # and started. Run only once
#                                  # ./deploy/preflight.sh is clean.
#
# Assumes bootstrap.sh has already run once (python, node, claude CLI, system
# deps).
set -euo pipefail

# The first Python 3.11+ on PATH: python3 itself, or a side-by-side install
# such as python3.12 or python3.11 (deadsnakes on Ubuntu 22.04).
find_python() {
    for c in python3 python3.13 python3.12 python3.11; do
        if command -v "$c" >/dev/null 2>&1 \
            && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
            echo "$c"; return 0
        fi
    done
    return 1
}

HOME_DIR="${AGENT_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$HOME_DIR"
export PATH="$HOME/.local/bin:$HOME/.npm-global/bin:/usr/local/bin:$PATH"

ENABLE=0
for a in "$@"; do
    case "$a" in
        --enable) ENABLE=1 ;;
        *) echo "usage: $0 [--enable]" >&2; exit 2 ;;
    esac
done

SERVICE=accounting-agent-slack
UNIT_SRC="deploy/${SERVICE}.service"
UNIT_DST="/etc/systemd/system/${SERVICE}.service"
CRON_TAG="accounting-agent ${HOME_DIR}"

echo "=== python venv ==="
if [ ! -d .venv ]; then
    PY=$(find_python) || { echo "no Python 3.11+ found: run deploy/bootstrap.sh" >&2; exit 1; }
    "$PY" -m venv .venv
fi
./.venv/bin/pip install --quiet --upgrade pip
./.venv/bin/pip install --quiet -r requirements.txt
./.venv/bin/pip install --quiet -e .

echo "=== exec bits ==="
# A missing exec bit kills a cron run while still reporting success.
chmod +x deploy/*.sh
[ -d .claude/hooks ] && chmod +x .claude/hooks/* 2>/dev/null || true

echo "=== directories ==="
mkdir -p data/logs data/retry data/reports data/statements data/intake

echo "=== group config ==="
if [ -f config/group.toml ]; then
    PYTHONPATH=src ./.venv/bin/python -m accounting_agent.config >/dev/null \
        && echo "  ok      config/group.toml loads" \
        || echo "  BROKEN  config/group.toml: run .venv/bin/python -m accounting_agent.config to see why"
else
    echo "  MISSING config/group.toml: cp config/group.example.toml config/group.toml and edit it (ADAPTING.md)"
fi

echo "=== secrets present? ==="
# .env and the Xero token are always needed. Gmail, Drive and the bank tokens
# only when the matching connector is configured; preflight.sh decides that
# properly, this is just a reminder of what to copy across.
missing=0
for f in .env .xero/tokens.json .gmail/tokens.json; do
    if [ -f "$f" ]; then
        printf '  ok      %s\n' "$f"
    else
        printf '  MISSING %s\n' "$f"
        missing=1
    fi
done
for f in .gdrive/tokens.json .revolut/tokens.json; do
    if [ -f "$f" ]; then
        printf '  ok      %s\n' "$f"
    else
        printf '  absent  %s   (needed only if that connector is configured)\n' "$f"
    fi
done
chmod 600 .env 2>/dev/null || true
for d in .xero .gmail .gdrive .revolut; do
    [ -d "$d" ] && chmod 700 "$d" && chmod 600 "$d"/* 2>/dev/null || true
done
[ "$missing" -eq 0 ] || echo "  -> copy the missing files from the laptop before running anything (docs/setup/SERVER.md)"

echo "=== slack listener service ==="
if [ -f "$UNIT_SRC" ]; then
    tmp=$(mktemp)
    sed -e "s|__REPO__|$HOME_DIR|g" -e "s|__HOME__|$HOME_DIR|g" -e "s|__USER_HOME__|$HOME|g" -e "s|__USER__|$(id -un)|g" "$UNIT_SRC" > "$tmp"
    if sudo -n true 2>/dev/null || [ -t 0 ]; then
        if ! sudo cmp -s "$tmp" "$UNIT_DST" 2>/dev/null; then
            sudo install -m 644 "$tmp" "$UNIT_DST"
            sudo systemctl daemon-reload
            echo "  installed $UNIT_DST"
        else
            echo "  $UNIT_DST up to date"
        fi
    else
        echo "  no sudo: install it by hand: sudo cp $tmp $UNIT_DST && sudo systemctl daemon-reload"
    fi
    rm -f "$tmp"
else
    echo "  $UNIT_SRC not found, skipped"
fi

echo
echo "=== versions ==="
printf '  python  %s\n' "$(./.venv/bin/python -V 2>&1)"
printf '  claude  %s\n' "$(claude --version 2>/dev/null || echo 'NOT INSTALLED: run deploy/bootstrap.sh')"

if [ "$ENABLE" -eq 0 ]; then
    echo
    echo "installed. next: ./deploy/preflight.sh, then ./deploy/install.sh --enable"
    exit 0
fi

echo
echo "=== crontab ==="
# A marked block, replaced in place on every run; every other line in the
# user's crontab is left exactly as it was.
current="$(crontab -l 2>/dev/null || true)"
others="$(printf '%s\n' "$current" | sed "/^# BEGIN ${CRON_TAG//\//\\/}\$/,/^# END ${CRON_TAG//\//\\/}\$/d")"
{
    [ -n "$others" ] && printf '%s\n' "$others"
    echo "# BEGIN ${CRON_TAG}"
    sed "s|__HOME__|$HOME_DIR|g" deploy/crontab.example
    echo "# END ${CRON_TAG}"
} | crontab -
echo "  crontab block installed ($(crontab -l | grep -cvE '^[[:space:]]*(#|$)') active lines in total)"

echo "=== slack listener ==="
if [ -f "$UNIT_DST" ]; then
    sudo systemctl enable --now "$SERVICE"
    echo "  $SERVICE: $(systemctl is-active "$SERVICE")"
else
    echo "  $UNIT_DST not installed, listener not started"
fi

echo
echo "enabled. logs: data/logs/ (cron.log, <job>-<date>.log), journalctl -u $SERVICE"
