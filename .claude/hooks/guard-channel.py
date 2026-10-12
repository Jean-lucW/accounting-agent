#!/usr/bin/env python3
"""PreToolUse guard: inside a run, nothing goes to the channel directly.

Active only while AGENT_RUN_ID is set (deploy/run-agent.sh, run-thread.sh and
run-consensus.sh set it for every run session). Otherwise it exits 0 at once.

A run's headline in the channel carries the run's cost, and the cost is known
only once the session has ended. So a run session queues every channel post
with scripts/slack_outbox.py and the runner sends the queue afterwards. This
denies the direct route: a Bash command, or a script being written, that names
the configured channel (config/group.toml [slack] channel_id) and calls
chat.postMessage. DMs to people are untouched.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

POST = re.compile(r"chat[._]postMessage|chat[._]scheduleMessage|chat_post_message")
TOOLS = {"Bash", "Write", "Edit", "MultiEdit"}

WHY = ("BLOCKED: inside a run nothing is posted to the report channel "
       "directly, because the headline has to carry the run's cost and the cost "
       "is known only when the session ends. Queue it instead and the runner "
       "posts it with the cost: "
       "`.venv/bin/python scripts/slack_outbox.py channel --headline \"<headline>\" "
       "--thread <file>` for a report or the manual items, "
       "`.venv/bin/python scripts/slack_outbox.py channel --top-level <file>` for "
       "the outstanding-items list. Write the headline without any cost. Do not "
       "rewrite the call to get around this.")


def channel_id() -> str:
    """The configured channel; "" when there is none (nothing to guard)."""
    root = Path(os.environ.get("CLAUDE_PROJECT_DIR") or Path(__file__).resolve().parents[2])
    sys.path.insert(0, str(root / "src"))
    try:
        from accounting_agent import config
        return config.slack().channel_id
    except Exception:                                            # noqa: BLE001
        return os.environ.get("SLACK_CHANNEL_ID", "")


def main() -> int:
    if not os.environ.get("AGENT_RUN_ID", "").strip():
        return 0
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return 0
    if payload.get("tool_name") not in TOOLS:
        return 0
    ti = payload.get("tool_input") or {}
    text = " ".join(str(ti.get(k) or "") for k in ("command", "content", "new_string"))
    for e in ti.get("edits") or []:
        text += " " + str((e or {}).get("new_string") or "")
    channel = channel_id()
    if channel and channel in text and POST.search(text):
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": WHY}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
