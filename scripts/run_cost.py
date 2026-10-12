#!/usr/bin/env python
"""What a run cost, in brackets on what it posts to Slack.

Every run that reports on Slack carries its cost in brackets after the
headline, split between the run's own session and any other Claude work the
run paid for (a helper that ran as its own session, say):

    BOOKKEEPING RUN (05 Oct 2026, 14:32 UTC) (total cost $2.14, run $1.64, helpers $0.50)

A session cannot know its own cost while it is still running, so nothing the
run sends to the channel leaves until the session has ended: the session queues
each post with scripts/slack_outbox.py, and the runner calls this afterwards,
which works out the cost and sends the queue with the bracket already in the
headline. Nothing is posted and then edited. The runners call this, never a
session:

    RUN_ID=$(.venv/bin/python scripts/run_cost.py begin --job "$JOB")
    export AGENT_RUN_ID="$RUN_ID"           # tags the outbox and any extra cost file
    claude -p ... --output-format json > session.json
    .venv/bin/python scripts/run_cost.py stamp --run-id "$RUN_ID" --session-json session.json [--session-json ...] [--thread-state data/slack/threads/<key>.json] [--reply answer.md]

`stamp` works out the figures:
  run     total_cost_usd of the run's own session(s). A resumed thread
          session reports its cumulative cost, so --thread-state keeps the
          last figure per session id and the turn is the difference.
  extra   every file data/runs/<run id>/extra/*.json, one per other piece of
          Claude work the run paid for, written by whatever spawned it:
              {"label": "helpers", "cost_usd": 0.50, "running": false}
          Costs with the same label add up, and each label gets its own
          figure in the bracket. "running": true counts the file as still
          going, and the bracket says so.
          The bookkeeping briefing's document reads (data/briefings_cost/,
          one file per read, written by scripts/bookkeep_extract.py) are
          an extra labelled "document reads": those tagged with the run id,
          and for a bookkeeping run the untagged ones the gate made in the
          three hours before it started, claimed here so they count once.
  total   the run and every extra added.

Then it sends the run's outbox to the channel in config/group.toml [slack]
with the bracket after each headline, and appends the bracket to the first
line of --reply, the text the runner is about to post into the admin's thread.
A queued post the channel refuses (channel_not_found: the bot is not in it) is
added to --reply in full, so it reaches the admin instead of being dropped.

`turn-cap` is the other thing a runner calls: a session that stopped at its
--max-turns did not finish, so its report (or one made for the purpose when
the session queued none) gets a `blocked` line saying so, ahead of `stamp`.

State under data/runs/: <run_id>.json (job, started, ended, the figures) and
<run_id>/outbox/ (the queued posts, each marked with its ts once sent).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / "data" / "runs"
BRIEF_COST = ROOT / "data" / "briefings_cost"
BRIEF_CLAIM_SECONDS = 3 * 3600
BRIEF_LABEL = "document reads"


def _now() -> float:
    return time.time()


def _read(p: Path) -> dict:
    try:
        d = json.loads(p.read_text())
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def _write(p: Path, d: dict) -> None:
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(d, indent=1, sort_keys=True) + "\n")
    tmp.replace(p)


def usd(x: float) -> str:
    return f"${x:,.2f}"


# ---------------------------------------------------------------- figures

def session_cost(paths: list[str], thread_state: str | None) -> tuple[float | None, list[str]]:
    """The run's own cost and its session ids. With a thread state, the turn's
    share of a resumed session's cumulative figure."""
    total, sids, seen = None, [], False
    st_path = Path(thread_state) if thread_state else None
    st = _read(st_path) if st_path else {}
    for p in paths:
        d = _read(Path(p))
        c, sid = d.get("total_cost_usd"), d.get("session_id")
        if sid:
            sids.append(sid)
        if not isinstance(c, (int, float)):
            continue
        seen = True
        turn = float(c)
        if st_path and sid:
            prev = st.get("cost_cumulative_usd") if st.get("cost_session_id") == sid else None
            if isinstance(prev, (int, float)) and prev <= turn:
                turn -= float(prev)
            st["cost_session_id"], st["cost_cumulative_usd"] = sid, float(c)
        total = (total or 0.0) + turn
    if st_path and seen:
        fresh = _read(st_path)            # the runner may have written since
        fresh.update({k: st[k] for k in ("cost_session_id", "cost_cumulative_usd") if k in st})
        _write(st_path, fresh)
    return total, sids


