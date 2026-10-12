#!/usr/bin/env python
"""Offline checks for the balance-report engine (prepayments, accruals, deposits).

No Xero, no Slack, no Drive, no model: a throwaway config and data folder, a
temporary outstanding-items register, a fake Xero client and a fake `claude`
command line. Covers date parsing, attribution, the Checks wording, the
quarter-end copies, the register mirror and the period reader.

    .venv/bin/python scripts/test_balance_report.py
"""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

TMP = Path(tempfile.mkdtemp())
text = (ROOT / "config" / "group.example.toml").read_text()
text = text.replace("[reports]\n", '[reports]\nfrom_year = 2025\nquarter_end_from = "2026-03"\n', 1)
(TMP / "group.toml").write_text(text)
os.environ["AGENT_GROUP_CONFIG"] = str(TMP / "group.toml")
os.environ["OUTSTANDING_NO_PUBLISH"] = "1"
os.environ.pop("AGENT_READONLY", None)

import balance_report as br  # noqa: E402
import outstanding  # noqa: E402
from accounting_agent import config  # noqa: E402

br.OUT_DIR = TMP / "reports"
outstanding.STORE = TMP / "outstanding.json"
outstanding.RENDERED = TMP / "OUTSTANDING.md"
outstanding.LOCK = TMP / ".outstanding.lock"
outstanding.DOCX = TMP / "Outstanding Items.docx"

FAILS: list[str] = []


def check(cond: bool, what: str) -> None:
    print(("ok    " if cond else "FAIL  ") + what)
    if not cond:
        FAILS.append(what)


ENT = config.entity_keys()[0]
CODE = "1200"


def empty_store(spec) -> dict:
    return {"report": spec.key, "version": 1, "accounts": {ENT: {"codes": [CODE], "base": "USD"}},
            "synced": {}, "rows": {}, "moves": {}, "xero": {}, "notes": {}}


def add_row(store, key, date, supplier, amount, desc="", number="", period=None, **extra):
    row = {"entity": ENT, "code": CODE, "account": CODE, "date": date, "supplier": supplier,
           "source": "MJ", "doc_id": key, "number": number, "currency": "USD",
           "doc_amount": amount, "amount": amount, "description": desc,
           "period_start": period[0] if period else None,
           "period_end": period[1] if period else None}
    row.update(extra)
    store["rows"][key] = row


def add_move(store, key, month, amount, narration, row=None):
    store["moves"][key] = {"entity": ENT, "code": CODE, "date": f"{month}-28", "month": month,
                           "amount": amount, "narration": narration, "row": row, "weak": False,
                           "contra": "", "source": "MJ", "doc_id": key}


# -- date parsing -------------------------------------------------------------------

check(br.parse_period("Cover 29 Sep 2026 to 24 Dec 2026, release over Oct, Nov and Dec 2026")
      == ("2026-10", "2026-12"), "a release over named months wins over the service dates beside it")
check(br.parse_period("released over Nov 25 - Jan 26") == ("2025-11", "2026-01"),
      "a release range reads as written")
check(br.parse_period("Stay 18 Aug to 31 Aug 2026") == ("2026-08", "2026-08"),
      "a range with no year on its start takes the end's year")
check(br.parse_period("Cover 1 Dec to 28 Feb 2027") == ("2026-12", "2027-02"),
      "a year-less start after the end falls in the year before")
check(br.parse_period("Annual licence Jan 26 - Dec 26") == ("2026-01", "2026-12"),
      "the ordinary month range still reads")
check(br.parse_period("licence, no dates here") is None, "no period in the line is None")

# -- attribution: prepayments and deposits go to a row raised in the month or earlier ------

PRE, DEP, ACC = br.PREPAYMENTS, br.DEPOSITS, br.ACCRUALS
s = empty_store(PRE)
add_row(s, "old", "2025-01-10", "Acme Hosting", 1200, "hosting Jan 25 - Dec 25", period=("2025-01", "2025-12"))
add_row(s, "new", "2026-09-10", "Acme Hosting", 1200, "hosting Sep 26 - Aug 27", period=("2026-09", "2027-08"))
add_move(s, "m1", "2025-06", 100, "Acme Hosting release")
br.attribute(PRE, ENT, s)
check(s["moves"]["m1"]["row"] == "old", "a release goes to the row raised before it, not one raised after")
check(not s["moves"]["m1"]["weak"], "a clear match is not marked weak")

