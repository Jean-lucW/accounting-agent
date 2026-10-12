#!/usr/bin/env python3
"""PreToolUse gate: a bookkeeping query is investigated before it is registered.

Every query a bookkeeping run raises goes first to the query investigation
(skill `query-investigation`, runner scripts/investigate_query.py), which
gathers the evidence and returns a certainty that maps to a grade. The run
takes the grade to the auto-resolve gate (`investigate_query.py gate`, the
decision scripts/resolve_gate.py makes from [auto_resolve]) and acts on act and
confirm. This hook is what makes the step unmissable: it refuses

    outstanding.py add --domain bookkeeping --kind queried ...

unless `--investigation` names a result.json that exists and is a failed
investigation, a verdict graded low, a verdict graded high or medium that the
gate said to query, or an investigation deferred by the daily cap whose
scheduled day has not passed (it is registered as scheduled). A verdict graded
high or medium that has not been through the gate is refused: it is run
through resolve_gate first.

A re-raise of a line the register already holds with a certainty passes
without the flag: it was investigated when it was first raised. So does a
re-raise of a line an admin has answered: an answer is never re-investigated.

Inert in a read-only session (nothing there may write the register anyway), for
every other domain, and when [investigation] enabled is false.
scripts/outstanding.py makes the same checks, so a call this hook does not see
is refused there too.

Tested by scripts/test_query_guard.py. Run it after touching this file.
"""
from __future__ import annotations

import datetime
import json
import os
import pathlib
import re
import shlex
import sys

REPO = pathlib.Path(os.environ.get("CLAUDE_PROJECT_DIR") or pathlib.Path(__file__).resolve().parents[2])
REGISTER = REPO / "docs" / "bookkept" / "outstanding.json"

HOW = ("Open the investigation first: `.venv/bin/python scripts/investigate_query.py open "
       "--domain bookkeeping --key <key> --question \"...\" --context \"...\"`, then `wait`, "
       "then `investigate_query.py gate --key <key> --amount <reporting currency>` and do what it "
       "says: act, confirm (report it under to confirm) or query. A verdict graded low, a "
       "failed investigation, a gate that says query or one deferred by the daily cap is "
       "registered with `--investigation <path to result.json>`. "
       "Do not rewrite the command to get around this.")


def _enabled() -> bool:
    """[investigation] enabled in config/group.toml; on when it cannot be read."""
    sys.path.insert(0, str(REPO / "src"))
    try:
        from accounting_agent import config
        return bool(config.investigation()["enabled"])
    except Exception:                                            # noqa: BLE001
        return True


def _opt(tokens: list[str], name: str) -> str | None:
    for n, t in enumerate(tokens):
        if t == name and n + 1 < len(tokens):
            return tokens[n + 1]
        if t.startswith(name + "="):
            return t.split("=", 1)[1]
    return None


def _segments(cmd: str) -> list[list[str]]:
    """Each simple command in the line that runs outstanding.py add."""
    out = []
    for part in re.split(r"&&|\|\||;|\n", cmd.replace("\\\n", " ")):
        if "outstanding.py" not in part:
            continue
        try:
            toks = shlex.split(part)
        except ValueError:
            toks = part.split()
        if "add" in toks:
            out.append(toks)
    return out


def _registered_with_certainty(key: str | None) -> bool:
    if not key:
        return False
    try:
        items = json.loads(REGISTER.read_text())
    except (OSError, ValueError):
        return False
    return any((i.get("key") or "").lower() == key.lower()
               and (i.get("certainty") is not None or i.get("answered")
                    or i.get("investigation_failed"))
               for i in items)


def _grade(v: dict) -> str:
    if v.get("grade") in ("high", "medium", "low"):
        return v["grade"]
    c = v.get("certainty")
    return "high" if c in (4, 5) else "medium" if c == 3 else "low"


def check(cmd: str) -> str | None:
    for toks in _segments(cmd):
        if (_opt(toks, "--domain") or "") != "bookkeeping":
            continue
        if (_opt(toks, "--kind") or "") != "queried":
            continue
        path = _opt(toks, "--investigation")
        if not path:
            if _registered_with_certainty(_opt(toks, "--key")):
                continue
            return "a bookkeeping query cannot be registered before it is investigated."
        p = pathlib.Path(path)
        p = p if p.is_absolute() else REPO / p
        try:
            v = json.loads(p.read_text())
        except (OSError, ValueError):
            return f"--investigation {path} is not a readable result.json."
        if v.get("status") == "failed":
            continue
        if v.get("status") == "deferred":
            # Held back by the daily investigation cap: registered as scheduled,
            # and check-queries fails it the day after if nobody investigated it.
            when = str(v.get("scheduled") or "")
            try:
                ok = datetime.date.fromisoformat(when) >= datetime.date.today()
            except ValueError:
                ok = False
            if ok:
                continue
            return (f"{path} is a deferred investigation whose day ({when or 'none'}) has passed: "
                    "open it again.")
        c = v.get("certainty")
        if v.get("status") != "done" or not isinstance(c, int):
            return f"{path} is not a finished verdict."
        grade = _grade(v)
        if grade != "low" and (v.get("gate") or {}).get("action") != "query":
            return (f"the investigation is graded {grade} (certainty {c}): run it through "
                    "resolve_gate, then act on it or report it under to confirm as the gate says, "
                    "instead of registering it as a question.")
    return None


def main() -> int:
    if os.environ.get("AGENT_READONLY", "").strip().lower() not in ("", "0", "false", "no"):
        return 0
    try:
        payload = json.load(sys.stdin)
    except (ValueError, OSError):
        return 0
    if payload.get("tool_name") != "Bash":
        return 0
    if not _enabled():
        return 0
    why = check((payload.get("tool_input") or {}).get("command") or "")
    if not why:
        return 0
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": f"BLOCKED, query investigation: {why} {HOW}",
    }}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