def extra_costs(run_id: str, runs: Path | None = None) -> tuple[dict[str, float], int]:
    """({label: cost} in the order first seen, how many are still running)
    from data/runs/<run id>/extra/*.json."""
    box = (runs or RUNS) / run_id / "extra"
    costs: dict[str, float] = {}
    running = 0
    for p in sorted(box.glob("*.json")) if box.is_dir() else []:
        d = _read(p)
        label = str(d.get("label") or "other").strip() or "other"
        c = d.get("cost_usd")
        costs[label] = costs.get(label, 0.0) + (float(c) if isinstance(c, (int, float)) else 0.0)
        if d.get("running"):
            running += 1
    return costs, running


def brief_cost(run_id: str, start: float, job: str | None, root: Path | None = None) -> float:
    """The briefing's document reads. A read made inside a run carries its
    id. The scheduled gate builds the briefing before the run begins, so a
    bookkeeping run claims the untagged reads of the three hours before its
    start, and writes its id into them so no other run counts them."""
    total = 0.0
    box = root or BRIEF_COST
    for p in sorted(box.glob("*/*.json")) if box.exists() else []:
        d = _read(p)
        cost = d.get("total_cost_usd")
        if not isinstance(cost, (int, float)):
            continue
        rid = d.get("run_id") or ""
        if rid == run_id:
            total += float(cost)
        elif not rid and job == "bookkeep" and start - BRIEF_CLAIM_SECONDS <= float(d.get("at") or 0) <= start + 60:
            d["run_id"] = run_id
            _write(p, d)
            total += float(cost)
    return total


def bracket(run: float, extra: dict[str, float], running: int = 0) -> str:
    parts = [f"total cost {usd(run + sum(extra.values()))}", f"run {usd(run)}"]
    parts += [f"{label} {usd(c)}" for label, c in extra.items()]
    if running:
        parts.append(f"{running} still running")
    return "(" + ", ".join(parts) + ")"


# ---------------------------------------------------------------- Slack

def stamp_reply(path: Path, text: str) -> None:
    body = path.read_text(errors="replace")
    if not body.strip():
        return
    lines = body.lstrip("\n").split("\n")
    lines[0] = f"{lines[0].rstrip()} {text}"
    path.write_text("\n".join(lines))


# ---------------------------------------------------------------- turn cap

# What a scheduled job's report is called; any other job is its own name.
JOB_TITLES = {"bookkeep": "bookkeeping run", "interco": "intercompany reconciliation",
              "review": "bookkeeping review", "ap-check": "accounts payable check",
              "outstanding": "outstanding items"}


def job_title(job: str) -> str:
    return JOB_TITLES.get(job) or re.sub(r"[-_]+", " ", job).strip() or "run"


def turn_cap_report(run_id: str, job: str, line: str, runs: Path | None = None) -> str:
    """Add `line` under a blocked heading to the run's own report in its
    outbox (the last threaded post that is not the manual items), or queue a
    report of its own when the session queued none. Returns what it did."""
    sys.path.insert(0, str(ROOT / "src"))
    from accounting_agent import config
    box = (runs or RUNS) / run_id / "outbox"
    box.mkdir(parents=True, exist_ok=True)
    items = sorted(box.glob("*.json"))
    for p in reversed(items):
        d = _read(p)
        if d.get("shape") == "thread" and not d.get("ts") \
                and "MANUAL ITEMS" not in d.get("headline", "").upper():
            d["body"] = d["body"].rstrip() + "\n\n*blocked*\n• " + line + "\n"
            p.write_text(json.dumps(d, indent=1, ensure_ascii=False) + "\n")
            return f"turn cap line added to the report in {p.name}"
    started = _read((runs or RUNS) / f"{run_id}.json").get("started") or _now()
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(str(config.company("timezone", "UTC") or "UTC"))
    except Exception:                                            # noqa: BLE001
        from datetime import timezone as _tz
        tz = _tz.utc
    from datetime import datetime
    when = datetime.fromtimestamp(started, tz).strftime("%d %b %Y, %H:%M %Z")
    item = {"shape": "thread",
            "headline": f"{config.company_name().upper()} {job_title(job).upper()} ({when})",
            "body": "*blocked*\n• " + line + "\n", "queued": _now()}
    p = box / f"{len(items) + 1:03d}.json"
    p.write_text(json.dumps(item, indent=1, ensure_ascii=False) + "\n")
    return f"no report was queued: turn cap report queued as {p.name}"


