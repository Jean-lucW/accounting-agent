"""Offline checks for the run cost bracket, the Slack outbox and the usage report.

No Slack, no Claude, no network: a fake session JSON, a fake Slack poster and a
temp data dir, with a throwaway config/group.toml (AGENT_GROUP_CONFIG). Run it
after touching scripts/run_cost.py, scripts/slack_outbox.py or
scripts/usage_report.py, on the laptop or the server:

    .venv/bin/python scripts/test_run_cost.py

Covers: the bracket from a session's total_cost_usd, extra cost files under
data/runs/<id>/extra/ (summed by label), a resumed thread session costed as the
difference from its last cumulative figure, a session with no figure, a queued
post sent with the bracket in its headline and the first line of a top-level
post, a refused channel post handed back to the admin's reply, a post outside a
run going out at once, a read-only session queueing nothing, the blocked line
a turn cap adds to a report, and the weekly usage report.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import sys
import tempfile
import time

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "src"))

T = pathlib.Path(tempfile.mkdtemp(prefix="run-cost-"))
CFG = T / "group.toml"
CFG.write_text("""
[company]
name = "Test Group"
reporting_currency = "USD"
timezone = "UTC"

[[entities]]
key = "PARENT"
xero_name = "Test Parent Limited"
short = "Parent"
base_currency = "GBP"

[slack]
channel_id = "CTESTCHAN1"
channel_name = "test-accounting"

