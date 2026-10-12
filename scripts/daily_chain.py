#!/usr/bin/env python
"""The plain-Python half of the daily chain: a Claude session starts only when
there is work that needs judgement.

deploy/run-scheduled.sh daily runs three links, the bookkeeping run, the
intercompany reconciliation and the accounts payable check, and each one is
gated here first:

    .venv/bin/python scripts/daily_chain.py bookkeep [--dry-run]
    .venv/bin/python scripts/daily_chain.py bookkeep-brief [--rebuild]
    .venv/bin/python scripts/daily_chain.py bookkeep-commit --rc N
    .venv/bin/python scripts/daily_chain.py interco [--dry-run]
    .venv/bin/python scripts/daily_chain.py ap-prepare [--dry-run]
    .venv/bin/python scripts/daily_chain.py ap-finish [--session] [--dir DIR] [--dry-run]

Exit codes, read by run-scheduled.sh:
    0   start the session
    10  handled here, nothing for a session (ap-prepare: no session needed,
        ap-finish posts)
    11  handled here, but an exit check failed: the chain stops, as it does
        after a failed session
    anything else: this script failed, and the session runs as it always has
    (fail-open: a broken gate costs a session, never a missed day)

[daily_chain] enabled = false in config/group.toml makes every gate answer
"start the session" at once.

BOOKKEEP. The session is skipped only when this proves there is nothing new
since the last scheduled bookkeeping session that finished: no message to the
Slack app (a DM, or a thread reply in one) and no email in the accounting
inbox since that session started; every message with an attachment in the last
[daily_chain] attachment_lookback_days days already has its line in
docs/bookkept/LEDGER.md (the skill's own completeness test); no bank feed line
after `bankfeed.py refresh all` that the last session did not have in front of
it, and no feed line marked "awaiting fetch" (a line waiting on a document the
run itself fetches); nothing due on the register (an investigation scheduled
for today or earlier, an admin answer since the last session, a query the
check-queries exit check would fail). Any doubt, any error, no record of an
earlier session: the session runs. On a skip the two exit checks still run,
the run still reports to the channel with the cost bracket (nil), and nothing
moves the record of the last session, so tomorrow compares against it again.
`bookkeep-commit --rc N` records a session that ran to the end as the new
baseline. When the session does run, the gate first builds its briefing
(scripts/bookkeep_brief.py): every refresh, the sweep, the downloads and the
document reads done here, so the session opens on one file. `bookkeep-brief`
prints it, building it first for a session the gate did not start;
`bookkeep-commit` deletes it once the session has finished.

INTERCO. deploy/run-interco.sh runs first, in plain Python: the recon, the FX
drafts, both matrices and the Drive publish. The session is skipped when no
pair breaks, scripts/interco_breaks.py finds no open item, no interco query is
answered or investigated and waiting on us, a missing mirror account has
nothing on its other side, and every cross-currency pair that is ready (nothing
one-sided) and still apart has been reported ready before. Otherwise the
session starts and reads what this wrote instead of re-running the recon.

AP CHECK. ap-prepare refreshes the bill feed and pairs every open bill with
the bank feed lines another entity paid it from (scripts/billfeed.py match
--cross: the same currency and amount, or a Revolut card leg whose own bill
amount equals the bill's AmountDue). A pair already on the register (the
bill's number in a bill-payments line) is standing and is reported, not
re-worked. Only new pairs start a session, which posts the legs and writes its
lines for the runner. ap-finish builds the reconcile list (scripts/
reconcile_list.py run --no-dm, so the payables DM stays the `bills` job's and
is never sent twice) and posts the report and the manual items thread, with
the session's cost in brackets when a session ran.

Locks: the gate takes the job's own lock (data/<job>.lock, the one run-agent.sh
takes) while it works, and the chain lock data/daily-chain.lock that
run-scheduled.sh daily already holds (AGENT_CHAIN_LOCK_HELD=1 says so; run by
hand, the gate takes it itself).

State: data/gates/ (the baseline of the last bookkeeping session, today's
intercompany decision) and data/ap_check/<date>/ (candidates and the session's
hand-back). Writes: the bank feed and the bill feed (their own refreshes), the
register through scripts/outstanding.py, the channel through
scripts/slack_outbox.py, and the write-back commit. Never writes to Xero.
Server only.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from accounting_agent import config  # noqa: E402

PY = str(ROOT / ".venv" / "bin" / "python") if (ROOT / ".venv" / "bin" / "python").exists() else sys.executable
GATES = ROOT / "data" / "gates"
LOGS = ROOT / "data" / "logs"
RUNS = ROOT / "data" / "runs"
RETRY = ROOT / "data" / "retry"
BANKFEED = ROOT / "data" / "bankfeed"
BILLFEED = ROOT / "data" / "billfeed"
AP_DIR = ROOT / "data" / "ap_check"
INTERCO_DIR = ROOT / "data" / "interco"
CHAIN_LOCK = ROOT / "data" / "daily-chain.lock"
LEDGER = ROOT / "docs" / "bookkept" / "LEDGER.md"
RECONCILE_MD = ROOT / "data" / "reports" / "reconcile_list.md"
RECONCILE_JSON = ROOT / "data" / "reports" / "reconcile_list.json"

RUN, HANDLED, HANDLED_FAILED = 0, 10, 11
LEDGER_TOLERANCE = 180   # seconds between a message and the time its ledger line gives
AWAITING_FETCH = "awaiting fetch"   # a bank feed note: the line waits on a document the run fetches itself
SLACK_API = "https://slack.com/api/"


# ---------------------------------------------------------------- plumbing

def now() -> float:
    return time.time()


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def zone() -> ZoneInfo:
    try:
        return ZoneInfo(config.company("timezone", "UTC") or "UTC")
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def local(ts: float) -> str:
    d = datetime.fromtimestamp(ts, timezone.utc).astimezone(zone())
    return f"{d.day} {d:%b %Y, %H:%M %Z}"


def company_up() -> str:
    return config.company_name().upper()


def shorts() -> dict[str, str]:
    """entity slug -> short name, in config order."""
    return {e.slug: e.short for e in config.entities()}


def entity_short(key: str) -> str:
    try:
        return config.entity(key).short
    except config.ConfigError:
        return key


def log(job: str, msg: str) -> None:
    line = f"{iso(now())} [{job}] {msg}"
    print(line, flush=True)
    try:
        LOGS.mkdir(parents=True, exist_ok=True)
        with (LOGS / f"{job}-{datetime.now(timezone.utc):%Y-%m-%d}.log").open("a") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def sh(job: str, cmd: list[str], timeout: int | None = None) -> tuple[int, str]:
    """Run a command from the repo root; its output goes to the job's log."""
    try:
        p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=timeout)
        rc, out = p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        rc, out = 124, f"timed out after {timeout} seconds"
    try:
        LOGS.mkdir(parents=True, exist_ok=True)
        with (LOGS / f"{job}-{datetime.now(timezone.utc):%Y-%m-%d}.log").open("a") as fh:
            fh.write(f"$ {' '.join(cmd)}\n{out.rstrip()}\n(exit {rc})\n")
    except OSError:
        pass
    return rc, out


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False, default=str) + "\n")
    tmp.replace(path)


