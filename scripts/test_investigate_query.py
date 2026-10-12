"""Offline checks for scripts/investigate_query.py and interco_breaks.py open.

No network, no Xero, no Claude CLI: the pre-check runs against a fake client,
the coordinator session against a stand-in `claude` script, everything in a
scratch tree (INVESTIGATION_HOME). Fictional data throughout.

  1. the certainty to grade mapping, the never-answered-alone caps, and the
     verdict checker
  2. the pre-check with a fake Xero client, and the register lines it carries
  3. the `posted since raised` rule that settles a query in Python
  4. the reuse rule
  5. the daily cap, deferral and the day's spend
  6. one batch end to end with the stand-in CLI: one session for the queries,
     equal shares, a missing verdict failing that key only, the cost written as
     an extra cost file scripts/run_cost.py reads, the gate, the cap deferring
     the next query, reuse and --fresh
  7. interco_breaks.py open: stable keys, reuse, a verdict that posted looked at again

    .venv/bin/python scripts/test_investigate_query.py
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parents[1]
EXAMPLE = REPO / "config" / "group.example.toml"
os.environ["AGENT_GROUP_CONFIG"] = str(EXAMPLE)
for k in ("AGENT_READONLY", "AGENT_RUN_ID", "AGENT_MODEL", "AGENT_SMALL_MODEL", "INVESTIGATION_BATCH"):
    os.environ.pop(k, None)
sys.path.insert(0, str(REPO / "scripts"))
import investigate_query as iq  # noqa: E402
import run_cost  # noqa: E402

tmp = pathlib.Path(tempfile.mkdtemp())
fails = 0
passed = 0
HOLD = "Example Holdings Limited"


def ok(cond: bool, what: str) -> None:
    global fails, passed
    if cond:
        passed += 1
    else:
        fails += 1
        print(f"FAIL {what}")


# the scratch tree
home = tmp / "home"
iq.ROOT, iq.BASE = home, home / "data" / "investigations"
iq.BATCHES, iq.QUEUE = iq.BASE / "_batches", iq.BASE / "_batches" / "queue"
iq.REGISTER = home / "docs" / "bookkept" / "outstanding.json"
iq.RUNS = home / "data" / "runs"
iq.REUSE_DAYS = 14

# ---------------------------------------------------------------- 1. grades, caps, validation

ok([iq.grade_of(c) for c in (5, 4, 3, 2, 1)] == ["high", "high", "medium", "low", "low"],
   "certainty 5 and 4 are high, 3 medium, 2 and 1 low")
ok(iq.grade_of(None) == "low" and iq.grade_of("4") == "low", "a certainty that is not an integer is low")

good = {"certainty": 2, "question": "q", "answer": "a", "report_line": "r", "unsure": "u",
        "question_for_admin": "qa", "actions": [{"type": "none", "what": "x"}]}
ok(not iq.validate(good), f"a complete verdict was rejected: {iq.validate(good)}")
ok(len(iq.validate(dict(good, unsure="", actions=[{"type": "payment"}]))) >= 2,
   "an incomplete verdict was accepted")
ok(any("idempotency_key" in p for p in iq.validate(
    dict(good, certainty=4, actions=[{"type": "post", "entity": HOLD, "payload": {"a": 1}}]))),
   "a post without an idempotency key was accepted")
ok(not iq.validate(dict(good, certainty=4, actions=[
    {"type": "post", "entity": HOLD, "payload": {"a": 1}, "idempotency_key": "k"}])),
   "a complete post was rejected")
ok(any("touches" in p for p in iq.validate(dict(good, touches=["whatever"]))), "an unknown cap was accepted")
ok(any("amount_reporting" in p for p in iq.validate(dict(good, amount_reporting="lots"))),
   "an amount that is not a number was accepted")

v = iq.finalise(dict(good, certainty=5))
ok(v["grade"] == "high" and v["touches"] == [], "an uncapped 5 is high")
v = iq.finalise(dict(good, certainty=5, touches=["same_purchase"]))
ok(v["grade"] == "low" and v["grade_from_certainty"] == "high",
   "a verdict touching a never-answered-alone item is capped at low")
v = iq.finalise(dict(good, certainty=4, actions=[{"type": "chase", "with": "admin", "what": "the invoice"}]))
ok(v["grade"] == "low" and "missing_document" in v["touches"], "a chase for a document is capped at low")
rec_pre = {"transactions": [{"found": [{"reconciled": True}]}]}
v = iq.finalise(dict(good, certainty=4, actions=[{"type": "update", "entity": HOLD, "payload": {"a": 1}}]), rec_pre)
ok(v["grade"] == "low" and "locked_or_reconciled" in v["touches"],
   "an update against a reconciled record is capped at low")

fenced = 'Done.\n```json\n{"certainty": 3, "x": 1}\n```'
ok((iq.parse_verdict(fenced) or {}).get("certainty") == 3, "a fenced verdict did not parse")
arr = [{"key": "a-1", "certainty": 2}, {"key": "B-2", "certainty": 4}, {"key": "zzz", "certainty": 5}]
ok(set(iq.parse_batch("```json\n" + json.dumps(arr) + "\n```", ["a-1", "B-2"])) == {"a-1", "B-2"},
   "a fenced array did not parse by key")
ok(set(iq.parse_batch("All done.\n" + json.dumps(arr), ["a-1"])) == {"a-1"},
   "an array after prose did not parse, or a stranger key was kept")
ok(iq.parse_batch('{"certainty": 3}', ["only"]).get("only", {}).get("certainty") == 3,
   "a lone object for a batch of one did not parse")
ok(iq.parse_batch("no json here", ["a-1"]) == {}, "prose parsed as a verdict")

# ---------------------------------------------------------------- 2. the pre-check, fake client


class FakeClient:
    """A read-only stand-in for XeroClient: one reconciled spend money of 37,250."""

    def __init__(self, user: str = "Pat Example"):
        self.user = user
        self.calls: list[str] = []

    def get(self, path, where=None, page=None):
        self.calls.append(path)
        if path == "BankTransactions":
            return {"BankTransactions": [{
                "BankTransactionID": "bt-1", "Type": "SPEND", "Status": "AUTHORISED", "IsReconciled": True,
                "DateString": "2026-10-13T00:00:00", "Total": 37250.0, "CurrencyCode": "USD",
                "Contact": {"Name": "Example Payee Ltd"}, "BankAccount": {"Name": "Main account"},
                "Reference": "REF-1", "LineItems": [
                    {"AccountCode": "400", "Description": "Transfer leg", "LineAmount": 37250.0}]},
                {"BankTransactionID": "bt-2", "Type": "SPEND", "Status": "DELETED", "Total": 37250.0}]}
        if path == "BankTransactions/bt-1/History":
            return {"HistoryRecords": [{"Changes": "Created", "DateUTCString": "2026-10-15T09:00:00",
                                        "User": self.user}]}
        if path in ("Invoices", "ManualJournals"):
            return {path: []}
        if path == "Accounts":
            if where and "BANK" in where:
                return {"Accounts": [{"AccountID": "acc-bank", "Name": "Main account", "Type": "BANK",
                                      "CurrencyCode": "USD"}]}
            return {"Accounts": [{"AccountID": "acc-400", "Code": "400", "Name": "Example Expense"},
                                 {"AccountID": "acc-999", "Code": "999", "Name": "Other"}]}
        if path == "Organisation":     # the wider match (xero/matching.py) reads the base currency
            return {"Organisations": [{"BaseCurrency": "USD", "Name": "Example Holdings Limited"}]}
        if path in ("Payments", "Prepayments", "Overpayments", "CreditNotes", "BankTransfers"):
            return {path: []}
        raise AssertionError(f"unexpected read {path}")

    def get_all(self, path, where=None):
        return self.get(path, where=where).get(path, [])


fake_clients: dict[str, FakeClient] = {}
iq._client = lambda ent: fake_clients.setdefault(ent, FakeClient())

# a register with a line on the same key and a watch line on the same amount
iq.REGISTER.parent.mkdir(parents=True)
iq.REGISTER.write_text(json.dumps([
    {"id": "OI-0001", "domain": "bookkeeping", "kind": "queried", "with": "admin", "key": "fx-legs",
     "text": "do the legs post on the usual pattern", "refs": "", "raised": "2026-10-14"},
    {"id": "OI-0002", "domain": "bookkeeping", "kind": "watch", "with": "admin", "key": "other",
     "text": "watching", "refs": "USD 37,250.00 · 13 Oct", "raised": "2026-10-14"},
    {"id": "OI-0003", "domain": "bookkeeping", "kind": "manual", "with": "admin", "key": "unrelated",
     "text": "something else", "refs": "USD 12.00", "raised": "2026-10-14"}]))

BRIEF = {
    "domain": "bookkeeping", "key": "fx-legs", "register_id": "OI-0001", "with": "admin",
    "question": "do the legs post on the usual pattern; nothing posted for the 13 Oct transfer",
    "refs": "Example Payee Ltd · 13 Oct · USD 37,250.00 · paid HoldCo", "context": "", "raised": "2026-10-14",
    "answered": "", "opened": "2026-10-18T08:10:33Z",
    "txns": [iq.parse_txn_arg("HOLDCO|2026-10-13|37250|USD|Example Payee")]}
ok(BRIEF["txns"][0]["entities"] == [HOLD], "--txn resolves an entity key through config")
ok(iq.parse_txn_arg("holdings|2026-10-13..2026-10-21|5")["to"] == "2026-10-21", "--txn takes a date range")
ok(iq.txns_from_text("Example Payee · 13 Oct · USD 37,250.00 · paid HoldCo", 2026)[0]["entities"] == [HOLD],
   "a register line's entity short code resolves through config")
ok(iq.txns_from_text("no money here", 2026) == [], "a line with no amount names no transactions")

pre = iq.precheck(BRIEF, home)
found = pre["transactions"][0]["found"]
ok(not pre["errors"], f"the pre-check should not error: {pre['errors']}")
ok(len(found) == 1 and found[0]["id"] == "bt-1" and found[0]["reconciled"] is True,
   "the pre-check finds the live record and drops the deleted one")
ok(found[0]["created"] == ["2026-10-15T09:00:00", "Pat Example"], "the pre-check reads who created it")
ok(pre["accounts"][HOLD] == {"400": "Example Expense"}, "the pre-check names the account it found")
ok(sorted(i["id"] for i in pre["register"]) == ["OI-0001", "OI-0002"],
   "the register lines on the same key or amount are part of the answer")
ok(all(c in ("BankTransactions", "Invoices", "ManualJournals", "Accounts", "Organisation", "Payments") or c.endswith("/History")
       for c in fake_clients[HOLD].calls), "the pre-check only reads")
try:
    from accounting_agent.xero import matching  # noqa: F401
except ImportError:
    ok("note_matching" not in pre or "skipped" in pre["note_matching"],
       "without the matching module the wider match is skipped, not failed")
ok("note" in iq.precheck(dict(BRIEF, txns=[]), home), "a brief with no transactions looks nothing up")

# ---------------------------------------------------------------- 3. settled in Python

s = iq.settle_from_precheck(BRIEF, pre)
ok(bool(s) and s["certainty"] == 4 and s["decided_by"] == "precheck" and not iq.validate(s),
   f"posted by a person since raised settles at 4: {s and iq.validate(s)}")
ok(bool(s) and iq.finalise(s, pre)["grade"] == "high" and s["amount_reporting"] == 0,
   "the settled verdict is graded high and moves no money")
ok(bool(s) and [a["type"] for a in s["actions"]] == ["none", "close"]
   and "Example Expense (400)" in s["answer"] and "OI-0001" in s["actions"][1]["what"],
   "the verdict names the account and closes its own register line, leaving the watch line")
fake_clients[HOLD] = FakeClient(user="System Generated")
sys_pre = iq.precheck(BRIEF, home)
s3 = iq.settle_from_precheck(BRIEF, sys_pre)
ok(bool(s3) and s3["certainty"] == 3 and iq.finalise(s3, sys_pre)["grade"] == "medium"
   and "an agent run" in s3["answer"].lower(),"created by an agent run settles at 3, graded medium")


def mutate(p: dict, **changes) -> dict:
    q = json.loads(json.dumps(p))
    q["transactions"][0]["found"][0].update(changes)
    return q


no_settle = {
    "a question about coding, not absence": (dict(BRIEF, question="confirm the coding of the leg"), pre),
    "a record created before the query": (BRIEF, mutate(pre, created=["2026-09-28T10:00:00", "Pat Example"])),
    "a record created the day it was raised": (BRIEF, mutate(pre, created=["2026-10-14T15:00:00", "Pat Example"])),
    "an unreconciled bank transaction": (BRIEF, mutate(pre, reconciled=False)),
    "another currency": (BRIEF, mutate(pre, currency="EUR")),
    "an account with no name": (BRIEF, dict(pre, accounts={})),
    "an answered line": (dict(BRIEF, answered="it is a transfer leg"), pre),
    "a pre-check error": (BRIEF, dict(pre, errors=["429"])),
}
two = json.loads(json.dumps(pre))
two["transactions"][0]["found"].append(dict(two["transactions"][0]["found"][0], id="bt-9"))
no_settle["two records for the amount"] = (BRIEF, two)
ask = json.loads(json.dumps(pre))
ask["register"][1]["kind"] = "manual"
no_settle["another register line asking for something"] = (BRIEF, ask)
for what, (b, p) in no_settle.items():
    ok(iq.settle_from_precheck(b, p) is None, f"should not settle: {what}")

# ---------------------------------------------------------------- 4. reuse


def prior(key: str, brief: dict, pre_: dict, result: dict, days_ago: float = 1) -> pathlib.Path:
    when = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days_ago)
    run = iq.BASE / iq.slug(key) / when.strftime("%Y%m%dT%H%M%SZ")
    run.mkdir(parents=True)
    iq._write(run / "brief.json", brief)
    iq._write(run / "precheck.json", pre_)
    iq._write(run / "result.json", {"key": key, "status": "done", **result})
    return run


TOMORROW = (dt.date.today() + dt.timedelta(days=1)).isoformat()
B = {"domain": "bookkeeping", "key": "reuse-a", "question": "which entity bears it", "context": "checked",
     "refs": "X · 13 Oct · GBP 10.00", "txns": [], "opened": "2026-10-13T00:00:00Z", "raised": "2026-10-13"}
P = {"transactions": [], "register": [], "errors": [], "checked": "2026-10-13T00:00:00Z"}
prior("reuse-a", B, P, dict(good))
ok(iq.reusable_prior(dict(B, opened="later", raised="2026-10-19"), dict(P, checked="later")) is not None,
   "an unchanged brief and pre-check should reuse a low verdict")
ok(iq.reusable_prior(dict(B, context="the invoice arrived"), P) is None, "new context should not reuse")
ok(iq.reusable_prior(B, dict(P, register=[{"id": "OI-1", "key": "reuse-a", "answered": "post to 431"}])) is None,
   "an admin's answer should never be reused over")
ok(iq.reusable_prior(dict(B, fresh=True), P) is None, "--fresh should never reuse")
ok(iq.reusable_prior(B, dict(P, errors=["timeout"])) is None, "a broken pre-check should not reuse")
prior("reuse-b", dict(B, key="reuse-b"), P,
      dict(good, certainty=4, actions=[{"type": "post", "entity": HOLD, "payload": {"a": 1}}]))
ok(iq.reusable_prior(dict(B, key="reuse-b"), P) is None, "a high verdict that posts is never reused")
prior("reuse-c", dict(B, key="reuse-c"), P,
      dict(good, certainty=4, actions=[{"type": "none", "what": "x"}, {"type": "close", "what": "y"}]))
ok(iq.reusable_prior(dict(B, key="reuse-c"), P) is not None, "a high verdict that only closes is reused")
prior("reuse-d", dict(B, key="reuse-d"), P, dict(good), days_ago=20)
ok(iq.reusable_prior(dict(B, key="reuse-d"), P) is None, "a verdict past reuse_days is not reused")
prior("reuse-e", dict(B, key="reuse-e"), P, dict(good), days_ago=2)
prior("reuse-e", dict(B, key="reuse-e"), P, {"status": "deferred", "scheduled": TOMORROW}, days_ago=1)
ok(iq.reusable_prior(dict(B, key="reuse-e"), P) is None, "a deferred verdict is never reused")
prior("reuse-g", dict(B, key="reuse-g"), P, {"status": "failed", "reason": "x"})
ok(iq.reusable_prior(dict(B, key="reuse-g"), P) is None, "a failed verdict is never reused")
EV = dict(B, key="reuse-f", evidence=["HOLDCO|2026-03-17|MJ|USD|761.42|9012|84e2"], context="rate 1.2875",
          refs="USD 1,694")
prior("reuse-f", EV, {}, dict(good))
ok(iq.reusable_prior(dict(EV, context="rate 1.3512", refs="USD 1,695"), None) is not None,
   "with --evidence a context and refs that moved with the rate still reuse")
ok(iq.reusable_prior(dict(EV, evidence=["HOLDCO|2026-03-17|MJ|USD|900.00|9012|84e2"]), None) is None,
   "changed evidence does not reuse")

# ---------------------------------------------------------------- 5. the cap, deferral, spend

iq.DAILY_CAP, iq.BATCH_MAX = 35.0, 8
ok(iq.allowed_batch(0.0, 1.2, 12) == 8, "a fresh day takes a full batch")
ok(iq.allowed_batch(33.0, 1.0, 8) == 5, "near the cap the batch shrinks to what fits")
ok(iq.allowed_batch(34.9, 3.0, 8) == 1, "under the cap one query still runs")
ok(iq.allowed_batch(35.0, 1.0, 8) == 0, "at the cap nothing starts")

d = iq.defer(prior("cap-a", dict(B, key="cap-a", with_=None), P, {}, days_ago=0.0001), 36.0)
ok(d["status"] == "deferred" and d["scheduled"] == TOMORROW and "outstanding.py add" in d["register_command"]
   and "--with admin" in d["register_command"], "a deferred query gets its day and the add command")
iq.REGISTER.write_text(json.dumps([{"id": "OI-7", "domain": "bookkeeping", "kind": "queried",
                                    "key": "cap-a", "text": "t", "refs": "", "raised": "2026-10-13"}]))
ok("update --key cap-a --schedule-investigation" in iq.register_command(
    dict(B, key="cap-a"), home, TOMORROW, {"register": []}),
   "a query the register already holds is rescheduled, not added twice")

today = dt.datetime.now(dt.timezone.utc)
tag = today.strftime("%Y%m%dT%H%M%SZ")
solo = iq.BASE / "spend-solo" / tag
solo.mkdir(parents=True)
iq._write(solo / "result.json", {"status": "done", "cost_usd": 1.0})
for k in ("spend-b1", "spend-b2"):
    r = iq.BASE / k / tag
    r.mkdir(parents=True)
    iq._write(r / "result.json", {"status": "done", "cost_usd": 1.0, "batch_id": f"{tag}-g"})
free = iq.BASE / "spend-free" / tag
free.mkdir(parents=True)
iq._write(free / "result.json", {"status": "done", "cost_usd": 3.0, "reused_from": "x"})
bd = iq.BATCHES / f"{tag}-g"
bd.mkdir(parents=True)
iq._write(bd / "manifest.json", {"queries": [{}, {}], "estimate_usd": 9.0})
(bd / "session.json").write_text(json.dumps({"total_cost_usd": 2.0}))
old = iq.BATCHES / ((today - dt.timedelta(days=1)).strftime("%Y%m%dT%H%M%SZ") + "-g")
old.mkdir(parents=True)
(old / "session.json").write_text(json.dumps({"total_cost_usd": 50.0}))
ok(abs(iq.spent_on(today.date()) - 3.0) < 1e-6, f"the day's spend is 3.00, got {iq.spent_on(today.date())}")

# ---------------------------------------------------------------- 6. one batch, end to end

e2e = tmp / "e2e"
(e2e / "docs" / "bookkept").mkdir(parents=True)
fake = tmp / "claude"
fake.write_text(r'''#!/usr/bin/env python3
import json, os, re, sys
argv = sys.argv[1:]
prompt = argv[argv.index("-p") + 1]
manifest = re.search(r"The manifest (\S+) lists", prompt).group(1)
m = json.load(open(manifest))
scratch = manifest.rsplit("/", 1)[0]
json.dump({"argv": argv, "env": {k: os.environ.get(k, "") for k in
           ("AGENT_READONLY", "AGENT_INVESTIGATION", "QUERY_DIR")}}, open(scratch + "/argv.json", "w"))
out = []
for q in m["queries"]:
    b = json.load(open(q["brief"]))
    v = {"key": q["key"], "certainty": 2, "question": b["question"], "answer": "a", "evidence": [],
         "unsure": "u", "question_for_admin": "qa", "actions": [{"type": "none", "what": "x"}],
         "report_line": "r", "amount_reporting": 250}
    if "OMIT" in b["question"]:
        continue
    if "SAVEONLY" in b["question"]:
        json.dump(v, open(scratch + "/verdicts/" + q["key"] + ".json", "w"))
        continue
    if "HIGH" in b["question"]:
        v["certainty"] = 5
    if "MEDIUM" in b["question"]:
        v["certainty"] = 3
    if "CAPPED" in b["question"]:
        v["certainty"] = 5
        v["touches"] = ["same_purchase"]
    if "BROKEN" in b["question"]:
        v["certainty"] = 9
    out.append(v)
print(json.dumps({"result": "Done.\n```json\n" + json.dumps(out) + "\n```",
                  "total_cost_usd": 0.9, "session_id": "s1"}))
''')
fake.chmod(0o755)
CAPPED_TOML = tmp / "capped.toml"
CAPPED_TOML.write_text(EXAMPLE.read_text().replace("daily_cap_usd = 35", "daily_cap_usd = 0.5"))
ENV = {**os.environ, "INVESTIGATION_HOME": str(e2e), "INVESTIGATION_CLAUDE_BIN": str(fake),
       "INVESTIGATION_POLL": "0.3", "AGENT_RUN_ID": "test-run-1"}
RUNNER = [sys.executable, str(REPO / "scripts" / "investigate_query.py")]


def cli(*a: str, env: dict | None = None, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(RUNNER + list(a), cwd=REPO, env=env or ENV, capture_output=True,
                          text=True, timeout=timeout)


def result(key: str) -> dict:
    runs = sorted((e2e / "data" / "investigations" / key).iterdir())
    return json.loads((runs[-1] / "result.json").read_text())


KEYS = {"e2e-a": "HIGH which entity bears A", "e2e-b": "MEDIUM which entity bears B",
        "e2e-c": "OMIT which entity bears C", "e2e-d": "SAVEONLY which entity bears D",
        "e2e-e": "CAPPED which entity bears E", "e2e-f": "BROKEN which entity bears F"}
for key, q in KEYS.items():
    r = cli("open", "--key", key, "--question", q, "--context", "checked")
    ok(r.returncode == 0, f"open {key} failed: {r.stderr}")
w = cli("wait", "--all-open", "--timeout", "90")
ok(w.returncode == 0, f"wait should finish the batch: rc {w.returncode} {w.stdout} {w.stderr}")
if w.returncode == 0:
    ra, rb, rc, rd, re_, rf = (result(k) for k in KEYS)
    ok(ra["status"] == rb["status"] == rd["status"] == re_["status"] == "done", "the answered keys are done")
    ok(rc["status"] == "failed" and "no verdict" in rc["reason"], "a missing verdict fails that key only")
    ok(rf["status"] == "failed" and "certainty" in rf["reason"], "a verdict that fails validation fails that key only")
    ok(len({r["batch_id"] for r in (ra, rb, rc, rd, re_, rf)}) == 1 and ra["batch_size"] == 6,
       "the six queries share one batch")
    ok(abs(ra["cost_usd"] - 0.15) < 1e-6, "each result carries an equal share")
    ok((ra["certainty"], ra["grade"]) == (5, "high") and (rb["certainty"], rb["grade"]) == (3, "medium")
       and (rd["certainty"], rd["grade"]) == (2, "low"), "certainty maps to grade in the stored results")
    ok(re_["certainty"] == 5 and re_["grade"] == "low" and re_["touches"] == ["same_purchase"],
       "a verdict touching a never-answered-alone item is stored capped at low")
    bdir = e2e / "data" / "investigations" / "_batches" / ra["batch_id"]
    seen = json.loads((bdir / "argv.json").read_text())
    argv = seen["argv"]
    ok(argv[argv.index("--effort") + 1] == "medium" and argv[argv.index("--model") + 1] == "opus"
       and argv[argv.index("--agent") + 1] == "query-investigator",
       "the coordinator runs the agent on the configured model at medium effort")
    ok(seen["env"]["AGENT_READONLY"] == "1" and seen["env"]["AGENT_INVESTIGATION"] == "1",
       "the session is read-only and marked as an investigation")
    ok("haiku" in argv[argv.index("-p") + 1], "the prompt names the researcher model from config")
    extra, running = run_cost.extra_costs("test-run-1", e2e / "data" / "runs")
    ok(abs(extra.get("investigations", 0) - 0.9) < 1e-6 and running == 0,
       f"run_cost reads the batch cost as an extra cost file: {extra} {running}")

    # the gate: grade and amount to config.resolve_action, recorded on the verdict
    g = cli("gate", "--key", "e2e-a", "--amount", "100")
    ok(g.stdout.split()[0] == "act", f"a high grade within the limit is acted on: {g.stdout}")
    g = cli("gate", "--key", "e2e-a", "--amount", "30000")
    ok(g.stdout.split()[0] == "query" and result("e2e-a")["gate"]["action"] == "query",
       f"a high grade over the high limit is queried and the gate is recorded: {g.stdout}")
    g = cli("gate", "--key", "e2e-b", "--amount", "100")
    ok(g.stdout.split()[0] == "confirm", f"a medium grade within the limit is to confirm: {g.stdout}")
    g = cli("gate", "--key", "e2e-e")
    ok(g.stdout.split()[0] == "query" and "low" in g.stdout, f"a capped verdict is queried: {g.stdout}")
    ok(cli("gate", "--key", "e2e-c").returncode == 1, "a failed investigation has no grade to gate")
    ok(cli("gate", "--key", "e2e-d", env={**ENV, "AGENT_READONLY": "1"}).returncode == 0
       and "gate" not in result("e2e-d"), "a read-only session gates without recording")

    # Over the cap: the next query is deferred with its day and its register command.
    capped = {**ENV, "AGENT_GROUP_CONFIG": str(CAPPED_TOML), "AGENT_RUN_ID": "test-run-2"}
    cli("open", "--key", "e2e-g", "--question", "which entity bears G", env=capped)
    w = cli("wait", "--all-open", "--timeout", "60", env=capped)
    rg = result("e2e-g")
    ok(rg.get("status") == "deferred" and rg.get("scheduled") == TOMORROW,
       f"over the cap the query is deferred to tomorrow: {rg}")
    ok("outstanding.py add" in rg.get("register_command", "") and "--investigation" in rg["register_command"],
       "a deferred query not in the register is given the add command")
    ok("deferred" in w.stdout and "Register it" in w.stdout, "wait prints the deferred line")

    # Reuse: the same low query again, nothing changed, costs no session.
    again = {**ENV, "AGENT_RUN_ID": "test-run-3"}
    cli("open", "--key", "e2e-d", "--question", KEYS["e2e-d"], "--context", "checked", env=again)
    cli("wait", "--all-open", "--timeout", "60", env=again)
    ok(result("e2e-d").get("reused_from"), "an unchanged low query reuses its verdict")
    cli("open", "--key", "e2e-d", "--question", KEYS["e2e-d"], "--context", "checked", "--fresh", env=again)
    cli("wait", "--all-open", "--timeout", "90", env=again)
    rn = result("e2e-d")
    ok(not rn.get("reused_from") and rn.get("batch_id") and rn["batch_id"] != ra["batch_id"],
       "--fresh investigates again in a new batch")
    # An answered register line is never investigated; a read-only session opens nothing.
    (e2e / "docs" / "bookkept" / "outstanding.json").write_text(json.dumps([
        {"id": "OI-9", "domain": "bookkeeping", "kind": "queried", "with": "admin", "key": "e2e-z",
         "text": "which entity", "refs": "", "raised": "2026-10-13", "answered": "OPCO_US"}]))
    ok(cli("open", "--from-register", "e2e-z").returncode == 3, "an answered line is not opened")
    ok(cli("open", "--key", "e2e-y", "--question", "q", env={**ENV, "AGENT_READONLY": "1"}).returncode == 3,
       "a read-only session opens no investigation")
    off = tmp / "off.toml"
    off.write_text(EXAMPLE.read_text().replace("enabled = true", "enabled = false", 1))
    ok(cli("open", "--key", "e2e-x", "--question", "q", env={**ENV, "AGENT_GROUP_CONFIG": str(off)}).returncode == 4,
       "with [investigation] switched off, open points the run at the gate")

# ---------------------------------------------------------------- 7. interco_breaks open

import interco_breaks  # noqa: E402

LINE = {"entity": "OPCO_EU", "date": "2026-03-17", "source": "BILL", "doc_amount": 1187.60, "base": 1187.60,
        "entity_base": "EUR", "currency": "EUR", "code": "2620", "party": "Example Hotel",
        "desc": "hotel for a trip", "doc_id": "3f9c1e70-0000-4000-8000-00000000a7d2"}


def recon(rate: float) -> dict:
    return {"rates": {"EUR": rate, "GBP": 1.3, "USD": 1.0}, "pairs": [{
        "pair": "HOLDCO<>OPCO_EU / loan", "flavour": "loan", "status": "BREAK", "break": 1694.0,
        "a": {"entity": "HOLDCO", "code": "1620", "balance": -1.0, "base": "GBP"},
        "b": {"entity": "OPCO_EU", "code": "2620", "balance": 1.0, "base": "EUR"},
        "unmatched_a": [], "unmatched_b": [LINE]}]}


first = interco_breaks.open_items(recon(1.0817))[0]
later = interco_breaks.open_items(recon(1.0900))[0]
ok(first["key"] != later["key"], "the fixture moves the key with the rate")
ok(first["key"].startswith("interco-holdco-opco-eu-loan-2026-03-17-"), f"the key shape: {first['key']}")
fb = interco_breaks.brief_for(first, recon(1.0817)["rates"], "", "interco")
ok(fb["with"] == "admin" and fb["domain"] == "bookkeeping", "the brief goes to the configured default admin")
ok("Example Holdings Limited" in fb["question"] and "Example Operations B.V." in fb["question"],
   "the question names both entities in full, from config")
ok(any(t["entities"] == ["Example Operations B.V."] and t["amount"] == 1187.60 for t in fb["txns"])
   and any(t["entities"] == [HOLD] and t["from"] < "2026-03-17" for t in fb["txns"]),
   "the line and the same amount in the other entity are the pre-check's transactions")
ok(fb["doc_ids"] == [LINE["doc_id"]] and fb["evidence"], "the lines are the evidence")
prior(first["key"], fb, {}, dict(good, actions=[{"type": "none", "what": "x"}]))
ok(interco_breaks.stable_key(later) == first["key"], "a rate move keeps the earlier key")
rj = tmp / "recon.json"
rj.write_text(json.dumps(recon(1.0900)))


def run_open(**kw):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        interco_breaks.cmd_open(argparse.Namespace(json=str(rj), key=None, fresh=False, max=0,
                                                   with_="", run_job="interco", **kw))
    return buf.getvalue()


# --max 0 so nothing here can ever open a pre-check from a laptop.
out = run_open()
newest = iq.latest(first["key"])
ok("reuse" in out and iq._read(newest / "result.json").get("reused_from"),
   f"an unchanged break is re-used without a pre-check: {out}")
prior(first["key"], fb, {}, dict(good, certainty=4,
                                 actions=[{"type": "post", "entity": HOLD, "payload": {"a": 1},
                                           "idempotency_key": "k"}]), days_ago=-0.0002)
ok("held" in run_open(), "a break whose verdict posted is looked at again")

total = passed + fails
print(f"{passed}/{total} passed")
sys.exit(1 if fails else 0)