[slack.admins]
"UTESTADM01" = "Avery Admin"
""")
os.environ["AGENT_GROUP_CONFIG"] = str(CFG)
for k in ("AGENT_RUN_ID", "AGENT_READONLY"):
    os.environ.pop(k, None)

import run_cost as RC          # noqa: E402
import slack_outbox as OB      # noqa: E402
import usage_report as UR      # noqa: E402

RUNS = T / "data" / "runs"
RUNS.mkdir(parents=True)
RC.RUNS = RUNS
OB.RUNS = RUNS
RC.BRIEF_COST = T / "data" / "briefings_cost"

POSTED: list[dict] = []
REFUSE: set[str] = set()


def fake_call(method: str, payload: dict) -> dict:
    """The Slack poster: records every call, refuses the channels in REFUSE."""
    assert method == "chat.postMessage", method
    if payload["channel"] in REFUSE:
        raise RuntimeError("chat.postMessage: channel_not_found")
    POSTED.append(payload)
    return {"ok": True, "ts": f"1000.{len(POSTED):04d}"}


OB._call = fake_call


def write(p: pathlib.Path, d) -> pathlib.Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(d))
    return p


def queued(run_id: str, headline: str, body: str) -> None:
    os.environ["AGENT_RUN_ID"] = run_id
    try:
        print("   ", OB.queue(headline=headline, thread=body))
    finally:
        os.environ.pop("AGENT_RUN_ID")


# 1. a run: begin, queue, stamp. The bracket is in the headline, nothing is edited.
RC.cmd_begin(type("A", (), {"job": "bookkeep"}))
ids = sorted(p.stem for p in RUNS.glob("*.json"))
assert len(ids) == 1 and ids[0].startswith("bookkeep-"), ids
run_id = ids[0]
queued(run_id, "TEST GROUP BOOKKEEPING RUN (05 Oct 2026, 14:32 UTC)", "*bookkept*\n• Example Supplier Ltd · INV-1\n")
queued(run_id, "TEST GROUP MANUAL ITEMS (05 Oct 2026, 14:32 UTC)", "from the bookkeeping run\n• reconcile INV-1\n")
assert not POSTED, "a queued post went out before the stamp"
session = write(T / "session.json", {"session_id": "S1", "total_cost_usd": 1.6437, "num_turns": 40})
RC.main(["stamp", "--run-id", run_id, "--session-json", str(session)])
assert [p["text"] for p in POSTED if "thread_ts" not in p] == [
    "TEST GROUP BOOKKEEPING RUN (05 Oct 2026, 14:32 UTC) (total cost $1.64, run $1.64)",
    "TEST GROUP MANUAL ITEMS (05 Oct 2026, 14:32 UTC) (total cost $1.64, run $1.64)"], POSTED
assert all(p["channel"] == "CTESTCHAN1" for p in POSTED), "posted outside the configured channel"
assert POSTED[1]["thread_ts"] == "1000.0001", POSTED
assert "bookkept" in POSTED[1]["text"], POSTED[1]
entry = json.loads((RUNS / f"{run_id}.json").read_text())
assert entry["run_usd"] == 1.6437 and entry["session_ids"] == ["S1"] and entry["ended"], entry
print("PASS the bracket goes in the headline once the session has ended; the channel comes from config")

# 1b. a second stamp sends nothing twice
n = len(POSTED)
RC.main(["stamp", "--run-id", run_id, "--session-json", str(session)])
assert len(POSTED) == n, "an item already sent was sent again"
print("PASS a post already sent is never sent twice")

# 2. extra cost files: summed by label, each label in the bracket, running ones counted
POSTED.clear()
RC.cmd_begin(type("A", (), {"job": "interco"}))
rid = sorted(p.stem for p in RUNS.glob("interco-*.json"))[0]
write(RUNS / rid / "extra" / "001.json", {"label": "helpers", "cost_usd": 0.30})
write(RUNS / rid / "extra" / "002.json", {"label": "helpers", "cost_usd": 0.20})
write(RUNS / rid / "extra" / "003.json", {"label": "matching", "cost_usd": 0.10, "running": True})
queued(rid, "TEST GROUP INTERCOMPANY RECONCILIATION (05 Oct 2026, 02:00 UTC)", "*blocked*\n• nothing\n")
write(T / "s2.json", {"session_id": "S2", "total_cost_usd": 1.00})
RC.main(["stamp", "--run-id", rid, "--session-json", str(T / "s2.json")])
assert POSTED[0]["text"].endswith("(total cost $1.60, run $1.00, helpers $0.50, matching $0.10, 1 still running)"), POSTED[0]
print("PASS extra cost files add up by label and the bracket names each one")

# 2b. the briefing's document reads: a bookkeeping run claims the gate's untagged
# reads of the hours before it, once; another job never does
POSTED.clear()
RC.cmd_begin(type("A", (), {"job": "bookkeep"}))
bid = sorted(p.stem for p in RUNS.glob("bookkeep-*.json"))[-1]
started = json.loads((RUNS / f"{bid}.json").read_text())["started"]
write(RC.BRIEF_COST / "2026-10-05" / "a.json", {"total_cost_usd": 0.04, "run_id": "", "at": started - 600})
write(RC.BRIEF_COST / "2026-10-05" / "b.json", {"total_cost_usd": 0.06, "run_id": "", "at": started - 900})
write(RC.BRIEF_COST / "2026-10-05" / "old.json", {"total_cost_usd": 9.0, "run_id": "", "at": started - 5 * 3600})
write(RC.BRIEF_COST / "2026-10-05" / "other.json", {"total_cost_usd": 3.0, "run_id": "someone-else", "at": started})
queued(bid, "TEST GROUP BOOKKEEPING RUN (05 Oct 2026, 02:05 UTC)", "*bookkept*\n• nothing\n")
write(T / "s5.json", {"session_id": "S5", "total_cost_usd": 1.00})
RC.main(["stamp", "--run-id", bid, "--session-json", str(T / "s5.json")])
assert POSTED[0]["text"].endswith("(total cost $1.10, run $1.00, document reads $0.10)"), POSTED[0]
claimed = [json.loads(p.read_text())["run_id"] for p in sorted((RC.BRIEF_COST / "2026-10-05").glob("*.json"))]
assert claimed == [bid, bid, "", "someone-else"], claimed
assert RC.brief_cost("interco-x", started, "interco") == 0.0, "another job claimed the reads"
print("PASS a bookkeeping run claims the briefing's reads once, as a document reads extra")

# 3. a resumed thread session: the turn is the difference from the last cumulative figure
st = write(T / "thread.json", {"turns": 1})
assert RC.session_cost([str(write(T / "t1.json", {"session_id": "S3", "total_cost_usd": 1.00}))], str(st))[0] == 1.00
assert abs(RC.session_cost([str(write(T / "t2.json", {"session_id": "S3", "total_cost_usd": 1.75}))], str(st))[0] - 0.75) < 1e-9
assert RC.session_cost([str(write(T / "t3.json", {"session_id": "S4", "total_cost_usd": 0.40}))], str(st))[0] == 0.40
assert json.loads(st.read_text())["turns"] == 1, "the thread state lost a field"
print("PASS a resumed session is costed as this turn's difference; a new session whole")

# 4. no figure: what was queued still goes out, with a note instead of a bracket
POSTED.clear()
RC.cmd_begin(type("A", (), {"job": "review"}))
rid = sorted(p.stem for p in RUNS.glob("review-*.json"))[0]
queued(rid, "TEST GROUP BOOKKEEPING REVIEW (05 Oct 2026, 10:00 UTC)", "*bookkept*\n• none\n")
write(T / "dead.json", {"is_error": True})
RC.main(["stamp", "--run-id", rid, "--session-json", str(T / "dead.json")])
assert "cost not reported" in POSTED[0]["text"], POSTED[0]
print("PASS a session that died without a figure still gets its report posted")

# 5. a top-level post: the bracket goes on its first line; a refused channel goes to the reply
POSTED.clear()
RC.cmd_begin(type("A", (), {"job": "outstanding"}))
rid = sorted(p.stem for p in RUNS.glob("outstanding-*.json"))[0]
os.environ["AGENT_RUN_ID"] = rid
OB.queue(top_level="OUTSTANDING ITEMS (05 Oct 2026)\n\n*manual*\n• reconcile INV-9\n")
os.environ.pop("AGENT_RUN_ID")
reply = T / "reply.md"
reply.write_text("outstanding · 1 item\nfull list in the channel\n")
REFUSE.add("CTESTCHAN1")
RC.main(["stamp", "--run-id", rid, "--session-json", str(session), "--reply", str(reply)])
text = reply.read_text()
assert text.splitlines()[0].endswith("(total cost $1.64, run $1.64)"), text
assert "the channel refused these" in text and "reconcile INV-9" in text, text
assert not POSTED
REFUSE.clear()
POSTED.clear()
RC.main(["stamp", "--run-id", rid, "--session-json", str(session)])
assert POSTED and POSTED[0]["text"].splitlines()[0].endswith("(total cost $1.64, run $1.64)"), POSTED
print("PASS a top-level post carries the bracket on its first line; a refused channel falls back to the reply, and is retried")

# 6. outside a run a post goes out at once; a read-only session posts nothing
POSTED.clear()
msg = OB.queue(headline="TEST GROUP USAGE", thread="detail")
assert "now" in msg and len(POSTED) == 2 and POSTED[0]["text"] == "TEST GROUP USAGE", (msg, POSTED)
os.environ["AGENT_READONLY"] = "1"
try:
    OB.queue(headline="x", thread="y")
    raise AssertionError("a read-only session queued a post")
except SystemExit:
    pass
finally:
    os.environ.pop("AGENT_READONLY")
print("PASS outside a run it posts at once; under AGENT_READONLY it refuses")

# 7. a turn cap: the blocked line joins the run's report, or makes one
RC.cmd_begin(type("A", (), {"job": "focused"}))
rid = sorted(p.stem for p in RUNS.glob("focused-*.json"))[0]
queued(rid, "TEST GROUP BOOKKEEPING RUN (06 Oct 2026, 02:00 UTC)", "*bookkept*\n• Example Supplier Ltd · INV-2\n")
queued(rid, "TEST GROUP MANUAL ITEMS (06 Oct 2026, 02:00 UTC)", "• reconcile INV-2\n")
print("   ", RC.turn_cap_report(rid, "bookkeep", "the session stopped at its turn cap · 500 turns"))
boxed = [json.loads(p.read_text()) for p in sorted((RUNS / rid / "outbox").glob("*.json"))]
assert "*blocked*" in boxed[0]["body"] and "turn cap" in boxed[0]["body"], boxed[0]
assert "blocked" not in boxed[1]["body"], "the line went onto the manual items"
RC.cmd_begin(type("A", (), {"job": "ap-check"}))
rid2 = sorted(p.stem for p in RUNS.glob("ap-check-*.json"))[0]
RC.turn_cap_report(rid2, "ap-check", "the session stopped at its turn cap")
made = json.loads(next((RUNS / rid2 / "outbox").glob("*.json")).read_text())
assert made["headline"].startswith("TEST GROUP ACCOUNTS PAYABLE CHECK (") and "turn cap" in made["body"], made
print("PASS a turn cap adds a blocked line to the run's report, or queues one with a headline from the company name")

# 8. the usage report: by job, against the previous period, deduped by session
root = T / "usage"
d = root / "data" / "runs"
end = dt.date(2026, 10, 8)
this_week = dt.datetime(2026, 10, 3, 12, tzinfo=dt.timezone.utc).timestamp()
last_week = dt.datetime(2026, 9, 26, 12, tzinfo=dt.timezone.utc).timestamp()
write(d / "bookkeep-a.json", {"job": "bookkeep", "started": this_week, "run_usd": 4.00, "session_ids": ["U1"]})
write(d / "bookkeep-a.session.json", {"session_id": "U1", "total_cost_usd": 4.00, "num_turns": 100,
      "modelUsage": {"opus-test-20260101": {"inputTokens": 10, "cacheCreationInputTokens": 5000,
                                           "cacheReadInputTokens": 90000, "outputTokens": 2000, "costUSD": 4.0}}})
write(d / "bookkeep-b.json", {"job": "bookkeep", "started": last_week, "run_usd": 1.00, "session_ids": ["U2"]})
write(d / "run-UTESTADM01-1-2-x.json", {"job": "run-UTESTADM01-1-2", "started": this_week, "run_usd": 0.50,
      "session_ids": ["U3"], "extra_usd": {"helpers": 0.25}})
write(d / "run-UTESTADM01-1-2-y.json", {"job": "run-UTESTADM01-1-2", "started": this_week + 60, "run_usd": 0.75,
      "session_ids": ["U3"]})
write(root / "data" / "query" / "q1" / "session.json", {"session_id": "U4", "total_cost_usd": 0.20, "num_turns": 5})
write(root / "data" / "query" / "q1-copy" / "session.json", {"session_id": "U4", "total_cost_usd": 0.20, "num_turns": 5})
os.utime(root / "data" / "query" / "q1" / "session.json", (this_week, this_week))
os.utime(root / "data" / "query" / "q1-copy" / "session.json", (this_week, this_week))
head, detail = UR.report(UR.collect(root), end, 7, "Test Group")
assert head == "TEST GROUP CLAUDE USAGE (01 Oct - 07 Oct 2026)", head
assert "• bookkeep: $4.00 · 1 session · avg 100 turns" in detail and "up 300% on $1.00" in detail, detail
assert "• slack threads: $1.25 · 2 thread turns" in detail, detail      # two turns of one session, both counted
assert "• query: $0.20 · 1 session" in detail, detail                   # the same session twice, counted once
assert "• helpers: $0.25" in detail and "notional" in detail, detail
assert "*up more than 50% on" in detail and "• opus-test: $4.00" in detail, detail
print("PASS the usage report sums by job against the week before and counts a session once")

import shutil
shutil.rmtree(T, ignore_errors=True)
print("\nall run cost checks passed")
