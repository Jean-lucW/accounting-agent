"""Offline checks for deploy/run-thread.sh: which session each turn runs in.

No Slack, no Claude, no network. A copy of the runner runs in a temp tree
against a stub `claude` that answers from a script of behaviours, and stubs
for every script the runner calls after a turn (reply, register, write-back,
phantom check, the outbox). scripts/thread_session.py and scripts/run_cost.py
are the real ones. Run it after touching either, on the laptop or the server:

    .venv/bin/python scripts/test_run_thread.py

Covers: a first turn, a normal resume, a rotation by size, a rotation by
idle, a pending confirmation carried whole into the handover, a resume that
ran and failed (kept), a resume refused as unknown (new session), the cost
bracket across a rotation, the effort, turn cap and cache TTL every turn is
given, a turn that hits its cap (reported, registered, closed by the next
turn that finishes), and the bookkeeping reminder standing down only on a
resumed turn of its own session, a read-only or focused run.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
T = Path(tempfile.mkdtemp(prefix="run-thread-"))
HOME_DIR = T / "repo"
BIN = T / "bin"
KEY = "1790000000-000100"
JOB = f"run-UTESTADM01-{KEY}"
STATE = HOME_DIR / "data/slack/threads" / f"{KEY}.json"
INBOX = HOME_DIR / "data/slack/threads" / f"{KEY}.inbox.md"
TRANSCRIPTS = T / "transcripts"
SCRIPT = T / "stub_script.json"
CALLS = T / "calls.jsonl"
REGISTER = T / "register.jsonl"
REPLIES = T / "replies.jsonl"


def _exe(p: Path, body: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body)
    p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def setup() -> None:
    for d in ("scripts", "deploy", "data/slack/threads", "data/logs", ".venv/bin"):
        (HOME_DIR / d).mkdir(parents=True, exist_ok=True)
    os.symlink(sys.executable, HOME_DIR / ".venv/bin/python")
    (HOME_DIR / ".env").write_text("CLAUDE_CODE_OAUTH_TOKEN=stub\n")
    for f in ("scripts/thread_session.py", "scripts/run_cost.py", "deploy/run-thread.sh"):
        shutil.copy(REPO / f, HOME_DIR / f)
    (HOME_DIR / "scripts/slack_outbox.py").write_text("def flush(run_id, bracket, log=print):\n    return []\n")
    (HOME_DIR / "scripts/slack_query.py").write_text(
        "import json, os, sys\n"
        f"open({str(REPLIES)!r}, 'a').write(json.dumps({{'text': open(sys.argv[2]).read()}}) + '\\n')\n")
    (HOME_DIR / "scripts/outstanding.py").write_text(
        "import json, sys\n"
        f"open({str(REGISTER)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n")
    (HOME_DIR / "scripts/check_no_phantom_payments.py").write_text("print('PASS')\n")
    _exe(HOME_DIR / "deploy/commit-writeback.sh", "#!/usr/bin/env bash\nexit 0\n")
    _exe(BIN / "flock", "#!/usr/bin/env bash\nexit 0\n")
    # The stub CLI: takes the next behaviour from the script, records its
    # argv and the hook variable, keeps each session's cumulative cost, and
    # touches a transcript per session the way the real CLI does.
    _exe(T / "home/.local/bin/claude", f"""#!{sys.executable}
import json, os, sys, uuid
from pathlib import Path
args = sys.argv[1:]
prompt = args[args.index("-p") + 1]
resume = args[args.index("--resume") + 1] if "--resume" in args else None
script = json.loads(Path({str(SCRIPT)!r}).read_text())
step = script.pop(0) if script else {{"mode": "ok"}}
Path({str(SCRIPT)!r}).write_text(json.dumps(script))
with open({str(CALLS)!r}, "a") as fh:
    fh.write(json.dumps({{"resume": resume, "prompt": prompt, "model": args[args.index("--model") + 1],
                          "effort": args[args.index("--effort") + 1] if "--effort" in args else None,
                          "max_turns": args[args.index("--max-turns") + 1] if "--max-turns" in args else None,
                          "format": args[args.index("--output-format") + 1] if "--output-format" in args else None,
                          "cache_ttl": os.environ.get("CLAUDE_CODE_PROMPT_CACHE_TTL"),
                          "run_id": os.environ.get("AGENT_RUN_ID"),
                          "resumed_env": os.environ.get("AGENT_THREAD_RESUMED")}}) + "\\n")
