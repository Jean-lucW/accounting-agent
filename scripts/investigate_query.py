#!/usr/bin/env python
"""Gather the evidence for a query before anyone is asked it.

Every question a bookkeeping run is about to put to an admin goes through here
first. Plain Python answers what it can; the rest goes to a coordinator
session that reads the rules, sends small-model researchers for the data,
decides the answer and rates its own certainty from 1 to 5. The investigation
only gathers evidence and proposes: certainty 5 and 4 map to the grade `high`,
3 to `medium`, 1 and 2 to `low`, and the run takes that grade, and the amount
the answer moves, to `gate` (the same decision as scripts/resolve_gate.py,
from [auto_resolve] in config/group.toml), which says act, confirm or query.
Nothing here decides what the agent may do alone.

    python scripts/investigate_query.py open --domain bookkeeping \\
        --key example-transfers-01-02oct --with <admin> \\
        --question "do the two USD 37,250 transfers post on the usual pattern" \\
        --context "what the run already checked" \\
        --refs "Example Holdings Limited · 01-14 Oct · USD 80,000.00" \\
        --txn "HOLDCO|2026-10-13|37250|USD|Example Payee"
    python scripts/investigate_query.py open --from-register OI-0901
    python scripts/investigate_query.py wait --key example-transfers-01-02oct
    python scripts/investigate_query.py wait --all-open      # every one this run opened
    python scripts/investigate_query.py show --key example-transfers-01-02oct
    python scripts/investigate_query.py gate --key example-transfers-01-02oct [--amount 80000]
    python scripts/investigate_query.py latest --key example-transfers-01-02oct
    python scripts/investigate_query.py list [--today]
    python scripts/investigate_query.py due [--open]         # register queries whose deferred day has come
    python scripts/investigate_query.py spend                # today's investigation cost against the cap

`open` writes the brief and returns at once. A detached worker then runs the
pre-check (plain Python, read-only Xero, below) and routes the query one of
three ways:

  settled  the pre-check answers it under a conservative rule (settle_from_precheck):
           every transaction the query names has exactly one live record in
           Xero, created by somebody after the query was raised, and the query
           says nothing was posted. The verdict is written here, decided_by
           precheck, with no session.
  reused   the last verdict for the same key is done, under reuse_days old,
           safe to act on twice, and nothing it rested on has changed: the same
           brief and the same pre-check evidence (reuse_fingerprints). Copied
           with reused_from, no session. --fresh forces a new investigation.
  queued   everything else joins its run's batch.

One coordinator session works one batch: every queued query of one run
(AGENT_RUN_ID; INVESTIGATION_BATCH or --batch otherwise; a hand-run in one
shell forms its own). A batch starts on the first `wait` that covers one of its
queries, after debounce_seconds with no new query queued, or once batch_max are
queued, whichever comes first; a query queued after its batch started goes in
the next. The session's final message is a JSON array, one verdict per key;
each is validated on its own, and a missing or broken one fails that key only.
The session output is stored once, under _batches/<batch_id>/session.json, and
each result carries the batch id and an equal share of the cost.

A daily cap (daily_cap_usd, UTC day, the CLI's total_cost_usd) is checked
before a batch starts. At or over it, every queued query of the group is
written `status: deferred` with the day it will run and the register command
the run must use; a batch whose estimate would take the day more than 10% over
the cap is cut down to what fits. Deferred is never dropped: `due --open`
reopens it. Each batch's cost is also written as an extra cost file for the
run (data/runs/<run id>/extra/, scripts/run_cost.py) so it shows on the run's
headline.

`wait` blocks for at most --timeout seconds (default 540, under the 10 minute
ceiling on one tool call) and exits 75 while anything is still pending, so the
caller simply calls it again.

The pre-check, before any agent, looks the query's transactions up in Xero and
the register: `--txn "<entity>|<YYYY-MM-DD>|<amount>[|<CCY>[|<payee>]]"` names
them, and a brief built `--from-register` has them read out of the line's
references. Besides the exact total it matches every payment, bank transaction
and document in the window on the payment's bank-side amount, the amount
converted from another currency and the payee's name, whatever the amount
(accounting_agent.xero.matching, skipped when that module is absent).

Every investigation is READ-ONLY. The pre-check and the coordinator run with
AGENT_READONLY=1, so XeroClient refuses every non-GET and
.claude/hooks/guard-readonly.py denies every other write; the CLI starts with
Write/Edit disallowed. Writes stay with the run that asked. The result is a
plan with exact payloads, not a posting, and it is never written back as a
rule.

Files, one folder per investigation, under data/investigations/<key>/<stamp>/:
  brief.json     what was asked and what the run had already checked
  precheck.json  the transactions found in Xero and the register lines on them
  queued.json    waiting for a batch;  batch.json  the batch it went into
  result.json    the verdict: certainty, grade, answer, evidence, actions
and per batch under data/investigations/_batches/<batch_id>/:
  manifest.json  the queries, the estimate;  session.json  the raw CLI output
  verdicts/      each verdict as the coordinator decided it;  run.log  stderr

Protocol and the certainty scale: .claude/skills/query-investigation/SKILL.md.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
from accounting_agent import config  # noqa: E402

# INVESTIGATION_HOME is for the offline tests only: the data folders move to a
# scratch tree. Code paths (src/, the agents) always come from REPO.
ROOT = Path(os.environ.get("INVESTIGATION_HOME") or REPO)
BASE = ROOT / "data" / "investigations"
BATCHES = BASE / "_batches"
QUEUE = BATCHES / "queue"
REGISTER = ROOT / "docs" / "bookkept" / "outstanding.json"
RUNS = ROOT / "data" / "runs"
CLAUDE_BIN = os.environ.get("INVESTIGATION_CLAUDE_BIN", "claude")

COORDINATOR_AGENT = "query-investigator"
_INV = config.investigation()
ENABLED = _INV["enabled"]
COORDINATOR_MODEL = _INV["coordinator_model"]
RESEARCHER_MODEL = _INV["researcher_model"]
COORDINATOR_EFFORT = _INV["effort"]
BATCH_MAX = _INV["batch_max"]
DEBOUNCE = _INV["debounce_seconds"]
DAILY_CAP = _INV["daily_cap_usd"]
REUSE_DAYS = _INV["reuse_days"]
RESEARCHERS_PARALLEL = _INV["researchers_parallel"]
MAX_ROUNDS = _INV["max_rounds"]
# Batch sessions at once; more and their researchers queue on the Xero minute
# limit (60 calls per tenant). Pre-checks read Xero too and get their own pool.
MAX_PARALLEL = int(os.environ.get("INVESTIGATION_PARALLEL", "3"))
PRECHECK_PARALLEL = int(os.environ.get("INVESTIGATION_PRECHECK_PARALLEL", "2"))
HARD_TIMEOUT = int(os.environ.get("INVESTIGATION_TIMEOUT", "1800"))   # a batch of one
PER_EXTRA_QUERY = 420                    # seconds added for each further query
MAX_TIMEOUT = 5400
PREP_GRACE = 300                         # a triggered batch waits this long for pre-checks
POLL = float(os.environ.get("INVESTIGATION_POLL", "5"))
CAP_SLACK = 0.10                         # "well over the cap" is more than 10% over
DEFAULT_ESTIMATE = 1.20                  # USD a query, before there is batch history
WAIT_DEFAULT = 540
STILL_RUNNING = 75

PRECHECK_WINDOW_DAYS = 4
PRECHECK_MAX_AMOUNTS = 6
MONTHS = {m: n for n, m in enumerate(
    "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split(), start=1)}
ACTION_TYPES = {"post", "update", "manual", "chase", "close", "match", "none"}
# A verdict graded high or medium is reused only when acting on it twice does no harm.
REPEATABLE_ACTIONS = {"none", "close", "match"}
# Xero's History shows a record the API created as "System Generated": an agent
# run (or a feed), not a person.
SYSTEM_USERS = {"system generated", ""}

# CLAUDE.md "Queries": never answered alone, whatever the evidence. A verdict
# that touches one is capped at grade low, so it always goes to the admin.
NEVER_ALONE = {
    "not_shown": "an amount, supplier or currency the document does not show",
    "missing_document": "a missing document (that is a chase)",
    "same_purchase": "whether two records are the same purchase",
    "safety_rule": "anything a safety rule governs",
    "locked_or_reconciled": "a write to a locked period or a reconciled transaction",
    "rules_change": "any change to rules/ beyond a supplier row",
}


def slug(text: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-").lstrip("._-")
    return s[:80] or "query"


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def today_utc() -> date:
    return datetime.now(timezone.utc).date()


def rel(p: Path) -> str:
    try:
        return str(p.relative_to(ROOT))
    except ValueError:
        return str(p)


def resolve(p: str) -> Path:
    q = Path(p)
    return q if q.is_absolute() else ROOT / q


def load_env() -> None:
    """The run that calls us has .env exported already; a hand-run on the
    server may not."""
    env = REPO / ".env"
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return
    if not env.exists():
        return
    for raw in env.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip().removeprefix("export ").strip(), v.strip().strip("'\""))


def readonly_flag() -> None:
    os.environ["AGENT_READONLY"] = "1"


# ---------------------------------------------------------------- grades

def grade_of(certainty) -> str:
    """The mapping that makes the investigation an evidence step and leaves
    the decision to the gate: 5 and 4 high, 3 medium, 1 and 2 low."""
    if not isinstance(certainty, int) or isinstance(certainty, bool):
        return "low"
    return "high" if certainty >= 4 else "medium" if certainty == 3 else "low"


def auto_caps(v: dict, pre: dict | None) -> list[str]:
    """What caps this verdict at low besides the coordinator's own `touches`:
    a post or update against a record the pre-check found reconciled, and a
    chase for a missing document."""
    caps = [t for t in (v.get("touches") or []) if t in NEVER_ALONE]
    acts = [(a or {}).get("type") for a in v.get("actions") or []]
    if "chase" in acts and "missing_document" not in caps:
        caps.append("missing_document")
    reconciled = any(r.get("reconciled") for t in (pre or {}).get("transactions") or []
                     for r in t.get("found") or [])
    if reconciled and ({"post", "update"} & set(acts)) and "locked_or_reconciled" not in caps:
        caps.append("locked_or_reconciled")
    return caps


def finalise(v: dict, pre: dict | None = None) -> dict:
    """Add the grade the run takes to the gate, capped at low where the verdict
    touches something the agent never answers alone."""
    caps = auto_caps(v, pre)
    v["touches"] = caps
    v["grade"] = "low" if caps else grade_of(v.get("certainty"))
    v["grade_from_certainty"] = grade_of(v.get("certainty"))
    return v


# ---------------------------------------------------------------- storage

def _read(p: Path) -> dict:
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return {}


def _write(p: Path, d) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(d, indent=2, ensure_ascii=False, default=str) + "\n")
    tmp.replace(p)


def key_dirs() -> list[Path]:
    if not BASE.exists():
        return []
    return [d for d in BASE.iterdir() if d.is_dir() and not d.name.startswith(("_", "."))]


def runs_for(key: str) -> list[Path]:
    d = BASE / slug(key)
    if not d.exists():
        return []
    return sorted(p for p in d.iterdir() if p.is_dir())


def latest(key: str) -> Path | None:
    r = runs_for(key)
    return r[-1] if r else None


def pid_alive(path: Path) -> bool:
    try:
        os.kill(int(path.read_text().strip()), 0)
        return True
    except (OSError, ValueError):
        return False


def state(run: Path) -> str:
    """done (result written, whatever its status), running, queued (waiting for
    its batch, or not picked up yet) or died."""
    if (run / "result.json").exists():
        return "done"
    b = _read(run / "batch.json")
    if b.get("batch_id"):
        return "running" if pid_alive(BATCHES / b["batch_id"] / "pid") else "died"
    if (run / "queued.json").exists():
        return "queued"
    if (run / "pid").exists():
        return "running" if pid_alive(run / "pid") else "died"
    return "queued"


def read_result(run: Path) -> dict:
    return json.loads((run / "result.json").read_text())


def write_result(run: Path, result: dict) -> None:
    _write(run / "result.json", result)
    (run / "queued.json").unlink(missing_ok=True)


# ---------------------------------------------------------------- entities

def _entity_needles() -> list[tuple[str, str]]:
    """(text a register line may carry, the entity's Xero name), longest first.
    Names and short codes only: an alias is often a common word."""
    out = []
    for e in config.entities():
        for needle in (e.xero_name, e.short, e.key):
            if needle:
                out.append((needle, e.xero_name))
    return sorted(set(out), key=lambda x: -len(x[0]))


def all_entities() -> list[str]:
    return [e.xero_name for e in config.entities()]


def short_of(xero_name: str) -> str:
    try:
        return config.entity(xero_name).short
    except config.ConfigError:
        return xero_name


def resolve_entity_name(raw: str) -> str:
    try:
        return config.entity(raw).xero_name
    except config.ConfigError:
        return raw


def _client(ent: str):
    """A read-only Xero client for an entity named by its Xero name. The one
    place the pre-check reaches Xero, so the tests can put a fake here."""
    from accounting_agent.xero import client_for
    return client_for(ent)


# ---------------------------------------------------------------- the pre-check

def txns_from_text(text: str, year: int) -> list[dict]:
    """Amounts, dates and entities read out of a register line, for a brief
    built --from-register. Loose by design: a wrong guess costs two GETs and
    finds nothing; the coordinator still has the line itself."""
    amounts = []
    for ccy, amt in re.findall(r"\b([A-Z]{3}) (\d{1,3}(?:,\d{3})+(?:\.\d{2})?|\d+\.\d{2}|\d+)\b", text):
        v = float(amt.replace(",", ""))
        if (ccy, v) not in amounts:
            amounts.append((ccy, v))
    days = []
    for d, m in re.findall(r"\b(\d{1,2}) (Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\b", text):
        days.append(date(year, MONTHS[m], int(d)))
    for d1, d2, m in re.findall(r"\b(\d{1,2})-(\d{1,2}) (Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\b", text):
        days += [date(year, MONTHS[m], int(d1)), date(year, MONTHS[m], int(d2))]
    entities, rest = [], text
    for needle, full in _entity_needles():           # longest names first
        pat = re.compile(rf"(?<![A-Za-z0-9]){re.escape(needle)}(?![A-Za-z0-9])")
        if pat.search(rest):
            rest = pat.sub(" ", rest)
            if full not in entities:
                entities.append(full)
    if not amounts or not days:
        return []
    out = []
    for ccy, v in amounts[:PRECHECK_MAX_AMOUNTS]:
        out.append({"entities": entities or all_entities(), "amount": v, "currency": ccy,
                    "from": min(days).isoformat(), "to": max(days).isoformat()})
    return out


def parse_txn_arg(raw: str) -> dict:
    parts = [p.strip() for p in raw.split("|")]
    if len(parts) < 3:
        raise SystemExit(f"--txn wants entity|YYYY-MM-DD|amount[|CCY[|payee]], got {raw!r}")
    lo, _, hi = parts[1].partition("..")      # one date, or a range for a mirror posted later
    lo = date.fromisoformat(lo).isoformat()
    hi = date.fromisoformat(hi).isoformat() if hi else lo
    return {"entities": [resolve_entity_name(parts[0])],
            "amount": float(parts[2].replace(",", "")),
            "currency": parts[3] if len(parts) > 3 else "", "from": lo, "to": hi,
            "payee": parts[4] if len(parts) > 4 else ""}


def _xdate(d: date) -> str:
    return f"DateTime({d.year},{d.month:02d},{d.day:02d})"


def _when(rec: dict) -> str:
    if rec.get("DateString"):
        return rec["DateString"][:10]
    m = re.search(r"\d+", rec.get("Date") or "")
    return datetime.fromtimestamp(int(m.group()) / 1000, timezone.utc).date().isoformat() if m else ""


def _created(hist: list[dict]) -> list | None:
    """[when, who] of the History record that created it."""
    for h in hist:
        if (h.get("Changes") or "").strip().lower() == "created":
            return [h.get("DateUTCString"), h.get("User")]
    return None


def register_lines(brief: dict) -> list[dict]:
    """Register lines on the brief's key or carrying one of its amounts: part
    of the answer, whatever their kind."""
    try:
        items = json.loads(REGISTER.read_text())
    except (OSError, ValueError):
        items = []
    needles = {f"{t['amount']:,.2f}" for t in brief.get("txns") or []}
    out = []
    for i in items:
        blob = f"{i.get('text', '')} {i.get('refs', '')}"
        if (i.get("key") and i["key"].lower() == brief["key"].lower()) or any(n in blob for n in needles):
            out.append({k: i.get(k) for k in ("id", "domain", "kind", "with", "key", "text", "refs",
                                              "answered", "raised")})
    return out


def precheck(brief: dict, run: Path) -> dict:
    """Look the query's transactions up before any model is paid to. Read-only:
    the detached process runs with AGENT_READONLY=1, so XeroClient refuses
    anything but GET. Never raises; a failure is written into the result."""
    out: dict = {"checked": now(), "transactions": [], "register": register_lines(brief), "errors": []}
    txns = brief.get("txns") or []
    if not txns:
        out["note"] = "no transactions named in the brief; nothing looked up in Xero"
        return out
    clients: dict[str, object] = {}
    codes: dict[str, set] = {}
    for t in txns:
        lo = date.fromisoformat(t["from"]) - timedelta(days=PRECHECK_WINDOW_DAYS)
        hi = date.fromisoformat(t["to"]) + timedelta(days=PRECHECK_WINDOW_DAYS)
        found = []
        for ent in t["entities"]:
            try:
                c = clients.get(ent) or clients.setdefault(ent, _client(ent))
                where = f"Date>={_xdate(lo)} AND Date<={_xdate(hi)} AND Total=={t['amount']:.2f}"
                # page=1: the list endpoints return LineItems only when paged.
                for kind, path in (("bank transaction", "BankTransactions"), ("invoice", "Invoices")):
                    for rec in c.get(path, where=where, page=1).get(path, []):
                        if rec.get("Status") in ("DELETED", "VOIDED"):
                            continue
                        rid = rec.get("BankTransactionID") or rec.get("InvoiceID")
                        hist = c.get(f"{path}/{rid}/History").get("HistoryRecords", [])
                        lines = [(li.get("AccountCode"), li.get("Description"), li.get("LineAmount"))
                                 for li in rec.get("LineItems") or []]
                        codes.setdefault(ent, set()).update(x[0] for x in lines if x[0])
                        found.append({
                            "entity": ent, "record": kind, "type": rec.get("Type"), "id": rid,
                            "date": _when(rec), "contact": (rec.get("Contact") or {}).get("Name"),
                            "total": rec.get("Total"), "currency": rec.get("CurrencyCode"),
                            "currency_rate": rec.get("CurrencyRate"), "status": rec.get("Status"),
                            "currency_differs": bool(t.get("currency")) and rec.get("CurrencyCode") != t["currency"],
                            "reconciled": rec.get("IsReconciled"),
                            "bank_account": (rec.get("BankAccount") or {}).get("Name"),
                            "number": rec.get("InvoiceNumber") or rec.get("Reference"),
                            "lines": lines,
                            "created": _created(hist),
                            "history": [(h.get("DateUTCString"), h.get("User"), h.get("Changes"))
                                        for h in hist[:4]],
                        })
                # Manual journals have no Total to filter on: one paged read
                # of the window, lines matched on the amount in Python. Most
                # intercompany mirrors are journals.
                mj_where = f"Date>={_xdate(lo)} AND Date<={_xdate(hi)}"
                for rec in c.get("ManualJournals", where=mj_where, page=1).get("ManualJournals", []):
                    if rec.get("Status") in ("DELETED", "VOIDED"):
                        continue
                    hits = [li for li in rec.get("JournalLines") or []
                            if abs(abs(float(li.get("LineAmount") or 0)) - t["amount"]) <= 0.01]
                    if hits:
                        found.append({
                            "entity": ent, "record": "manual journal", "date": _when(rec),
                            "id": rec.get("ManualJournalID"),
                            "status": rec.get("Status"), "narration": (rec.get("Narration") or "")[:160],
                            "lines": [(li.get("AccountCode"), li.get("Description"), li.get("LineAmount"))
                                      for li in rec.get("JournalLines") or []],
                        })
            except Exception as e:                               # noqa: BLE001
                out["errors"].append(f"{ent} {t['amount']:,.2f}: {type(e).__name__}: {e}")
        out["transactions"].append({**t, "found": found})
    # Account names, so a verdict names every account as `Name (code)`.
    out["accounts"] = {}
    for ent, cs in codes.items():
        try:
            accts = clients[ent].get("Accounts").get("Accounts", [])
            out["accounts"][ent] = {a["Code"]: a.get("Name") for a in accts if a.get("Code") in cs}
        except Exception as e:                                   # noqa: BLE001
            out["errors"].append(f"{ent} accounts: {type(e).__name__}: {e}")
    _match_beyond_amount(brief, txns, clients, out)
    return out


def _match_beyond_amount(brief: dict, txns: list[dict], clients: dict, out: dict) -> None:
    """The exact-total lookup above misses a record held in another currency or
    dated away from the cash. One window per entity, every transaction matched
    on bank amount, converted amount and payee name
    (accounting_agent.xero.matching). That module is optional here: when it is
    not installed this signal is skipped and the exact-total lookup stands."""
    try:
        from accounting_agent.xero.matching import candidates, describe, load_window, name_tokens
    except ImportError:
        out["note_matching"] = "wider matching skipped: accounting_agent.xero.matching is not installed"
        return
    lo = min(date.fromisoformat(t["from"]) for t in txns)
    hi = max(date.fromisoformat(t["to"]) for t in txns)
    fallback = (brief.get("refs") or "").split(" · ")[0]
    windows: dict[str, dict] = {}
    for t, rec in zip(txns, out["transactions"]):
        names = name_tokens(t.get("payee") or fallback)
        currency = t.get("currency") or ""
        matches = []
        for ent in t["entities"]:
            try:
                if ent not in windows:
                    c = clients.get(ent) or clients.setdefault(ent, _client(ent))
                    windows[ent] = load_window(c, lo, hi)
                win = windows[ent]
                day = date.fromisoformat(t["from"]) + (date.fromisoformat(t["to"]) - date.fromisoformat(t["from"])) / 2
                for spent in (True, False):              # the brief does not say which way the cash went
                    for cand in candidates(win, amount=t["amount"], currency=currency or win["base"],
                                           day=day, names=names, spent=spent, limit=6):
                        matches.append({"entity": ent, "strength": cand["strength"],
                                        "reasons": cand["reasons"], "summary": describe(cand),
                                        "id": cand["id"]})
            except Exception as e:                               # noqa: BLE001
                out["errors"].append(f"{ent} {t['amount']:,.2f} wider match: {type(e).__name__}: {e}")
        matches.sort(key=lambda m: -m["strength"])
        rec["matches"] = matches[:8]
        if any({"bank amount", "name"} <= set(m["reasons"]) for m in matches):
            rec["verdict_hint"] = ("a payment in Xero for this exact bank amount, to the payee named, "
                                   "already accounts for this cash: the query's premise is likely wrong")


# ---------------------------------------------------------------- settled in Python

# The query says nothing is in Xero for it. Only these words open the rule:
# a question about coding, period or entity is a judgement, whatever Xero holds.
ABSENCE = re.compile(
    r"\bnothing (is |has been |was )?(behind|posted|booked|in xero)"
    r"|\b(not|never) (yet )?(been )?(posted|booked|bookkept|recorded|recognised|recognized)\b"
    r"|\bno (bill|invoice|receipt|credit note|entry|record|spend money|receive money|transaction)"
    r"[^.;]{0,60}\b(behind|in xero|posted|booked)"
    r"|\bwith no (bill|invoice|credit note|entry|intercompany entry|spend money|receive money)\b"
    r"|\b(has|have) no bill\b|\bno bill (can be|has been|was) posted\b|\bcannot be posted\b"
    r"|\bmissing from xero\b|\bnot in xero\b|\bunbooked\b|\bunposted\b", re.I)
RECORD_WORDS = {("bank transaction", "SPEND"): "spend money",
                ("bank transaction", "RECEIVE"): "receive money",
                ("invoice", "ACCPAY"): "bill", ("invoice", "ACCREC"): "sales invoice"}


def _day(iso: str) -> str:
    try:
        return date.fromisoformat(iso[:10]).strftime("%-d %b %Y")
    except ValueError:
        return iso


def _raised_threshold(brief: dict) -> str:
    """The moment after which a record counts as made since the query: the
    open itself when the query was raised today, else the end of the day it
    was raised (a record made that day may predate it)."""
    raised = (brief.get("raised") or "")[:10]
    opened = (brief.get("opened") or "").rstrip("Z")
    if not raised:
        return opened or "9999"
    if opened[:10] == raised:
        return opened
    return f"{raised}T23:59:59"


def settle_from_precheck(brief: dict, pre: dict) -> dict | None:
    """The verdict the pre-check alone supports, or None. Rule `posted since
    raised`, every condition required:

      - the query's own words say nothing is in Xero for it (ABSENCE)
      - every transaction it names has exactly one live record in Xero: a bank
        transaction (reconciled) or a bill or invoice (AUTHORISED or PAID), in
        the currency named, and nothing else of that amount in the window
      - each record was created after the query was raised, so somebody dealt
        with it since, and its accounts can be named
      - no other register line on the same amounts asks for anything (a watch
        line is fine), so nothing the coordinator would update is left out

    Certainty 4 (grade high) when a person created every record: their posting
    is their ruling on it. Certainty 3 (medium) when an agent run or a feed
    created one: the premise is still wrong and nothing reconciled is touched
    by doing nothing. Either way the verdict posts nothing and moves no money.
    """
    if brief.get("answered") or pre.get("errors"):
        return None
    txns = pre.get("transactions") or []
    if not txns:
        return None
    if not ABSENCE.search(f"{brief.get('question', '')} {brief.get('refs', '')}"):
        return None
    after = _raised_threshold(brief)
    names = pre.get("accounts") or {}
    records, seen = [], set()
    for t in txns:
        found = t.get("found") or []
        if len(found) != 1:
            return None
        r = found[0]
        kind = RECORD_WORDS.get((r.get("record"), r.get("type")))
        if not kind:
            return None
        if r["record"] == "bank transaction":
            if r.get("status") != "AUTHORISED" or not r.get("reconciled"):
                return None
        elif r.get("status") not in ("AUTHORISED", "PAID"):
            return None
        if t.get("currency") and r.get("currency") and r["currency"] != t["currency"]:
            return None
        created = r.get("created") or next(([h[0], h[1]] for h in r.get("history") or []
                                            if (h[2] or "").strip().lower() == "created"), None)
        if not created or not created[0] or created[0].rstrip("Z") <= after:
            return None
        accts = []
        for code, _desc, _amt in r.get("lines") or []:
            name = (names.get(r["entity"]) or {}).get(code)
            if not (code and name):
                return None
            if f"{name} ({code})" not in accts:
                accts.append(f"{name} ({code})")
        ident = r.get("id") or (r["entity"], r.get("date"), r.get("contact"), r.get("total"))
        if ident in seen:
            continue
        seen.add(ident)
        records.append((r, kind, accts, created))
    own = {(brief.get("key") or "").lower(), (brief.get("register_id") or "").lower()} - {""}
    own_lines = [i for i in pre.get("register") or []
                 if (i.get("key") or "").lower() in own or (i.get("id") or "").lower() in own]
    others = [i for i in pre.get("register") or [] if i not in own_lines]
    if any(i.get("kind") != "watch" for i in others):
        return None

    by_person = all((c[1] or "").strip().lower() not in SYSTEM_USERS for _, _, _, c in records)
    certainty = 4 if by_person else 3
    said, evidence, refs = [], [], []
    for r, kind, accts, created in records:
        who = created[1] if (created[1] or "").strip().lower() not in SYSTEM_USERS else "an agent run"
        state_words = ("reconciled" if r["record"] == "bank transaction"
                       else "paid" if r.get("status") == "PAID" else "approved, unpaid")
        said.append(f"{who} posted it on {_day(created[0])} as {kind} "
                    f"{'from' if kind in ('receive money', 'sales invoice') else 'to'} "
                    f"{r.get('contact')}, coded to {', '.join(accts)}, {state_words}")
        ref = " · ".join(x for x in (r.get("contact"), _day(r.get("date") or ""),
                                     f"{r.get('currency')} {float(r.get('total') or 0):,.2f}",
                                     short_of(r["entity"]), r.get("number") or "") if x)
        refs.append(ref)
        evidence.append(f"{kind} in {r['entity']}, {r.get('status')}"
                        f"{', reconciled' if r.get('reconciled') else ''}, coded to {', '.join(accts)},"
                        f" created {created[0]} by {created[1] or 'unknown'} · {ref} · pre-check, Xero History")
    evidence.append(f"the query was raised on {_day(brief.get('raised') or '')}, before every record "
                    f"above was created · {brief.get('register_id') or brief.get('key')}")
    for i in others:
        evidence.append(f"register watch line on the same amount, left as it is · {i.get('id')} {i.get('key')}")
    answer = ("Nothing is left to do: the query says nothing is posted for it, and that is no longer "
              "true. " + "; ".join(s[0].upper() + s[1:] for s in said) + ".")
    actions = [{"type": "none", "what": "nothing to post: " + "; ".join(said)}]
    for i in own_lines:
        if i.get("kind") == "queried":
            actions.append({"type": "close", "key": i.get("key"),
                            "what": f"register line {i.get('id')} (key {i.get('key')}): investigated, "
                                    f"certainty {certainty}: already posted since it was raised"})
    return {
        "certainty": certainty,
        "question": brief.get("question", ""),
        "answer": answer,
        "evidence": evidence,
        "unsure": "",
        "question_for_admin": "",
        "amount_reporting": 0,
        "actions": actions,
        "report_line": "already posted since the query was raised, nothing to do: " + " / ".join(refs),
        "decided_by": "precheck",
        "rule": "posted since raised",
    }


# ---------------------------------------------------------------- reuse

VOLATILE_BRIEF = {"opened", "run_id", "raised_by", "raised", "prior_investigations", "batch_group",
                  "fresh", "reuse_on_evidence", "with", "register_id"}


def _h(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:16]


def brief_fingerprint(brief: dict) -> str:
    """What the query asks and rests on. --evidence, where given, stands in for
    the context and the references: an intercompany break's carry
    reporting-currency figures at rates that move daily."""
    b = {k: v for k, v in brief.items() if k not in VOLATILE_BRIEF}
    if b.get("evidence"):
        b.pop("context", None)
        b.pop("refs", None)
    b["txns"] = sorted(
        [(sorted(t.get("entities") or []), round(float(t.get("amount") or 0), 2),
          t.get("currency") or "", t.get("from"), t.get("to"), t.get("payee") or "")
         for t in brief.get("txns") or []], key=str)
    for k in ("question", "context", "refs", "entity", "answered"):
        if k in b:
            b[k] = " ".join(str(b[k] or "").split())
    return _h(b)


def evidence_fingerprint(pre: dict) -> str | None:
    """What the pre-check found, without its timestamp. None when the pre-check
    hit an error: a partial look-up proves nothing has changed."""
    if pre.get("errors"):
        return None
    recs = []
    for t in pre.get("transactions") or []:
        for r in t.get("found") or []:
            hist = r.get("history") or []
            recs.append([r.get("entity"), r.get("record"), r.get("type"), r.get("date"),
                         r.get("contact"), r.get("total"), r.get("currency"), r.get("status"),
                         r.get("reconciled"), r.get("number"), r.get("narration"),
                         [[x[0], x[2]] for x in r.get("lines") or []],
                         hist[0][0] if hist else None])
        for m in t.get("matches") or []:
            recs.append(["match", m.get("entity"), m.get("id"), m.get("summary")])
    reg = [[i.get("id"), i.get("kind"), i.get("key"), i.get("text"), i.get("refs"), i.get("answered")]
           for i in pre.get("register") or []]
    return _h({"records": sorted(recs, key=str), "register": sorted(reg, key=str)})


def _origin_time(prior: Path, res: dict) -> datetime | None:
    src = res.get("reused_from")
    name = Path(src).name if src else prior.name
    try:
        return datetime.strptime(name, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def reusable_prior(brief: dict, pre: dict | None, exclude: Path | None = None) -> tuple[Path, dict] | None:
    """The last finished verdict for this key, if it may stand for this one.
    Never a failed or deferred one, never one older than reuse_days, never
    across an admin's answer, never one graded high or medium whose actions
    would do harm twice, and only on the same brief and, unless pre is None,
    the same pre-check evidence."""
    if brief.get("fresh") or brief.get("answered"):
        return None
    prior = next((p for p in reversed(runs_for(brief["key"]))
                  if p != exclude and (p / "result.json").exists()), None)
    if prior is None:
        return None
    res = _read(prior / "result.json")
    if res.get("status") != "done" or not isinstance(res.get("certainty"), int):
        return None
    when = _origin_time(prior, res)
    if when is None or datetime.now(timezone.utc) - when > timedelta(days=REUSE_DAYS):
        return None
    if res.get("grade", grade_of(res["certainty"])) != "low" and any(
            (a or {}).get("type") not in REPEATABLE_ACTIONS for a in res.get("actions") or []):
        return None
    if brief_fingerprint(_read(prior / "brief.json")) != brief_fingerprint(brief):
        return None
    if pre is not None:
        mine = evidence_fingerprint(pre)
        if mine is None or (pre.get("register") and any(i.get("answered") for i in pre["register"]
                                                       if (i.get("key") or "").lower() == brief["key"].lower())):
            return None
        if mine != evidence_fingerprint(_read(prior / "precheck.json")):
            return None
    return prior, res


def reused_result(brief: dict, run: Path, prior: Path, res: dict) -> dict:
    out = {k: v for k, v in res.items()
           if k not in ("batch_id", "batch_size", "batch_cost_usd", "cost_share_usd", "session_id",
                        "seconds", "dir", "started", "reused_from", "reused_at", "gate")}
    out.update({"key": brief["key"], "domain": brief.get("domain"), "dir": rel(run),
                "started": brief.get("opened"), "status": "done", "cost_usd": 0.0,
                "decided_by": res.get("decided_by") or "coordinator",
                "reused_from": res.get("reused_from") or rel(prior), "reused_at": now()})
    return out


# ---------------------------------------------------------------- the session

PROMPT = """You are the coordinator of a batch of {n} query investigations for one
accounting run. The manifest {manifest} lists each query's key, its brief and its
pre-check. Read .claude/skills/query-investigation/SKILL.md once, then the
manifest, then every brief and pre-check, and work as your agent instructions
say: answer a query from its pre-check alone where that settles it; otherwise
read only the rules the questions touch, once for the whole batch, and send
query-researcher agents (model {researcher_model}, {parallel} at a time at most,
{rounds} rounds per query at most) for the data, one researcher serving every
query that needs the same entity and period. Decide each query on its own
evidence and rate each on the skill's scale.

You gather evidence and propose; you do not decide what the run may do alone.
You are read-only and you are not the run. Nothing you do changes Xero, Slack,
Gmail, Drive, the register or any file outside {scratch}. Extracts and working
files go under {scratch} only. The moment a verdict is decided, save it with
Bash as {scratch}/verdicts/<key>.json, one JSON object with its "key", so it
survives if the session is cut short.

Your FINAL message is one JSON array holding one verdict object per key in the
manifest, each in the skill's shape with its "key" added, and nothing else."""


def parse_verdict(text: str) -> dict | None:
    """One JSON object, fenced or bare."""
    if not text:
        return None
    fenced = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    candidates = fenced[::-1] + [text[text.find("{"): text.rfind("}") + 1]]
    for c in candidates:
        try:
            obj = json.loads(c)
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def parse_batch(text: str, keys: list[str]) -> dict[str, dict]:
    """The batch's final message, a JSON array of verdicts, by key. A lone
    object counts for a batch of one."""
    if not text:
        return {}
    arr = None
    cands = [text.strip()] + re.findall(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.S)[::-1]
    end = text.rfind("]")
    cands += [text[m.start(): end + 1] for m in reversed(list(re.finditer(r"^\s*\[", text, re.M)))]
    for c in cands:
        try:
            obj = json.loads(c)
        except ValueError:
            continue
        if isinstance(obj, list):
            arr = obj
            break
        if isinstance(obj, dict) and len(keys) == 1:
            arr = [dict(obj, key=obj.get("key") or keys[0])]
            break
    if arr is None and len(keys) == 1:
        v = parse_verdict(text)
        arr = [dict(v, key=v.get("key") or keys[0])] if v else None
    by_slug = {slug(k).lower(): k for k in keys}
    out = {}
    for v in arr or []:
        if isinstance(v, dict):
            k = by_slug.get(slug(str(v.get("key") or "")).lower())
            if k and k not in out:
                out[k] = v
    return out


def validate(v: dict) -> list[str]:
    problems = []
    c = v.get("certainty")
    if not isinstance(c, int) or isinstance(c, bool) or not 1 <= c <= 5:
        problems.append("certainty is not an integer from 1 to 5")
    for f in ("question", "answer", "report_line"):
        if not str(v.get(f) or "").strip():
            problems.append(f"{f} is empty")
    if isinstance(c, int) and c < 3:
        if not str(v.get("unsure") or "").strip():
            problems.append("certainty below 3 but nothing says what is unsure")
        if not str(v.get("question_for_admin") or "").strip():
            problems.append("certainty below 3 but no question_for_admin")
    amt = v.get("amount_reporting")
    if amt is not None and (isinstance(amt, bool) or not isinstance(amt, (int, float))):
        problems.append("amount_reporting is not a number")
    touches = v.get("touches")
    if touches is not None and (not isinstance(touches, list)
                                or any(t not in NEVER_ALONE for t in touches)):
        problems.append(f"touches is not a list drawn from {', '.join(NEVER_ALONE)}")
    acts = v.get("actions")
    if not isinstance(acts, list):
        problems.append("actions is not a list")
    else:
        for n, a in enumerate(acts):
            t = (a or {}).get("type")
            if t not in ACTION_TYPES:
                problems.append(f"action {n} has type {t!r}")
            if t in ("post", "update") and not (a.get("entity") and a.get("payload")):
                problems.append(f"action {n} is a {t} without an entity and a payload")
            if t == "post" and not a.get("idempotency_key"):
                problems.append(f"action {n} is a post without an idempotency_key")
            if t == "match" and not (isinstance(a.get("doc_ids"), list) and len(a["doc_ids"]) >= 2):
                problems.append(f"action {n} is a match without the doc_ids of its lines")
    return problems


def acquire_slot(prefix: str = "slot", n: int | None = None):
    """A free slot out of n, held for as long as the handle stays open."""
    BASE.mkdir(parents=True, exist_ok=True)
    n = n or MAX_PARALLEL
    while True:
        for i in range(n):
            fh = (BASE / f".{prefix}-{i}").open("a+")
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fh
            except OSError:
                fh.close()
        time.sleep(POLL)


# ---------------------------------------------------------------- spend and the cap

def _batch_cost(bdir: Path) -> float:
    """A batch session counted once: its CLI figure, or its estimate while it
    runs or when it died without one."""
    c = _read(bdir / "session.json").get("total_cost_usd")
    if isinstance(c, (int, float)):
        return float(c)
    est = _read(bdir / "manifest.json").get("estimate_usd")
    return float(est) if isinstance(est, (int, float)) else 0.0


def batch_dirs() -> list[Path]:
    if not BATCHES.exists():
        return []
    return sorted(d for d in BATCHES.iterdir() if d.is_dir() and d.name[:1].isdigit())


def spent_on(day: date) -> float:
    """Investigation cost of one UTC day: every session that started that day,
    each counted once. Batch results carry shares and are skipped here; reused
    and pre-check verdicts cost nothing."""
    tag = day.strftime("%Y%m%d")
    total = 0.0
    for kd in key_dirs():
        for run in kd.iterdir():
            if not run.is_dir() or not run.name.startswith(tag):
                continue
            res = _read(run / "result.json")
            if res.get("batch_id") or res.get("reused_from") or res.get("decided_by") == "precheck":
                continue
            c = res.get("cost_usd")
            if isinstance(c, (int, float)):
                total += float(c)
    for b in batch_dirs():
        if b.name.startswith(tag):
            total += _batch_cost(b)
    return round(total, 4)


def average_cost(last: int = 40) -> float:
    """Average cost of one coordinator-investigated query over the last few
    sessions, batch sessions spread over their queries."""
    rows = []
    for kd in key_dirs():
        for run in kd.iterdir():
            res = _read(run / "result.json") if run.is_dir() else {}
            c = res.get("cost_usd")
            if (isinstance(c, (int, float)) and c > 0 and not res.get("batch_id")
                    and not res.get("reused_from") and res.get("decided_by") != "precheck"):
                rows.append((run.name, float(c)))
    for b in batch_dirs():
        s = _read(b / "session.json").get("total_cost_usd")
        n = len(_read(b / "manifest.json").get("queries") or [])
        if isinstance(s, (int, float)) and n:
            rows += [(b.name, float(s) / n)] * n
    rows = sorted(rows)[-last:]
    return sum(c for _, c in rows) / len(rows) if rows else DEFAULT_ESTIMATE


def allowed_batch(spent: float, avg: float, queued: int) -> int:
    """How many queued queries the next batch may take: 0 at or over the cap;
    otherwise as many as fit under the cap plus CAP_SLACK at the average cost,
    at least one, at most batch_max."""
    if spent >= DAILY_CAP or queued <= 0:
        return 0
    room = DAILY_CAP * (1 + CAP_SLACK) - spent
    fit = int(room // avg) if avg > 0 else queued
    return max(1, min(queued, BATCH_MAX, fit))


def record_cost(bdir: Path, queries: list[dict], cost: float | None, running: bool) -> None:
    """The batch's cost as an extra cost file of each run that raised its
    queries (data/runs/<run id>/extra/, read by scripts/run_cost.py), shared
    equally by the queries. Written when the batch starts (running, no cost
    yet) and again when it ends."""
    by_run: dict[str, int] = {}
    for q in queries:
        rid = _read(resolve(q["dir"]) / "brief.json").get("run_id")
        if rid:
            by_run[rid] = by_run.get(rid, 0) + 1
    for rid, n in by_run.items():
        share = (float(cost) * n / len(queries)) if isinstance(cost, (int, float)) and queries else 0.0
        _write(RUNS / rid / "extra" / f"investigations-{bdir.name}.json",
               {"label": "investigations", "cost_usd": round(share, 4), "running": running,
                "batch_id": bdir.name})


def register_command(brief: dict, run: Path, when: str, pre: dict | None = None) -> str:
    """What the run sends to the register for a deferred query: an update of
    the line it already has, or a new queried line carrying the deferred verdict."""
    pre = pre if pre is not None else _read(run / "precheck.json")
    has = any((i.get("key") or "").lower() == brief["key"].lower() and i.get("kind") == "queried"
              for i in pre.get("register") or [])
    if not has:
        try:
            has = any((i.get("key") or "").lower() == brief["key"].lower()
                      for i in json.loads(REGISTER.read_text()))
        except (OSError, ValueError):
            pass
    if has:
        return (f".venv/bin/python scripts/outstanding.py update --key {shlex.quote(brief['key'])} "
                f"--schedule-investigation {when}")
    parts = [".venv/bin/python scripts/outstanding.py add", "--domain", brief.get("domain") or "bookkeeping",
             "--kind queried", "--with", shlex.quote(brief.get("with") or config.default_admin()),
             "--key", shlex.quote(brief["key"]), "--text", shlex.quote(brief.get("question") or ""),
             "--refs", shlex.quote(brief.get("refs") or ""),
             "--investigation", shlex.quote(rel(run / "result.json"))]
    return " ".join(parts)


def defer(run: Path, spent: float) -> dict:
    brief = _read(run / "brief.json")
    when = (today_utc() + timedelta(days=1)).isoformat()
    res = {"key": brief.get("key"), "domain": brief.get("domain"), "dir": rel(run),
           "started": brief.get("opened"), "status": "deferred", "scheduled": when,
           "spent_today_usd": round(spent, 2), "cap_usd": DAILY_CAP,
           "reason": (f"daily investigation cap reached: USD {spent:,.2f} of USD {DAILY_CAP:,.2f} "
                      f"spent today (UTC); investigated on {when}"),
           "register_command": register_command(brief, run, when)}
    write_result(run, res)
    return res


# ---------------------------------------------------------------- the queue

def group_of() -> str:
    for g in (os.environ.get("AGENT_RUN_ID"), os.environ.get("INVESTIGATION_BATCH")):
        if g and g.strip():
            return g.strip()
    return f"hand-{os.getppid()}"


def _gpaths(group: str) -> tuple[Path, Path, Path, Path]:
    g = slug(group)
    return (BATCHES / f".group-{g}.lock", BATCHES / f".group-{g}.wait",
            BATCHES / f".trigger-{g}", QUEUE / g)


def marker(run: Path, group: str) -> Path:
    return _gpaths(group)[3] / f"{run.name}-{slug(run.parent.name)}.json"


def set_marker(run: Path, group: str, st: str) -> None:
    _write(marker(run, group), {"run": rel(run), "key": _read(run / "brief.json").get("key"),
                                "state": st, "at": time.time()})


def scan_queue(group: str) -> tuple[list[dict], list[dict]]:
    """(queued, still in pre-check) for a group, oldest first. Markers whose
    query has finished or whose worker died are dropped."""
    qdir = _gpaths(group)[3]
    queued, prepping = [], []
    if not qdir.exists():
        return queued, prepping
    for m in sorted(qdir.glob("*.json")):
        d = _read(m)
        run = resolve(d.get("run") or "")
        if not d or (run / "result.json").exists() or (run / "batch.json").exists() or not run.exists():
            m.unlink(missing_ok=True)
            continue
        d["marker"], d["path"] = m, run
        if (run / "queued.json").exists():
            d["at"] = _read(run / "queued.json").get("at") or d.get("at") or 0
            queued.append(d)
        elif (run / "pid").exists() and pid_alive(run / "pid"):
            prepping.append(d)
        elif time.time() - float(d.get("at") or 0) < 60:
            prepping.append(d)            # the worker has not started yet
        else:
            m.unlink(missing_ok=True)     # its worker died; `wait` writes the failure
    queued.sort(key=lambda d: d.get("at") or 0)
    return queued, prepping


def spawn(*args: str) -> None:
    BATCHES.mkdir(parents=True, exist_ok=True)
    with (BATCHES / "dispatch.log").open("a") as log:
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()), *args], cwd=REPO,
                         stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)


