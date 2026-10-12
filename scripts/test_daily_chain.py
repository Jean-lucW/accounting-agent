#!/usr/bin/env python
"""Offline checks for scripts/daily_chain.py: each gate's skip and start
conditions against fakes, the exit codes run-scheduled.sh reads, fail-open on
any error, the flock chain lock, the baseline of the last finished session and
the AP hand-off. Fictional group from config/group.example.toml; no network,
no Claude session, nothing written outside a temporary folder.

    .venv/bin/python scripts/test_daily_chain.py
"""
from __future__ import annotations

import fcntl
import json
import os
import sys
import tempfile
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TMP = Path(tempfile.mkdtemp(prefix="test_daily_chain_"))
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "src"))
os.environ["AGENT_GROUP_CONFIG"] = str(REPO / "config" / "group.example.toml")
for k in ("AGENT_RUN_ID", "AGENT_READONLY", "AGENT_CHAIN_LOCK_HELD"):
    os.environ.pop(k, None)

import billfeed  # noqa: E402
import daily_chain as dc  # noqa: E402
import outstanding  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("ok   " if cond else "FAIL ") + name + (f" - {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


# ---------------------------------------------------------------- the fake world

dc.GATES, dc.LOGS, dc.RUNS, dc.RETRY = TMP / "gates", TMP / "logs", TMP / "runs", TMP / "retry"
dc.BANKFEED, dc.BILLFEED, dc.AP_DIR, dc.INTERCO_DIR = TMP / "bank", TMP / "bill", TMP / "ap", TMP / "interco"
dc.CHAIN_LOCK = TMP / "data" / "daily-chain.lock"
dc.LEDGER = TMP / "LEDGER.md"
dc.RECONCILE_MD, dc.RECONCILE_JSON = TMP / "reconcile_list.md", TMP / "reconcile_list.json"
dc.ROOT = TMP                      # job locks and relative paths land in the temporary folder
billfeed.FEED, billfeed.BANKFEED = TMP / "bill", TMP / "bank"
for d in (dc.BANKFEED, dc.BILLFEED, dc.INTERCO_DIR):
    d.mkdir(parents=True, exist_ok=True)

REGISTER: list[dict] = []
outstanding.load = lambda: REGISTER

POSTS: list[tuple[str, str]] = []
CMDS: list[list[str]] = []
CHECKS = {"phantom-payment check": "PASS", "register query check": "PASS"}
BANK_RC = [0]
SLACK = {"msgs": [], "replies": 0, "boom": False}
MAIL = {"n": 0, "found": [], "boom": False}
SH_RC: dict[str, int] = {}
WRITEBACKS: list[str] = []


def fake_sh(job, cmd, timeout=None):
    CMDS.append(list(cmd))
    key = next((k for k in SH_RC if k in " ".join(cmd)), "")
    return (SH_RC[key] if key else 0), ""


def fake_slack(since, window_start):
    if SLACK["boom"]:
        raise RuntimeError("slack down")
    return list(SLACK["msgs"]), SLACK["replies"]


def fake_mail(since, window_start, deep):
    if MAIL["boom"]:
        raise RuntimeError("gmail down")
    return MAIL["n"], list(MAIL["found"]) if deep else []


dc.sh = fake_sh
dc.load_env = lambda: None
dc.refresh_bankfeed = lambda job: BANK_RC[0]
dc.slack_intake = fake_slack
dc.gmail_intake = fake_mail
dc.post = lambda job, posts, dry_run=False: POSTS.extend(posts)
dc.end_checks = lambda job, queries, dry_run: dict(CHECKS)
dc.writeback = lambda job, dry_run: WRITEBACKS.append(job)


def reset() -> None:
    POSTS.clear(), CMDS.clear(), REGISTER.clear(), WRITEBACKS.clear(), SH_RC.clear()
    CHECKS.update({"phantom-payment check": "PASS", "register query check": "PASS"})
    BANK_RC[0] = 0
    SLACK.update({"msgs": [], "replies": 0, "boom": False})
    MAIL.update({"n": 0, "found": [], "boom": False})
    for d in (dc.GATES, dc.RETRY):
        if d.exists():
            for f in d.glob("*"):
                f.unlink()
    for f in dc.BANKFEED.glob("*.md"):
        f.unlink()
    dc.LEDGER.write_text("")
    dc.gates_enabled = lambda: True


def args(**kw):
    class A:
        dry_run = False
        session = False
        dir = ""
        rc = 0
    a = A()
    for k, v in kw.items():
        setattr(a, k, v)
    return a


FEED_LINE = "2026-10-02 | Corner Cafe | spent | GBP 7.60 | A. Holder | | rev:1a2b3c01"


def write_feed(*lines: str) -> None:
    (dc.BANKFEED / "holdco.md").write_text("# Bank feed: Example Holdings Limited (HoldCo)\n\n"
                                           "## GBP Main · last checked 2026-10-02T10:00Z\n" + "\n".join(lines) + "\n")


def baseline(feed_lines=(FEED_LINE,), answered=(), started=None) -> float:
    started = started or (time.time() - 86400)
    write_feed(*feed_lines)
    keys = sorted(dc.feed_key(l) for l in dc.feed_lines())
    dc.write_json(dc.GATES / "bookkeep.json", {"session_started": started, "feed": keys, "answered": list(answered)})
    return started


def stamp_line(ts: float, src: str) -> str:
    d = datetime.fromtimestamp(ts, timezone.utc)
    return f"{d:%Y-%m-%d} {d:%H:%M} | {src} | A. Sender | 1/1 | Acme Hosting | INV-1 | HOLDCO | posted\n"


# ---------------------------------------------------------------- ledger helpers

t0 = time.time() - 3 * 86400
stamps = dc.ledger_stamps(stamp_line(t0, "slack") + stamp_line(t0 + 3600, "email"))
check("ledger line: a slack time is read", dc.in_ledger(t0, stamps["slack"]) and not dc.in_ledger(t0, stamps["email"]))
check("ledger line: a time 10 minutes off is not the message", not dc.in_ledger(t0 + 600, stamps["slack"]))
multi = dc.ledger_stamps("2026-10-02 23:50 + 00:10 | slack | A. Sender | 2/2 | x | y | HOLDCO | posted\n")
day = datetime(2026, 10, 3, 0, 10, tzinfo=timezone.utc).timestamp()
check("ledger line: a second time earlier than the first is taken on the next day too",
      dc.in_ledger(day, multi["slack"]))

# ---------------------------------------------------------------- bookkeep gate: skip

reset()
since = baseline()
rc = dc.cmd_bookkeep(args())
check("bookkeep: nothing new skips the session (exit 10)", rc == dc.HANDLED, str(rc))
check("bookkeep: the skip posts one run headed with the company name",
      len(POSTS) == 1 and POSTS[0][0].startswith("EXAMPLE GROUP BOOKKEEPING RUN (")
      and "nothing needed judgement" in POSTS[0][1], str(POSTS))
check("bookkeep: the skip writes the write-back commit", WRITEBACKS == ["bookkeep"])
check("bookkeep: a skip leaves the baseline alone",
      dc.read_json(dc.GATES / "bookkeep.json")["session_started"] == since)

reset()
baseline()
CHECKS["phantom-payment check"] = "FAILED"
check("bookkeep: a failed exit check on a skip stops the chain (exit 11)", dc.cmd_bookkeep(args()) == dc.HANDLED_FAILED)
check("bookkeep: the failed check is in the report", "FAILED" in POSTS[0][1])

# ---------------------------------------------------------------- bookkeep gate: start

cases = {}


def start_case(name, setup, expect):
    reset()
    baseline()
    setup()
    POSTS.clear()
    rc = dc.cmd_bookkeep(args())
    reasons = dc.read_json(dc.GATES / "bookkeep.pending.json", {}).get("reasons", [])
    check(f"bookkeep: {name} starts the session (exit 0)", rc == dc.RUN and not POSTS, f"{rc} {reasons}")
    check(f"bookkeep: {name} says why", any(expect in r for r in reasons), str(reasons))


def _msg():
    SLACK["msgs"] = [{"ts": time.time() - 60, "user": "U0000000002", "files": 0, "channel": "D1"}]


start_case("a new Slack app message", _msg, "messages to the Slack app")
start_case("a thread reply", lambda: SLACK.update(replies=1), "threads with a reply")
start_case("a new inbox email", lambda: MAIL.update(n=2), "emails in the accounting inbox")
start_case("a Slack file message with no ledger line",
           lambda: SLACK.update(msgs=[{"ts": time.time() - 2 * 86400, "user": "U0000000002", "files": 1, "channel": "D1"}]),
           "no ledger line")
start_case("an inbox attachment with no ledger line",
           lambda: MAIL.update(found=[{"id": "m1", "stamps": [time.time() - 2 * 86400]}]), "no ledger line")
start_case("a new bank feed line", lambda: write_feed(FEED_LINE, "2026-10-06 | Station Deli | spent | GBP 11.45 | B. Holder | | rev:1a2b3c02"),
           "bank feed lines the last session did not have")
start_case("a line awaiting a run-fetched document",
           lambda: write_feed(FEED_LINE, FEED_LINE.replace("rev:1a2b3c01", "awaiting fetch · rev:1a2b3c01")) or
           (dc.BANKFEED / "holdco.md").write_text(
               "## GBP Main · last checked 2026-10-02T10:00Z\n" + FEED_LINE.replace("| |", "| awaiting fetch |") + "\n"),
           "wait on a document the run fetches")
start_case("a bank feed refresh that failed", lambda: BANK_RC.__setitem__(0, 3), "refresh failed")
start_case("an investigation due today",
           lambda: REGISTER.append({"id": "BK-001", "domain": "bookkeeping", "kind": "queried", "key": "q-1",
                                    "certainty": None, "investigation_scheduled": date.today().isoformat()}),
           "investigation is due")
start_case("an admin answer not yet acted on",
           lambda: REGISTER.append({"id": "BK-002", "domain": "bookkeeping", "kind": "queried", "key": "q-2",
                                    "answered": "yes, post it", "certainty": 4, "grade": "high", "gate": "act"}),
           "has answered")
start_case("a query the register check would fail",
           lambda: REGISTER.append({"id": "BK-003", "domain": "bookkeeping", "kind": "queried", "key": "q-3",
                                    "certainty": None}), "register query check would fail")
start_case("a deferred run waiting for the rate limit",
           lambda: (dc.RETRY.mkdir(exist_ok=True), (dc.RETRY / "bookkeep.retry").write_text("1\tx\n")),
           "deferred bookkeeping run")
start_case("a Slack read that fails (fail-open)", lambda: SLACK.update(boom=True), "could not finish")
start_case("a Gmail read that fails (fail-open)", lambda: MAIL.update(boom=True), "could not finish")

reset()
write_feed(FEED_LINE)
check("bookkeep: no record of an earlier session starts the session", dc.cmd_bookkeep(args()) == dc.RUN and not POSTS)

# An interco line on the register is the intercompany run's, not a reason here.
reset()
baseline()
REGISTER.append({"id": "BK-009", "domain": "bookkeeping", "kind": "queried", "key": "interco-x-2026-10-01-100",
                 "answered": "post it", "certainty": 4, "grade": "high", "gate": "act"})
check("bookkeep: an interco register line is left to the interco gate", dc.cmd_bookkeep(args()) == dc.HANDLED)

# A Slack file message that does have its ledger line is not a gap.
reset()
baseline()
ts = time.time() - 2 * 86400
dc.LEDGER.write_text(stamp_line(ts, "slack") + stamp_line(ts + 30, "email"))
SLACK["msgs"] = [{"ts": ts, "user": "U0000000002", "files": 2, "channel": "D1"}]
MAIL["found"] = [{"id": "m1", "stamps": [ts + 30]}]
check("bookkeep: every attachment message in the ledger skips the session", dc.cmd_bookkeep(args()) == dc.HANDLED)
check("bookkeep: the report counts them", "1 in the Slack app, 1 in the inbox" in POSTS[0][1], POSTS[0][1])

# disabled, dry run, lock busy
reset()
baseline()
dc.gates_enabled = lambda: False
check("bookkeep: [daily_chain] enabled = false starts the session without looking", dc.cmd_bookkeep(args()) == dc.RUN and not CMDS)
reset()
baseline()
check("bookkeep: --dry-run decides, posts nothing and writes no pending file",
      dc.cmd_bookkeep(args(dry_run=True)) == dc.HANDLED and not (dc.GATES / "bookkeep.pending.json").exists())
reset()
baseline()
(TMP / "data").mkdir(exist_ok=True)
held = open(TMP / "data" / "bookkeep.lock", "a")
fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
check("bookkeep: a session holding the job lock is left to run-agent.sh (exit 0)",
      dc.cmd_bookkeep(args()) == dc.RUN and not POSTS)
fcntl.flock(held, fcntl.LOCK_UN)
held.close()

# ---------------------------------------------------------------- bookkeep-commit

reset()
t = time.time() - 600
dc.write_json(dc.GATES / "bookkeep.pending.json", {"gate_started": t, "feed": ["k"], "answered": ["a|b"], "reasons": [], "facts": {}})
dc.LOGS.mkdir(parents=True, exist_ok=True)
log_file = dc.LOGS / f"bookkeep-{datetime.now(timezone.utc):%Y-%m-%d}.log"
log_file.write_text(f"{dc.iso(t + 5)} [bookkeep] start\n{dc.iso(t + 60)} [bookkeep] finished rc=0\n")
dc.cmd_bookkeep_commit(args(rc=1))
check("commit: a session that exited non-zero leaves the old record", not (dc.GATES / "bookkeep.json").exists())
log_file.write_text(f"{dc.iso(t + 5)} [bookkeep] start\n{dc.iso(t + 20)} [bookkeep] Xero daily rate limit reached, deferred to x\n")
dc.cmd_bookkeep_commit(args(rc=0))
check("commit: a deferred session leaves the old record", not (dc.GATES / "bookkeep.json").exists())
log_file.write_text(f"{dc.iso(t + 5)} [bookkeep] start\n{dc.iso(t + 60)} [bookkeep] finished rc=0\n")
dc.cmd_bookkeep_commit(args(rc=0))
rec = dc.read_json(dc.GATES / "bookkeep.json", {})
check("commit: a finished session becomes the baseline", rec.get("session_started") == t and rec.get("feed") == ["k"], str(rec))
check("commit: the pending file is cleared", not (dc.GATES / "bookkeep.pending.json").exists())

# ---------------------------------------------------------------- interco gate

TOL = float(dc.config.intercompany_settings()["tolerance_abs"])


def txn(entity, base, amount, doc_id, date_="2026-10-03"):
    return {"entity": entity, "entity_base": base, "base": amount, "doc_amount": amount, "currency": base,
            "date": date_, "source": "MJ", "party": "", "desc": "monthly entry", "doc_id": doc_id, "code": "1620"}


def pair(name, a_base, b_base, status, brk, unmatched_a=(), fx_dates=(), flavour="loan"):
    fx = [txn("HOLDCO", a_base, 1.0, "fx", d) for d in fx_dates]
    return {"pair": name, "flavour": flavour, "break": brk, "status": status,
            "a": {"entity": "HOLDCO", "code": "1620", "base": a_base, "balance": 100.0},
            "b": {"entity": "OPCO_US", "code": "2620", "base": b_base, "balance": -100.0},
            "matched": [], "groups": [], "probable": [], "confirmed": [],
            "fx_adjustments": {"a": fx, "b": []}, "contras": {"a": [], "b": []},
            "unmatched_a": list(unmatched_a), "unmatched_b": []}


AS_AT = date(2026, 10, 7)
LME = "2026-09-30"


def recon(*pairs):
    return {"window": ["2026-01-01", "2026-10-07"], "as_at": AS_AT.isoformat(), "reporting_currency": "USD",
            "rates": {"USD": 1.0, "GBP": 1.3}, "pairs": list(pairs)}


def decide(rc, items=(), reported=()):
    import interco_breaks
    return dc.interco_decide(rc, list(items), interco_breaks.open_items(rc), AS_AT, set(reported))


r, f = decide(recon(pair("HOLDCO<>OPCO_US / loan", "GBP", "USD", "AGREED", 0.0)))
check("interco: an agreed pair needs no session", not r and len(f["agreed"]) == 1, str(r))
r, _ = decide(recon(pair("HOLDCO<>OPCO_US / loan", "GBP", "USD", "BREAK (2 unmatched)", 900.0)))
check("interco: a break starts the session", any("breaks by" in x for x in r), str(r))
r, _ = decide(recon(pair("HOLDCO<>OPCO_US / loan", "USD", "USD", "small diff (1 unmatched)", 40.0)))
check("interco: a small gap inside one currency starts the session", any("one currency" in x for x in r), str(r))
r, f = decide(recon(pair("HOLDCO<>OPCO_US / loan", "GBP", "USD", "small diff (0 unmatched)", 40.0, fx_dates=(LME,))))
check("interco: a small gap across currencies is translation, no session", not r and len(f["translation"]) == 1, str(r))
r, _ = decide(recon(pair("HOLDCO<>OPCO_US / loan", "GBP", "USD", "small diff (1 unmatched)", 40.0,
                         unmatched_a=[txn("HOLDCO", "GBP", 500.0, "d1")], fx_dates=())))
check("interco: a one-sided item is an open break item", any("open items" in x for x in r), str(r))
r, _ = decide(recon({"pair": "HOLDCO<>OPCO_EU / loan", "status": "no_mirror_account", "missing_in": "OPCO_EU",
                     "other_balance": 250.0}))
check("interco: a missing mirror with a balance on the other side starts the session", any("no mirror account" in x for x in r))
r, f = decide(recon({"pair": "HOLDCO<>OPCO_EU / loan", "status": "no_mirror_account", "missing_in": "OPCO_EU",
                     "other_balance": 0.0}))
check("interco: a missing mirror with nothing on the other side does not", not r and len(f["no_mirror"]) == 1, str(r))
r, _ = decide(recon(pair("HOLDCO<>OPCO_US / loan", "GBP", "USD", "AGREED", 0.0)),
              items=[{"id": "BK-020", "key": "interco-x", "answered": "post it"}])
check("interco: an admin answer waiting starts the session", any("has answered" in x for x in r), str(r))
r, _ = decide(recon(pair("HOLDCO<>OPCO_US / loan", "GBP", "USD", "AGREED", 0.0)),
              items=[{"id": "BK-021", "key": "interco-y", "certainty": 4}])
check("interco: a verdict waiting starts the session", any("verdict" in x for x in r), str(r))
r, _ = decide(recon(pair("HOLDCO<>OPCO_US / loan", "GBP", "USD", "AGREED", 0.0)),
              items=[{"id": "BK-022", "key": "INV-1", "answered": "x"}])
check("interco: a bookkeeping answer is not the interco run's", not r, str(r))
r, f = decide(recon(pair("HOLDCO<>OPCO_US / loan", "GBP", "USD", "small diff (0 unmatched)", 40.0)))
check("interco: a ready revaluation not yet reported starts the session",
      any("ready for its" in x for x in r) and f["ready_keys"], str(r))
r, f = decide(recon(pair("HOLDCO<>OPCO_US / loan", "GBP", "USD", "small diff (0 unmatched)", 40.0)), reported=f["ready_keys"])
check("interco: a ready revaluation already reported does not start it again", not r and len(f["ready"]) == 1, str(r))

# the whole command
GOOD = recon(pair("HOLDCO<>OPCO_US / loan", "GBP", "USD", "AGREED", 0.0))
BAD = recon(pair("HOLDCO<>OPCO_US / loan", "GBP", "USD", "BREAK (2 unmatched)", 900.0))
RUN_OUT = {"rc": 0, "recon": GOOD, "write": True}


def fake_run_interco(job, frm, as_at):
    CMDS.append(["run-interco", frm, as_at.isoformat()])
    if RUN_OUT["write"]:
        (dc.INTERCO_DIR / f"interco_recon_{as_at.isoformat()}.json").write_text(json.dumps(RUN_OUT["recon"]))
    return RUN_OUT["rc"], "Drive: published to the Drive folder\n"


dc.run_interco = fake_run_interco
today = date.today()
for f_ in dc.INTERCO_DIR.glob("*.json"):
    f_.unlink()
reset()
RUN_OUT.update(rc=0, recon={**GOOD, "as_at": today.isoformat()}, write=True)
rc = dc.cmd_interco(args())
check("interco: nothing breaking skips the session (exit 10)", rc == dc.HANDLED, str(rc))
check("interco: the skip posts the report with the company name",
      len(POSTS) == 1 and POSTS[0][0].startswith("EXAMPLE GROUP INTERCOMPANY RECONCILIATION (")
      and "no session started" in POSTS[0][1], str(POSTS))
check("interco: the recon window starts at the financial year (or the quarter before in its first quarter)",
      CMDS[0][1] <= today.isoformat() and CMDS[0][2] == today.isoformat(), str(CMDS[0]))
check("interco: the gate state is written", (dc.GATES / f"interco-{today.isoformat()}.json").exists())

reset()
RUN_OUT.update(rc=0, recon={**BAD, "as_at": today.isoformat()}, write=True)
check("interco: a break starts the session (exit 0) and posts nothing", dc.cmd_interco(args()) == dc.RUN and not POSTS)
gate = dc.read_json(dc.GATES / f"interco-{today.isoformat()}.json", {})
check("interco: the reasons are in the gate file the session reads", any("breaks by" in x for x in gate.get("reasons", [])))

reset()
RUN_OUT.update(rc=2, write=False)
for f_ in dc.INTERCO_DIR.glob("*.json"):
    f_.unlink()
check("interco: run-interco.sh failing fails open (exit 1)", dc.cmd_interco(args()) == 1 and not POSTS)
reset()
RUN_OUT.update(rc=0, write=False)
check("interco: no recon file written fails open (exit 1)", dc.cmd_interco(args()) == 1)
reset()
RUN_OUT.update(rc=0, write=True, recon={"pairs": "not a list"})
check("interco: an unreadable recon starts the session (exit 0)", dc.cmd_interco(args()) == dc.RUN and not POSTS)
reset()
dc.gates_enabled = lambda: False
CMDS.clear()
check("interco: disabled fails open without running the recon", dc.cmd_interco(args()) == 1 and not CMDS)

# ---------------------------------------------------------------- the interco window

check("interco window: late in the year starts at the financial year start",
      dc.interco_window(date(2026, 10, 7)) == dc.config.fy_start_for(date(2026, 10, 7)).isoformat())
check("interco window: early in the year reaches back a quarter",
      dc.interco_window(date(2026, 1, 8)) < dc.config.fy_start_for(date(2026, 1, 8)).isoformat())

# ---------------------------------------------------------------- AP gate

BILL = "2026-10-01 | Acme Hosting | USD 120.00 | Hosting | INV-1001 | PO 7 | 2026-10-31 | xid:abcd1234\n"
OTHER_BANK = ("# Bank feed: OpCo US\n\n## USD Main · last checked 2026-10-02T10:00Z\n"
              "2026-10-02 | Acme Hosting | spent | USD 120.00 | A. Holder | | paid by card\n")


def ap_world(bank=OTHER_BANK, bill=BILL):
    for d in (dc.BILLFEED, dc.BANKFEED):
        for f_ in d.glob("*.md"):
            f_.unlink()
    (dc.BILLFEED / "holdco.md").write_text("# Bill feed: Example Holdings Limited · pulled 2026-10-03T01:00Z\n" + bill)
    if bank:
        (dc.BANKFEED / "opco-us.md").write_text(bank)


reset()
ap_world()
rc = dc.cmd_ap_prepare(args())
cand = dc.read_json(dc.AP_DIR / date.today().isoformat() / "candidates.json", {})
check("ap: a bill another entity's bank paid is a new pair, the session starts (exit 0)", rc == dc.RUN, str(rc))
check("ap: the pair is in candidates.json with what it matched on",
      len(cand.get("new", [])) == 1 and cand["new"][0]["bill"]["number"] == "INV-1001"
      and cand["new"][0]["line"]["entity"] == "opco-us" and cand["new"][0]["matched_on"] == "same currency and amount",
      str(cand))
check("ap: the bill feed is refreshed first", any("billfeed.py" in " ".join(c) and "refresh" in c for c in CMDS))

reset()
ap_world(bank="")
check("ap: no cross-entity pair needs no session (exit 10)", dc.cmd_ap_prepare(args()) == dc.HANDLED)

reset()
ap_world(bank=OTHER_BANK.replace("opco", "holdco").replace("OpCo US", "HoldCo"))
(dc.BANKFEED / "holdco.md").write_text(OTHER_BANK)
(dc.BANKFEED / "opco-us.md").unlink(missing_ok=True)
check("ap: a bill paid from its own entity's bank is not a cross-entity pair (exit 10)", dc.cmd_ap_prepare(args()) == dc.HANDLED)

reset()
ap_world()
REGISTER.append({"id": "BK-050", "domain": "bookkeeping", "kind": "manual", "key": "interco-bill-INV-1001",
                 "text": "reconcile the Acme Hosting bill to intercompany by hand", "refs": "INV-1001"})
rc = dc.cmd_ap_prepare(args())
cand = dc.read_json(dc.AP_DIR / date.today().isoformat() / "candidates.json", {})
check("ap: a pair already on the register is standing, not new (exit 10)",
      rc == dc.HANDLED and not cand["new"] and len(cand["standing"]) == 1, f"{rc} {cand}")
check("ap: a standing pair is counted on the register, not worked",
      any("bump" in c and "BK-050" in c for c in CMDS), str(CMDS))

reset()
ap_world()
SH_RC["billfeed.py"] = 1
check("ap: a bill feed refresh that fails fails open (exit 1)", dc.cmd_ap_prepare(args()) == 1)
reset()
dc.gates_enabled = lambda: False
check("ap: disabled fails open (exit 1)", dc.cmd_ap_prepare(args()) == 1)

# ap-finish
dc.build_reconcile_list = lambda job, started, dry: ("*to match in Xero*\n• HoldCo · 2 Oct · GBP 7.60 · INV-2", [{"entity": "HOLDCO"}], [])
reset()
d = dc.AP_DIR / date.today().isoformat()
dc.write_json(d / "candidates.json", {"started": time.time() - 60, "bills": {"HoldCo": 4}, "new": [], "standing": []})
(d / "finished").unlink(missing_ok=True)
rc = dc.cmd_ap_finish(args(dir=str(d)))
check("ap-finish: a quiet day posts the check and the manual items thread, exit 0",
      rc == 0 and [h.split(" (")[0] for h, _ in POSTS] == ["EXAMPLE GROUP ACCOUNTS PAYABLE CHECK", "EXAMPLE GROUP MANUAL ITEMS"],
      str(POSTS))
check("ap-finish: the report says nothing was posted and counts the bills",
      "no open bill was paid from another entity's bank" in POSTS[0][1] and "4 open bills checked" in POSTS[0][1])
check("ap-finish: the finished marker is written", (d / "finished").exists())
check("ap-finish: reconcile_list.py always runs with --no-dm (the bills job owns the payables DM)",
      '"--no-dm"' in Path(dc.__file__).read_text())

reset()
(d / "report.md").write_text("*bill payments*\n• Acme Hosting posted to the intercompany account · INV-1001")
(d / "manual.md").write_text("reconcile INV-1001 to intercompany by hand")
dc.write_json(d / "candidates.json", {"started": time.time() - 60, "bills": {"HoldCo": 4},
                                      "new": [{"bill": {}}], "standing": []})
rc = dc.cmd_ap_finish(args(dir=str(d), session=True))
check("ap-finish --session: the session's own report and manual lines are posted, no exit checks of its own",
      rc == 0 and "Acme Hosting posted" in POSTS[0][1] and "reconcile INV-1001" in POSTS[1][1], str(POSTS))
(d / "report.md").unlink()
reset()
dc.cmd_ap_finish(args(dir=str(d), session=True))
check("ap-finish --session: a session that wrote no report is said so, not hidden", "wrote no report" in POSTS[0][1], str(POSTS))
(d / "manual.md").unlink()

reset()
CHECKS["phantom-payment check"] = "FAILED"
check("ap-finish: a failed exit check returns 11", dc.cmd_ap_finish(args(dir=str(d))) == dc.HANDLED_FAILED)

# ---------------------------------------------------------------- the locks

os.environ.pop("AGENT_CHAIN_LOCK_HELD", None)
with dc.chain_lock() as got:
    check("lock: the chain lock is free for a hand run", got is True)
    other = open(dc.CHAIN_LOCK, "a")
    try:
        fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        second = False
    except BlockingIOError:
        second = True
    other.close()
    check("lock: it really is held while the gate works", second)
holder = open(dc.CHAIN_LOCK, "a")
fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
with dc.chain_lock() as got:
    check("lock: a hand run refuses while another chain holds data/daily-chain.lock", got is False)
with dc.locks("bookkeep") as busy:
    check("lock: locks() gives the reason", "daily-chain.lock" in busy, busy)
os.environ["AGENT_CHAIN_LOCK_HELD"] = "1"
with dc.chain_lock() as got:
    check("lock: inside the chain (AGENT_CHAIN_LOCK_HELD) the shell's own lock is trusted, not flocked again", got is True)
with dc.locks("bookkeep") as busy:
    check("lock: and the job lock is then free", busy == "")
os.environ.pop("AGENT_CHAIN_LOCK_HELD")
fcntl.flock(holder, fcntl.LOCK_UN)
holder.close()
check("lock: the chain lock file is the one run-scheduled.sh uses",
      "daily-chain.lock" in (REPO / "deploy" / "run-scheduled.sh").read_text() and dc.CHAIN_LOCK.name == "daily-chain.lock")

# ---------------------------------------------------------------- post(): the cost bracket

queued: list[dict] = []
import slack_outbox  # noqa: E402

slack_outbox.queue = lambda **kw: (queued.append({**kw, "run": os.environ.get("AGENT_RUN_ID", "")}) or "queued")
ARGS_SEEN: list[list[str]] = []


def post_sh(job, cmd, timeout=None):
    ARGS_SEEN.append(cmd)
    return 0, ("job-20261007T000000Z-1\n" if "begin" in cmd else "(total cost $0.00, run $0.00)\n")


dc.sh = post_sh
real_post = None
import importlib.util  # noqa: E402

src = importlib.util.spec_from_file_location("daily_chain_real", dc.__file__)
real = importlib.util.module_from_spec(src)
src.loader.exec_module(real)
real.RUNS = TMP / "runs"
real.LOGS = TMP / "logs"
real.sh = post_sh
real.post("bookkeep", [("EXAMPLE GROUP BOOKKEEPING RUN (x)", "body")])
check("post: outside a session it opens a run record, queues under its id and stamps it at nil cost",
      queued and queued[0]["run"] == "job-20261007T000000Z-1"
      and any("stamp" in c for c in ARGS_SEEN) and "AGENT_RUN_ID" not in os.environ, str(queued))
queued.clear()
os.environ["AGENT_RUN_ID"] = "session-run-1"
ARGS_SEEN.clear()
real.post("ap-check", [("EXAMPLE GROUP ACCOUNTS PAYABLE CHECK (x)", "body")])
check("post: inside a session it only joins the session's outbox, which the runner stamps with the cost",
      queued and queued[0]["run"] == "session-run-1" and not ARGS_SEEN)
os.environ.pop("AGENT_RUN_ID")
real.post("bookkeep", [("h", "  ")])
check("post: an empty body posts nothing", len(queued) == 1)

# ---------------------------------------------------------------- main: read-only and fail-open

os.environ["AGENT_READONLY"] = "1"
check("main: AGENT_READONLY refuses a gate that writes (exit 1, so the read-only session runs as before)",
      dc.main(["bookkeep"]) == 1)
os.environ.pop("AGENT_READONLY")
def _boom(a):
    raise RuntimeError("boom")


dc.cmd_bookkeep = _boom
check("main: an unexpected error is exit 1, so the session runs as it always has", dc.main(["bookkeep"]) == 1)

print()
if FAILS:
    print(f"FAILED: {len(FAILS)}")
    sys.exit(1)
print("PASS")
