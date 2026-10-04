#!/usr/bin/env bash
# One-time host preparation for the accounting agent (Ubuntu or Debian).
# Everything here is idempotent, but it is separate from install.sh because it
# touches the machine rather than the project.
#
#   ./deploy/bootstrap.sh
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

echo "=== system packages ==="
# jq is required by the payment-guard hooks in .claude/settings.json.
# flock serialises cron runs. python3-venv builds the project's virtualenv.
need=""
for p in jq flock git curl; do
    command -v "$p" >/dev/null 2>&1 || need="$need $p"
done
PY=$(find_python || echo python3)
if ! "$PY" -c 'import venv, ensurepip' >/dev/null 2>&1; then
    need="$need ${PY}-venv"
fi
if [ -n "$need" ]; then
    echo "  installing:$need"
    sudo apt-get update -qq
    # flock ships in util-linux, which is nearly always present; map the rest directly.
    sudo apt-get install -y -qq jq git curl util-linux "${PY}-venv"
else
    echo "  jq, flock, git, curl, python3-venv all present"
fi

echo "=== python ==="
if ! PY=$(find_python); then
    echo "  no Python 3.11+ found (tried python3, python3.13, python3.12, python3.11)." >&2
    echo "  install one (on Ubuntu 22.04: the deadsnakes PPA, python3.11 python3.11-venv) and re-run" >&2
    exit 1
fi
printf '  %s (%s, ok)\n' "$("$PY" -V)" "$PY"

export PATH="$HOME/.local/bin:$HOME/.npm-global/bin:/usr/local/bin:$PATH"

echo "=== node ==="
if command -v claude >/dev/null 2>&1; then
    echo "  claude is already on PATH; node not needed for installing it"
elif ! command -v node >/dev/null 2>&1; then
    echo "  no node found and no claude CLI: install Node 18+ or the native Claude Code installer, then re-run (docs/setup/SERVER.md)" >&2
    exit 1
else
    node_major=$(node -v | sed 's/^v\([0-9]*\).*/\1/')
    printf '  node %s' "$(node -v)"
    if [ "$node_major" -lt 18 ]; then
        printf ': TOO OLD, Claude Code needs 18+\n' >&2
        exit 1
    fi
    printf ' (ok)\n'
fi

echo "=== claude code cli ==="
if command -v claude >/dev/null 2>&1; then
    printf '  already installed: %s\n' "$(claude --version)"
else
    # npm's global prefix is often /usr/lib/node_modules, which needs root and
    # puts the CLI on every user's PATH. Install under the user prefix ~/.local
    # instead: no sudo, and the binary lands in ~/.local/bin, which is where
    # every deploy script and the systemd unit look for it.
    mkdir -p "$HOME/.local"
    npm config set prefix "$HOME/.local"
    echo "  npm global prefix set to ~/.local"
    npm install -g @anthropic-ai/claude-code
    printf '  installed: %s\n' "$(claude --version)"
fi

# cron runs with a minimal PATH; the deploy scripts add ~/.local/bin
# themselves, but an interactive login should find claude too.
if ! grep -q 'HOME/.local/bin' "$HOME/.profile" 2>/dev/null; then
    echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.profile"
    echo "  added ~/.local/bin to ~/.profile"
fi

echo
echo "bootstrapped. next: ./deploy/install.sh"