def trigger(group: str) -> None:
    t = _gpaths(group)[2]
    t.parent.mkdir(parents=True, exist_ok=True)
    t.touch()
    spawn("_dispatch", group)


def dispatch(group: str) -> int:
    """One per group at a time: wait for the batch to be ready, run it, repeat
    until the group's queue is empty. A dispatcher spawned while another runs
    waits for it (one waiter at most) and then rescans, so a query queued at
    any moment is picked up."""
    lock_p, wait_p, trig_p, _ = _gpaths(group)
    BATCHES.mkdir(parents=True, exist_ok=True)
    waiter = wait_p.open("a+")
    try:
        fcntl.flock(waiter, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        waiter.close()
        return 0                          # another dispatcher is waiting and will rescan
    lock = lock_p.open("a+")
    fcntl.flock(lock, fcntl.LOCK_EX)
    fcntl.flock(waiter, fcntl.LOCK_UN)
    waiter.close()
    load_env()
    readonly_flag()
    last_start = 0.0
    first_trig = None                     # `wait` touches the trigger every minute
    try:
        while True:
            queued, prepping = scan_queue(group)
            if not queued and not prepping:
                return 0
            nowt = time.time()
            trig = trig_p.stat().st_mtime if trig_p.exists() else 0.0
            if trig > last_start and first_trig is None:
                first_trig = nowt
            newest = max((float(q.get("at") or 0) for q in queued), default=nowt)
            ready = queued and (len(queued) >= BATCH_MAX or nowt - newest >= DEBOUNCE
                                or (first_trig is not None
                                    and (not prepping or nowt - first_trig >= PREP_GRACE)))
            if not ready:
                time.sleep(POLL)
                continue
            last_start, first_trig = nowt, None
            run_batch(group, [q["path"] for q in queued])
    finally:
        lock.close()


def run_batch(group: str, runs: list[Path]) -> str | None:
    """Check the cap, take what fits, run one coordinator session over it and
    write every result. Returns the batch id, or None when it deferred."""
    BATCHES.mkdir(parents=True, exist_ok=True)
    with (BATCHES / ".cap.lock").open("a+") as caplock:
        fcntl.flock(caplock, fcntl.LOCK_EX)
        spent = spent_on(today_utc())
        n = allowed_batch(spent, average_cost(), len(runs))
        if n == 0:
            for r in runs:
                defer(r, spent)
            return None
        take = runs[:n]
        batch_id = f"{stamp()}-{slug(group)[:40]}"
        bdir = BATCHES / batch_id
        (bdir / "verdicts").mkdir(parents=True, exist_ok=False)
        (bdir / "pid").write_text(str(os.getpid()))
        queries = [{"key": _read(r / "brief.json").get("key"), "dir": rel(r),
                    "brief": rel(r / "brief.json"), "precheck": rel(r / "precheck.json")} for r in take]
        _write(bdir / "manifest.json", {
            "batch_id": batch_id, "group": group, "queries": queries, "started": now(),
            "estimate_usd": round(average_cost() * len(take), 2), "spent_before_usd": spent,
            "model": COORDINATOR_MODEL, "effort": COORDINATOR_EFFORT})
        for r in take:
            _write(r / "batch.json", {"batch_id": batch_id})
            (r / "queued.json").unlink(missing_ok=True)
    record_cost(bdir, queries, None, running=True)
    try:
        slot = acquire_slot()
        try:
            _session(bdir, queries)
        finally:
            slot.close()
    finally:
        (bdir / "pid").unlink(missing_ok=True)
    return batch_id


def _session(bdir: Path, queries: list[dict]) -> None:
    n = len(queries)
    keys = [q["key"] for q in queries]
    started = time.time()
    timeout = min(MAX_TIMEOUT, HARD_TIMEOUT + PER_EXTRA_QUERY * (n - 1))
    env = dict(os.environ)
    env.update({
        # The read-only locks key on these, and the bookkeeping prompt hook
        # stands down for a session that is an investigation.
        "AGENT_READONLY": "1",
        "AGENT_INVESTIGATION": "1",
        "QUERY_JOB": f"investigate-{bdir.name}",
        "QUERY_DIR": rel(bdir),
        "PATH": f"{Path.home() / '.local' / 'bin'}:{env.get('PATH', '')}",
        # The coordinator waits on its researchers for minutes at a time; the
        # bookkeeping run that opens it may run on five (deploy/run-agent.sh).
        "CLAUDE_CODE_PROMPT_CACHE_TTL": "1h",
    })
    prompt = PROMPT.format(n=n, manifest=rel(bdir / "manifest.json"), scratch=rel(bdir),
                           researcher_model=RESEARCHER_MODEL, parallel=RESEARCHERS_PARALLEL,
                           rounds=MAX_ROUNDS)
    cmd = [CLAUDE_BIN, "-p", prompt,
           "--agent", COORDINATOR_AGENT, "--model", COORDINATOR_MODEL, "--effort", COORDINATOR_EFFORT,
           "--permission-mode", "bypassPermissions", "--output-format", "json",
           "--disallowedTools", "Write", "Edit", "NotebookEdit"]
    out, rc, why = {}, None, ""
    try:
        with (bdir / "run.log").open("a") as err:
            proc = subprocess.run(cmd, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=err,
                                  text=True, timeout=timeout)
        rc = proc.returncode
        (bdir / "session.json").write_text(proc.stdout or "")
        try:
            out = json.loads(proc.stdout or "{}")
        except ValueError:
            out = {"result": proc.stdout}
        if rc:
            why = f"the batch session exited {rc}"
    except subprocess.TimeoutExpired:
        why = f"the batch session timed out after {timeout} seconds"
    except Exception as e:                                       # noqa: BLE001
        why = f"{type(e).__name__}: {e}"
    verdicts = parse_batch(out.get("result") or "", keys)
    for k in keys:                       # what the coordinator saved as it went
        if k not in verdicts:
            v = _read(bdir / "verdicts" / f"{slug(k)}.json") or _read(bdir / "verdicts" / f"{k}.json")
            if v:
                verdicts[k] = v
    cost = out.get("total_cost_usd")
    share = round(float(cost) / n, 4) if isinstance(cost, (int, float)) else None
    for q in queries:
        run = resolve(q["dir"])
        brief = _read(run / "brief.json")
        base = {"key": q["key"], "domain": brief.get("domain"), "dir": q["dir"],
                "started": brief.get("opened"), "model": COORDINATOR_MODEL,
                "effort": COORDINATOR_EFFORT, "decided_by": "coordinator",
                "batch_id": bdir.name, "batch_size": n, "batch_cost_usd": cost,
                "cost_usd": share, "cost_share_usd": share,
                "session_id": out.get("session_id"), "seconds": round(time.time() - started)}
        v = verdicts.get(q["key"])
        if v is None:
            reason = why or "the batch session's final message held no verdict for this key"
            write_result(run, {**base, "status": "failed", "reason": reason})
            continue
        v = {k2: v2 for k2, v2 in v.items() if k2 != "key"}
        problems = validate(v)
        if problems:
            write_result(run, {**v, **base, "status": "failed",
                               "reason": "verdict incomplete: " + "; ".join(problems)})
            continue
        finalise(v, _read(run / "precheck.json"))
        write_result(run, {**v, **base, "status": "done"})
    m = _read(bdir / "manifest.json")
    m.update({"ended": now(), "cost_usd": cost, "error": why or None})
    _write(bdir / "manifest.json", m)
    record_cost(bdir, queries, cost, running=False)


# ---------------------------------------------------------------- the worker

def prepare(run: Path, group: str, start_dispatcher: bool = True) -> str:
    """The detached half of `open`: pre-check, then settle, reuse or queue.
    Always ends in a result or a queued query, so `wait` never hangs."""
    brief = _read(run / "brief.json")
    (run / "pid").write_text(str(os.getpid()))
    load_env()
    readonly_flag()                           # the pre-check reads Xero GET-only
    base = {"key": brief.get("key"), "domain": brief.get("domain"), "dir": rel(run),
            "started": brief.get("opened")}
    try:
        slot = acquire_slot("prep", PRECHECK_PARALLEL)
        try:
            pre = precheck(brief, run)
        finally:
            slot.close()
        _write(run / "precheck.json", pre)
        v = settle_from_precheck(brief, pre)
        if v:
            finalise(v, pre)
            write_result(run, {**base, **v, "status": "done", "cost_usd": 0.0})
            marker(run, group).unlink(missing_ok=True)
            return "settled"
        hit = reusable_prior(brief, pre, exclude=run)
        if hit:
            write_result(run, reused_result(brief, run, *hit))
            marker(run, group).unlink(missing_ok=True)
            return "reused"
        _write(run / "queued.json", {"group": group, "at": time.time()})
        set_marker(run, group, "queued")
        if start_dispatcher:
            spawn("_dispatch", group)
        return "queued"
    except Exception as e:                                       # noqa: BLE001
        write_result(run, {**base, "status": "failed", "reason": f"pre-check: {type(e).__name__}: {e}"})
        marker(run, group).unlink(missing_ok=True)
        return "failed"
    finally:
        (run / "pid").unlink(missing_ok=True)


# ---------------------------------------------------------------- commands

def brief_from_register(item_id: str) -> dict:
    items = json.loads(REGISTER.read_text())
    hit = next((i for i in items if (i.get("key") or "").lower() == item_id.lower()), None) \
        or next((i for i in items if i["id"].lower() == item_id.lower()), None)
    if not hit:
        raise SystemExit(f"{item_id} is not in the register")
    if hit["kind"] != "queried":
        raise SystemExit(f"{hit['id']} is a {hit['kind']} item, not a query")
    return {
        "domain": hit["domain"], "key": hit.get("key") or hit["id"],
        "register_id": hit["id"], "with": hit["with"],
        "question": hit["text"], "refs": hit.get("refs", ""),
        "context": hit.get("note", ""), "raised": hit["raised"],
        "answered": hit.get("answered", ""),
    }


def open_investigation(brief: dict, fresh: bool = False, reuse_on_evidence: bool = False,
                       foreground: bool = False, group: str | None = None) -> tuple[str, Path]:
    """Write the brief and route it. Returns (what happened, the run folder):
    opened (worker started), running (one is already in flight for this key),
    reused (reuse_on_evidence: the same --evidence, so no pre-check at all),
    or, in the foreground, the final state."""
    group = group or group_of()
    if not fresh:
        # Opened twice by one run: the second joins the first.
        cur = latest(brief["key"])
        if (cur is not None and state(cur) in ("queued", "running")
                and _read(cur / "brief.json").get("batch_group") == group):
            return "running", cur
    if fresh:
        brief["fresh"] = True
    if os.environ.get("AGENT_RUN_ID"):
        # scripts/run_cost.py puts the investigation's cost on the run's headline.
        brief["run_id"] = os.environ["AGENT_RUN_ID"]
    brief["batch_group"] = group
    brief["opened"] = now()
    prior = [rel(p) for p in runs_for(brief["key"]) if (p / "result.json").exists()]
    if prior:
        brief["prior_investigations"] = prior
    run = BASE / slug(brief["key"]) / stamp()
    while run.exists():
        time.sleep(1)
        run = BASE / slug(brief["key"]) / stamp()
    run.mkdir(parents=True)
    _write(run / "brief.json", brief)
    if reuse_on_evidence and brief.get("evidence"):
        hit = reusable_prior(brief, None, exclude=run)
        if hit:
            write_result(run, reused_result(brief, run, *hit))
            return "reused", run
    if foreground:
        group = f"fg-{run.name}-{slug(brief['key'])[:30]}"
        brief["batch_group"] = group
        _write(run / "brief.json", brief)
        set_marker(run, group, "prep")
        route = prepare(run, group, start_dispatcher=False)
        if route == "queued":
            marker(run, group).unlink(missing_ok=True)
            run_batch(group, [run])
        return route, run
    set_marker(run, group, "prep")
    with (run / "run.log").open("a") as log:
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "_run", str(run), group],
                         cwd=REPO, stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                         start_new_session=True)
    return "opened", run