# ---------------------------------------------------------------- commands

def cmd_begin(a) -> int:
    RUNS.mkdir(parents=True, exist_ok=True)
    job = re.sub(r"[^A-Za-z0-9._-]+", "-", a.job)[:80]
    run_id = f"{job}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{os.getpid()}"
    _write(RUNS / f"{run_id}.json", {"job": a.job, "started": _now(), "ended": None})
    print(run_id)
    return 0


def cmd_stamp(a) -> int:
    def log(msg: str) -> None:
        print(f"run_cost: {msg}", file=sys.stderr)

    entry_path = RUNS / f"{a.run_id}.json"
    entry = _read(entry_path)
    run, sids = session_cost(a.session_json or [], a.thread_state)
    extra, running = extra_costs(a.run_id)
    reads = brief_cost(a.run_id, float(entry.get("started") or _now()), entry.get("job"))
    if reads:
        extra[BRIEF_LABEL] = extra.get(BRIEF_LABEL, 0.0) + reads
    entry.update({"ended": _now(), "session_ids": sids, "run_usd": run,
                  "extra_usd": extra, "extra_running": running})
    # A session that died reports no cost; what it queued still goes out.
    if run is None:
        log("no cost in the session output")
        text = "(cost not reported: the session ended without a figure)"
    else:
        text = bracket(run, extra, running)
    entry["bracket"] = text
    _write(entry_path, entry)
    print(text)
    if a.reply:
        try:
            stamp_reply(Path(a.reply), text)
        except OSError as e:
            log(f"reply not stamped: {e}")
    failed = []
    if not a.no_slack:
        sys.path.insert(0, str(ROOT / "scripts"))
        import slack_outbox                                      # noqa: E402
        failed = slack_outbox.flush(a.run_id, text, log)
    if failed and a.reply:
        add = ["", "the channel refused these, so they are here instead:"]
        for item in failed:
            add += ["", f"{item.get('headline', '')} {text}".strip(), item["body"].strip()]
        with Path(a.reply).open("a") as fh:
            fh.write("\n".join(add) + "\n")
    return 0


def cmd_turn_cap(a) -> int:
    print(turn_cap_report(a.run_id, a.job, a.line))
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("begin", help="open a run and print its id")
    b.add_argument("--job", required=True)
    b.set_defaults(func=cmd_begin)
    s = sub.add_parser("stamp", help="close a run and send its posts with the cost")
    s.add_argument("--run-id", required=True)
    s.add_argument("--session-json", action="append",
                   help="the CLI's --output-format json output; repeat for each session")
    s.add_argument("--thread-state", help="a run-thread.sh state file: the turn's share of a resumed session")
    s.add_argument("--reply", help="the reply about to be posted: the bracket goes on its first line")
    s.add_argument("--no-slack", action="store_true", help="work out the figures, send nothing")
    s.set_defaults(func=cmd_stamp)
    t = sub.add_parser("turn-cap", help="a session stopped at its turn cap: say so in its report")
    t.add_argument("--run-id", required=True)
    t.add_argument("--job", required=True)
    t.add_argument("--line", required=True, help="the blocked line, in COMMS shape")
    t.set_defaults(func=cmd_turn_cap)
    a = p.parse_args(argv)
    return a.func(a)


if __name__ == "__main__":
    raise SystemExit(main())