s = empty_store(PRE)
add_row(s, "a", "2025-01-10", "Acme Hosting", 1200, "hosting", period=("2025-01", "2025-12"))
add_row(s, "b", "2025-01-12", "Acme Hosting", 1200, "hosting", period=("2026-01", "2026-12"))
add_move(s, "m1", "2026-03", 100, "Acme Hosting release")
br.attribute(PRE, ENT, s)
check(s["moves"]["m1"]["row"] == "b", "of two earlier rows, the one whose period covers the month wins")

s = empty_store(PRE)
add_row(s, "late", "2026-09-10", "Acme Hosting", 1200, "hosting", period=("2026-09", "2027-08"))
add_move(s, "m1", "2026-02", 100, "Acme Hosting release")
br.attribute(PRE, ENT, s)
check(s["moves"]["m1"]["row"] == "late", "with no earlier row, every row is a candidate again")

s = empty_store(PRE)
add_row(s, "old", "2025-01-10", "Acme Hosting", 1200, "hosting", number="INV-20001")
add_row(s, "new", "2026-09-10", "Acme Hosting", 1200, "hosting", number="INV-20002")
add_move(s, "m1", "2025-06", 100, "Acme Hosting release INV-20002")
br.attribute(PRE, ENT, s)
check(s["moves"]["m1"]["row"] == "new", "an invoice number in the narration wins outright")

s = empty_store(ACC)
add_row(s, "bill", "2026-09-10", "Acme Hosting", 500, "accrual")
add_move(s, "m1", "2026-08", 500, "Acme Hosting reversal")
br.attribute(ACC, ENT, s)
check(s["moves"]["m1"]["row"] == "bill", "accruals may take a row raised after the reversal")

s = empty_store(PRE)
add_row(s, "x", "2025-01-10", "Acme Hosting", 1200, "hosting", period=("2025-01", "2025-12"))
add_row(s, "y", "2025-02-10", "Acme Hosting", 900, "hosting", period=("2025-01", "2025-12"))
add_move(s, "m1", "2025-06", 100, "Acme Hosting release")
br.attribute(PRE, ENT, s)
check(not s["moves"]["m1"]["weak"] or s["moves"]["m1"]["row"] is not None, "a weak guess still lands on a row")
check(all(c[1] != "releases matched on balance" for c in br.collect_checks(PRE, s)),
      "a weak guess raises no check")

# -- checks: only what is outstanding, one line for a missing release on an expired row ----