mode = step.get("mode", "ok")
if mode == "unknown":
    print(f"No conversation found with session ID: {{resume}}", file=sys.stderr)
    sys.exit(1)
costs_p = Path({str(T)!r}) / "stub_costs.json"
costs = json.loads(costs_p.read_text()) if costs_p.exists() else {{}}
sid = resume or step.get("sid") or str(uuid.uuid4())
costs[sid] = costs.get(sid, 0.0) + step.get("cost", 0.10)
costs_p.write_text(json.dumps(costs))
Path({str(TRANSCRIPTS)!r}).mkdir(exist_ok=True)
(Path({str(TRANSCRIPTS)!r}) / f"{{sid}}.jsonl").touch()
ctx = step.get("ctx", 50000)
out = {{"session_id": sid, "num_turns": 4, "total_cost_usd": costs[sid],
        "usage": {{"iterations": [{{"input_tokens": 2, "cache_read_input_tokens": ctx - 1002,
                                    "cache_creation_input_tokens": 0, "output_tokens": 1000}}]}}}}
if mode == "cap":
    out.update({{"is_error": True, "subtype": "error_max_turns", "terminal_reason": "max_turns"}})
    print(json.dumps(out)); sys.exit(1)
if mode == "fail-ran":
    out.update({{"is_error": True, "subtype": "error_during_execution"}})
    print(json.dumps(out)); sys.exit(1)