def cmd_open(a) -> int:
    from accounting_agent import readonly
    if readonly.active():
        print("a read-only session answers from what it reads; it opens no investigation",
              file=sys.stderr)
        return 3
    if not ENABLED:
        print("[investigation] enabled is false in config/group.toml: grade the answer yourself "
              "and take it to scripts/resolve_gate.py", file=sys.stderr)
        return 4
    if a.from_register:
        brief = brief_from_register(a.from_register)
        if a.context:
            brief["context"] = "\n".join(x for x in (brief["context"], a.context) if x)
    else:
        if not (a.key and a.question):
            raise SystemExit("open needs --key and --question, or --from-register")
        brief = {"domain": a.domain, "key": a.key, "with": a.with_ or config.default_admin(),
                 "question": a.question, "context": a.context or "",
                 "refs": a.refs or "", "raised": date.today().isoformat()}
    if brief.get("answered"):
        print(f"{brief.get('register_id') or brief['key']} is answered by an admin: "
              f"{brief['answered']}. An answer is never re-investigated; post the ruling.",
              file=sys.stderr)
        return 3
    if a.entity:
        brief["entity"] = a.entity
    if a.txn:
        brief["txns"] = [parse_txn_arg(t) for t in a.txn]
    elif a.from_register:
        year = int((brief.get("raised") or date.today().isoformat())[:4])
        brief["txns"] = txns_from_text(f"{brief['question']} · {brief['refs']}", year)
    if a.evidence:
        brief["evidence"] = sorted(a.evidence)
    if a.run_job:
        brief["raised_by"] = a.run_job
    route, run = open_investigation(brief, fresh=a.fresh, reuse_on_evidence=a.reuse_on_evidence,
                                    foreground=a.foreground, group=a.batch)
    if a.foreground:
        print(json.dumps(read_result(run), indent=2, ensure_ascii=False) if (run / "result.json").exists()
              else f"{route} {rel(run)}")
        return 0 if _read(run / "result.json").get("status") == "done" else 1
    print(rel(run))
    return 0


