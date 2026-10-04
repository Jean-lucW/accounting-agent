#!/usr/bin/env bash
# Verify the server can actually do the job, before trusting it to cron.
# Read-only: touches no ledger, writes nothing to Xero, Gmail, Drive or a bank.
#
#   ./deploy/preflight.sh
#
# Checks every connector config/group.toml says is in use: Xero always, the
# Slack bot token, the Gmail token, the Drive token when [drive]
# publish_folder_id is set, and each Revolut and Mercury slug named under an
# entity's bank_accounts. Then the CLI and the two safety guards.
set -uo pipefail

HOME_DIR="${AGENT_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$HOME_DIR"
export PATH="$HOME/.local/bin:$HOME/.npm-global/bin:/usr/local/bin:$PATH"

if [ ! -f .env ]; then
    echo "no .env in $HOME_DIR: copy .env.example to .env and fill it in" >&2
    exit 1
fi
set -a
# shellcheck disable=SC1091
. ./.env
set +a

PY=./.venv/bin/python
fails=0
ok()   { printf '  \033[32mPASS\033[0m  %s\n' "$1"; }
bad()  { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; fails=$((fails+1)); }
skip() { printf '  \033[33mSKIP\033[0m  %s\n' "$1"; }

cfg() {
    "$PY" -c "import sys; sys.path.insert(0, 'src')
from accounting_agent import config
print($1)" 2>/dev/null
}

echo "=== 0. group config ==="
if PYTHONPATH=src "$PY" -m accounting_agent.config >/tmp/preflight-config.$$ 2>&1; then
    sed 's/^/    /' /tmp/preflight-config.$$
    ok "config/group.toml loads"
else
    sed 's/^/    /' /tmp/preflight-config.$$
    bad "config/group.toml does not load: fix it before anything else"
fi
rm -f /tmp/preflight-config.$$

echo "=== 0b. rules ==="
# The rules/ files ship as examples. A run decides on what they say, so none
# may still carry the example marker, and every entity needs its own file.
MARKER='EXAMPLE: replace'
if [ -d rules ]; then
    # files named _*.md (the blank entity template) are scaffolding, not rules
    left=$(grep -rlF --exclude='_*' -- "$MARKER" rules 2>/dev/null || true)
    if [ -z "$left" ]; then
        ok "no rules/ file still carries the example marker"
    else
        printf '%s\n' "$left" | sed 's/^/    /'
        bad "rules/ files above still contain '$MARKER': replace the example rules with your group's own"
    fi
else
    bad "no rules/ folder: the agent has nothing to decide on"
fi
missing_rules="$(cfg '"\n".join(f"{e.key} {e.rules_file}" for e in config.entities() if not __import__("pathlib").Path(e.rules_file).is_file())')"
if [ -z "$missing_rules" ]; then
    ok "every entity's rules_file exists"
else
    printf '%s\n' "$missing_rules" | sed 's/^/    missing: /'
    bad "an entity's rules_file is missing: write rules/entities/<KEY>.md for it (or fix rules_file in config/group.toml)"
fi

echo "=== 1. python runtime ==="
if "$PY" -c "import certifi, openpyxl, pypdf, docx, PIL, lxml, slack_sdk" 2>/dev/null; then
    ok "all runtime dependencies import"
else
    bad "a runtime dependency is missing: re-run deploy/install.sh"
fi

echo "=== 2. xero ==="
# Read-only GETs against every connected entity. Exercises the refresh-token
# rotation path as well as connectivity.
if "$PY" scripts/smoke_test.py 2>&1 | sed 's/^/    /'; [ "${PIPESTATUS[0]}" -eq 0 ]; then
    ok "Xero token valid, every entity readable"
else
    bad "Xero smoke test failed: the refresh token may have been rotated elsewhere (deploy/README.md)"
fi

echo "=== 3. gmail ==="
if "$PY" -c "
import sys; sys.path.insert(0, 'src')
from accounting_agent.gmail_auth import get_access_token
t = get_access_token()
assert t and len(t) > 20
" 2>/dev/null; then
    ok "Gmail token refreshes"
else
    bad "Gmail auth failed. Gmail is required: the invoice intake reads the accounting inbox through it, and no other mail provider is supported yet. Bootstrap on the laptop (python -m accounting_agent.gmail_auth), copy .gmail/tokens.json here"
fi

echo "=== 3b. google drive ==="
# Needed only to publish workbooks. Same Google OAuth client as Gmail; Google
# does not rotate the refresh token, so the laptop copy is valid here.
if [ -n "$(cfg 'config.drive_settings()["publish_folder_id"]')" ]; then
    if "$PY" -c "
import sys; sys.path.insert(0, 'src')
from accounting_agent import gdrive
who = gdrive.whoami()
drives = gdrive.shared_drives()
print(f'    {who}, {len(drives)} shared drive(s) visible')
" 2>&1; then
        ok "Google Drive token refreshes and lists shared drives"
    else
        bad "Google Drive auth failed: bootstrap on the laptop (python -m accounting_agent.gdrive_auth), copy .gdrive/tokens.json here"
    fi
else
    skip "[drive] publish_folder_id is blank: workbooks are not published"
fi