def load_env() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    for raw in env.read_text().splitlines():
        line = raw.strip()
        if line.startswith("export "):
            line = line[7:]
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


@contextmanager
def _flock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = path.open("a")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        yield False
        return
    try:
        yield True
    finally:
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()


@contextmanager
def chain_lock():
    """data/daily-chain.lock, the lock `run-scheduled.sh daily` holds for the
    whole chain and `bills`, `balance-reports` and `outstanding-check` wait on.
    Inside the chain the shell already holds it (a second flock from this child
    would block on the shell's own), so it says so with AGENT_CHAIN_LOCK_HELD;
    run by hand, the gate takes it. Yields False when another chain holds it."""
    if os.environ.get("AGENT_CHAIN_LOCK_HELD") == "1":
        yield True
        return
    with _flock(CHAIN_LOCK) as got:
        yield got


@contextmanager
def job_lock(job: str):
    """The same lock run-agent.sh takes for the job, held while the gate works,
    so a session another admin started is never raced. Yields False when busy."""
    with _flock(ROOT / "data" / f"{job}.lock") as got:
        yield got


@contextmanager
def locks(job: str):
    """Both locks, chain first. Yields the reason it could not have them, or ""."""
    with chain_lock() as chain:
        if not chain:
            yield "another daily chain holds data/daily-chain.lock"
            return
        with job_lock(job) as got:
            yield "" if got else f"a {job} session holds data/{job}.lock"


def gates_enabled() -> bool:
    return bool(config.daily_chain()["enabled"])


def _ssl():
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def slack_get(method: str, **params) -> dict:
    """A read on the Slack Web API with the bot token, waiting out a rate limit."""
    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not token:
        raise RuntimeError("SLACK_BOT_TOKEN not set")
    url = SLACK_API + method + "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
    for _ in range(6):
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req, timeout=60, context=_ssl()) as r:
                out = json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(int(e.headers.get("Retry-After", "5")) + 1)
                continue
            raise
        if out.get("ok"):
            return out
        if out.get("error") == "ratelimited":
            time.sleep(5)
            continue
        raise RuntimeError(f"{method}: {out.get('error', 'unknown error')}")
    raise RuntimeError(f"{method}: still rate limited")


def slack_pages(method: str, key: str, **params) -> list[dict]:
    out, cursor = [], ""
    while True:
        r = slack_get(method, cursor=cursor, **params)
        out += r.get(key) or []
        cursor = (r.get("response_metadata") or {}).get("next_cursor") or ""
        if not cursor:
            return out


def gmail_get(token: str, path: str, **q) -> dict:
    """A read on the Gmail API, backing off on the per-user rate limit (a bare 403)."""
    url = "https://gmail.googleapis.com/gmail/v1/users/me/" + path + ("?" + urllib.parse.urlencode(q) if q else "")
    for attempt in range(6):
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req, timeout=60, context=_ssl()) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (403, 429) or e.code >= 500:
                time.sleep(1.5 * 2 ** attempt)
                continue
            raise
    raise RuntimeError(f"Gmail API kept failing: {path}")


# ---------------------------------------------------------------- the channel

def post(job: str, posts: list[tuple[str, str]], *, dry_run: bool = False) -> None:
    """Send each (headline, thread) with the cost in brackets after the
    headline (scripts/run_cost.py). Inside a session (AGENT_RUN_ID set) the
    posts join its outbox and the runner sends them with the session's cost.
    Outside one this is a plain-Python run: its own run record at nil cost,
    sent at once."""
    posts = [(h, b) for h, b in posts if b.strip()]
    if dry_run:
        for h, b in posts:
            print(f"DRY RUN, would post:\n{h}\n---\n{b.rstrip()}\n")
        return
    if not posts:
        return
    import slack_outbox
    if os.environ.get("AGENT_RUN_ID", "").strip():
        for h, b in posts:
            log(job, slack_outbox.queue(headline=h, thread=b))
        return
    rc, out = sh(job, [PY, "scripts/run_cost.py", "begin", "--job", job])
    run_id = out.strip().splitlines()[-1] if rc == 0 and out.strip() else ""
    if not run_id:
        raise RuntimeError(f"run_cost.py begin failed: {out.strip()[:200]}")
    session = RUNS / f"{run_id}.session.json"
    write_json(session, {"total_cost_usd": 0.0, "session_id": None, "result": "plain Python, no session"})
    os.environ["AGENT_RUN_ID"] = run_id
    try:
        for h, b in posts:
            log(job, slack_outbox.queue(headline=h, thread=b))
    finally:
        os.environ.pop("AGENT_RUN_ID", None)
    rc, out = sh(job, [PY, "scripts/run_cost.py", "stamp", "--run-id", run_id, "--session-json", str(session)])
    log(job, f"posted, {out.strip().splitlines()[-1] if out.strip() else 'no cost line'}")


def end_checks(job: str, queries: bool, dry_run: bool) -> dict[str, str]:
    """The exit checks every bookkeeping run ends on, read-only."""
    out = {}
    if dry_run:
        return {"phantom-payment check": "not run (dry run)"}
    rc, _ = sh(job, [PY, "scripts/check_no_phantom_payments.py"])
    out["phantom-payment check"] = "PASS" if rc == 0 else "FAILED"
    log(job, f"phantom-payment check {out['phantom-payment check']}")
    if queries:
        rc, _ = sh(job, [PY, "scripts/outstanding.py", "check-queries"])
        out["register query check"] = "PASS" if rc == 0 else "FAILED"
        log(job, f"register query check {out['register query check']}")
    return out