def summary(run: Path) -> str:
    st = state(run)
    if st != "done":
        q = _read(run / "queued.json")
        tail = f" (batch group {q.get('group')})" if q.get("group") else ""
        return f"{st:8} {rel(run)}{tail}"
    r = read_result(run)
    if r.get("status") == "deferred":
        return (f"deferred {r['key']} - {r.get('reason', '')}. Register it: "
                f"{r.get('register_command', '')}")
    if r.get("status") != "done":
        return f"failed   {r['key']} - {r.get('reason', '')} · {rel(run)}"
    how = ("pre-check" if r.get("decided_by") == "precheck"
           else f"reused {r['reused_from']}" if r.get("reused_from")
           else f"batch {r['batch_id']}" if r.get("batch_id") else "session")
    cap = f" (capped by {', '.join(r['touches'])})" if r.get("touches") else ""
    return (f"certainty {r['certainty']} grade {r.get('grade', grade_of(r['certainty']))}{cap} "
            f"{r['key']} - {r.get('answer', '')[:160]}"
            f" · {rel(run)}/result.json · {how}")


def _groups_waiting(runs: list[Path]) -> set[str]:
    return {(_read(r / "queued.json").get("group") or "") for r in runs
            if (r / "queued.json").exists() and not (r / "result.json").exists()} - {""}


