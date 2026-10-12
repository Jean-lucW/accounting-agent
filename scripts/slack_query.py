"""Deliver a headless session's output to the Slack thread that asked.

Used by both Slack runners: deploy/run-query.sh (a read-only `query`) and
deploy/run-thread.sh (an agent's turn). The runner calls `reply` with the
session's final answer; the session itself calls `upload` when a file is the
answer.

The only two ways anything leaves such a session:

    .venv/bin/python scripts/slack_query.py reply <file | ->
        post the text (chunked under Slack's message size) into the thread

    .venv/bin/python scripts/slack_query.py upload <path> [--title T] [--comment C]
        send a file (a workbook, a CSV extract) into the same thread

Destination comes from the environment the runner sets, never from an
argument: QUERY_CHANNEL (the asker's DM channel) and QUERY_THREAD_TS, or the
neutral SLACK_REPLY_CHANNEL / SLACK_REPLY_THREAD_TS. So a session cannot
address anyone else, whatever it is asked.

Uploads need the Slack app scope `files:write` (docs/setup/SLACK.md). Without
it Slack returns missing_scope and this exits 1 with a plain message the
session can pass on.

No slack_sdk dependency: plain urllib, so it runs from the server's venv or
the system python.
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
API = "https://slack.com/api/"
CHUNK = 3500          # Slack renders up to about 4,000 characters per message comfortably


def _env(key: str) -> str:
    val = os.environ.get(key, "").strip()
    if val:
        return val
    # .env is loaded by the runner; a hand run from the shell may not have it.
    env_file = REPO / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if line.startswith(f"{key}=") or line.startswith(f"export {key}="):
                return line.split("=", 1)[1].strip().strip("\"'")
    return ""


def _call(method: str, payload: dict | None = None, *, get: bool = False) -> dict:
    token = _env("SLACK_BOT_TOKEN")
    if not token:
        sys.exit("SLACK_BOT_TOKEN missing")
    headers = {"Authorization": f"Bearer {token}"}
    if get:
        url = API + method + ("?" + urllib.parse.urlencode(payload or {}) if payload else "")
        req = urllib.request.Request(url, headers=headers)
    else:
        headers["Content-Type"] = "application/json; charset=utf-8"
        req = urllib.request.Request(API + method, data=json.dumps(payload or {}).encode(),
                                     headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        out = json.loads(r.read() or b"{}")
    if not out.get("ok"):
        raise RuntimeError(f"{method}: {out.get('error', 'unknown error')}")
    return out


def _dest() -> tuple[str, str]:
    """The one thread this process may post into.

    QUERY_* is the pair every runner exports; the SLACK_REPLY_* names are
    the same thing said neutrally, because the query session is not the only
    caller.
    """
    channel = _env("QUERY_CHANNEL") or _env("SLACK_REPLY_CHANNEL")
    thread = _env("QUERY_THREAD_TS") or _env("SLACK_REPLY_THREAD_TS")
    if not channel:
        sys.exit("QUERY_CHANNEL is not set; this only runs inside a session "
                 "started by one of the deploy/run-*.sh runners")
    return channel, thread


def _chunks(text: str) -> list[str]:
    text = text.strip()
    if not text:
        return []
    out, cur = [], ""
    for line in text.splitlines(keepends=True):
        while len(line) > CHUNK:               # one absurdly long line
            out.append((cur + line[:CHUNK]).rstrip())
            cur, line = "", line[CHUNK:]
        if len(cur) + len(line) > CHUNK:
            out.append(cur.rstrip())
            cur = ""
        cur += line
    if cur.strip():
        out.append(cur.rstrip())
    return out


def reply(text: str) -> int:
    channel, thread = _dest()
    parts = _chunks(text)
    if not parts:
        return 0
    for part in parts:
        body = {"channel": channel, "text": part}
        if thread:
            body["thread_ts"] = thread
        _call("chat.postMessage", body)
    return len(parts)


def upload(path: str, title: str = "", comment: str = "") -> str:
    channel, thread = _dest()
    p = Path(path)
    if not p.is_file():
        sys.exit(f"no such file: {path}")
    data = p.read_bytes()
    try:
        step1 = _call("files.getUploadURLExternal",
                      {"filename": p.name, "length": len(data)}, get=True)
    except RuntimeError as exc:
        if "missing_scope" in str(exc):
            sys.exit("cannot send files: the Slack app lacks the files:write scope. "
                     "An admin adds it under OAuth & Permissions and reinstalls the app.")
        raise
    ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
    req = urllib.request.Request(step1["upload_url"], data=data, method="POST",
                                 headers={"Content-Type": ctype})
    with urllib.request.urlopen(req, timeout=300) as r:
        r.read()
    body: dict = {"files": [{"id": step1["file_id"], "title": title or p.name}],
                  "channel_id": channel}
    if thread:
        body["thread_ts"] = thread
    if comment:
        body["initial_comment"] = comment
    done = _call("files.completeUploadExternal", body)
    return (done.get("files") or [{}])[0].get("id", step1["file_id"])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("reply", help="post text into the asking thread")
    r.add_argument("source", help="file with the text, or - for stdin")
    u = sub.add_parser("upload", help="send a file into the asking thread")
    u.add_argument("path")
    u.add_argument("--title", default="")
    u.add_argument("--comment", default="")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "reply":
            text = sys.stdin.read() if a.source == "-" else Path(a.source).read_text(errors="replace")
            n = reply(text)
            print(f"posted {n} message(s)")
        else:
            fid = upload(a.path, a.title, a.comment)
            print(f"sent {Path(a.path).name} ({fid})")
    except (RuntimeError, urllib.error.URLError) as exc:
        print(f"slack error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