br.AS_AT = "2026-08"          # the clock reads 2026-09
try:
    s = empty_store(PRE)
    # period ended Jun 26, Jan to Apr released, 200 still held
    add_row(s, "gap", "2026-01-05", "Acme Hosting", 600, "hosting", period=("2026-01", "2026-06"))
    for i, m in enumerate(("2026-01", "2026-02", "2026-03", "2026-04")):
        add_move(s, f"g{i}", m, 100, "Acme Hosting release", row="gap")
    # every month released but 50 still held
    add_row(s, "short", "2026-01-05", "Beta Cloud", 650, "cloud", period=("2026-01", "2026-06"))
    for i, m in enumerate(("2026-01", "2026-02", "2026-03", "2026-04", "2026-05", "2026-06")):
        add_move(s, f"s{i}", m, 100, "Beta Cloud release", row="short")
    # no period, balance held, nothing read
    add_row(s, "np1", "2026-05-05", "Gamma Print", 300, "print")
    add_row(s, "np2", "2026-05-06", "Delta Print", 300, "print", period_read={"on": "x", "files": 0, "found": False})
    add_row(s, "np3", "2026-05-07", "Epsilon Print", 300, "print", period_read={"on": "x", "files": 2, "found": False})
    # no period and released in full: nothing to say
    add_row(s, "np4", "2026-05-08", "Zeta Print", 300, "print")
    add_move(s, "z0", "2026-06", 300, "Zeta Print release", row="np4")
    # a weak match and decisions are not checks
    add_row(s, "wk", "2026-02-01", "Eta Rent", 400, "rent", period=("2026-02", "2027-01"))
    add_move(s, "w0", "2026-02", 100, "Eta Rent", row="wk")
    s["moves"]["w0"]["weak"] = True
    s["notes"] = {"n1": {"entity": ENT, "kind": "accepted", "detail": "nothing to do", "as_of": "2026-01"},
                  "n2": {"entity": ENT, "kind": "refund due", "detail": "waiting on the landlord", "as_of": "2026-01"}}
    s["rows"]["np4"]["cleared"] = "cleared by hand"
    checks = br.keyed_checks(PRE, s)
    kinds = {}
    for c in checks:
        kinds.setdefault(c["kind"], []).append(c)
    gap = [c for c in checks if c["key"] == "gap:release missing"]
    check(len(gap) == 1 and "May 26, Jun 26" in gap[0]["detail"]
          and "period ended Jun 26 with 200.00 still held" in gap[0]["detail"],
          "a missing release that leaves an expired balance is one line")
    check(not any(c["key"] == "gap:expired with a balance" for c in checks),
          "expired with a balance is not raised beside a missing release")
    check(any(c["key"] == "short:expired with a balance" for c in checks)
          and not any(c["key"] == "short:release missing" for c in checks),
          "expired with a balance when no release is missing")
    pn = {c["key"]: c["detail"] for c in checks if c["kind"] == "period not stated"}
    check("invoice not read yet" in pn.get("np1:period not stated", ""), "period not stated: invoice not read yet")
    check("no invoice attached" in pn.get("np2:period not stated", ""), "period not stated: no invoice attached")
    check("2 documents" in pn.get("np3:period not stated", ""), "period not stated: N documents")
    check("np4:period not stated" not in pn, "no period-not-stated line once nothing is held")
    check(not any(c["kind"] in ("accepted", "cleared by hand", "cleared to nil")
                  or "matched on balance" in c["kind"] for c in checks),
          "no informational line on the Checks")
    check([c["key"] for c in checks if c["note"]] == ["rpt-note:prepayments:n2"],
          "only an open note is a check; a decision note is not")
finally:
    br.AS_AT = None

s = empty_store(ACC)
add_row(s, "old", "2026-01-05", "Theta Services", 100, "accrual")
add_row(s, "paid", "2026-01-05", "Iota Services", 100, "accrual")
add_move(s, "p", "2026-02", 100, "Iota Services reversal", row="paid")
br.AS_AT = "2026-08"
try:
    c = br.keyed_checks(ACC, s)
finally:
    br.AS_AT = None
check([x["key"] for x in c] == ["old:open a long time"], "open a long time only while the row holds a balance")

# -- quarter-end copies ------------------------------------------------------------

check(br.first_quarter_end() == "2026-03", "the first quarter end on or after the configured month")
check(br.quarter_path(PRE, "2026-09").name == "Example_Group_Prepayments_Q3_2026.xlsx",
      "quarter file named from the company name, financial quarter and year")
qs = br.quarter_ends(PRE)
check(qs[:1] == ["2026-03"] and all(b == br.month_add(a, 3) for a, b in zip(qs, qs[1:])),
      "every quarter end since the first is built when none exists")
for q in qs:
    p = br.quarter_path(PRE, q)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("x")
later = br.quarter_ends(PRE)
check(len(later) <= 1 and all(br.month_add(q, 3) >= f"{dt.date.today():%Y-%m}" for q in later),
      "a quarter whose next quarter end has passed is left as it was built")
for q in qs:
    br.quarter_path(PRE, q).unlink()

s = empty_store(PRE)
add_row(s, "early", "2026-02-01", "Acme Hosting", 600, "hosting", period=("2026-02", "2026-07"))
add_row(s, "after", "2026-05-01", "Beta Cloud", 600, "cloud", period=("2026-05", "2026-10"))
add_move(s, "e1", "2026-03", 100, "Acme Hosting release", row="early")
add_move(s, "e2", "2026-05", 100, "Acme Hosting release", row="early")
add_move(s, "a1", "2026-05", 100, "Beta Cloud release", row="after")
s["xero"] = {ENT: {"2026-03": 500.0, "2026-05": 800.0}}
s["notes"] = {"n1": {"entity": ENT, "kind": "refund due", "detail": "later", "as_of": "2026-05"}}
at = br.as_at_store(s, "2026-03")
check(set(at["rows"]) == {"early"} and set(at["moves"]) == {"e1"}, "as at a quarter end: later rows and movements removed")
check(at["xero"][ENT] == {"2026-03": 500.0} and at["notes"] == {}, "as at a quarter end: later balances and notes removed")
check(set(s["rows"]) == {"early", "after"}, "the live store is not touched")
br.AS_AT = "2026-03"
try:
    check(br.this_month() == "2026-04" and br.last_closed_month() == "2026-03",
          "this month is the month after the quarter end")