def writeback(job: str, dry_run: bool) -> None:
    """Commit what the run changed under docs/ (the reconcile list, rules), the
    same step run-agent.sh ends a session on."""
    if not dry_run:
        sh(job, ["./deploy/commit-writeback.sh", job])


def session_finished(job: str, since: float) -> bool:
    """Did a session of this job run to the end after `since`, without being
    deferred for the Xero rate limit or skipped for a busy lock? Read from the
    log run-agent.sh writes."""
    finished = False
    pat = re.compile(rf"^(\S+Z) \[{re.escape(job)}\] (.*)$")
    for path in sorted(LOGS.glob(f"{job}-*.log")):
        try:
            if path.stat().st_mtime < since - 60:
                continue
            text = path.read_text(errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            m = pat.match(line)
            if not m:
                continue
            try:
                ts = datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
            except ValueError:
                continue
            if ts < since - 5:
                continue
            what = m.group(2)
            if "rate limit reached, deferred" in what or "holds the lock, skipping" in what:
                return False
            if re.match(r"finished rc=0\b", what):
                finished = True
    retry = RETRY / f"{job}.retry"
    if retry.exists() and retry.stat().st_mtime >= since - 5:
        return False
    return finished


# ---------------------------------------------------------------- the feeds

FEED_HEAD = re.compile(r"^## (.+?)(?: · last checked (\S+))?\s*$")
MARKER = re.compile(r"\b(?:rev|mer):[0-9a-f]{8}\b")
DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def feed_lines(folder: Path | None = None) -> list[dict]:
    """Every line on the reconstructed bank feed, one dict per line."""
    out = []
    for path in sorted((folder or BANKFEED).glob("*.md")):
        account = ""
        for raw in path.read_text(errors="replace").splitlines():
            m = FEED_HEAD.match(raw)
            if m:
                account = m.group(1)
                continue
            parts = [p.strip() for p in raw.split("|")]
            if len(parts) < 4 or not DAY.match(parts[0]):
                continue
            try:
                ccy, amt = parts[3].split(" ", 1)
                amount = float(amt.replace(",", ""))
            except ValueError:
                continue
            mk = MARKER.search(raw)
            out.append({"entity": path.stem, "account": account, "date": parts[0], "payee": parts[1],
                        "direction": parts[2], "ccy": ccy, "amount": amount, "marker": mk.group(0) if mk else "",
                        "raw": raw.rstrip()})
    return out


def feed_key(line: dict) -> str:
    ident = line["marker"] or line["payee"].lower()
    return (f"{line['entity']}|{line['account'].lower()}|{line['date']}|{line['direction']}|"
            f"{line['ccy']} {line['amount']:.2f}|{ident}")


def bill_feed(folder: Path | None = None) -> dict[str, list[dict]]:
    """Every AUTHORISED unpaid bill, per entity slug, as the bill feed holds it."""
    out: dict[str, list[dict]] = {}
    for slug in shorts():
        path = (folder or BILLFEED) / f"{slug}.md"
        rows = []
        if path.exists():
            for raw in path.read_text(errors="replace").splitlines():
                if raw.startswith("#") or raw.count("|") < 6:
                    continue
                p = [x.strip() for x in raw.split("|")]
                try:
                    ccy, amt = p[2].split(" ", 1)
                    rows.append({"entity": slug, "date": p[0], "supplier": p[1], "ccy": ccy,
                                 "due": float(amt.replace(",", "")), "account": p[3], "number": p[4],
                                 "reference": p[5], "due_date": p[6]})
                except (ValueError, IndexError):
                    continue
        out[slug] = rows
    return out


# ---------------------------------------------------------------- bookkeeping

LEDGER_LINE = re.compile(r"^(\d{4}-\d{2}-\d{2}) ([^|]*)\|\s*(slack|email)\s*\|", re.I)
HHMM = re.compile(r"\b(\d{1,2}):(\d{2})\b")


def ledger_stamps(text: str) -> dict[str, list[float]]:
    """{source: [epoch seconds]} for every slack or email line of the ledger.
    A line covering several messages lists each time after the first, `+`
    separated; a time earlier than the first is taken on either adjacent day
    too, since a thread can run past midnight."""
    out: dict[str, list[float]] = {"slack": [], "email": []}
    for line in text.splitlines():
        m = LEDGER_LINE.match(line)
        if not m:
            continue
        day = date.fromisoformat(m.group(1))
        src = m.group(3).lower()
        times = [(int(h), int(mi)) for h, mi in HHMM.findall(m.group(2)) if int(h) < 24 and int(mi) < 60]
        for n, (h, mi) in enumerate(times):
            base = datetime(day.year, day.month, day.day, h, mi, tzinfo=timezone.utc)
            out[src].append(base.timestamp())
            if n and (h, mi) < times[0]:
                out[src] += [(base + timedelta(days=1)).timestamp(), (base - timedelta(days=1)).timestamp()]
    return out


def in_ledger(ts: float, stamps: list[float]) -> bool:
    return any(abs(ts - s) <= LEDGER_TOLERANCE for s in stamps)


def register_reasons(items: list[dict], answered_seen: set[str], today: str) -> list[str]:
    """What on the register is the bookkeeping run's to do today. The
    intercompany lines (key `interco-...`) are the intercompany run's."""
    import outstanding
    out = []
    for i in items:
        if i.get("domain") != "bookkeeping" or (i.get("key") or "").startswith("interco-"):
            continue
        when = i.get("investigation_scheduled") or ""
        if (when and when <= today and i.get("certainty") is None
                and not i.get("investigation_failed") and not i.get("answered")):
            out.append(f"an investigation is due on the register, scheduled {when} · {i['id']}")
        if i.get("answered") and f"{i['id']}|{i['answered']}" not in answered_seen:
            out.append(f"an admin has answered a query since the last run and the posting is ours · {i['id']}")
    bad = [i for i, _ in outstanding.unsolved_breaches(items)
           if not (i.get("key") or "").startswith("interco-")]
    if bad:
        out.append("the register query check would fail · " + ", ".join(i["id"] for i in bad[:10]))
    return out


def answered_marks(items: list[dict]) -> list[str]:
    return sorted(f"{i['id']}|{i['answered']}" for i in items
                  if i.get("domain") == "bookkeeping" and i.get("answered"))


def refresh_bankfeed(job: str) -> int:
    rc, _ = sh(job, [PY, "scripts/bankfeed.py", "refresh", "all", "--brief"], timeout=900)
    return rc


def slack_intake(since: float, window_start: float) -> tuple[list[dict], int]:
    """(every human message in the Slack app's DMs since window_start, how many
    threads have a human reply since `since`). Metadata only. The app is the
    bot the token belongs to."""
    me = slack_get("auth.test").get("user_id", "")
    msgs, replies = [], 0
    for im in slack_pages("conversations.list", "channels", types="im", limit=200):
        if im.get("user") in (me, "USLACKBOT") or im.get("is_user_deleted"):
            continue
        for m in slack_pages("conversations.history", "messages", channel=im["id"],
                             oldest=str(int(window_start)), limit=200):
            human = (m.get("user") and m["user"] != me and not m.get("bot_id")
                     and m.get("subtype") in (None, "file_share", "thread_broadcast"))
            if human:
                msgs.append({"ts": float(m["ts"]), "user": m["user"], "files": len(m.get("files") or []),
                             "channel": im["id"]})
            latest = float(m.get("latest_reply") or 0)
            if latest >= since and any(u != me for u in m.get("reply_users") or []):
                replies += 1
    return msgs, replies


def gmail_intake(since: float, window_start: float, deep: bool) -> tuple[int, list[dict]]:
    """(messages in the accounting inbox since `since`, and when `deep`, every
    message with an attachment since window_start with its two timestamps)."""
    from accounting_agent.gmail_auth import get_access_token
    token = get_access_token()
    skip = "-in:sent -in:drafts -in:chats"
    new = gmail_get(token, "messages", q=f"after:{int(since)} {skip}", maxResults=20).get("messages") or []
    found = []
    if deep and not new:
        ids, page = [], None
        while True:
            q = {"q": f"after:{int(window_start)} has:attachment {skip}", "maxResults": 100}
            if page:
                q["pageToken"] = page
            r = gmail_get(token, "messages", **q)
            ids += [m["id"] for m in r.get("messages") or []]
            page = r.get("nextPageToken")
            if not page or len(ids) > 1000:
                break
        import email.utils
        for mid in ids:
            d = gmail_get(token, f"messages/{mid}", format="metadata", metadataHeaders="Date")
            stamps = [int(d.get("internalDate") or 0) / 1000.0]
            hdr = next((h["value"] for h in (d.get("payload") or {}).get("headers") or []
                        if h.get("name", "").lower() == "date"), "")
            try:
                stamps.append(email.utils.parsedate_to_datetime(hdr).timestamp())
            except (TypeError, ValueError):
                pass
            found.append({"id": mid, "stamps": stamps})
            time.sleep(0.05)
    return len(new), found


def bookkeep_reasons(job: str, state: dict, items: list[dict], started: float, today: str,
                     dry_run: bool) -> tuple[list[str], dict, dict[str, dict]]:
    """(why the session must run, the facts a Python report gives, the bank
    feed as it stands). Any error is a reason: doubt means run."""
    window_days = int(config.daily_chain()["attachment_lookback_days"])
    reasons: list[str] = []
    facts: dict = {"window_days": window_days}
    keys: dict[str, dict] = {}
    since = float(state.get("session_started") or 0)
    window_start = started - window_days * 86400
    if not since:
        reasons.append("no record of an earlier scheduled session that finished")
        since = window_start
    try:
        if (RETRY / f"{job}.retry").exists():
            reasons.append("a deferred bookkeeping run is waiting for the Xero rate limit")
        reasons += register_reasons(items, set(state.get("answered") or []), today)

        # The session refreshes the feed first thing anyway, so this costs
        # nothing extra. A dry run reads the feed as it stands.
        if not dry_run:
            rc = refresh_bankfeed(job)
            if rc:
                reasons.append(f"the bank feed refresh failed (exit {rc})")
        lines = feed_lines()
        keys = {feed_key(l): l for l in lines}
        seen = set(state.get("feed") or [])
        new_lines = [l for k, l in keys.items() if k not in seen]
        facts["feed_open"], facts["feed_new"] = len(lines), len(new_lines)
        short = shorts()
        if new_lines:
            reasons.append(f"{len(new_lines)} bank feed lines the last session did not have, "
                           + ", ".join(f"{short.get(l['entity'], l['entity'])} {l['date']} {l['ccy']} {l['amount']:,.2f}"
                                       for l in new_lines[:5]) + (" and more" if len(new_lines) > 5 else ""))
        waiting = [l for l in lines if AWAITING_FETCH in l["raw"].lower()]
        if waiting:
            reasons.append(f"{len(waiting)} bank feed lines wait on a document the run fetches")

        msgs, replies = slack_intake(since, min(since, window_start))
        new_msgs = [m for m in msgs if m["ts"] >= since]
        msgs = [m for m in msgs if m["ts"] >= window_start]
        if new_msgs:
            reasons.append(f"{len(new_msgs)} messages to the Slack app since {local(since)}")
        if replies:
            reasons.append(f"{replies} Slack threads with a reply since {local(since)}")
        deep = not reasons
        if config.company("accounting_inbox", ""):
            n_mail, mail = gmail_intake(since, window_start, deep)
        else:
            n_mail, mail = 0, []
        if n_mail:
            reasons.append(f"{n_mail if n_mail < 20 else '20 or more'} emails in the accounting inbox "
                           f"since {local(since)}")

        if deep and not reasons:
            text = LEDGER.read_text(errors="replace") if LEDGER.exists() else ""
            stamps = ledger_stamps(text)
            slack_files = [m for m in msgs if m["files"]]
            gap_s = [m for m in slack_files if not in_ledger(m["ts"], stamps["slack"])]
            gap_e = [m for m in mail if not any(in_ledger(t, stamps["email"]) for t in m["stamps"])]
            facts["window_slack"], facts["window_email"] = len(slack_files), len(mail)
            if gap_s:
                reasons.append(f"{len(gap_s)} Slack messages with files in the last {window_days} days have no "
                               "ledger line, " + ", ".join(local(m["ts"]) for m in gap_s[:5]))
            if gap_e:
                reasons.append(f"{len(gap_e)} emails with attachments in the last {window_days} days have no "
                               "ledger line, " + ", ".join(local(m["stamps"][0]) for m in gap_e[:5]))
    except BaseException as e:                                       # noqa: BLE001 - doubt means run
        reasons.append(f"the gate could not finish ({type(e).__name__}: {str(e)[:200]})")
        try:
            keys = {feed_key(l): l for l in feed_lines()}
        except BaseException:                                        # noqa: BLE001
            keys = {}
    return reasons, facts, keys


def bookkeep_report(facts: dict, checks: dict[str, str], since: float) -> str:
    clean = "FAILED" not in checks.values()
    return "\n".join([
        f"bookkeeping · nothing needed judgement, no session started · check {'PASS' if clean else 'FAILED'}",
        "",
        f"• nothing new since the run of {local(since)}: no message to the Slack app or the accounting "
        "inbox, no new bank line, nothing due on the register",
        f"• every message with an attachment in the last {facts.get('window_days')} days has its ledger line: "
        f"{facts.get('window_slack', 0)} in the Slack app, {facts.get('window_email', 0)} in the inbox",
        f"• bank feed refreshed: {facts.get('feed_open', 0)} lines open, none new",
        "• " + " · ".join(f"{k} {v}" for k, v in checks.items()),
        "",
        "*queries*",
        "• nothing queried",
    ])


def cmd_bookkeep(a) -> int:
    job = "bookkeep"
    if not gates_enabled():
        log(job, "gate: [daily_chain] is switched off; the session runs")
        return RUN
    load_env()
    started = now()
    today = date.today().isoformat()
    import bookkeep_brief
    bookkeep_brief.prune_stale()
    with locks(job) as busy:
        if busy:
            log(job, f"gate: {busy}; leaving it to run-agent.sh")
            return RUN
        import outstanding
        state = read_json(GATES / "bookkeep.json", {}) or {}
        items = outstanding.load()
        reasons, facts, keys = bookkeep_reasons(job, state, items, started, today, a.dry_run)
        since = float(state.get("session_started") or 0)
        pending = {"gate_started": started, "feed": sorted(keys), "answered": answered_marks(items),
                   "reasons": reasons, "facts": facts}
        if not a.dry_run:
            write_json(GATES / "bookkeep.pending.json", pending)
        if reasons:
            log(job, "gate: the session runs - " + "; ".join(reasons))
            if not a.dry_run:
                try:
                    bookkeep_brief.build(refresh_bank=any("bank feed refresh failed" in r for r in reasons))
                    log(job, "gate: briefing built, " + str(bookkeep_brief.BRIEF.relative_to(ROOT)))
                except BaseException as e:                       # noqa: BLE001 - the session builds its own
                    log(job, f"gate: the briefing could not be built ({type(e).__name__}: {str(e)[:200]})")
            return RUN

        log(job, f"gate: nothing new since the session of {local(since)}; no session started")
        checks = end_checks(job, queries=True, dry_run=a.dry_run)
        post(job, [(f"{company_up()} BOOKKEEPING RUN ({local(started)})", bookkeep_report(facts, checks, since))],
             dry_run=a.dry_run)
        writeback(job, a.dry_run)
        return HANDLED_FAILED if "FAILED" in checks.values() else HANDLED


def cmd_bookkeep_commit(a) -> int:
    """After the session: if it ran to the end, it becomes the record the next
    gate compares against. A failed, deferred or skipped session leaves the
    old record, so the next gate finds the same work again."""
    job = "bookkeep"
    pending = read_json(GATES / "bookkeep.pending.json")
    if not pending:
        log(job, "gate: nothing pending to record")
        return 0
    started = float(pending["gate_started"])
    if a.rc != 0 or not session_finished(job, started):
        log(job, f"gate: the session did not finish cleanly (rc={a.rc}); the last record stands")
        return 0
    write_json(GATES / "bookkeep.json", {"session_started": started, "feed": pending["feed"],
                                         "answered": pending["answered"], "recorded": iso(now())})
    (GATES / "bookkeep.pending.json").unlink(missing_ok=True)
    import bookkeep_brief
    bookkeep_brief.wipe()             # its documents are attached in Xero now; keep nothing beyond the run
    log(job, f"gate: recorded the session of {local(started)} as the last one that finished")
    return 0


def cmd_bookkeep_brief(a) -> int:
    """Print the run's briefing, building it first when none is fresh. One
    call, the first of every bookkeeping session."""
    import bookkeep_brief
    load_env()
    if a.rebuild or not bookkeep_brief.is_fresh():
        bookkeep_brief.build(refresh_bank=True)
    text = bookkeep_brief.BRIEF.read_text()
    if len(text) <= config.briefing()["inline_chars"]:
        print(text, end="")
    else:
        # Past about 30,000 characters the harness cuts the output short and
        # saves it aside; the Read tool takes it whole, once.
        print("\n".join(text.splitlines()[:4]))
        print(f"\nthe briefing is {len(text):,} characters: Read {bookkeep_brief.BRIEF.relative_to(ROOT)} "
              "once, whole, before anything else")
    return 0


# ---------------------------------------------------------------- interco

def month_end_before(d: date) -> date:
    return d.replace(day=1) - timedelta(days=1)


def interco_window(as_at: date) -> str:
    """Window start for the daily recon: the financial year start, or in the
    first quarter of the year the start of the quarter before, so a pair's
    opening transactions are in the window."""
    start = config.fy_start_for(as_at)
    if (as_at - start).days < 92:
        return config.fy_quarter_start(start - timedelta(days=1)).isoformat()
    return start.isoformat()


def interco_decide(recon: dict, items: list[dict], open_items: list[dict], as_at: date,
                   reported: set[str]) -> tuple[list[str], dict]:
    """(why the session must run, the facts a Python report gives)."""
    tol = float(config.intercompany_settings()["tolerance_abs"])
    rates = recon.get("rates") or {}
    reasons, facts = [], {"agreed": [], "translation": [], "no_mirror": [], "ready": [], "ready_keys": []}
    if open_items:
        top = sorted(open_items, key=lambda i: -i["size"])[:5]
        reasons.append(f"{len(open_items)} open items: " + ", ".join(
            f"{i['pair']} / {i['flavour']} {i['kind']} {recon.get('reporting_currency', '')} {i['size']:,.2f}"
            for i in top))
    pairs = recon.get("pairs") or []
    for p in pairs:
        st = p.get("status") or ""
        if st == "no_mirror_account":
            other = p.get("other_balance")
            if other is None or abs(float(other)) >= 0.01:
                reasons.append(f"{p['pair']} has no mirror account in {p.get('missing_in')} and its other side "
                               + ("holds a balance" if other is not None else "was not read"))
            facts["no_mirror"].append(p)
        elif st.startswith("BREAK"):
            reasons.append(f"{p['pair']} breaks by {abs(float(p.get('break') or 0)):,.2f}")
        elif st in ("AGREED", "nil both sides"):
            facts["agreed"].append(p)
        elif st.startswith("small diff") and p["a"].get("base") == p["b"].get("base"):
            # Two ledgers in one currency have no translation between them:
            # a gap there is a posting or a transfer-leg difference.
            reasons.append(f"{p['pair']} is {abs(float(p.get('break') or 0)):,.2f} apart in one currency")
        elif st.startswith("small diff"):
            facts["translation"].append(p)
        else:
            reasons.append(f"{p['pair']} has a status this gate does not know, {st!r}")
    for i in items:
        if not (i.get("key") or "").startswith("interco-"):
            continue
        if i.get("answered"):
            reasons.append(f"an admin has answered an intercompany query and the posting is ours · {i['id']}")
        elif i.get("certainty") is not None and not i.get("investigation_failed"):
            reasons.append(f"an investigation verdict is waiting to be acted on · {i['id']}")

    # A cross-currency pair with nothing one-sided that still stands apart is
    # ready for its month-end revaluation, which an admin posts from Slack.
    # It is reported once per month end, not every morning.
    lme = month_end_before(as_at).isoformat()
    for p in pairs:
        if "a" not in p or p["a"].get("base") == p["b"].get("base"):
            continue
        unmatched = [t for t in (p.get("unmatched_a") or []) + (p.get("unmatched_b") or [])
                     if abs(float(t.get("base") or 0) * float(rates.get(t.get("entity_base") or "", 1.0) or 1.0)) >= tol]
        fx = p.get("fx_adjustments") or {}
        done = any((t.get("date") or "") == lme for t in (fx.get("a") or []) + (fx.get("b") or []))
        if unmatched or done or abs(float(p.get("break") or 0)) < tol:
            continue
        facts["ready"].append(p)
        rk = f"fx-ready|{p['pair']}|{lme}"
        facts["ready_keys"].append(rk)
        if rk not in reported:
            reasons.append(f"{p['pair']} is ready for its {lme} revaluation and has not been reported ready")
    facts["month_end"] = lme
    return reasons, facts


def interco_report(recon: dict, facts: dict, drive: list[str]) -> str:
    pairs = recon.get("pairs") or []
    out = [f"intercompany · {len(pairs)} pairs · no open break · no session started", "",
           "*agree*",
           f"• {len(facts['agreed'])} of the {len(pairs)} pairs agree"]
    if facts["translation"]:
        out.append(f"• every line matched on {len(facts['translation'])} more, apart only in translation: "
                   + ", ".join(f"{p['pair']} {abs(float(p.get('break') or 0)):,.2f}" for p in facts["translation"]))
    for p in facts["no_mirror"]:
        out.append(f"• no mirror account in {p.get('missing_in')} for {p['pair']}, and nothing on its other side")
    if facts["ready"]:
        out += ["", "*to do by an admin*"]
        out.append(f"• revaluation at {facts['month_end']} ready, not posted: "
                   + ", ".join(p["pair"] for p in facts["ready"]))
    out += ["", "*queries*", "• nothing queried"]
    if drive:
        out += ["", "• both matrices rebuilt · " + " · ".join(drive)]
    return "\n".join(out)


def run_interco(job: str, frm: str, as_at: date) -> tuple[int, str]:
    return sh(job, ["./deploy/run-interco.sh", "--from", frm, "--as-at", as_at.isoformat()], timeout=5400)


def cmd_interco(a) -> int:
    job = "interco"
    if not gates_enabled():
        log(job, "gate: [daily_chain] is switched off; the session runs the recon itself")
        return 1
    load_env()
    started = now()
    as_at = date.today()
    frm = interco_window(as_at)
    with locks(job) as busy:
        if busy:
            log(job, f"gate: {busy}; leaving it to run-agent.sh")
            return 1
        if a.dry_run:
            # Read-only: decide on the last recon on disk, rebuild and publish nothing.
            rc, out, jpath = 0, "", (INTERCO_DIR / "latest.json").resolve()
            as_at = date.fromisoformat(json.loads(jpath.read_text())["as_at"])
        else:
            rc, out = run_interco(job, frm, as_at)
            jpath = INTERCO_DIR / f"interco_recon_{as_at.isoformat()}.json"
            if rc or not jpath.exists() or jpath.stat().st_mtime < started - 5:
                log(job, f"gate: deploy/run-interco.sh failed (exit {rc}); the session runs the recon itself")
                return 1
        gate_file = GATES / f"interco-{as_at.isoformat()}.json"
        drive = [l.strip() for l in out.splitlines() if l.strip().startswith("Drive:")]
        reported_file = GATES / "interco-reported.json"
        reported = set(read_json(reported_file, []) or [])
        try:
            recon = json.loads(jpath.read_text())
            import interco_breaks
            import outstanding
            items = interco_breaks.open_items(recon)
            reasons, facts = interco_decide(recon, outstanding.load(), items, as_at, reported)
        except BaseException as e:                                   # noqa: BLE001 - doubt means run
            reasons, facts, items = [f"the gate could not read the recon ({type(e).__name__}: {e})"], {}, []
        if not a.dry_run:
            # A ready revaluation is reported once: the session starting for it
            # (or the report below) is the telling.
            write_json(reported_file, sorted(reported | set(facts.get("ready_keys") or [])))
            write_json(gate_file, {"started": started, "from": frm, "as_at": as_at.isoformat(),
                                   "json": str(jpath.relative_to(ROOT)) if jpath.is_relative_to(ROOT) else str(jpath),
                                   "reasons": reasons,
                                   "open_items": [{k: i.get(k) for k in ("key", "pair", "flavour", "kind", "size")}
                                                  for i in items], "drive": drive})
        if reasons:
            log(job, "gate: the session runs - " + "; ".join(reasons))
            return RUN
        # Nothing was written to Xero, so there is no phantom-payment check to
        # run here; the nightly check and the next session's still run.
        log(job, "gate: no open break and nothing to post; no session started")
        body = interco_report(recon, facts, drive)
        post(job, [(f"{company_up()} INTERCOMPANY RECONCILIATION ({local(started)})", body)], dry_run=a.dry_run)
        writeback(job, a.dry_run)
        return HANDLED


# ---------------------------------------------------------------- accounts payable

BILLPAY_WORDS = re.compile(r"interco|intercompany|funding leg|\bloan\b", re.I)


def ap_candidates(legs: dict | None = None, bank=None) -> list[dict]:
    """Every open bill paired with a spent bank line of another entity, from
    scripts/billfeed.py's own matching (the same currency and amount to the
    cent, or a Revolut card leg's own bill amount and currency)."""
    import billfeed
    _, cross = billfeed.pairs(0, legs, bank)
    out = []
    for key, line, ent, acct, bdate, payee, score, how in cross:
        p = [x.strip() for x in line.split("|")]
        ccy, amt = p[2].split(" ", 1)
        out.append({"bill": {"entity": key, "date": p[0], "supplier": p[1], "ccy": ccy, "due": float(amt),
                             "account": p[3], "number": p[4], "reference": p[5], "due_date": p[6]},
                    "line": {"entity": ent, "account": acct, "date": bdate, "payee": payee},
                    "matched_on": how, "name_score": score})
    return out


def split_standing(cands: list[dict], items: list[dict]) -> tuple[list[dict], list[dict]]:
    """A pair whose bill number sits in an open bill-payments register line is
    standing: reported from the register, not worked again."""
    lines = [i for i in items if i.get("domain") == "bookkeeping" and i.get("kind") in ("manual", "blocked", "queried")
             and BILLPAY_WORDS.search(i.get("text") or "")]
    new, standing = [], []
    for c in cands:
        num = (c["bill"].get("number") or "").strip()
        hit = next((i for i in lines if len(num) >= 4 and num in f"{i.get('text', '')} {i.get('refs', '')}"), None)
        if hit:
            standing.append({**c, "register": {k: hit.get(k, "") for k in ("id", "key", "kind", "text", "refs")}})
        else:
            new.append(c)
    return new, standing


def load_bank_and_legs():
    import billfeed
    bank = billfeed._bank_lines()
    try:
        legs = billfeed._revolut_legs(bank)
    except Exception as e:                                           # noqa: BLE001 - a bank outage costs the cross-currency rule only
        log("ap-check", f"gate: Revolut legs not read ({type(e).__name__}: {e}); cross-currency pairs not looked for")
        legs = None
    return bank, legs


def cmd_ap_prepare(a) -> int:
    job = "ap-check"
    if not gates_enabled():
        log(job, "gate: [daily_chain] is switched off; the session runs the whole check")
        return 1
    load_env()
    started = now()
    out_dir = AP_DIR / date.today().isoformat()
    with locks(job) as busy:
        if busy:
            log(job, f"gate: {busy}; leaving it to run-agent.sh")
            return 1
        if not a.dry_run:
            rc, _ = sh(job, [PY, "scripts/billfeed.py", "refresh", "all"], timeout=1800)
            if rc:
                log(job, f"gate: the bill feed refresh failed (exit {rc}); the session runs as before")
                return 1
        bills = bill_feed()
        bank, legs = load_bank_and_legs()
        cands = ap_candidates(legs, bank)
        import outstanding
        new, standing = split_standing(cands, outstanding.load())
        short = shorts()
        doc = {"started": started, "built": iso(now()), "bills": {short[k]: len(v) for k, v in bills.items()},
               "new": new, "standing": standing}
        if a.dry_run:
            print(json.dumps(doc, indent=1, default=str))
        else:
            write_json(out_dir / "candidates.json", doc)
            for name in ("report.md", "manual.md", "finished"):
                (out_dir / name).unlink(missing_ok=True)
            # Another run repeating a standing line counts on the register.
            for s in standing:
                sh(job, [PY, "scripts/outstanding.py", "bump", "--id", s["register"]["id"], "--defer"])
        log(job, f"gate: {sum(len(v) for v in bills.values())} open bills, {len(new)} new cross-entity pairs, "
                 f"{len(standing)} standing")
        return RUN if new else HANDLED


def bills_line(counts: dict[str, int]) -> str:
    return f"{sum(counts.values())} open bills checked: " + ", ".join(f"{k} {v}" for k, v in counts.items())


def standing_line(s: dict) -> str:
    r = s["register"]
    return f"• {r['text'].rstrip('.')} · {r['id']} · {r['refs']}".rstrip(" ·")


def bullets(text: str) -> list[str]:
    return [l if l.startswith("•") else f"• {l.lstrip('-* ')}" for l in text.splitlines() if l.strip()]


def build_reconcile_list(job: str, started: float, dry_run: bool) -> tuple[str, list[dict], list[str]]:
    """(the block for the manual items thread, its lines, the blocked line if
    it could not be built). A failed build is registered as blocked and the
    line closed again on the next build that works. Never a DM: the `bills`
    job sends the payables DM, which carries this list."""
    import outstanding
    rc, out = sh(job, [PY, "scripts/reconcile_list.py", "run", "--no-dm"] + (["--dry-run"] if dry_run else []),
                 timeout=3600)
    if dry_run:
        return ("(dry run: the list reconcile_list.py printed is in the log)" if rc == 0 else ""), [], []
    key = "reconcile-list-build"
    open_line = next((i for i in outstanding.load() if i.get("key") == key), None)
    if rc == 0 and RECONCILE_MD.exists() and RECONCILE_MD.stat().st_mtime >= started - 5:
        if open_line:
            sh(job, [PY, "scripts/outstanding.py", "close", "--key", key, "--reason", "the list built again"])
        items = (read_json(RECONCILE_JSON, {}) or {}).get("items") or []
        return RECONCILE_MD.read_text(), items, []
    why = next((l for l in out.splitlines() if l.startswith("FAILED")), f"reconcile_list.py exited {rc}")
    sh(job, [PY, "scripts/outstanding.py", "add", "--domain", "bookkeeping", "--kind", "blocked", "--with", "admin",
             "--key", key, "--text", "build the reconcile list: the bank reconcile check did not run, so nobody "
             "was asked to reconcile a bank line", "--refs", f"scripts/reconcile_list.py · {why[:160]}"])
    rid = next((i["id"] for i in outstanding.load() if i.get("key") == key), "")
    return "", [], [f"• the reconcile list was not built: {why.rstrip('.')}; nothing written and nobody asked "
                    f"to reconcile · {rid}".rstrip(" ·")]


def cmd_ap_finish(a) -> int:
    """The reconcile list, then the report and the manual items thread. Run by
    run-scheduled.sh when no session was needed, and as the last step of the
    session when one was (--session), so its posts join that session's outbox
    and carry its cost, a deferred session's re-run included."""
    job = "ap-check"
    load_env()
    out_dir = ROOT / a.dir if a.dir else AP_DIR / date.today().isoformat()
    cand = read_json(out_dir / "candidates.json", {}) or {}
    started = float(cand.get("started") or now())
    rec_md, rec_items, blocked = build_reconcile_list(job, started, a.dry_run)
    # Inside a session run-agent.sh runs the phantom-payment check after it.
    checks = {} if a.session else end_checks(job, queries=False, dry_run=a.dry_run)

    counts, standing, new = cand.get("bills") or {}, cand.get("standing") or [], cand.get("new") or []
    session_report = (out_dir / "report.md").read_text().strip() if (out_dir / "report.md").exists() else ""
    session_manual = (out_dir / "manual.md").read_text().strip() if (out_dir / "manual.md").exists() else ""
    if a.session and session_report:
        report = [session_report]
    elif a.session:
        report = ["*bill payments*",
                  f"• the session for the {len(new)} new pairs of an open bill and another entity's bank line wrote "
                  f"no report, so none of them is confirmed · data/logs/{job}-{date.today().isoformat()}.log"]
    else:
        lead = ("no new open bill was paid from another entity's bank" if standing
                else "no open bill was paid from another entity's bank")
        report = ["*bill payments*", f"• {lead}, so no spend money was posted"
                  + (f". {bills_line(counts)}" if counts else "")]
    manual = [standing_line(s) for s in standing] + bullets(session_manual)
    by_ent: dict[str, int] = {}
    for i in rec_items:
        name = entity_short(i.get("entity") or "")
        by_ent[name] = by_ent.get(name, 0) + 1
    report_manual = list(manual)
    if rec_items:
        report_manual.append(f"• {len(rec_items)} bank lines to match by hand to what is already posted, listed in "
                             f"the {company_up()} MANUAL ITEMS thread. " + ", ".join(f"{k} {v}" for k, v in by_ent.items()))
    if report_manual:
        report += ["", "*manual*"] + report_manual
    if blocked:
        report += ["", "*blocked*"] + blocked
    if checks:
        report += ["", "• " + " · ".join(f"{k} {v}" for k, v in checks.items())]

    stamp = local(started)
    posts = [(f"{company_up()} ACCOUNTS PAYABLE CHECK ({stamp})", "\n".join(report))]
    if manual or rec_md:
        thread = [f"from the {company_up()} ACCOUNTS PAYABLE CHECK, {stamp}"]
        if manual:
            thread += ["", "*manual*"] + manual
        if rec_md:
            thread += ["", rec_md.rstrip()]
        posts.append((f"{company_up()} MANUAL ITEMS ({stamp})", "\n".join(thread)))
    post(job, posts, dry_run=a.dry_run)
    if not a.dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "finished").write_text(iso(now()) + "\n")
    if not os.environ.get("AGENT_RUN_ID"):
        writeback(job, a.dry_run)
    return HANDLED_FAILED if "FAILED" in checks.values() else 0


