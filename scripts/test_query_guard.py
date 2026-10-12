"""Offline checks for the query investigation gate.

No network, no Xero, no model, no register write. Checked:

  1. the PreToolUse hook .claude/hooks/guard-queries.py, run as a real
     subprocess with the real JSON payload shape: a bookkeeping query without
     a verdict, or with a verdict graded high or medium that the gate has not
     said to query, is denied; a low grade, a failed investigation, a gate that
     says query and a deferred one (until its day) are let through
  2. scripts/outstanding.py's own check, `investigation()`, called directly,
     and the `check-queries` exit check's rule
  3. both stand down when [investigation] enabled is false

Run after touching any of those files:

    .venv/bin/python scripts/test_query_guard.py
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import subprocess
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parents[1]
HOOK = REPO / ".claude" / "hooks" / "guard-queries.py"
os.environ["AGENT_GROUP_CONFIG"] = str(REPO / "config" / "group.example.toml")
os.environ.pop("AGENT_READONLY", None)
sys.path.insert(0, str(REPO / "scripts"))
import outstanding  # noqa: E402

tmp = pathlib.Path(tempfile.mkdtemp())
fails = 0
passed = 0


def ok(cond: bool, what: str) -> None:
    global fails, passed
    if cond:
        passed += 1
    else:
        fails += 1
        print(f"FAIL {what}")


def verdict(name: str, **kw) -> str:
    p = tmp / f"{name}.json"
    p.write_text(json.dumps(kw))
    return str(p)


TOMORROW = (dt.date.today() + dt.timedelta(days=1)).isoformat()
LOW = verdict("low", status="done", certainty=2, grade="low", unsure="which FX account")
HIGH = verdict("high", status="done", certainty=4, grade="high")
MEDIUM = verdict("medium", status="done", certainty=3, grade="medium")
HIGH_ACT = verdict("high-act", status="done", certainty=5, grade="high", gate={"action": "act"})
HIGH_QUERY = verdict("high-query", status="done", certainty=5, grade="high", gate={"action": "query"})
MEDIUM_CONFIRM = verdict("medium-confirm", status="done", certainty=3, grade="medium",
                         gate={"action": "confirm"})
FAILED = verdict("failed", status="failed", reason="timed out")
RUNNING = verdict("running", status="queued")
DEFERRED = verdict("deferred", status="deferred", scheduled=TOMORROW, reason="daily cap")
STALE = verdict("stale", status="deferred", scheduled="2000-01-01", reason="daily cap")

ADD = (".venv/bin/python scripts/outstanding.py add --domain bookkeeping --kind queried "
       "--with admin --key k1 --text 'which entity'")


def hook(cmd: str, env: dict | None = None) -> bool:
    """True when the hook denies."""
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}})
    e = {k: v for k, v in os.environ.items() if k != "AGENT_READONLY"}
    e.update(env or {})
    out = subprocess.run([sys.executable, str(HOOK)], input=payload, text=True,
                         capture_output=True, env=e, check=True).stdout
    return bool(out.strip()) and json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny"


# ---------------------------------------------------------------- 1. the hook

DENY = [
    ADD,
    f"{ADD} --investigation {HIGH}",
    f"{ADD} --investigation {MEDIUM}",
    f"{ADD} --investigation {HIGH_ACT}",
    f"{ADD} --investigation {MEDIUM_CONFIRM}",
    f"{ADD} --investigation {RUNNING}",
    f"{ADD} --investigation {STALE}",
    f"{ADD} --investigation /nonexistent/result.json",
    f"cd /x && {ADD} --defer",
    ADD.replace("--kind queried", "--kind=queried"),
]
ALLOW = [
    f"{ADD} --investigation {LOW}",
    f"{ADD} --investigation={LOW}",
    f"{ADD} --investigation {HIGH_QUERY}",
    f"{ADD} --investigation {FAILED}",
    f"{ADD} --investigation {DEFERRED}",
    ".venv/bin/python scripts/outstanding.py update --key k1 --schedule-investigation " + TOMORROW,
    ADD.replace("--kind queried", "--kind manual"),
    ADD.replace("--domain bookkeeping", "--domain tax-returns"),
    ".venv/bin/python scripts/outstanding.py close --key k1 --reason done",
    ".venv/bin/python scripts/outstanding.py list --kind queried",
    "echo nothing to do with the register",
]
for cmd in DENY:
    ok(hook(cmd), f"hook should deny: {cmd}")
for cmd in ALLOW:
    ok(not hook(cmd), f"hook should allow: {cmd}")
ok(not hook(ADD, {"AGENT_READONLY": "1"}), "the hook should stand down in a read-only session")
ok("resolve_gate" in subprocess.run(
    [sys.executable, str(HOOK)],
    input=json.dumps({"tool_name": "Bash", "tool_input": {"command": f"{ADD} --investigation {HIGH}"}}),
    text=True, capture_output=True, env={k: v for k, v in os.environ.items()}).stdout,
   "a high verdict should be refused with the pointer to resolve_gate")

# ---------------------------------------------------------------- 2. outstanding.py


def args(**kw):
    base = dict(domain="bookkeeping", kind="queried", investigation="")
    base.update(kw)
    return argparse.Namespace(**base)


def refused(a, existing=None) -> bool:
    try:
        outstanding.investigation(a, existing)
        return False
    except SystemExit:
        return True


checks = [
    (refused(args()), True, "no verdict"),
    (refused(args(investigation=HIGH)), True, "graded high, not gated"),
    (refused(args(investigation=MEDIUM)), True, "graded medium, not gated"),
    (refused(args(investigation=HIGH_ACT)), True, "graded high, the gate said act"),
    (refused(args(investigation=MEDIUM_CONFIRM)), True, "graded medium, the gate said confirm"),
    (refused(args(investigation=HIGH_QUERY)), False, "graded high, the gate said query"),
    (refused(args(investigation=RUNNING)), True, "unfinished verdict"),
    (refused(args(investigation=LOW)), False, "graded low"),
    (refused(args(investigation=FAILED)), False, "failed investigation"),
    (refused(args(investigation=STALE)), True, "a deferral whose day has passed"),
    (refused(args(), {"certainty": 2}), False, "re-raise of an investigated line"),
    (refused(args(), {"answered": "post it to 431"}), False, "re-raise of an answered line"),
    (refused(args(), {"investigation_failed": "timed out"}), False, "re-raise of a failed line"),
    (refused(args(kind="manual")), False, "a manual item"),
    (refused(args(domain="tax-returns")), False, "another domain"),
]
for got, want, what in checks:
    ok(got == want, f"outstanding.investigation, {what}: refused={got}, expected {want}")
low = outstanding.investigation(args(investigation=LOW), None)
ok(low.get("certainty") == 2 and low.get("grade") == "low", "the certainty and grade are stored on the line")
hq = outstanding.investigation(args(investigation=HIGH_QUERY), None)
ok(hq.get("grade") == "high" and hq.get("gate") == "query", "a gated high verdict stores its grade and gate")
dd = outstanding.investigation(args(investigation=DEFERRED), None) or {}
ok(dd.get("investigation_scheduled") == TOMORROW, "a deferred verdict is stored as scheduled")

# the exit check: only investigated queries the agent could not settle may stay
reg = [
    {"id": "A", "domain": "bookkeeping", "kind": "queried", "certainty": 2, "grade": "low"},
    {"id": "B", "domain": "bookkeeping", "kind": "queried", "investigation_failed": "x"},
    {"id": "C", "domain": "bookkeeping", "kind": "queried", "answered": "yes"},
    {"id": "D", "domain": "bookkeeping", "kind": "queried"},
    {"id": "E", "domain": "bookkeeping", "kind": "queried", "certainty": 4, "grade": "high"},
    {"id": "F", "domain": "bookkeeping", "kind": "manual"},
    {"id": "G", "domain": "tax-returns", "kind": "queried"},
    {"id": "H", "domain": "bookkeeping", "kind": "queried", "investigation_scheduled": "2999-01-01"},
    {"id": "I", "domain": "bookkeeping", "kind": "queried", "investigation_scheduled": "2000-01-01"},
    {"id": "J", "domain": "bookkeeping", "kind": "queried", "certainty": 5, "grade": "high", "gate": "query"},
    {"id": "K", "domain": "bookkeeping", "kind": "queried", "certainty": 3, "grade": "medium"},
]
ok(sorted(i["id"] for i, _ in outstanding.unsolved_breaches(reg)) == ["D", "E", "I", "K"],
   "check-queries flags the wrong lines")
ok(outstanding.kind_label("bookkeeping", "queried", "queried") == "queried - could not answer"
   and outstanding.kind_label("tax-returns", "queried", "queried") == "queried - not investigated"
   and outstanding.kind_label("bookkeeping", "manual", "manual items") == "manual items",
   "the queried heading says whether the agent could answer")
ok("investigated, certainty 2" in outstanding.line(
    {"text": "t", "with": "admin", "raised": "2026-01-02", "certainty": 2}),
   "a line says it was investigated and at what certainty")

# ---------------------------------------------------------------- 3. switched off

OFF = tmp / "off.toml"
OFF.write_text((REPO / "config" / "group.example.toml").read_text()
               .replace("enabled = true", "enabled = false", 1))
off_env = {"AGENT_GROUP_CONFIG": str(OFF)}
ok(not hook(ADD, off_env), "the hook should stand down when [investigation] is switched off")
ok(hook(ADD), "and be live with it on")

total = passed + fails
print(f"{passed}/{total} passed")
sys.exit(1 if fails else 0)