def cmd_wait(a) -> int:
    if a.all_open:
        cutoff = time.time() - a.since_hours * 3600
        rid = "" if a.any_run else os.environ.get("AGENT_RUN_ID", "")
        runs = [p for d in key_dirs() for p in d.iterdir()
                if p.is_dir() and p.stat().st_mtime >= cutoff
                and (not rid or _read(p / "brief.json").get("run_id") == rid)]
    else:
        runs = []
        for k in a.key or []:
            r = latest(k)
            if r is None:
                print(f"no investigation for {k}", file=sys.stderr)
                return 1
            runs.append(r)
    deadline = time.time() + a.timeout
    kicked = 0.0
    while True:
        if time.time() - kicked >= 60:
            # The run is waiting: its queued queries start now, not after the debounce.
            for g in _groups_waiting(runs):
                trigger(g)
            kicked = time.time()
        for r in runs:
            if state(r) == "died":
                write_result(r, {"key": _read(r / "brief.json").get("key"), "dir": rel(r),
                                 "status": "failed", "reason": "the investigation died without a verdict"})
        pending = [r for r in runs if state(r) in ("running", "queued")]
        if not pending or time.time() >= deadline:
            break
        time.sleep(min(10, POLL * 2))
    for r in sorted(runs):
        print(summary(r))
    return STILL_RUNNING if pending else 0


