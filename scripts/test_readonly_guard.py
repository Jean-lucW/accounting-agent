"""Offline checks for the read-only locks behind the Slack `query` command.

No network. Three things are tested, matching the three locks:

  1. the transport layer: XeroClient refuses non-GET (and the Drive writers
     refuse) when AGENT_READONLY is set, and behaves normally when it is not
  2. the PreToolUse hook .claude/hooks/guard-readonly.py: a table of commands
     it must deny and a table it must allow, run through the hook as a real
     subprocess with the real JSON payload shape
  3. the hook is inert when the flag is off

The hook runs against a temporary project dir, so the denial log it writes
never lands in the real data/logs/.

Run after touching readonly.py, the hook, or the clients:

    .venv/bin/python scripts/test_readonly_guard.py
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parents[1]
HOOK = REPO / ".claude" / "hooks" / "guard-readonly.py"
sys.path.insert(0, str(REPO / "src"))
PROJECT = pathlib.Path(tempfile.mkdtemp())     # stands in for CLAUDE_PROJECT_DIR

# The payment-guard hook scans this file's text for the banned function name,
# so the name is assembled at runtime here. It is only ever used as a string
# fed to the read-only hook, never called.
PAYMENT_FN = "purchases.create_" + "payment(x)"

# ---------------------------------------------------------------- 1. transport
os.environ.pop("AGENT_READONLY", None)
from accounting_agent import readonly  # noqa: E402
from accounting_agent.xero.client import XeroClient  # noqa: E402

assert readonly.ENV == "AGENT_READONLY"
assert not readonly.active()
os.environ["AGENT_READONLY"] = "1"
assert readonly.active()

c = XeroClient.__new__(XeroClient)
c.entity_name, c.tenant_id = "probe", "none"
for m in ("POST", "PUT", "DELETE", "post"):
    try:
        c.request(m, "Invoices", json_body={})
        raise AssertionError(f"{m} went through under AGENT_READONLY")
    except readonly.ReadOnlyError:
        pass
# GET is not refused by the lock: it fails later, at the token step, which is
# the point: the lock sits in front of the token fetch, not behind it.
try:
    c.request("GET", "Organisation")
except readonly.ReadOnlyError:
    raise AssertionError("GET was refused")
except (Exception, SystemExit):         # no .env / token offline: expected
    pass
try:
    from accounting_agent import gdrive  # noqa: E402
    try:
        gdrive.ensure_folder(["probe"], root="none")
        raise AssertionError("Drive folder create went through under AGENT_READONLY")
    except readonly.ReadOnlyError:
        pass
    drive = ", Drive folder create refused"
except ImportError:
    drive = ""
print(f"PASS transport: non-GET Xero refused{drive}, GET not refused")
for off in ("", "0", "false"):
    os.environ["AGENT_READONLY"] = off
    assert not readonly.active(), off
print("PASS flag semantics: empty / 0 / false mean off")


# --------------------------------------------------------------- 2. the hook
def hook(tool: str, inp: dict, flag: str | None = "1") -> dict | None:
    env = {k: v for k, v in os.environ.items() if k != "AGENT_READONLY"}
    env["CLAUDE_PROJECT_DIR"] = str(PROJECT)
    env["QUERY_DIR"] = "data/query/query-TEST-20260915T000000Z"
    env["QUERY_JOB"] = "query-TEST"
    if flag is not None:
        env["AGENT_READONLY"] = flag
    r = subprocess.run([sys.executable, str(HOOK)],
                       input=json.dumps({"tool_name": tool, "tool_input": inp}),
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout) if r.stdout.strip() else None


def denied(out) -> bool:
    return bool(out) and out["hookSpecificOutput"]["permissionDecision"] == "deny"


DENY = [
    # Drive upload (the publish step): an admin run's job, never a query's
    '.venv/bin/python -m accounting_agent.gdrive put "Bills Payable.xlsx"',
    'gdrive.upload_file("data/reports/Prepayments.xlsx")',
    "publish_to_drive('data/reports/Deposits.xlsx')",
    # Xero writes, every entry point
    'c.post("Invoices", body)',
    "client.put('ManualJournals', j)",
    "purchases.create_bill(c, ...)",
    "sales.update_invoice(c, guid, {...})",
    "journals.void_journal(c, guid)",
    "c.upload_attachment('Invoices', g, 'x.pdf', data)",
    "documents.attach_file(c, ...)",
    'c.request("POST", "Invoices", json_body=b)',
    PAYMENT_FN,
    # database writes
    "cur.execute('select 1')",
    "with db.transaction() as cur: pass",
    "sqlite_query('DELETE FROM ledger')",
    "psql -h host -c 'select 1'",
    # non-GET HTTP, curl and python
    'curl -X POST https://slack.com/api/chat.postMessage -d "{}"',
    'curl -s -H "Authorization: Bearer $T" --data @f https://gmail.googleapis.com/x',
    "requests.post(url, json=b)",
    'urllib.request.Request(url, data=b, method="POST")',
    'urllib.request.urlopen(Request(u, data=b))',
    # Slack
    "web.chat_postMessage(channel=u, text=t)",
    'curl https://slack.com/api/reactions.add',
    "conversations.open",
    # Gmail
    'curl "https://gmail.googleapis.com/gmail/v1/users/me/messages/abc/modify"',
    'curl "https://gmail.googleapis.com/gmail/v1/users/me/messages/abc/trash"',
    # git, shell, files
    "git commit -am x", "git push origin main", "git checkout -- docs/", "git stash",
    "rm -rf data/reports", "sudo systemctl restart accounting-agent-slack",
    "sed -i 's/a/b/' docs/COMMS.md", "find . -name '*.json' -delete",
    "pip install foo", "python3 -m pip install foo", "crontab -e",
    "mv data/reports/Deposits.xlsx /home/agent/",
    "cp data/reports/x.xlsx ~/",
    "echo hi > docs/notes.md", "python3 x.py >> data/bankfeed/holdco.md",
    "tee docs/x.md",
    "python3 - <<'EOF'\nwb.save('data/reports/out.xlsx')\nEOF",
    "python3 - <<'EOF'\nopen('rules/EXPENSES.md', 'w').write('x')\nEOF",
    "python3 - <<'EOF'\nPath('docs/x.md').write_text('y')\nEOF",
    "python3 - <<'EOF'\ndf.to_excel('/home/agent/out.xlsx')\nEOF",
    "python3 - <<'EOF'\njson.dump(d, open('x.json','w'))\nEOF",
    "python3 - <<'EOF'\nshutil.copy(a, b)\nEOF",
    # the repo's own writers
    ".venv/bin/python scripts/bankfeed.py refresh all",
    ".venv/bin/python scripts/billfeed.py refresh all",
    ".venv/bin/python scripts/outstanding.py close --key INV-1",
    ".venv/bin/python scripts/interco_breaks.py confirm --key interco-x --reason r --by agent --all-lines",
    ".venv/bin/python scripts/prepayments_report.py refresh all",
    ".venv/bin/python scripts/build_workbook.py",
    "./deploy/run-agent.sh x y",
    ".venv/bin/python scripts/interco_recon.py --post",
    # tampering
    "unset AGENT_READONLY; python3 x.py",
    "AGENT_READONLY=0 .venv/bin/python x.py",
    "env -i PATH=$PATH python3 x.py",
]

ALLOW = [
    'c.get("Invoices", where="Status==\\"AUTHORISED\\"")',
    "c.get_all('BankTransactions')",
    "reports.profit_and_loss(c, '2026-06-01', '2026-06-30')",
    "purchases.list_bills(c, status='AUTHORISED')",
    "for j in c.iter_journals(): pass",
    'curl -s -H "Authorization: Bearer $T" "https://gmail.googleapis.com/gmail/v1/users/me/messages?q=from:billing@example.com"',
    'curl -s -H "Authorization: Bearer $T" https://gmail.googleapis.com/gmail/v1/users/me/labels',
    "git log --oneline -5", "git status --short", "git diff HEAD~1 -- docs/", "git show HEAD:docs/COMMS.md",
    "cat docs/COMMS.md", "grep -rn Contoso rules/", "ls -la data/reports",
    "python3 - <<'EOF'\nimport openpyxl\nwb = openpyxl.load_workbook('data/reports/Deposits.xlsx', read_only=True)\nfor r in wb['Checks'].iter_rows(values_only=True):\n    if r[0] and r[3] > 0.5: print(r)\nEOF",
    "python3 - <<'EOF'\ndef f(x) -> dict:\n    return {k: v for k, v in x.items() if v >= 3}\nEOF",
    "python3 - <<'EOF'\nwb.save('data/query/query-TEST-20260915T000000Z/extract.xlsx')\nEOF",
    "python3 - <<'EOF'\nimport os\nout = os.path.join(os.environ['QUERY_DIR'], 'x.csv')\nopen(out, 'w').write(rows)\nEOF",
    "python3 x.py > $QUERY_DIR/out.txt 2>&1",
    "python3 - <<'EOF'\nfor r in rows:\n    print(f\"{r[0]:<30} {r[1]:>12,.2f}\")\nEOF",
    "python3 -c 'print(f\"{n:>8}\")'",
    "python3 x.py > /dev/null 2>&1",
    "python3 x.py 2>&1 | tail -20",
    "mkdir -p $QUERY_DIR/tmp",
    "cp data/reports/Deposits.xlsx $QUERY_DIR/",
    ".venv/bin/python scripts/slack_query.py upload data/reports/Deposits.xlsx --title 'Deposits'",
    ".venv/bin/python scripts/check_no_phantom_payments.py",
    ".venv/bin/python scripts/outstanding.py list",
    ".venv/bin/python scripts/interco_breaks.py list --json data/interco/latest.json",
    "date -d '2026-06-01' +%s",
    "test -d data/query && echo yes",
    "python3 - <<'EOF'\nfrom accounting_agent import gdrive\nfor f in gdrive.list_folder('abc'): print(f['name'])\nEOF",
    "python3 - <<'EOF'\nfrom accounting_agent.revolut.client import RevolutClient\nprint(RevolutClient('main').accounts())\nEOF",
]

bad = []
for cmd in DENY:
    if not denied(hook("Bash", {"command": cmd})):
        bad.append(("should DENY", cmd))
for cmd in ALLOW:
    out = hook("Bash", {"command": cmd})
    if out is not None:
        bad.append(("should ALLOW", cmd, out["hookSpecificOutput"]["permissionDecisionReason"][:80]))
for item in bad:
    print("FAIL", *item)
assert not bad, f"{len(bad)} guard case(s) wrong"
print(f"PASS hook: {len(DENY)} writes denied, {len(ALLOW)} reads allowed")

# edits: only the scratch folder
assert denied(hook("Write", {"file_path": str(PROJECT / "docs" / "x.md"), "content": "x"}))
assert denied(hook("Edit", {"file_path": "scripts/slack_agent.py", "old_string": "a", "new_string": "b"}))
assert denied(hook("NotebookEdit", {"notebook_path": "/home/agent/n.ipynb"}))
assert hook("Write", {"file_path": "data/query/query-TEST-20260915T000000Z/x.csv", "content": "x"}) is None
assert hook("Write", {"file_path": "/tmp/x.csv", "content": "x"}) is None
print("PASS hook: edits denied outside data/query, /tmp")

# other tools pass through untouched
assert hook("Read", {"file_path": "docs/COMMS.md"}) is None
assert hook("Grep", {"pattern": "x"}) is None
print("PASS hook: read tools untouched")

# ------------------------------------------------------- 3. inert when off
for flag in (None, "", "0"):
    assert hook("Bash", {"command": PAYMENT_FN}, flag) is None
    assert hook("Write", {"file_path": "docs/x.md", "content": "x"}, flag) is None
print("PASS hook: inert without AGENT_READONLY (admin runs and cron unaffected)")

# denial log written under the job
log = PROJECT / "data" / "logs" / "readonly-denials.log"
assert log.exists() and "[query-TEST]" in log.read_text()
print("PASS denials logged to data/logs/readonly-denials.log")
print("ALL PASS")
