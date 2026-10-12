#!/usr/bin/env python
"""Which session a Slack thread's next turn runs in.

deploy/run-thread.sh gives every Slack thread one Claude session and resumes
it for each message in the thread. A resume sends the whole history again: a
long thread runs at hundreds of thousands of tokens a call, and a reply that
comes after the prompt cache has expired writes all of it into the cache again
at a premium. This decides, before each turn, whether the next turn resumes
the session or starts a new one from a handover note built here in Python (no
model call), and keeps the thread's own record of what was said so that note
can be built:

    plan       resume | fresh | rotate <reason>     what the next turn does
    handover   the prompt for a new session that continues the thread
    finish     after the CLI exits: the answer file, the session id, the
               context size and the end time into the thread state
    ran        exit 0 if the session behind a turn's JSON really started
    log-turn   one line per turn into <key>.history.jsonl: message and reply

THRESHOLDS. A new session costs its fixed prefix (the system prompt, tools,
CLAUDE.md) plus what it has to read again (skills, COMMS, the data it works
on), so starting over is worth roughly 90k tokens of cache writes. A cache
read costs a small fraction of an input token and a write costs more than one.
Measure your own threads (the context_tokens in data/slack/threads/*.json)
and move the lines if your sessions are larger or smaller.

  ROTATE_COLD 100k  idle past the cache TTL: the first call writes the whole
                    context again, so past about 90k a new session writes less.
  ROTATE_AT   300k  cache still warm: each call reads the whole context; a
                    typical follow-up turn makes several calls, so past 300k
                    two such turns read more than a new session costs to start.

SLACK_THREAD_ROTATE_TOKENS and SLACK_THREAD_ROTATE_COLD_TOKENS override them.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

ROTATE_AT = int(os.environ.get("SLACK_THREAD_ROTATE_TOKENS", "300000"))
ROTATE_COLD = int(os.environ.get("SLACK_THREAD_ROTATE_COLD_TOKENS", "100000"))

# Where the first-turn prompt built by scripts/slack_agent.py splits. The test
# suite checks these against the constants there.
RULES_MARK = "HOW TO WRITE. docs/COMMS.md is a HARD RULE"
EXTRA_MARKS = ("\nTHREAD this run was started from", "\nNOTES from ",
               "\nUSER-TIER INPUT since the last run")
FOLLOWUP_MARK = "Newest last:\n"

# The handover's own budget, in characters (about 4 per token). The last
# reply and the new message are always whole and are not counted.
OLD_MESSAGE = 400
OLD_REPLY = 600
RECENT = 3000
CONTEXT_EXTRA = 1500
OUTBOX_POST = 4000
BUDGET = 16000


def _read(p: Path) -> dict:
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return {}


def _write(p: Path, d: dict) -> None:
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(d, indent=1, sort_keys=True) + "\n")
    tmp.replace(p)


def _update_state(p: Path, **fields) -> dict:
    d = _read(p)
    d.update(fields)
    _write(p, d)
    return d


def cache_ttl() -> int:
    """Seconds the main session's prompt cache lives, as the runner set it."""
    v = os.environ.get("CLAUDE_CODE_PROMPT_CACHE_TTL", "1h").strip().lower()
    m = re.fullmatch(r"(\d+)\s*([smh]?)", v)
    if not m:
        return 300
    n, unit = int(m.group(1)), m.group(2) or "s"
    return n * {"s": 1, "m": 60, "h": 3600}[unit]


def transcripts_dir(root: Path = ROOT) -> Path:
    if os.environ.get("THREAD_TRANSCRIPTS"):
        return Path(os.environ["THREAD_TRANSCRIPTS"])
    base = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    return base / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(root))


def transcript_path(sid: str, root: Path = ROOT) -> Path:
    return transcripts_dir(root) / f"{sid}.jsonl"


def _ctx(u: dict) -> int:
    return sum(int(u.get(k) or 0) for k in ("input_tokens", "cache_read_input_tokens",
                                             "cache_creation_input_tokens", "output_tokens"))


def context_from_turn(d: dict) -> int | None:
    """The context the next call starts from: the last call's input, cached or
    not, plus what it wrote. The CLI's JSON carries that call as the last of
    usage.iterations (it matches the transcript's last main-session message)."""
    it = (d.get("usage") or {}).get("iterations") or []
    return _ctx(it[-1]) if it else None