finally:
    br.AS_AT = None

# -- the register mirror -----------------------------------------------------------

outstanding.STORE.write_text("[]\n")
outstanding.RENDERED.write_text("")
br.AS_AT = "2026-08"
try:
    s = empty_store(PRE)
    add_row(s, "gap", "2026-01-05", "Acme Hosting", 600, "hosting", number="INV-30001",
            period=("2026-01", "2026-06"))
    add_row(s, "np1", "2026-05-05", "Gamma Print", 300, "print")
    add_row(s, "dup", "2026-01-05", "Beta Cloud", 600, "cloud", number="INV-40001", period=("2026-01", "2026-06"))
    checks = br.keyed_checks(PRE, s)
finally:
    br.AS_AT = None
reg_items = [{"id": "OI-0001", "domain": "bookkeeping", "kind": "manual", "with": "admin",
              "raised": "2026-09-01", "last": "2026-09-01", "runs": 1, "key": "bill-fix",
              "text": "attach the corrected document", "refs": "INV-40001, Beta Cloud"},
             {"id": "OI-0002", "domain": "bookkeeping", "kind": "documents", "with": "supplier",
              "raised": "2026-09-01", "last": "2026-09-01", "runs": 1, "key": "chase INV-30001",
              "text": "chased for the invoice", "refs": "INV-30001"}]
outstanding.save(list(reg_items))
ids = br.sync_register(PRE, checks)
items = outstanding.load()
mine = [i for i in items if i["key"].startswith(br.CHECK_PREFIX)]
check(ids["dup:release missing"] == "OI-0001" and not any("dup" in i["key"] for i in mine),
      "a check folds into another run's manual line holding the same invoice number")
check(any(i["key"] == "rpt-check:prepayments:gap:release missing" for i in mine),
      "a chase for the document does not absorb the check")
check(all(i["domain"] == "reports" for i in mine), "register lines are in the reports domain")
kinds = {i["key"].split(":", 2)[2]: i["kind"] for i in mine}
check(kinds["gap:release missing"] == "manual" and kinds["np1:period not stated"] == "manual",
      "a missing release and an unstated period are manual lines")
check(len(mine) == 2 and set(ids) == {c["key"] for c in checks}, "every check has a register id for its Checks column")
n = len(outstanding.load())
br.sync_register(PRE, checks)
check(len(outstanding.load()) == n, "a second run adds nothing")
br.sync_register(PRE, [c for c in checks if c["key"] != "np1:period not stated"])
check(not any("np1" in i["key"] for i in outstanding.load()), "a check no longer raised leaves the register")
check(any(i["id"] == "OI-0001" for i in outstanding.load()), "another run's line is never closed by this one")

s = empty_store(PRE)
add_row(s, "r1", "2026-01-05", "Acme Hosting", 600, "hosting")
s["notes"] = {"n1": {"entity": ENT, "kind": "refund due", "detail": "waiting on the landlord", "as_of": "2026-01"},
              "n2": {"entity": ENT, "kind": "accepted", "detail": "ledger is right", "as_of": "2026-01"}}
check(br.sync_notes(PRE, s) is True, "an open note is tied to a register line")
check(s["notes"]["n1"]["register_key"] == "rpt-note:prepayments:n1" and "register_key" not in s["notes"]["n2"],
      "only an open note gets a register key")
w = [i for i in outstanding.load() if i["key"] == "rpt-note:prepayments:n1"]
check(len(w) == 1 and w[0]["kind"] == "watch" and w[0]["domain"] == "reports", "the note is a watch line")
outstanding.save([i for i in outstanding.load() if i["key"] != "rpt-note:prepayments:n1"])
check(br.sync_notes(PRE, s) is True and "n1" not in s["notes"] and "n2" in s["notes"],
      "a note is dropped once its register line is closed")