def cmd_show(a) -> int:
    r = latest(a.key)
    if r is None or not (r / "result.json").exists():
        print(f"no finished investigation for {a.key}", file=sys.stderr)
        return 1
    print(json.dumps(read_result(r), indent=2, ensure_ascii=False))
    return 0


def cmd_gate(a) -> int:
    """The grade and the amount to the gate, [auto_resolve] in config/group.toml
    (the same decision as scripts/resolve_gate.py). Prints act, confirm or
    query first and, outside a read-only session, records it on the verdict so
    the register accepts a high or medium verdict that the gate says to query."""
    r = latest(a.key)
    if r is None or not (r / "result.json").exists():
        print(f"no finished investigation for {a.key}", file=sys.stderr)
        return 1
    res = read_result(r)
    if res.get("status") != "done":
        print(f"{a.key}: the investigation is {res.get('status')}; there is no grade to gate",
              file=sys.stderr)
        return 1
    amount = a.amount if a.amount is not None else res.get("amount_reporting")
    if amount is None:
        print(f"{a.key}: the verdict names no amount_reporting; pass --amount, the money the "
              "answer moves in the reporting currency", file=sys.stderr)
        return 2
    grade = res.get("grade") or grade_of(res.get("certainty"))
    action, why = config.resolve_action(grade, float(amount))
    print(f"{action:<10} {why}")
    from accounting_agent import readonly
    if not readonly.active():
        res["gate"] = {"action": action, "why": why, "grade": grade, "amount": float(amount),
                       "at": now()}
        _write(r / "result.json", res)
    return 0