# ---------------------------------------------------------------- main

def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    for name, fn, help_ in (("bookkeep", cmd_bookkeep, "gate the bookkeeping session; post the run if skipped"),
                            ("interco", cmd_interco, "run the recon in Python; post it if nothing needs a session"),
                            ("ap-prepare", cmd_ap_prepare, "refresh the bill feed and find the cross-entity pairs"),
                            ("ap-finish", cmd_ap_finish, "build the reconcile list and post the check")):
        s = sub.add_parser(name, help=help_)
        s.add_argument("--dry-run", action="store_true", help="decide and print; write, post and close nothing")
        if name == "ap-finish":
            s.add_argument("--session", action="store_true",
                           help="the last step of the session that posted the legs")
            s.add_argument("--dir", default="", help="the folder ap-prepare wrote, e.g. data/ap_check/<date>")
        s.set_defaults(func=fn)
    c = sub.add_parser("bookkeep-commit", help="record a finished session for the next gate")
    c.add_argument("--rc", type=int, required=True, help="run-agent.sh's exit code")
    c.set_defaults(func=cmd_bookkeep_commit)
    b = sub.add_parser("bookkeep-brief", help="print the bookkeeping session's briefing, building it if none is fresh")
    b.add_argument("--rebuild", action="store_true", help="build it again even when a fresh one exists")
    b.set_defaults(func=cmd_bookkeep_brief)
    a = p.parse_args(argv)
    if os.environ.get("AGENT_READONLY") and not getattr(a, "dry_run", False):
        print("AGENT_READONLY is set: the gates write state and post, so they refuse", file=sys.stderr)
        return 1
    try:
        return a.func(a)
    except Exception as e:                                           # noqa: BLE001 - fail-open: the session runs
        log(a.cmd, f"gate failed ({type(e).__name__}: {e}); the session runs as it always has")
        return 1


if __name__ == "__main__":
    sys.exit(main())
