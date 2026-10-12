"""Offline checks for scripts/outstanding_check.py: which lines go to the
agents and which are left alone, the Python closing pass against a fake Xero
client, the done-in-thread claim, batching and its light and full tiers,
verdicts applied through a fake register CLI (NOT_NEEDED and TRIVIAL among
them), the grouped post, the skip rules and the rate-limit skip; and the
trivial threshold in scripts/outstanding.py (`add` refusing and `--keep`,
`prune-trivial`). Fictional group from config/group.example.toml, fake data;
no network, no Claude session, nothing written outside a temporary folder.

    .venv/bin/python scripts/test_outstanding_check.py
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parents[1]
TMP = pathlib.Path(tempfile.mkdtemp(prefix="test_outstanding_check_"))
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "src"))
os.environ["AGENT_GROUP_CONFIG"] = str(REPO / "config" / "group.example.toml")
os.environ.pop("AGENT_RUN_ID", None)
os.environ.pop("AGENT_READONLY", None)
import outstanding_check as oc  # noqa: E402

oc.REGISTER, oc.RECONCILE, oc.BASE, oc.RUNS = (TMP / "outstanding.json", TMP / "reconcile.json",
                                               TMP / "checks", TMP / "runs")
FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("ok   " if cond else "FAIL ") + name + (f" - {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def item(i: int, domain="bookkeeping", kind="manual", key="", text="x", refs="", **kw) -> dict:
    return {"id": f"{domain[:2].upper()}-{i:03d}", "domain": domain, "kind": kind, "with": "admin",
            "key": key, "text": text, "refs": refs, "raised": "2026-01-02", "answered": "", **kw}


# -- the lines: owned keys are left alone, repeated keys are not closable -------
items = [
    item(1, key="INV-9001"),
    item(2, key="rpt-check:prepayments"),
    item(3, key="rpt-note:accruals:x"),
    item(4, key="turn-cap-bookkeep"),
    item(5, key="DUP-1"), item(6, key="DUP-1"),
    item(7, domain="reports", kind="queried", key=""),
]
oc.REGISTER.write_text(json.dumps(items))
oc.RECONCILE.write_text(json.dumps({"built": "2026-01-02T01:00:00", "items": [
    {"entity": "HoldCo", "text": "match the line to the bill", "refs": "Acme Hosting · INV-1"}]}))
rows = oc.lines(None)
refs = [r["ref"] for r in rows]
check("balance report, note and turn-cap lines are skipped",
      not any(r.startswith(("rpt-check", "rpt-note", "turn-cap")) for r in refs), str(refs))
check("a unique key is the ref and closable", "INV-9001" in refs and rows[0]["closable"])
check("a repeated key falls back to id@domain and is not closable",
      [r["closable"] for r in rows if r["id"] in ("BO-005", "BO-006")] == [False, False]
      and all("@" in r["ref"] for r in rows if r["id"] in ("BO-005", "BO-006")))
check("the reconcile list is appended as REC lines", rows[-1]["ref"] == "REC-01" and rows[-1]["kind"] == "reconcile")
check("a domain filter drops the other domains and the reconcile list",
      all(r["domain"] == "reports" for r in oc.lines("reports")))
check("the reconcile list counts as fresh on the day it was built",
      oc.reconcile_fresh("2026-01-02") and not oc.reconcile_fresh("2026-01-03"))


# -- the bill rule --------------------------------------------------------------
def settle(text: str, refs: str, **kw) -> dict:
    return item(1, key="K", text=text, refs=refs, ref="K", closable=True, **kw)


R = "Acme Hosting · 4 Jan · GBP 240.00 · paid HoldCo · recognised OpCo US · INV-1013"
check("a one-bill settle line is read",
      oc.bill_rule(settle("manually reconcile the bill to intercompany (2150)", R)) == ("OpCo US", "INV-1013", "2150"))
check("a leading entity and no account code is read",
      oc.bill_rule(settle("in OpCo US, settle the bill against the intercompany loan", R)) == ("OpCo US", "INV-1013", None))
check("a second step is left to the coordinator",
      oc.bill_rule(settle("reconcile the bill to intercompany, then void the spend money", R)) is None)
check("two entities are left to the coordinator",
      oc.bill_rule(settle("reconcile the bill to intercompany",
                          "Acme Hosting · recognised HoldCo · recognised OpCo US · INV-1013")) is None)
check("a line that is not a settle is left to the coordinator",
      oc.bill_rule(settle("void the duplicate bill, intercompany", R)) is None)
check("a query is never read by the rule",
      oc.bill_rule({**settle("reconcile the bill to intercompany", R), "kind": "queried"}) is None)
check("a line with no unique key is never read by the rule",
      oc.bill_rule({**settle("reconcile the bill to intercompany", R), "closable": False}) is None)


class FakeXero:
    """The reads the closing pass makes, from a dict."""
    def __init__(self, bills, payments, chart):
        self.bills, self.payments, self.chart, self.calls = bills, payments, chart, []

    def get(self, path, **params):
        self.calls.append(path)
        if path == "Invoices":
            return {"Invoices": self.bills}
        if path == "Accounts":
            return {"Accounts": self.chart}
        if path.startswith("Payments/"):
            return {"Payments": [self.payments[path.split("/")[1]]]}
        raise AssertionError(path)


CHART = [{"AccountID": "a-loan", "Code": "2150", "Name": "Intercompany loan", "Type": "CURRLIAB"},
         {"AccountID": "a-bank", "Code": "1000", "Name": "Main account", "Type": "BANK"}]


def paid_bill(account_id="a-loan", **kw) -> FakeXero:
    bill = {"Status": "PAID", "AmountDue": 0, "Payments": [{"PaymentID": "p1"}], **kw}
    return FakeXero([bill], {"p1": {"Account": {"AccountID": account_id}}}, CHART)


line = settle("manually reconcile the bill to intercompany (2150)", R)
fx = paid_bill()
out = oc.python_first([line], client=lambda e: fx)
check("a bill paid from the named loan account is closed", len(out) == 1 and out[0]["definite"] is True
      and out[0]["verdict"] == "CLOSED" and "INV-1013" in out[0]["evidence"], str(out))
check("the Xero client is only asked to read", set(fx.calls) <= {"Invoices", "Accounts", "Payments/p1"})
check("a bill paid from a bank account is not closed",
      oc.python_first([line], client=lambda e: paid_bill("a-bank")) == [])
check("a bill paid from another account than the one named is not closed",
      oc.python_first([settle("reconcile the bill to intercompany (2160)", R)], client=lambda e: paid_bill()) == [])
check("an unpaid bill is not closed",
      oc.python_first([line], client=lambda e: paid_bill(Status="AUTHORISED", AmountDue=240)) == [])
check("a bill with a credit note is not closed",
      oc.python_first([line], client=lambda e: paid_bill(CreditNotes=[{"x": 1}])) == [])
two = FakeXero([{"Status": "PAID"}, {"Status": "PAID"}], {}, CHART)
check("two bills with the number are not closed", oc.python_first([line], client=lambda e: two) == [])


def boom(entity):
    raise RuntimeError("no token")


check("a failed read leaves the line to the coordinator", oc.python_first([line], client=boom) == [])
check("the read-only flag is put back after the pass", "AGENT_READONLY" not in os.environ)

# -- done in the thread is a claim ----------------------------------------------
admin = "U0000000001"
oc.config.slack().admins.setdefault(admin, "First Admin")
title = f"{oc.config.company_name().upper()} MANUAL ITEMS"


def fake_pages(method, key, **params):
    if method == "conversations.history":
        return [{"text": f"{title} (05 Jan 2026, 09:00 UTC)", "ts": "1767600000.000100", "reply_count": 2},
                {"text": "something else", "ts": "1767600001.000100", "reply_count": 3}]
    return [{"text": "head", "ts": "1"},
            {"text": "reconcile INV-9001 and BO-002", "bot_id": "B1", "ts": "2"},
            {"text": "done", "user": admin, "ts": "3"},
            {"text": "done OTHER", "user": "U9999999999", "ts": "4"}]


oc.slack_pages = fake_pages
said = oc.done_replies([{"ref": "INV-9001", "id": "BO-001", "key": "INV-9001", "kind": "manual", "refs": ""},
                        {"ref": "OTHER", "id": "BO-003", "key": "OTHER", "kind": "manual", "refs": ""}])
check("a bare done names the lines the thread carried", list(said) == ["INV-9001"], str(said))
check("the claim names who said it and where", "First Admin" in said["INV-9001"] and title in said["INV-9001"])
check("a done from someone who is not an admin is ignored", "OTHER" not in said)
claimed = {**item(1, key="INV-9001"), "ref": "INV-9001", "closable": True, "done_in_thread": said["INV-9001"]}
check("a done in the thread closes nothing on its own", oc.python_first([claimed]) == [])

# -- batching ---------------------------------------------------------------------
many = [{"ref": f"K{i}", "closable": True, **item(i, key=f"K{i}")} for i in range(60)]
many += [{"ref": f"Q{i}", "closable": True, **item(i, kind="queried", key=f"Q{i}")} for i in range(3)]
many += [{"ref": f"R{i}", "closable": True, **item(i, domain="reports", kind="queried", key=f"R{i}")} for i in range(2)]
n = oc.write_batches(many, TMP / "b1")
files = sorted((TMP / "b1").glob("*.json"))
sizes = [len(json.loads(f.read_text())) for f in files]
check("batches hold at most 25 lines by default", n == len(files) == 5 and max(sizes) == 25, str(sizes))
check("one domain and kind to a batch",
      all(len({(x["domain"], x["kind"]) for x in json.loads(f.read_text())}) == 1 for f in files))
check("the closable flag is not sent to the agents",
      all("closable" not in x for f in files for x in json.loads(f.read_text())))
check("a configured batch size is honoured", oc.write_batches(many, TMP / "b2", 10) == 8)

# -- verdicts applied through a fake register CLI ---------------------------------
calls: list[tuple] = []
oc.outstanding = lambda *a: calls.append(a) or ""
vrows = [{"ref": r, "closable": c, **item(i, key=r)} for i, (r, c) in enumerate(
    [("A", True), ("B", True), ("C", True), ("D", True), ("E", True), ("F", False), ("G", True)], 1)]
vrows.append({"ref": "REC-01", "closable": True, "id": "REC-01", "domain": "bookkeeping",
              "kind": "reconcile", "with": "admin", "text": "t", "refs": "Acme Hosting"})
oc.RECONCILE.write_text(json.dumps({"built": "2026-01-01", "items": [
    {"entity": "HoldCo", "text": "t", "refs": "Acme Hosting"}, {"entity": "HoldCo", "text": "u", "refs": "Other"}]}))
verdicts = [
    {"ref": "A", "verdict": "CLOSED", "definite": True, "evidence": "bill PAID"},
    {"ref": "B", "verdict": "CLOSED", "definite": False, "evidence": "likely"},
    {"ref": "C", "verdict": "PARTLY", "definite": False, "evidence": "e", "left": "void the copy"},
    {"ref": "D", "verdict": "OPEN"},
    {"ref": "E", "verdict": "UNVERIFIED", "evidence": "cannot read"},
    {"ref": "F", "verdict": "CLOSED", "definite": True, "evidence": "no key"},
    {"ref": "REC-01", "verdict": "CLOSED", "definite": True, "evidence": "reconciled"},
    {"ref": "NOPE", "verdict": "CLOSED", "definite": True},
]
done = oc.apply(vrows, verdicts, "2026-01-02", dry_run=False)
closes = [c for c in calls if c[0] == "close"]
check("only a CLOSED and definite line with a unique key is closed",
      [c[2] for c in closes] == ["A"], str(closes))
check("the close carries a dated reason and defers the republish",
      "checked 2026-01-02: bill PAID" in closes[0] and "--defer" in closes[0])
check("a CLOSED that is not definite is kept and reported", [r["ref"] for r, _ in done["kept"]] == ["B", "F"])
check("a partly done line stays and says what is left", done["partly"][0][1] == "void the copy")
check("unverified, open and missing are counted",
      len(done["unverified"]) == 1 and done["open"] == 1 and [r["ref"] for r in done["missing"]] == ["G"])
check("a closed reconcile line is dropped from the reconcile list",
      any(c[0] == "set-reconcile" for c in calls)
      and [r["refs"] for r in json.loads((oc.BASE / "reconcile.next.json").read_text())["items"]] == ["Other"])
check("the Word copy is republished once", [c for c in calls if c[0] == "publish"] == [("publish",)])
calls.clear()
oc.apply(vrows, verdicts, "2026-01-02", dry_run=True)
check("a dry run closes nothing", calls == [])
text = oc.report(done, 8, ["a note"])
check("the report counts and names the sections",
      text.startswith("checked 8 lines: 2 done and deleted") and "*partly done*" in text
      and "*could not check*" in text and "- a note" in text)
check("the report has no em dash", "—" not in text)

# -- the session's reply ----------------------------------------------------------
good = 'thinking\n```json\n{"verdicts": [{"ref": "A", "verdict": "OPEN"}]}\n```'
check("the verdicts object is read from a fenced reply", oc.parse(good)["verdicts"][0]["ref"] == "A")
check("bare JSON is read", oc.parse('{"verdicts": []}') == {"verdicts": []})
check("anything else is no verdict", oc.parse("all done") is None and oc.parse("") is None)

# -- the run: skips, rate limit, no session ---------------------------------------
posts: list[tuple] = []
oc.post = lambda *a, **k: posts.append(a)
oc.begin_run = lambda: ("", True)
oc.xero_budget = lambda: (120, "OpCo US")
oc.REGISTER.write_text(json.dumps([item(1, key="INV-9001")]))
args = type("A", (), dict(domain=None, dry_run=False, no_session=False))()
check("under the Xero floor the day is skipped and the post says why",
      oc.cmd_run(args) == 0 and len(posts) == 1 and "120 Xero calls left" in posts[0][1]
      and "800" in posts[0][1], str(posts))
oc.xero_budget = lambda: (5000, "OpCo US")
SETTINGS = {"enabled": True, "batch_size": 25, "min_xero_calls": 6000, "checker_model": "m",
            "light_checker_model": "haiku", "trivial_amount": 0.0}
oc.config.outstanding_check = lambda: SETTINGS
posts.clear()
oc.cmd_run(args)
check("the floor is the configured one", len(posts) == 1 and "6000" in posts[0][1])
SETTINGS = {**SETTINGS, "enabled": False}
posts.clear()
oc.cmd_run(args)
check("disabled in config: nothing is done", posts == [])
SETTINGS = {**SETTINGS, "enabled": True, "min_xero_calls": 800}
sessions: list[tuple] = []
oc.run_session = lambda *a: sessions.append(a) or ({"verdicts": []}, "", 0.0)
oc.done_replies = lambda rows: {}
oc.RECONCILE.write_text(json.dumps({"built": "2026-01-01", "items": []}))
oc.REGISTER.write_text(json.dumps([item(1, key="INV-9001", text="reconcile the bill to intercompany (2150)",
                                        refs=R)]))
oc.python_first = lambda rows, client=None: [{"ref": "INV-9001", "verdict": "CLOSED", "definite": True,
                                              "evidence": "PAID"}]
calls.clear()
posts.clear()
oc.cmd_run(args)
check("when Python leaves nothing no session starts",
      sessions == [] and len(posts) == 1 and [c[2] for c in calls if c[0] == "close"] == ["INV-9001"])
oc.python_first = lambda rows, client=None: []
oc.REGISTER.write_text(json.dumps([item(1, key="INV-9001"), item(2, key="rpt-check:x")]))
posts.clear()
calls.clear()
oc.cmd_run(type("A", (), dict(domain=None, dry_run=False, no_session=True))())
check("--no-session starts no session and posts nothing", sessions == [] and posts == [] and calls == [])
oc.cmd_run(args)
check("what Python leaves goes to one session, owned lines excluded",
      len(sessions) == 1 and sessions[0][1] == 1)

# -- batches carry their tier, the prompt names each tier's model -------------------
mixed = [{"ref": f"D{i}", "closable": True, **item(i, kind="documents", key=f"D{i}")} for i in range(2)]
mixed += [{"ref": f"W{i}", "closable": True, **item(i, domain="reports", kind="watch", key=f"W{i}")} for i in range(1)]
mixed += [{"ref": f"M{i}", "closable": True, **item(i, kind="manual", key=f"M{i}")} for i in range(2)]
mixed += [{"ref": f"B{i}", "closable": True, **item(i, kind="blocked", key=f"B{i}")} for i in range(1)]
oc.write_batches(mixed, TMP / "tiers")
names = sorted(f.name for f in (TMP / "tiers").glob("*.json"))
check("documents and watches go in -light- batches, the rest in -full-",
      sorted(n.split("-", 1)[1] for n in names) == ["full-bookkeeping-blocked.json", "full-bookkeeping-manual.json",
                                                    "light-bookkeeping-documents.json", "light-reports-watch.json"],
      str(names))
check("the Agent tool's model is found in a configured name",
      oc.agent_model("claude-haiku-4-5") == "haiku" and oc.agent_model("Sonnet") == "sonnet"
      and oc.agent_model("house-model-1") is None)
ml = oc.models_line({"light_checker_model": "claude-haiku-4-5", "checker_model": "claude-sonnet-5-5"})
check("the prompt sends light batches and full batches each with its model",
      'model "haiku" for a file whose name has -light-' in ml and 'model "sonnet" for a file whose name has -full-' in ml)
check("a model with no Agent tool name goes with no override",
      "no model override for a file whose name has -light-" in oc.models_line(
          {"light_checker_model": "house-model-1", "checker_model": "sonnet"}))

# -- the trivial threshold in the register ------------------------------------------
os.environ["OUTSTANDING_NO_PUBLISH"] = "1"
import contextlib  # noqa: E402
import io  # noqa: E402
import outstanding as register  # noqa: E402
import review_checks  # noqa: E402

register.STORE, register.RENDERED, register.LOCK = TMP / "reg.json", TMP / "REG.md", TMP / ".reg.lock"


def reg(*argv: str) -> str:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        register.main(list(argv))
    return buf.getvalue()


def stored() -> list[dict]:
    return json.loads(register.STORE.read_text())


def add(key: str, text: str, refs: str = "", *extra: str, domain: str = "bookkeeping", kind: str = "manual") -> str:
    return reg("add", "--domain", domain, "--kind", kind, "--with", "admin", "--key", key,
               "--text", text, "--refs", refs, "--defer", *extra)


review_checks.INTERCO_LATEST = TMP / "interco-latest.json"
review_checks.INTERCO_LATEST.write_text(json.dumps({"rates": {"EUR": 1.1, "GBP": 1.25}}))
register._RATES = None
rt = register.rates()
check("rates come from the last intercompany run, the reporting currency at 1",
      rt.get("EUR") == 1.1 and rt.get("GBP") == 1.25 and rt.get("USD") == 1.0, str(rt))
check("no built-in rate table: a currency nobody rated has no rate", "JPY" not in rt and "CHF" not in rt)

SMALL = "Westwind Stationery · 14 Feb · EUR 18.40 · paid OpCo EU · recognised OpCo EU · WS-2207"
SETTINGS = {**SETTINGS, "trivial_amount": 0.0}
check("the threshold is off by default", oc.config.outstanding_check()["trivial_amount"] == 0.0
      and not register.trivial({"domain": "bookkeeping", "text": "recode the bill", "refs": SMALL}))
add("WS-2207", "recode the bill to stationery", SMALL)
check("with the threshold off a small line is registered", [i["key"] for i in stored()] == ["WS-2207"])
out = reg("prune-trivial", "--json", "--defer")
check("with the threshold off prune-trivial deletes nothing", json.loads(out.splitlines()[-1]) == []
      and len(stored()) == 1)
check("with the threshold off the agents are told TRIVIAL is off", "TRIVIAL is off" in oc.trivial_line(SETTINGS))

SETTINGS = {**SETTINGS, "trivial_amount": 60.0}
check("a line is sized from its own amounts at the run's rates",
      abs(register.size({"text": "x", "refs": SMALL}) - 20.24) < 0.01)
check("a currency with no rate leaves the size unknown, so never trivial",
      register.size({"text": "void the copy", "refs": "Kestrel Ferries · CHF 9.00"}) is None
      and not register.trivial({"domain": "bookkeeping", "text": "void the copy", "refs": "Kestrel Ferries · CHF 9.00"}))
check("a line counting more payments than it names amounts has no size",
      register.size({"text": "attach receipts for 14 card payments, none over EUR 12.00"}) is None)
check("a line naming no amount has no size", register.size({"text": "retype the account"}) is None)
check("only bookkeeping lines are trivial",
      not register.trivial({"domain": "reports", "text": "x", "refs": SMALL})
      and register.trivial({"domain": "bookkeeping", "text": "x", "refs": SMALL}))

out = add("WS-2207", "recode the bill to stationery", SMALL)
check("add refuses a small bookkeeping line and deletes the open one it would bump",
      out.startswith("not registered: a bookkeeping line under USD 60.00") and "deleted" in out and stored() == [],
      out)
out = add("WS-2208", "recode the bill to stationery", SMALL.replace("WS-2207", "WS-2208"))
check("add refuses a new small bookkeeping line", "not registered" in out and stored() == [])
add("WS-2208", "recode the bill to stationery", SMALL.replace("WS-2207", "WS-2208"), "--keep")
check("--keep registers it all the same, marked kept",
      [(i["key"], i.get("keep")) for i in stored()] == [("WS-2208", True)])
add("WS-2208", "recode the bill to stationery", SMALL.replace("WS-2207", "WS-2208"))
check("a kept line is bumped, never refused", stored()[0].get("runs") == 2)
add("RP-77", "explain the variance", "EUR 4.00", domain="reports", kind="queried")
add("KF-310", "void the copy", "Kestrel Ferries · CHF 9.00")
add("BIG-1", "recode the bill", "Harbour Lighting · EUR 900.00")
with open(register.STORE) as fh:
    reg_items = json.load(fh)
reg_items.append({"id": "OI-0090", "domain": "bookkeeping", "kind": "documents", "with": "admin",
                  "raised": "2026-01-02", "last": "2026-01-02", "runs": 1, "key": "TQ-5",
                  "text": "chase the receipt; the bank shows the charge", "refs": "Tern Quay Parking · GBP 6.40"})
register.STORE.write_text(json.dumps(reg_items))
out = reg("prune-trivial", "--dry-run", "--json", "--defer")
gone = json.loads(out.splitlines()[-1])
check("prune-trivial --dry-run --json names the small line and deletes nothing",
      [g["key"] for g in gone] == ["TQ-5"] and gone[0]["currency"] == "USD" and gone[0]["amount"] == 8.0
      and len(stored()) == 5, out)
out = reg("prune-trivial", "--dry-run", "--defer")
check("prune-trivial --dry-run in words says would delete", "would delete OI-0090" in out
      and "1 lines under USD 60.00" in out, out)
reg("prune-trivial", "--json", "--defer")
check("prune-trivial deletes only small bookkeeping lines: kept, other domains, unrated and large stay",
      sorted(i["key"] for i in stored()) == ["BIG-1", "KF-310", "RP-77", "WS-2208"], str(stored()))
tl = oc.trivial_line(SETTINGS)
check("with the threshold on the agents get it and the only rates to use",
      "under USD 60.00" in tl and "EUR 1.1" in tl and "GBP 1.25" in tl and "JPY" not in tl, tl)

# -- the close fix: an id that repeats across domains -------------------------------
twin = [{"id": "OI-0500", "domain": "bookkeeping", "kind": "manual", "with": "admin", "raised": "2026-01-02",
         "last": "2026-01-02", "runs": 1, "key": "TW-1", "text": "a", "refs": ""},
        {"id": "OI-0500", "domain": "reports", "kind": "manual", "with": "admin", "raised": "2026-01-02",
         "last": "2026-01-02", "runs": 1, "key": "TW-2", "text": "b", "refs": ""}]
register.STORE.write_text(json.dumps(twin))
reg("close", "--key", "TW-1", "--defer")
check("close deletes the item itself, not every item with its id", [i["key"] for i in stored()] == ["TW-2"])

# -- NOT_NEEDED and TRIVIAL verdicts -------------------------------------------------
calls.clear()
nrows = [{"ref": r, "closable": True, **item(i, domain=d, kind=k, key=r)} for i, (r, d, k) in enumerate(
    [("N1", "bookkeeping", "manual"), ("N2", "bookkeeping", "queried"), ("N3", "reports", "manual"),
     ("N4", "bookkeeping", "manual"), ("T1", "bookkeeping", "documents"), ("T2", "reports", "watch"),
     ("T3", "bookkeeping", "queried")], 1)]
nverdicts = [
    {"ref": "N1", "verdict": "NOT_NEEDED", "definite": True, "evidence": "the copy is already voided"},
    {"ref": "N2", "verdict": "NOT_NEEDED", "definite": True, "evidence": "q"},
    {"ref": "N3", "verdict": "NOT_NEEDED", "definite": True, "evidence": "r"},
    {"ref": "N4", "verdict": "NOT_NEEDED", "definite": False, "evidence": "maybe"},
    {"ref": "T1", "verdict": "TRIVIAL", "definite": True, "evidence": "receipt for EUR 7.20"},
    {"ref": "T2", "verdict": "TRIVIAL", "definite": True, "evidence": "w"},
    {"ref": "T3", "verdict": "TRIVIAL", "definite": True, "evidence": "bill for GBP 11.00"},
]
nd = oc.apply(nrows, nverdicts, "2026-01-02", dry_run=False, trivial_on=True)
check("NOT_NEEDED closes only a definite bookkeeping manual line",
      [r["ref"] for r, _ in nd["not_needed"]] == ["N1"], str(nd["not_needed"]))
check("TRIVIAL closes any definite bookkeeping line", [r["ref"] for r, _ in nd["trivial"]] == ["T1", "T3"])
check("the rest stay open", nd["open"] == 4 and not nd["closed"])
check("each is closed by key with its reason, and the Word copy republished once",
      [c[2] for c in calls if c[0] == "close"] == ["N1", "T1", "T3"]
      and any("not needed, the copy is already voided" in x for c in calls for x in c)
      and [c for c in calls if c[0] == "publish"] == [("publish",)])
calls.clear()
off = oc.apply(nrows, nverdicts, "2026-01-02", dry_run=False, trivial_on=False)
check("with the threshold off TRIVIAL closes nothing", off["trivial"] == [] and off["open"] == 6)

# -- the grouped post ----------------------------------------------------------------
grows = [{"ref": f"G{i}", "closable": True, **item(i, key=f"G{i}")} for i in range(1, 5)]
gdone = {"closed": [], "kept": [], "partly": [], "open": 0, "missing": [], "not_needed": [], "trivial": [],
         "unverified": [(grows[0], "the expense claims cannot be read."), (grows[1], "the expense claims cannot be read"),
                        (grows[2], "the expense claims cannot be read"), (grows[3], "the bank API refused")]}
gtext = oc.report(gdone, 4, threshold="USD 60.00")
check("three lines or more with one reason are said once with a count and an id range",
      "- the expense claims cannot be read · 3 bookkeeping lines, BO-001 to BO-003" in gtext
      and gtext.count("expense claims") == 1, gtext)
check("fewer than three are each said", "- the bank API refused · BO-004" in gtext)
gdone = {**gdone, "unverified": [], "not_needed": [(grows[0], "the copy is already voided")],
         "trivial": [(grows[1], "chase the receipt, USD 8.12 in all")]}
gtext = oc.report(gdone, 2, threshold="USD 60.00")
check("not needed and under the threshold each have their section",
      "*not needed, deleted*" in gtext and "*under USD 60.00, deleted*" in gtext
      and "2 not needed or under USD 60.00 and deleted" in gtext, gtext)
check("a trivial line is named by its first clause",
      oc.short("chase the receipt; the bank shows the charge") == "chase the receipt")

# -- the run prunes before any agent --------------------------------------------------
PRUNED = [{"id": "OI-0090", "key": "TQ-5", "kind": "documents", "text": "chase the receipt; the bank shows it",
           "refs": "", "currency": "USD", "amount": 8.12}]
oc.outstanding = lambda *a: calls.append(a) or (json.dumps(PRUNED) if a[0] == "prune-trivial" else "")
oc.REGISTER.write_text(json.dumps([item(1, key="INV-9001")]))
calls.clear()
posts.clear()
sessions.clear()
oc.cmd_run(args)
prune_calls = [c for c in calls if c[0] == "prune-trivial"]
check("the run prunes with the register's own command before the session",
      prune_calls == [("prune-trivial", "--json", "--defer")] and len(sessions) == 1)
check("the post reports the pruned line under the threshold",
      len(posts) == 1 and "*under USD 60.00, deleted*" in posts[0][1]
      and "chase the receipt, USD 8.12 in all · OI-0090" in posts[0][1], str(posts))
calls.clear()
oc.cmd_run(type("A", (), dict(domain=None, dry_run=False, no_session=True))())
check("--no-session only asks what would go", [c for c in calls if c[0] == "prune-trivial"]
      == [("prune-trivial", "--json", "--defer", "--dry-run")])
SETTINGS = {**SETTINGS, "trivial_amount": 0.0}
calls.clear()
oc.cmd_run(args)
check("with the threshold off the run does not prune", not [c for c in calls if c[0] == "prune-trivial"])

# -- the extra cost file ------------------------------------------------------------
p = oc.write_extra_cost("job-1", 1.23456)
d = json.loads(p.read_text())
check("the session cost is an extra cost file the bracket reads",
      p == oc.RUNS / "job-1" / "extra" / "outstanding-check.json"
      and d == {"label": "check", "cost_usd": 1.2346, "running": False})
import run_cost  # noqa: E402
costs, running = run_cost.extra_costs("job-1", oc.RUNS)
check("run_cost reads it", costs == {"check": 1.2346} and running == 0, str(costs))

print()
print("PASS" if not FAILS else f"FAIL ({len(FAILS)})")
sys.exit(1 if FAILS else 0)