def cmd_latest(a) -> int:
    r = latest(a.key)
    if r is None or not (r / "result.json").exists():
        return 1
    print(rel(r / "result.json"))
    return 0


def cmd_list(a) -> int:
    today = today_utc().strftime("%Y%m%d")
    for d in sorted(key_dirs()):
        for r in sorted(p for p in d.iterdir() if p.is_dir()):
            if a.today and not r.name.startswith(today):
                continue
            print(summary(r))
    return 0


def cmd_due(a) -> int:
    """Register queries whose deferred investigation day has come."""
    try:
        items = json.loads(REGISTER.read_text())
    except (OSError, ValueError):
        items = []
    today = today_utc().isoformat()
    due = [i for i in items if i.get("domain") == "bookkeeping" and i.get("kind") == "queried"
           and i.get("certainty") is None and not i.get("investigation_failed")
           and not i.get("answered") and i.get("investigation_scheduled")
           and i["investigation_scheduled"] <= today]
    for i in due:
        ref = i.get("key") or i["id"]
        if not a.open:
            print(f"due      {i['id']} {ref} (scheduled {i['investigation_scheduled']})")
            continue
        brief = brief_from_register(ref)
        brief["txns"] = txns_from_text(f"{brief['question']} · {brief['refs']}",
                                       int((brief.get("raised") or today)[:4]))
        route, run = open_investigation(brief)
        print(f"{route:8} {i['id']} {ref} {rel(run)}")
    if not due:
        print("nothing due")
    return 0


