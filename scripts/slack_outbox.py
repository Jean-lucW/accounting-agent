#!/usr/bin/env python
"""Every post a run makes to the channel, sent once the run's cost is known.

A run's headline carries its cost in brackets, and a session cannot know its
own cost while it is still running. So inside a run nothing is posted to the
channel directly: the session queues each post here, and the runner sends the
queue after the session has ended, the cost already in the headline. Nothing is
posted and then edited.

    # a run report, or the manual items: headline at top level, detail as the
    # first reply in its thread
    .venv/bin/python scripts/slack_outbox.py channel --headline "BOOKKEEPING RUN (05 Oct 2026, 14:32 UTC)" --thread report.md

    # the outstanding-items list: top level only, split under 3,500 characters
    .venv/bin/python scripts/slack_outbox.py channel --top-level list.md

`--thread` and `--top-level` take a file, or - for stdin. Posts go out in the
order they were queued. The channel is config/group.toml [slack] channel_id;
this cannot address anything else.

Inside a run (AGENT_RUN_ID set by deploy/run-agent.sh, run-thread.sh or
run-consensus.sh) `channel` queues under data/runs/<run id>/outbox/ and the
runner's scripts/run_cost.py stamp sends it. Outside a run (a hand run on the
server) it posts at once, with no cost to add.

The PreToolUse hook .claude/hooks/guard-channel.py refuses a direct
chat.postMessage to the channel from a run session, so the queue is the only way
in. A read-only session (AGENT_READONLY) queues nothing.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
RUNS = ROOT / "data" / "runs"
CHUNK = 3500


def channel_id() -> str:
    from accounting_agent import config
    cid = config.slack().channel_id
    if not cid:
        raise SystemExit("no channel to post to: set channel_id in config/group.toml [slack]")
    return cid


def _chunks(text: str) -> list[str]:
    """Split at line boundaries, under Slack's comfortable message size."""
    text = text.strip()
    out, cur = [], ""
    for line in text.splitlines(keepends=True):
        while len(line) > CHUNK:
            out.append((cur + line[:CHUNK]).rstrip())
            cur, line = "", line[CHUNK:]
        if len(cur) + len(line) > CHUNK:
            out.append(cur.rstrip())
            cur = ""
        cur += line
    if cur.strip():
        out.append(cur.rstrip())
    return out


def _call(method: str, payload: dict) -> dict:
    sys.path.insert(0, str(ROOT / "scripts"))
    import slack_query                                           # noqa: E402
    return slack_query._call(method, payload)


def outbox(run_id: str) -> Path:
    return RUNS / run_id / "outbox"


def _post(item: dict, bracket: str = "") -> str:
    """Send one queued item. Returns the top-level ts."""
    channel = channel_id()
    if item["shape"] == "thread":
        head = f"{item['headline']} {bracket}".rstrip()
        top = _call("chat.postMessage", {"channel": channel, "text": head})
        for part in _chunks(item["body"]):
            _call("chat.postMessage", {"channel": channel, "text": part, "thread_ts": top["ts"]})
        return top["ts"]
    parts = _chunks(item["body"])
    if bracket and parts:
        first, _, rest = parts[0].partition("\n")
        parts[0] = f"{first.rstrip()} {bracket}" + (f"\n{rest}" if rest else "")
    ts = ""
    for part in parts:
        ts = ts or _call("chat.postMessage", {"channel": channel, "text": part})["ts"]
    return ts


def queue(*, headline: str = "", thread: str = "", top_level: str = "") -> str:
    """Queue a post for the run, or post it at once outside a run. Returns
    what happened, in words, for the caller to print."""
    if os.environ.get("AGENT_READONLY"):
        raise SystemExit("AGENT_READONLY is set: a read-only session posts nothing to the channel")
    if headline:
        item = {"shape": "thread", "headline": headline.strip(), "body": thread}
    else:
        item = {"shape": "top-level", "body": top_level}
    if not item["body"].strip():
        raise SystemExit("nothing to post: the body is empty")
    run_id = os.environ.get("AGENT_RUN_ID", "").strip()
    if not run_id:
        ts = _post(item)
        return f"posted to {channel_id()} now, {ts} (not inside a run, so no cost to add)"
    box = outbox(run_id)
    box.mkdir(parents=True, exist_ok=True)
    n = len(list(box.glob("*.json"))) + 1
    item["queued"] = time.time()
    path = box / f"{n:03d}.json"
    path.write_text(json.dumps(item, indent=1, ensure_ascii=False) + "\n")
    what = item.get("headline") or item["body"].strip().splitlines()[0]
    shown = path.relative_to(ROOT) if path.is_relative_to(ROOT) else path
    return (f"queued {what!r} for {channel_id()}: the runner posts it when the run ends, "
            f"with the run's cost in brackets ({shown})")


def flush(run_id: str, bracket: str, log=print) -> list[dict]:
    """Send every unsent item in the run's outbox. Returns the ones that
    failed, so a runner with a thread can hand them to the admin instead."""
    failed = []
    for path in sorted(outbox(run_id).glob("*.json")):
        item = json.loads(path.read_text())
        if item.get("ts"):
            continue
        try:
            item["ts"] = _post(item, bracket)
            item["sent"] = time.time()
            log(f"posted {path.name}, {item['ts']}")
        except (Exception, SystemExit) as e:                     # noqa: BLE001
            item["error"] = f"{type(e).__name__}: {e}"
            failed.append(item)
            log(f"could not post {path.name}: {item['error']}")
        path.write_text(json.dumps(item, indent=1, ensure_ascii=False) + "\n")
    return failed


def _read(src: str) -> str:
    return sys.stdin.read() if src == "-" else Path(src).read_text(errors="replace")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("channel", help="queue a post to the channel")
    c.add_argument("--headline", help="the top-level line; the detail goes in its thread")
    c.add_argument("--thread", help="file (or -) with the detail, the first reply under --headline")
    c.add_argument("--top-level", help="file (or -) posted at top level, no thread")
    a = p.parse_args(argv)
    if bool(a.headline) == bool(a.top_level):
        p.error("give --headline with --thread, or --top-level")
    if a.headline and not a.thread:
        p.error("--headline needs --thread: the detail always goes in the headline's thread")
    try:
        print(queue(headline=a.headline or "", thread=_read(a.thread) if a.thread else "",
                    top_level=_read(a.top_level) if a.top_level else ""))
    except RuntimeError as e:
        print(f"slack error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