def context_from_transcript(path: Path, tail: int = 4 << 20) -> int | None:
    """The same figure read from the end of the session's transcript."""
    try:
        with path.open("rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - tail))
            lines = fh.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        if '"assistant"' not in line:
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("type") == "assistant" and not e.get("isSidechain"):
            u = (e.get("message") or {}).get("usage")
            if u:
                return _ctx(u)
    return None


def _epoch(iso: str) -> float | None:
    try:
        return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None


def session_context(state: dict, state_path: Path, root: Path = ROOT) -> int | None:
    sid = state.get("session_id")
    if not sid:
        return None
    if state.get("context_session_id") == sid and isinstance(state.get("context_tokens"), int):
        return state["context_tokens"]
    turn = _read(state_path.with_name(state_path.name.replace(".json", ".turn.json")))
    if turn.get("session_id") == sid:
        c = context_from_turn(turn)
        if c is not None:
            return c
    return context_from_transcript(transcript_path(sid, root))


def plan(state_path: Path, followup: bool, now: float | None = None,
         root: Path = ROOT) -> tuple[str, str, int | None, float | None]:
    """(action, reason, context tokens, idle seconds)."""
    st = _read(state_path)
    sid = st.get("session_id")
    if not sid:
        # A follow-up with no session (a consensus thread, a lost state) still
        # gets the thread's rules and history, not the bare message.
        return ("rotate", "no session", None, None) if followup else ("fresh", "", None, None)
    tdir = transcripts_dir(root)
    if tdir.is_dir() and not transcript_path(sid, root).exists():
        return "rotate", "transcript gone", None, None
    ctx = session_context(st, state_path, root)
    end = st.get("last_end") if isinstance(st.get("last_end"), (int, float)) else _epoch(st.get("last", ""))
    idle = ((now or time.time()) - end) if end else None
    if ctx is None:
        return "resume", "", None, idle
    if ctx >= ROTATE_AT:
        return "rotate", "size", ctx, idle
    # The cache lives from the start of its last read; a turn's last call is
    # within a minute or two of its end, so allow that much early.
    ttl = cache_ttl()
    if idle is not None and idle >= ttl - min(120, ttl // 5) and ctx >= ROTATE_COLD:
        return "rotate", "idle", ctx, idle
    return "resume", "", ctx, idle


# ---------------------------------------------------------------- the record

def split_first_prompt(prompt: str) -> tuple[str, str, str]:
    """(task, rules, extras) of a first-turn prompt from scripts/slack_agent.py."""
    i = prompt.find(RULES_MARK)
    if i < 0:
        return prompt.strip(), "", ""
    task, rest = prompt[:i].strip(), prompt[i:]
    cut = min([j for j in (rest.find(m) for m in EXTRA_MARKS) if j >= 0] or [len(rest)])
    return task, rest[:cut].strip(), rest[cut:].strip()


def followup_text(prompt: str) -> str:
    """The admin's own words out of a follow-up prompt."""
    i = prompt.find(FOLLOWUP_MARK)
    return prompt[i + len(FOLLOWUP_MARK):].strip() if i >= 0 else prompt.strip()


def history_path(state_path: Path) -> Path:
    return state_path.with_name(state_path.name.replace(".json", ".history.jsonl"))


def brief_path(state_path: Path) -> Path:
    return state_path.with_name(state_path.name.replace(".json", ".brief.json"))


def read_history(state_path: Path) -> list[dict]:
    p = history_path(state_path)
    out = []
    if p.exists():
        for line in p.read_text(errors="replace").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


def history_from_logs(state: dict, root: Path = ROOT) -> tuple[list[dict], str]:
    """A thread older than the history file: rebuilt from its runner logs.
    Returns the turns and the first turn's whole prompt."""
    job = state.get("job")
    if not job:
        return [], ""
    turns, first, cur, mode = [], "", None, None
    buf: list[str] = []
    for log in sorted((root / "data" / "logs").glob(f"{job}-*.log")):
        for line in log.read_text(errors="replace").splitlines():
            m = re.match(r"^(\S+) \[" + re.escape(job) + r"\] start turn (\d+)", line)
            if m:
                cur = {"turn": int(m.group(2)), "at": m.group(1), "message": "", "reply": ""}
                turns.append(cur)
                mode = None
                continue
            if cur is None:
                continue
            if mode is None and line.startswith("prompt: "):
                mode, buf = "prompt", [line[len("prompt: "):]]
            elif mode == "prompt" and re.fullmatch(r"=+", line):
                text = "\n".join(buf)
                if cur["turn"] == 1 and not first:
                    first = text
                    cur["message"] = split_first_prompt(text)[0]
                else:
                    cur["message"] = followup_text(text)
                mode = "done"
            elif mode == "prompt":
                buf.append(line)
            elif line == "--- answer ---":
                mode, buf = "answer", []
            elif mode == "answer" and line == "--- end answer ---":
                cur["reply"] = "\n".join(buf).strip()
                mode = "done"
            elif mode == "answer":
                buf.append(line)
    return turns, first


def _short(text: str, n: int) -> str:
    text = text.strip()
    if len(text) <= n:
        return text
    return text[:n].rstrip() + f" [... {len(text) - n} more characters in the record]"


def _outbox(run_id: str, root: Path) -> list[dict]:
    box = root / "data" / "runs" / run_id / "outbox"
    return [_read(p) for p in sorted(box.glob("*.json"))] if run_id and box.is_dir() else []


def build_handover(state_path: Path, message: str, reason: str = "",
                   root: Path = ROOT) -> str:
    st = _read(state_path)
    history = read_history(state_path)
    first_prompt = ""
    if not history or int(history[0].get("turn") or 1) > 1:
        # Turns from before the record existed come from the runner logs.
        legacy, first_prompt = history_from_logs(st, root)
        start = int(history[0].get("turn") or 1) if history else 10 ** 6
        history = [t for t in legacy if t["turn"] < start] + history
    brief = _read(brief_path(state_path))
    if brief:
        task, rules, extras = brief.get("task", ""), brief.get("rules", ""), brief.get("context", "")
    else:
        if not first_prompt:
            p = state_path.with_name(state_path.name.replace(".json", ".prompt.md"))
            first_prompt = p.read_text(errors="replace") if p.exists() else ""
        if not first_prompt:
            _, first_prompt = history_from_logs(st, root)
        task, rules, extras = split_first_prompt(first_prompt) if first_prompt else (st.get("task", ""), "", "")
    sessions = [s for s in (st.get("previous_sessions") or []) if s]
    if st.get("session_id") and st["session_id"] not in sessions:
        sessions.append(st["session_id"])
    why = {
        "size": "the earlier session's context had grown past the point where resuming it costs more than starting again",
        "idle": "the earlier session's prompt cache had expired and resuming it would have written its whole context again",
        "no session": "this thread has no session to resume",
        "transcript gone": "the earlier session's transcript is no longer on the server",
        "resume refused": "the earlier session could not be resumed",
    }.get(reason, reason or "the thread moved to a new session")

    head = [
        "CONTINUATION OF A SLACK THREAD IN A NEW SESSION. This session takes over the",
        f"thread {st.get('channel', '?')}/{st.get('thread_ts', '?')} because {why}.",
        "Everything the earlier session did is done and was reported in the thread",
        "below. Do not repeat it and do not start the original request again: act only",
        "on the NEW MESSAGE at the end, with the thread as its context. When the new",
        "message confirms something the last reply proposed, the last reply is that",
        "proposal, in full: carry it out as proposed, checking the current state in",
        "Xero before any write (the duplicate guard applies as always). The rules",
        "below are the ones the first turn was given; they govern how you work and",
        "how you reply, not what you do now. A detail the record does not carry (an",
        "exact payload, an ID, a figure the earlier session found) is in its",
        "transcript; search it, never read it whole:",
    ]
    for s in sessions[-3:]:
        head.append(f"  grep -n '<term>' {transcript_path(s, root)}")
    out = ["\n".join(head)]
    if rules:
        out.append("RULES THIS THREAD RUNS UNDER\n" + rules)
    orig = "THE ORIGINAL REQUEST (turn 1), handled by the earlier session\n" + (task or "(not on record)")
    if extras:
        orig += "\n\n" + _short(extras, CONTEXT_EXTRA)
    out.append(orig)

    turns = [h for h in history if h.get("message") or h.get("reply")]
    lines, used = [], 0
    n = len(turns)
    for i, h in enumerate(turns):
        last, recent = i == n - 1, i >= n - 2
        msg = h.get("message", "")
        if i == 0:
            msg = "(the original request above)" if task else msg
        msg = _short(msg, RECENT if recent else OLD_MESSAGE)
        reply = h.get("reply", "")
        reply = reply.strip() if last else _short(reply, RECENT if recent else OLD_REPLY)
        block = f"turn {h.get('turn', i + 1)} · {h.get('at', '')}\n  admin: {msg}\n  reply: {reply}"
        if last:
            for item in _outbox(h.get("run_id", ""), root):
                body = (item.get("body") or "").strip()
                block += (f"\n  queued to the channel: {item.get('headline', '').strip() or '(top level)'}\n"
                          + _short(body, OUTBOX_POST))
        lines.append(block)
    # Keep the newest whole and drop from the oldest end (after turn 1) until
    # the record fits.
    budget = BUDGET
    total = sum(len(b) for b in lines[:-1])
    dropped = 0
    while total > budget and len(lines) > 3:
        total -= len(lines.pop(1))
        dropped += 1
    if dropped:
        lines.insert(1, f"[{dropped} earlier turns left out; they are in the transcript]")
    if lines:
        out.append("THE THREAD SO FAR, oldest first\n" + "\n\n".join(lines))
    else:
        out.append("THE THREAD SO FAR: no earlier replies on record.")
    out.append("NEW MESSAGE, the instruction for this turn, newest last. Your FINAL message is\n"
               "posted verbatim into the thread.\n" + message.strip())
    return "\n\n".join(out) + "\n"


# ---------------------------------------------------------------- commands

def cmd_plan(a) -> int:
    action, reason, ctx, idle = plan(Path(a.state), a.followup)
    print(action, reason.replace(" ", "-") or "-",
          ctx if ctx is not None else "-", int(idle) if idle is not None else "-")
    return 0


def cmd_handover(a) -> int:
    msg = Path(a.message_file).read_text(errors="replace")
    text = build_handover(Path(a.state), msg, a.reason.replace("-", " "), Path(a.root))
    Path(a.out).write_text(text)
    print(len(text))
    return 0


def cmd_ran(a) -> int:
    """0 if the session behind this turn started, 1 if it never did: no JSON,
    no session id or no turn taken, or the CLI refused the resume as unknown."""
    try:
        err = Path(a.err).read_text(errors="replace") if a.err else ""
    except OSError:
        err = ""
    if "No conversation found" in err:
        return 1
    d = _read(Path(a.turn_json))
    return 0 if d.get("session_id") and int(d.get("num_turns") or 0) > 0 else 1


def cmd_finish(a) -> int:
    """After the CLI exits: the answer file and the thread state."""
    d = _read(Path(a.turn_json))
    Path(a.answer).write_text((d.get("result") or "").strip() + "\n")
    sid = d.get("session_id")
    st_path = Path(a.state)
    st = _read(st_path)
    fields: dict = {"last_end": int(time.time())}
    if sid:
        old = st.get("session_id")
        if old and old != sid:
            prev = [s for s in (st.get("previous_sessions") or []) if s != old] + [old]
            fields["previous_sessions"] = prev[-10:]
        if a.mode == "rotate":
            fields["rotations"] = int(st.get("rotations") or 0) + 1
        fields["session_id"] = sid
        ctx = context_from_turn(d)
        if ctx is not None:
            fields["context_tokens"], fields["context_session_id"] = ctx, sid
    _update_state(st_path, **fields)
    return 0


def cmd_log_turn(a) -> int:
    st_path = Path(a.state)
    msg = Path(a.message_file).read_text(errors="replace") if a.message_file else ""
    msg = split_first_prompt(msg)[0] if a.first else followup_text(msg)
    reply = Path(a.answer).read_text(errors="replace").strip() if a.answer else ""
    d = _read(Path(a.turn_json)) if a.turn_json else {}
    rec = {"turn": a.turn, "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "mode": a.mode, "session_id": d.get("session_id") or _read(st_path).get("session_id"),
           "run_id": a.run_id or "", "message": msg, "reply": reply}
    with history_path(st_path).open("a") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("plan")
    s.add_argument("--state", required=True)
    s.add_argument("--followup", action="store_true", help="the turn is a follow-up, not the first message")
    s.set_defaults(func=cmd_plan)
    s = sub.add_parser("handover")
    s.add_argument("--state", required=True)
    s.add_argument("--message-file", required=True)
    s.add_argument("--reason", default="")
    s.add_argument("--out", required=True)
    s.add_argument("--root", default=str(ROOT))
    s.set_defaults(func=cmd_handover)
    s = sub.add_parser("ran")
    s.add_argument("--turn-json", required=True)
    s.add_argument("--err", default="")
    s.set_defaults(func=cmd_ran)
    s = sub.add_parser("finish")
    s.add_argument("--state", required=True)
    s.add_argument("--turn-json", required=True)
    s.add_argument("--answer", required=True)
    s.add_argument("--mode", default="resume")
    s.set_defaults(func=cmd_finish)
    s = sub.add_parser("log-turn")
    s.add_argument("--state", required=True)
    s.add_argument("--turn", type=int, required=True)
    s.add_argument("--message-file", default="")
    s.add_argument("--first", action="store_true")
    s.add_argument("--answer", default="")
    s.add_argument("--turn-json", default="")
    s.add_argument("--run-id", default="")
    s.add_argument("--mode", default="resume")
    s.set_defaults(func=cmd_log_turn)
    a = p.parse_args(argv)
    return a.func(a)


if __name__ == "__main__":
    raise SystemExit(main())