out.update({{"subtype": "success", "result": step.get("result", "done · reply " + str(len(script)))}})
print(json.dumps(out))
""")


def run(prompt: str = "", script: list | None = None, env: dict | None = None) -> None:
    SCRIPT.write_text(json.dumps(script or []))
    e = {**os.environ, "AGENT_HOME": str(HOME_DIR), "HOME": str(T / "home"),
         "PATH": f"{BIN}:{os.environ['PATH']}", "THREAD_TRANSCRIPTS": str(TRANSCRIPTS),
         "CLAUDE_CODE_PROMPT_CACHE_TTL": "1h", **(env or {})}
    e.pop("AGENT_THREAD_RESUMED", None)
    r = subprocess.run(["bash", str(HOME_DIR / "deploy/run-thread.sh"), JOB, "UTESTADM01",
                        "D1", "1790000000.000100", KEY, prompt], env=e,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode in (0, 1), (r.returncode, r.stderr[-2000:])


def calls() -> list[dict]:
    return [json.loads(l) for l in CALLS.read_text().splitlines()] if CALLS.exists() else []


def replies() -> list[str]:
    return [json.loads(l)["text"] for l in REPLIES.read_text().splitlines()] if REPLIES.exists() else []


def register() -> list[list[str]]:
    return [json.loads(l) for l in REGISTER.read_text().splitlines()] if REGISTER.exists() else []


def state() -> dict:
    return json.loads(STATE.read_text())


def say(text: str) -> None:
    with INBOX.open("a") as fh:
        fh.write(f"- (2026-01-01T10:00:00Z) [Avery Admin, admin, UTESTADM01] {text}\n")


setup()
RULES = "HOW TO WRITE. docs/COMMS.md is a HARD RULE and it governs every message.\nRULES-BODY-MARKER"
FIRST = "check the tax split on the Parent bills\n\n" + RULES
STATE.write_text(json.dumps({"user_id": "UTESTADM01", "channel": "D1", "thread_ts": "1790000000.000100",
                             "job": JOB, "task": "check the tax split", "kind": "focused",
                             "started": "2026-01-01T09:00:00Z", "turns": 0}))
(STATE.with_name(f"{KEY}.brief.json")).write_text(json.dumps(
    {"task": "check the tax split on the Parent bills", "rules": RULES, "context": ""}))

# 1. the first turn: a new session from the listener's prompt, recorded
run(FIRST, [{"sid": "S1", "cost": 1.00, "ctx": 60000, "result": "tax · 3 bills checked"}])
c = calls()
assert len(c) == 1 and c[0]["resume"] is None and c[0]["prompt"].startswith("check the tax split"), c
assert c[0]["model"] == "opus", c[0]["model"]
assert state()["session_id"] == "S1" and state()["context_tokens"] == 60000, state()
assert "(total cost $1.00, run $1.00" in replies()[-1], replies()[-1]
hist = [json.loads(l) for l in STATE.with_name(f"{KEY}.history.jsonl").read_text().splitlines()]
assert hist[0]["message"] == "check the tax split on the Parent bills", hist[0]
print("PASS first turn starts a session and records the request and reply")

# 2. a normal resume: under both lines, the cache warm
say("also check the hotel one")
run(script=[{"cost": 0.50, "ctx": 90000, "result": "hotel · coded 4123"}])
c = calls()[-1]
assert c["resume"] == "S1" and c["resumed_env"] == "S1", c
assert c["prompt"].startswith("FOLLOW-UP in this thread") and "hotel" in c["prompt"], c["prompt"]
assert "RULES-BODY-MARKER" not in c["prompt"], "a resumed turn re-sent the rules"
assert "(total cost $0.50, run $0.50" in replies()[-1], replies()[-1]
print("PASS a follow-up resumes the session with the words alone; cost is the turn's share")

# 3. rotation by idle: past the cache TTL with 100k+ of context
st = state(); st.update(context_tokens=150000, last_end=int(time.time()) - 4000); STATE.write_text(json.dumps(st))
say("and the train receipt")
run(script=[{"sid": "S2", "cost": 0.40, "ctx": 70000, "result": "train · 1 bill"}])
c = calls()[-1]
assert c["resume"] is None and c["resumed_env"] is None, c
p = c["prompt"]
assert p.startswith("CONTINUATION OF A SLACK THREAD"), p[:200]
assert "prompt cache had expired" in p and "RULES-BODY-MARKER" in p, "no reason or no rules"
assert "hotel · coded 4123" in p and "train" in p, "last reply or new message missing"
assert str(TRANSCRIPTS / "S1.jsonl") in p, "no transcript path"
st = state()
assert st["session_id"] == "S2" and st["previous_sessions"] == ["S1"] and st["rotations"] == 1, st
assert "(total cost $0.40, run $0.40" in replies()[-1], replies()[-1]
print("PASS idle past the TTL with 150k of context: a new session from the handover, cost right")

# 3b. idle but small: resume, writing 70k again is cheaper than starting over
st = state(); st.update(last_end=int(time.time()) - 4000); STATE.write_text(json.dumps(st))
say("thanks, one more: the taxi")
run(script=[{"cost": 0.30, "ctx": 80000, "result": "taxi · fine"}])
c = calls()[-1]
assert c["resume"] == "S2", c
assert "(total cost $0.30, run $0.30" in replies()[-1], replies()[-1]
print("PASS idle with 70k of context resumes")

# 4. rotation by size, carrying a pending confirmation whole
proposal = "proposed · 2 journals\n" + "\n".join(f"• line {i} DR Travel (400) CR Suspense (800) USD {i}.00" for i in range(200)) + "\nEND-OF-PROPOSAL"
say("propose the reclass journals")
run(script=[{"cost": 0.20, "ctx": 320000, "result": proposal}])
assert state()["context_tokens"] == 320000
say("yes, post them")
run(script=[{"sid": "S3", "cost": 0.60, "ctx": 75000, "result": "posted · 2 journals"}])
c = calls()[-1]
assert c["resume"] is None and c["prompt"].startswith("CONTINUATION"), c["prompt"][:100]
assert "context had grown past" in c["prompt"]
assert proposal in c["prompt"], "the pending proposal was not carried whole"
assert c["prompt"].rstrip().endswith("yes, post them"), c["prompt"][-200:]
assert len(c["prompt"]) - len(proposal) < 22000, len(c["prompt"])
st = state()
assert st["session_id"] == "S3" and st["previous_sessions"] == ["S1", "S2"], st
assert "(total cost $0.60, run $0.60" in replies()[-1], replies()[-1]
print(f"PASS past 300k: a new session; the proposal and the yes go across whole "
      f"({len(c['prompt']) - len(proposal)} characters of handover besides the proposal)")

# 5. a resume that ran and failed part way: kept, never restarted
n = len(calls())
say("now the hotel booking ones")
run(script=[{"mode": "fail-ran", "cost": 0.25, "ctx": 90000}])
assert len(calls()) == n + 1, "a failed resume that ran was started again"
assert state()["session_id"] == "S3", state()
assert "reply continue in this thread" in replies()[-1], replies()[-1]
print("PASS a resumed turn that ran and failed keeps its session for continue")

# 6. a resume refused as unknown: never started, so a new session takes it
n = len(calls())
say("continue")
run(script=[{"mode": "unknown"}, {"sid": "S4", "cost": 0.35, "ctx": 72000, "result": "booking · 3 bills"}])
c = calls()[n:]
assert len(c) == 2 and c[0]["resume"] == "S3" and c[1]["resume"] is None, c
assert c[1]["prompt"].startswith("CONTINUATION") and "could not be resumed" in c[1]["prompt"], c[1]["prompt"][:300]
assert c[1]["resumed_env"] is None
assert state()["session_id"] == "S4", state()
assert replies()[-1].startswith("booking · 3 bills") and "run $0.35" in replies()[-1], replies()[-1]
print("PASS a resume refused as unknown continues in a new session with the record")

# 7. no session at all on a follow-up (a consensus thread): the record, not the bare text
st = state(); st.pop("session_id"); STATE.write_text(json.dumps(st))
say("what did the consensus decide")
run(script=[{"sid": "S5", "ctx": 60000, "result": "consensus · 2 agreed"}])
assert calls()[-1]["prompt"].startswith("CONTINUATION") and "no session to resume" in calls()[-1]["prompt"]
print("PASS a follow-up with no session gets the thread's rules and record")

# 7b. every turn gets an effort, a turn cap, a one-hour cache and a run id
c = calls()[-1]
assert c["effort"] == "medium" and c["max_turns"] == "500" and c["cache_ttl"] == "1h", c
assert c["format"] == "json" and c["run_id"], c
say("and again")
run(script=[{"ctx": 60000}], env={"AGENT_THREAD_EFFORT": "low", "AGENT_THREAD_MAX_TURNS": "7"})
c = calls()[-1]
assert c["effort"] == "low" and c["max_turns"] == "7", c
print("PASS effort, turn cap and cache TTL are set on every turn and overridable")

# 7c. a turn that meets its cap: reported, registered once, closed by the next turn that finishes
n = len(calls())
say("do the whole review")
run(script=[{"mode": "cap", "ctx": 60000}])
assert len(calls()) == n + 1, "a capped turn was started again"
assert "stopped at its turn cap" in replies()[-1] and "continue" in replies()[-1], replies()[-1]
adds = [r for r in register() if r[0] == "add"]
assert adds and "turn-cap-" + KEY in adds[-1], register()
say("continue")
run(script=[{"ctx": 61000, "result": "review · done"}])
assert register()[-1][0] == "close" and "turn-cap-" + KEY in register()[-1], register()
print("PASS a turn at its cap is reported and registered, and the next finished turn closes it")

# 8. the bookkeeping reminder: stands down only on a resumed turn of its own session
hook = json.loads((REPO / ".claude/settings.json").read_text())["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]


def fire(sid: str, prompt: str, **env) -> bool:
    e = {k: v for k, v in os.environ.items()
         if k not in ("AGENT_THREAD_RESUMED", "AGENT_INVESTIGATION", "AGENT_READONLY", "AGENT_RUN")}
    e["AGENT_RUN"] = "1"
    e.update(env)
    r = subprocess.run(["sh", "-c", hook], input=json.dumps({"session_id": sid, "prompt": prompt}),
                       capture_output=True, text=True, env=e)
    return "BOOKKEEPING RUN DETECTED" in r.stdout


assert fire("S1", "bookkeep the invoice")
assert not fire("S1", "bookkeep the invoice", AGENT_THREAD_RESUMED="S1")
assert fire("S9", "bookkeep the invoice", AGENT_THREAD_RESUMED="S1"), "a nested session lost its reminder"
assert not fire("S1", "bookkeep the invoice", AGENT_INVESTIGATION="1")
assert not fire("S1", "bookkeep the invoice", AGENT_READONLY="1"), "a read-only session got the reminder"
assert not fire("S1", "SCOPE: this is not the daily bookkeeping run. Check the invoice."), "a focused run got the reminder"
assert not fire("S1", "bookkeep the invoice", AGENT_RUN="0"), "a session that is not a run got the reminder"
assert not fire("S1", "hello")
print("PASS the bookkeeping reminder stands down on its own resumed session only")

shutil.rmtree(T, ignore_errors=True)
print("all run-thread checks passed")
