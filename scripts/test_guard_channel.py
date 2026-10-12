"""Offline checks for .claude/hooks/guard-channel.py.

The hook is run as Claude Code runs it: a JSON tool call on stdin, a throwaway
config/group.toml (AGENT_GROUP_CONFIG) naming the channel, AGENT_RUN_ID set or
not. No Slack, no Claude, no network.

    .venv/bin/python scripts/test_guard_channel.py

Denied, inside a run: a Bash command, a Write, an Edit or a MultiEdit that names
the configured channel and calls chat.postMessage. Allowed: the same outside a
run, a DM to a person, the channel named without a post, the queue command, a
post naming another channel, and tools that are not writes.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parents[1]
HOOK = REPO / ".claude" / "hooks" / "guard-channel.py"
T = pathlib.Path(tempfile.mkdtemp(prefix="guard-channel-"))
CFG = T / "group.toml"
CFG.write_text("""
[company]
name = "Test Group"
reporting_currency = "USD"

[[entities]]
key = "PARENT"
xero_name = "Test Parent Limited"
base_currency = "GBP"

[slack]
channel_id = "CTESTCHAN1"
""")
FAILS: list[str] = []


def fire(tool: str, tool_input: dict, run: bool = True, config: pathlib.Path | None = CFG) -> bool:
    """True when the hook denies the call."""
    env = {k: v for k, v in os.environ.items() if k not in ("AGENT_RUN_ID", "AGENT_GROUP_CONFIG", "SLACK_CHANNEL_ID")}
    if run:
        env["AGENT_RUN_ID"] = "bookkeep-20260101T000000Z-1"
    if config:
        env["AGENT_GROUP_CONFIG"] = str(config)
    r = subprocess.run([sys.executable, str(HOOK)], input=json.dumps({"tool_name": tool, "tool_input": tool_input}),
                       capture_output=True, text=True, env=env, cwd=str(REPO))
    assert r.returncode == 0, r.stderr
    return "deny" in r.stdout


def check(cond: bool, what: str) -> None:
    print(("ok    " if cond else "FAIL  ") + what)
    if not cond:
        FAILS.append(what)


POST = ('curl -s -X POST https://slack.com/api/chat.postMessage -H "Authorization: Bearer $SLACK_BOT_TOKEN" '
        '-d \'{"channel": "CTESTCHAN1", "text": "report"}\'')
check(fire("Bash", {"command": POST}), "a curl post to the channel from Bash is denied")
check(fire("Bash", {"command": "python -c \"web.chat_postMessage(channel='CTESTCHAN1', text='x')\""}),
      "the Python client's chat_postMessage to the channel is denied")
check(fire("Write", {"file_path": "/tmp/post.py", "content": "web.chat_postMessage(channel='CTESTCHAN1', text='x')"}),
      "a script written to post to the channel is denied")
check(fire("Edit", {"file_path": "/tmp/post.py", "old_string": "a", "new_string": "chat.postMessage CTESTCHAN1"}),
      "an Edit that adds a channel post is denied")
check(fire("MultiEdit", {"file_path": "/tmp/post.py",
                         "edits": [{"old_string": "a", "new_string": "b"},
                                   {"old_string": "c", "new_string": "chat.postMessage CTESTCHAN1"}]}),
      "a MultiEdit that adds a channel post is denied")
check(fire("Bash", {"command": "curl https://slack.com/api/chat.scheduleMessage -d channel=CTESTCHAN1"}),
      "a scheduled message to the channel is denied")

check(not fire("Bash", {"command": POST}, run=False), "the same post outside a run is allowed")
check(not fire("Bash", {"command": POST.replace("CTESTCHAN1", "UTESTADM01")}), "a DM to a person is allowed")
check(not fire("Bash", {"command": POST.replace("CTESTCHAN1", "COTHERCHAN")}), "a post to another channel is allowed")
check(not fire("Bash", {"command": "grep CTESTCHAN1 config/group.toml"}), "naming the channel without posting is allowed")
check(not fire("Bash", {"command": '.venv/bin/python scripts/slack_outbox.py channel --headline "TEST RUN (1 Jan)" --thread report.md'}),
      "the queue command is allowed")
check(not fire("Read", {"file_path": "/tmp/post.py"}), "a tool that is not a write is allowed")
check(not fire("Bash", {"command": POST}, config=None), "no configured channel: nothing to guard, and no crash")

import shutil
shutil.rmtree(T, ignore_errors=True)
print()
print("PASS" if not FAILS else f"FAIL ({len(FAILS)})")
sys.exit(1 if FAILS else 0)