os.environ["AGENT_READONLY"] = "1"
try:
    before = outstanding.STORE.read_text()
    check(br.sync_register(PRE, checks) == {} and br.sync_notes(PRE, s) is False
          and outstanding.STORE.read_text() == before, "a read-only session writes nothing to the register")
finally:
    del os.environ["AGENT_READONLY"]

# -- the whole build: live copy, quarter copy, Checks register column, summary ---------

outstanding.STORE.write_text("[]\n")
spec = br.PREPAYMENTS
s = empty_store(spec)
add_row(s, "gap", "2026-01-05", "Acme Hosting", 600, "hosting", number="INV-30001", period=("2026-01", "2026-06"))
for i, m in enumerate(("2026-01", "2026-02", "2026-03")):
    add_move(s, f"g{i}", m, 100, "Acme Hosting release", row="gap")
s["xero"] = {ENT: {"2026-03": 300.0}}
br.save_store(spec, s)
path, live = br.build_all(spec, br.load_store(spec), False)
from openpyxl import load_workbook  # noqa: E402

check(path.exists() and (br.OUT_DIR / "quarter-end").is_dir(), "the live workbook and the quarter-end folder are written")
q1 = br.quarter_path(spec, "2026-03")
check(q1.exists(), "the first quarter-end copy is written")
wb = load_workbook(q1)
check("as at 31 Mar 2026" in str(wb[spec.title]["A1"].value), "the quarter copy says when it is as at")
check(wb["Checks"]["D4"].value == "Register", "the Checks sheet has a Register column")
store = br.load_store(spec)
check(store["quarters"]["2026-03"]["file"] == q1.name, "the store records the quarter copy built")
head = br.summary_sections(spec, store)[0]
latest = sorted(store["quarters"])[-1]
check(f"as at {br.quarter_label(latest)}:" in head and "to look at" in head,
      "the summary names the latest quarter copy")
check(not any("recorded decisions" in x for x in br.summary_lines(spec, store)), "the summary carries no decisions count")

# the Drive stamp survives the save after a publish
stamp = {"at": "2026-01-01T00:00:00+00:00", "line": "x"}
orig = br.publish


def fake_publish(spec_, path_):
    st = br.load_store(spec_)
    st["published"] = stamp
    br.save_store(spec_, st)
    return "published"


br.publish = fake_publish
try:
    br.build_all(spec, br.load_store(spec), True)
finally:
    br.publish = orig
check(br.load_store(spec).get("published") == stamp, "the publish stamp is kept when the build saves the store")

# -- the period reader, with a fake client and a fake command line ------------------------


class FakeClient:
    def __init__(self):
        self.calls = []

    def get(self, path, **params):
        self.calls.append((path, params))
        return {"Invoices": [], "Contacts": []}

    def list_attachments(self, endpoint, guid):
        self.calls.append(("attachments", endpoint, guid))
        return [{"FileName": "invoice one.pdf"}] if endpoint == "ManualJournals" else []

    def download_attachment(self, endpoint, guid, att):
        return b"%PDF fake"


RUNS: list[list[str]] = []
REPLY = {"result": "", "total_cost_usd": 0.01}


def fake_run(cmd, input=None, **kw):
    RUNS.append(cmd)
    return subprocess.CompletedProcess(cmd, REPLY.get("rc", 0), json.dumps(REPLY), "")