echo "=== 4. slack ==="
if [ -n "${SLACK_BOT_TOKEN:-}" ] &&
   curl -sS -H "Authorization: Bearer $SLACK_BOT_TOKEN" \
        https://slack.com/api/auth.test | grep -q '"ok":true'; then
    ok "Slack bot token authenticates"
else
    bad "Slack auth failed: reports, intake from the Slack app and the listener will not work"
fi
if [ -n "${SLACK_APP_TOKEN:-}" ]; then
    ok "SLACK_APP_TOKEN is set (Socket Mode listener)"
else
    bad "SLACK_APP_TOKEN is empty: the Slack listener cannot connect"
fi
if [ -n "$(cfg 'config.slack().channel_id')" ]; then
    ok "report channel set: #$(cfg 'config.slack().channel_name')"
else
    bad "[slack] channel_id is blank in config/group.toml: scheduled runs have nowhere to report"
fi

echo "=== 5. bank APIs ==="
banks="$(cfg '"\n".join(f"{b.provider} {b.slug}" for b in config.bank_accounts() if b.has_api)')"
if [ -z "$banks" ]; then
    skip "no Revolut or Mercury account configured"
else
    while read -r provider slug; do
        [ -n "$provider" ] || continue
        if "$PY" "scripts/${provider}_check.py" "$slug" 2>&1 | sed 's/^/    /'; [ "${PIPESTATUS[0]}" -eq 0 ]; then
            ok "$provider $slug reads, read-only"
        else
            bad "$provider $slug failed: see the stage above and docs/setup/$(echo "$provider" | tr '[:lower:]' '[:upper:]').md"
        fi
    done <<< "$banks"
fi

echo "=== 6. claude cli ==="
if command -v claude >/dev/null 2>&1; then
    ok "claude $(claude --version)"
else
    bad "claude CLI not installed: run deploy/bootstrap.sh"
fi
if [ -n "${ANTHROPIC_API_KEY:-}" ] || [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
    ok "Claude credential present in .env"
else
    bad "neither ANTHROPIC_API_KEY nor CLAUDE_CODE_OAUTH_TOKEN is set: every unattended run will abort"
fi

echo "=== 7. payment guard fires under bypassPermissions ==="
if command -v jq >/dev/null 2>&1; then
    ok "jq installed (the payment guard reads every tool call with it)"
else
    bad "jq is missing: the payment guard denies every Bash, Write and Edit call without it. install jq (deploy/bootstrap.sh)"
fi
# THE important check. Unattended runs have no permission prompt, so the hook
# is the only thing standing between a confused session and a phantom payment.
guard_out=$(printf '%s' \
  '{"tool_name":"Bash","tool_input":{"command":"purchases.create_payment(x)"}}' \
  | bash .claude/hooks/guard-payments.sh 2>/dev/null)
# jq pretty-prints, so allow whitespace around the colon.
if printf '%s' "$guard_out" | grep -qE '"permissionDecision"[[:space:]]*:[[:space:]]*"deny"'; then
    ok "guard-payments.sh denies a payment write"
else
    bad "guard-payments.sh did NOT deny: do not run unattended until fixed"
fi

echo "=== 8. read-only locks for query sessions ==="
# `query` sessions run under AGENT_READONLY=1. Two things must hold: the Xero
# client refuses a POST before fetching a token, and the guard hook denies a
# write while allowing a read and staying silent when the flag is off.
if AGENT_READONLY=1 "$PY" -c "
import sys; sys.path.insert(0, 'src')
from accounting_agent import readonly
from accounting_agent.xero.client import XeroClient
err = getattr(readonly, 'ReadOnlyError', PermissionError)
c = XeroClient.__new__(XeroClient); c.entity_name = 'probe'; c.tenant_id = 'none'
try:
    c.request('POST', 'Invoices', json_body={}); raise SystemExit(1)
except err:
    pass
" 2>/dev/null; then
    ok "XeroClient refuses non-GET under AGENT_READONLY"
else
    bad "transport lock did NOT refuse a write under AGENT_READONLY: do not enable query"
fi
ro_deny=$(printf '%s' '{"tool_name":"Bash","tool_input":{"command":"c.post(\"Invoices\", body)"}}' \
  | AGENT_READONLY=1 python3 .claude/hooks/guard-readonly.py 2>/dev/null)
ro_allow=$(printf '%s' '{"tool_name":"Bash","tool_input":{"command":"c.get(\"Invoices\")"}}' \
  | AGENT_READONLY=1 python3 .claude/hooks/guard-readonly.py 2>/dev/null)
ro_off=$(printf '%s' '{"tool_name":"Bash","tool_input":{"command":"c.post(\"Invoices\", body)"}}' \
  | env -u AGENT_READONLY python3 .claude/hooks/guard-readonly.py 2>/dev/null)
if printf '%s' "$ro_deny" | grep -q '"deny"' && [ -z "$ro_allow" ] && [ -z "$ro_off" ]; then
    ok "guard-readonly.py denies a write, allows a read, and stays silent when the flag is off"
else
    bad "guard-readonly.py misbehaves (deny=$ro_deny allow=$ro_allow off=$ro_off)"
fi

echo
if [ "$fails" -eq 0 ]; then
    echo "preflight clean. safe to schedule: ./deploy/install.sh --enable"
else
    echo "$fails check(s) failed. fix before scheduling."
fi
exit "$fails"