def cmd_spend(a) -> int:
    day = date.fromisoformat(a.day) if a.day else today_utc()
    s = spent_on(day)
    print(f"investigations {day.isoformat()} (UTC): USD {s:,.2f} of a USD {DAILY_CAP:,.2f} cap; "
          f"average USD {average_cost():,.2f} a query")
    return 0


def main(argv=None) -> int:
    if argv is None and len(sys.argv) >= 3 and sys.argv[1] == "_run":
        run = Path(sys.argv[2])
        group = sys.argv[3] if len(sys.argv) > 3 else _read(run / "brief.json").get("batch_group") or group_of()
        prepare(run, group)
        return 0
    if argv is None and len(sys.argv) == 3 and sys.argv[1] == "_dispatch":
        return dispatch(sys.argv[2])
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    o = sub.add_parser("open", help="start an investigation, detached")
    o.add_argument("--domain", default="bookkeeping")
    o.add_argument("--key", help="the register key the query will carry")
    o.add_argument("--with", dest="with_", default="",
                   help="who the query would go to if it stays open (default: the first admin in config)")
    o.add_argument("--question", help="the query, as it would be put")
    o.add_argument("--context", default="",
                   help="what the run already checked and found, and why it stopped")
    o.add_argument("--refs", default="", help="the reference tail")
    o.add_argument("--entity", default="", help="the entity the query is about, in full")
    o.add_argument("--from-register", help="build the brief from a register item (its key, or OI-0901)")
    o.add_argument("--txn", action="append",
                   help="a transaction the query is about, looked up before any agent starts: "
                        "entity|YYYY-MM-DD|amount[|CCY[|payee]]; repeat for each")
    o.add_argument("--evidence", action="append",
                   help="a stable line the query rests on (an intercompany line's doc id, date and "
                        "amount); reuse compares these in place of --context. Repeat for each")
    o.add_argument("--reuse-on-evidence", action="store_true",
                   help="the --evidence lines are the whole evidence: reuse an unchanged verdict "
                        "without a pre-check")
    o.add_argument("--fresh", action="store_true", help="investigate again even where a verdict stands")
    o.add_argument("--batch", help="the batch group; default the run id, else this shell")
    o.add_argument("--run-job", default=os.environ.get("RUN_JOB", ""),
                   help="the run that raised it, for the trail")
    o.add_argument("--foreground", action="store_true", help="block and print the verdict")
    o.set_defaults(func=cmd_open)

    w = sub.add_parser("wait", help="start the waiting batches and block until they finish")
    w.add_argument("--key", action="append")
    w.add_argument("--all-open", action="store_true",
                   help="every investigation this run opened in the last --since-hours "
                        "(every run's with --any-run)")
    w.add_argument("--any-run", action="store_true")
    w.add_argument("--since-hours", type=float, default=12)
    w.add_argument("--timeout", type=int, default=WAIT_DEFAULT)
    w.set_defaults(func=cmd_wait)

    s = sub.add_parser("show", help="the latest verdict for a key")
    s.add_argument("--key", required=True)
    s.set_defaults(func=cmd_show)

    g = sub.add_parser("gate", help="take the verdict's grade and amount to the auto-resolve gate")
    g.add_argument("--key", required=True)
    g.add_argument("--amount", type=float,
                   help="the money the answer moves, in the reporting currency "
                        "(default: the verdict's amount_reporting)")
    g.set_defaults(func=cmd_gate)

    lt = sub.add_parser("latest", help="path of the latest finished verdict for a key")
    lt.add_argument("--key", required=True)
    lt.set_defaults(func=cmd_latest)

    ls = sub.add_parser("list", help="every investigation on disk")
    ls.add_argument("--today", action="store_true")
    ls.set_defaults(func=cmd_list)

    du = sub.add_parser("due", help="register queries whose deferred investigation day has come")
    du.add_argument("--open", action="store_true", help="open each of them")
    du.set_defaults(func=cmd_due)

    sp = sub.add_parser("spend", help="the day's investigation cost against the cap")
    sp.add_argument("--day", help="YYYY-MM-DD, UTC; default today")
    sp.set_defaults(func=cmd_spend)

    a = p.parse_args(argv)
    if a.cmd == "wait" and not (a.key or a.all_open):
        p.error("wait needs --key or --all-open")
    return a.func(a)


if __name__ == "__main__":
    raise SystemExit(main())