real_run = subprocess.run
br.subprocess.run = fake_run
try:
    s = empty_store(PRE)
    add_row(s, "ok", "2026-05-05", "Acme Hosting", 600, "hosting", doc_id="guid-ok")
    add_row(s, "guess", "2026-05-06", "Beta Cloud", 600, "cloud", doc_id="guid-guess")
    add_row(s, "long", "2026-05-07", "Gamma Print", 600, "print", doc_id="guid-long")
    add_row(s, "fixed", "2026-05-08", "Delta Print", 600, "print", doc_id="guid-fixed", fixed={"period": True})
    REPLY["result"] = "Here you go:\n```json\n" + json.dumps([
        {"key": "ok", "from": "2026-06", "to": "2027-05", "source": "document",
         "quote": "Licence period 1 Jun 2026 to 31 May 2027"},
        {"key": "guess", "from": "2026-06", "to": "2026-11", "source": "document",
         "quote": "paid in full on receipt"},
        {"key": "long", "from": "2026-01", "to": "2036-01", "source": "document",
         "quote": "term Jan 2026 to Jan 2036"},
    ]) + "\n```"
    client = FakeClient()
    br.read_periods(PRE, ENT, s, client=client)
    check(len(RUNS) == 1 and "haiku" in RUNS[0] and "low" in RUNS[0] and "--max-budget-usd" in RUNS[0]
          and RUNS[0][RUNS[0].index("--tools") + 1] == "Read", "one headless call, small model, low effort, Read only")
    check(s["rows"]["ok"]["period_start"] == "2026-06" and s["rows"]["ok"]["period_end"] == "2027-05"
          and s["rows"]["ok"]["period_read"]["found"] is True and s["rows"]["ok"]["period_read"]["files"] == 1,
          "a period is accepted when the quoted words carry a date")
    check(not s["rows"]["guess"].get("period_start") and s["rows"]["guess"]["period_read"]["found"] is False
          and "paid in full" in s["rows"]["guess"]["period_read"]["why"],
          "a period whose quote carries no month or date is refused")
    check(not s["rows"]["long"].get("period_start"), "a period beyond the month cap is refused")
    check("period_read" not in s["rows"]["fixed"], "a row with a period set by hand is not read")
    n = len(RUNS)
    br.read_periods(PRE, ENT, s, client=client)
    check(len(RUNS) == n, "a row already read is not read again")
    br.read_periods(PRE, ENT, s, client=client, again=True)
    check(len(RUNS) == n + 1, "`again` reads the unread ones a second time")

    REPLY["rc"] = 1
    s2 = empty_store(PRE)
    add_row(s2, "k", "2026-05-05", "Acme Hosting", 600, "hosting", doc_id="guid-k")
    br.read_periods(PRE, ENT, s2, client=FakeClient())
    check("period_read" not in s2["rows"]["k"], "a failed call leaves the row to be read next time")
    REPLY.pop("rc")

    # the invoice number named in the line is looked up by exact number
    class NumberClient(FakeClient):
        def get(self, path, **params):
            self.calls.append((path, params))
            if path == "Invoices" and "InvoiceNumbers" in params:
                return {"Invoices": [{"InvoiceNumber": "INV-50001", "HasAttachments": True, "InvoiceID": "g1"},
                                     {"InvoiceNumber": "INV-50001-X", "HasAttachments": True, "InvoiceID": "g2"}]}
            return {"Invoices": [], "Contacts": []}

    nc = NumberClient()
    folder = TMP / "docs"
    files = br._documents_for(nc, ENT, {"supplier": "Acme", "description": "see INV-50001", "number": "",
                                        "source": "BILL", "doc_id": "", "date": "2026-05-05"}, folder, 8)
    check(any(c[0] == "Invoices" and c[1].get("InvoiceNumbers") == "INV-50001" for c in nc.calls)
          and ("attachments", "Invoices", "g2") not in nc.calls, "a bill is matched on its exact number")
    check(files == [] , "a bill with no attachment yields no file")
    files = br._documents_for(nc, ENT, {"supplier": "Acme", "description": "x", "number": "", "source": "MJ",
                                        "doc_id": "guid", "date": "2026-05-05"}, folder, 1)
    check(len(files) == 1, "the file limit caps the documents read for a row")
finally:
    br.subprocess.run = real_run

# -- Drive routing of the quarter-end copies ----------------------------------------------

from accounting_agent import publish as pub  # noqa: E402

qname = br.quarter_path(PRE, "2026-09").name
check(pub.drive_kind(qname) == "quarter_end" and pub.drive_kind("Prepayments.xlsx") == "reports",
      "a quarter-end copy routes to its own kind, the live copy to reports")
check(pub.drive_subfolder(qname) == ("Reports", "Quarter End"), "quarter-end copies default to a folder inside Reports")

print()
print("PASS" if not FAILS else f"FAIL ({len(FAILS)})")
sys.exit(1 if FAILS else 0)
