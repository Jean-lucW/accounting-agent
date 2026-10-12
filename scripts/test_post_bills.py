"""Offline checks for scripts/post_bills.py. No network, no Xero, no bank.

A fake client stands in for Xero: it answers the reads post_bills makes
(Organisation, Accounts, TaxRates, Contacts, Invoices, the matching window,
CreditNotes) from fixtures in Xero's own shapes, and records every write. The
tests prove the spec checks refuse what they should, the duplicate guard
blocks what it should, a dry run sends nothing, a live run sends only the
bill, the contact and the attachments, a read-only session cannot post, and
the payment guard hook passes the file. A fictional three-entity group is
written to a temporary config.

    .venv/bin/python scripts/test_post_bills.py
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parents[1]
TMP = pathlib.Path(tempfile.mkdtemp(prefix="test_post_bills_"))
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "src"))

(TMP / "group.toml").write_text('''
[company]
name = "Test Group"
reporting_currency = "GBP"
timezone = "UTC"

[[entities]]
key = "ALPHA"
xero_name = "Alpha Holdings Limited"
short = "Alpha"
aliases = ["parent"]
base_currency = "GBP"
required_tracking = ["Region"]

[[entities]]
key = "BETA"
xero_name = "Beta Operations Inc."
short = "Beta"
base_currency = "USD"

[[entities]]
key = "GAMMA_EU"
xero_name = "Gamma Services Ltd"
short = "Gamma"
base_currency = "EUR"
reconcile_check = "off"

[posting]
restricted_tax_types = { "XTAX" = ["BETA"] }
''')
os.environ["AGENT_GROUP_CONFIG"] = str(TMP / "group.toml")
os.environ.pop("AGENT_READONLY", None)

import post_bills as pb  # noqa: E402
from accounting_agent import readonly  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("ok   " if cond else "FAIL ") + name + (f" - {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def doc(name: str, data: bytes = b"%PDF-1.4 test\n") -> str:
    (TMP / name).write_bytes(data)
    return name


RECEIPT = doc("S-2026-10-06-1054-Receipt.jpg")
INVOICE = doc("E-2026-10-06-1634-INV_105071598.PDF")
PLUSNAME = doc("Rent+Invoice.pdf")


def bill(**kw) -> dict:
    b = {"entity": "ALPHA", "type": "bill", "contact": "Acme Hosting",
         "number": "ACME-06OCT2026", "date": "2026-10-06", "due": "2026-10-06", "currency": "GBP",
         "total": 18.35, "files": [RECEIPT],
         "lines": [{"description": "Hosting", "amount": 18.35, "account": "7100", "tax": "NONE",
                    "tracking": {"Region": "North"}}]}
    b.update(kw)
    return {k: v for k, v in b.items() if v is not None}


def line(**kw) -> dict:
    ln = {"description": "x", "amount": 18.35, "account": "7100", "tax": "NONE", "tracking": {"Region": "North"}}
    ln.update(kw)
    return {k: v for k, v in ln.items() if v is not None}


def spec_file(bills, name="spec.json") -> pathlib.Path:
    p = TMP / name
    p.write_text(json.dumps({"docs": str(TMP), "bills": bills}))
    return p


def refused(bills, needle: str) -> bool:
    try:
        pb.load_spec(spec_file(bills))
    except pb.SpecError as e:
        return needle in str(e)
    return False


# ------------------------------------------------------------ spec checks

ok = pb.load_spec(spec_file([bill()]))
check("a clean spec loads", len(ok) == 1 and ok[0]["entity"] == "ALPHA" and ok[0]["files"][0].is_file())
check("the idempotency key is deterministic",
      pb.load_spec(spec_file([bill()]))[0]["key"] == ok[0]["key"] and ok[0]["key"].startswith("postbills-ALPHA-bill-"))
check("aliases and short names resolve", pb.load_spec(spec_file([bill(entity="parent")]))[0]["entity"] == "ALPHA"
      and pb.load_spec(spec_file([bill(entity="Alpha")]))[0]["entity"] == "ALPHA")
check("an unknown entity is refused", refused([bill(entity="NOPE")], "unknown entity"))
check("a bill without a due date is refused", refused([bill(due=None)], "due is required"))
check("a due date before the date is refused", refused([bill(due="2026-10-01")], "before the date"))
check("a line without a required tracking option is refused",
      refused([bill(lines=[line(tracking=None)])], "needs a Region tracking option"))
check("a line with other tracking only is refused",
      refused([bill(lines=[line(tracking={"Team": "Ops"})])], "needs a Region tracking option"))
check("an entity with no required tracking takes lines without it",
      len(pb.load_spec(spec_file([bill(entity="BETA", currency="USD", lines=[line(tracking=None)])]))) == 1)
check("tracking as Xero's own list is accepted",
      len(pb.load_spec(spec_file([bill(lines=[line(tracking=[{"Name": "Region", "Option": "South"}])])]))) == 1)
check("tracking in another shape is refused", refused([bill(lines=[line(tracking="North")])], "tracking"))
check("a restricted tax type outside its entity is refused",
      refused([bill(lines=[line(tax="XTAX")])], "allowed in BETA only"))
check("a restricted tax type inside its entity is accepted",
      len(pb.load_spec(spec_file([bill(entity="BETA", currency="USD", lines=[line(tax="XTAX", tracking=None)])]))) == 1)
check("lines that do not add up are refused", refused([bill(total=18.00)], "the lines come to"))
check("an unknown field is refused", refused([bill(due_date="2026-10-06")], "unknown field"))
check("a missing file is refused", refused([bill(files=["nope.pdf"])], "file not found"))
check("an empty file list is refused", refused([bill(files=[])], "missing"))
check("eleven files are refused", refused([bill(files=[RECEIPT] * 11)], "per document"))
big = TMP / "big.pdf"
big.write_bytes(b"0" * (pb.MAX_BYTES + 1))
check("a file over the attachment limit is refused", refused([bill(files=["big.pdf"])], "over 10 MB"))
check("the same number twice in one entity is refused", refused([bill(), bill()], "same number twice"))
check("an ack without a reason is refused", refused([bill(ack={"X1": " "})], "a reason for each"))
check("a feed line on another entity is refused", refused([bill(feed={"entity": "BETA", "match": "rev:1"})],
                                                          "bill-payments"))
check("a negative total is refused", refused([bill(total=-5, lines=[line(amount=-5)])], "positive"))
check("a credit note with a due date is refused", refused([bill(type="credit")], "no due date"))
check("tax is explicit on every line", refused([bill(lines=[line(tax=None)])], "tax is required"))
check("one bad document refuses the whole batch",
      refused([bill(), bill(number="B-2", due=None)], "document 2"))

s = pb.load_spec(spec_file([bill(entity="BETA", currency="USD", contact="Paperclip Supplies", number="SINV-1",
                                 total=211.05,
                                 lines=[line(amount=201.00, account="7200", tax="INPUT", tracking=None)])]))[0]
check("exclusive tax needs a rate offline", pb.expected_total(s, None)[0] is None)
check("exclusive tax is computed from the rate", pb.expected_total(s, {"INPUT": 5.0})[0] == 211.05)
s2 = pb.load_spec(spec_file([bill(number="WF-1", contact="Corner Market", total=24.66, amounts="Inclusive",
                                  lines=[line(amount=19.25, tax="ZERORATEDINPUT"),
                                         line(amount=5.41, tax="INPUT2", tax_amount=0.90)])]))[0]
check("inclusive lines are already gross", pb.expected_total(s2, {})[0] == 24.66)
check("tax_amount reaches the payload", pb.make_lines(s2)[1]["TaxAmount"] == 0.90)
check("tracking reaches the payload", pb.make_lines(s2)[0]["Tracking"] == [{"Name": "Region", "Option": "North"}])


# ------------------------------------------------------------ a fake Xero

ACME = {"ContactID": "db12e776-0000-0000-0000-000000000001", "Name": "Acme Hosting"}


class FakeXero:
    """Answers post_bills' reads from fixtures; records every write."""

    def __init__(self, key, docs=(), txns=(), contacts=None):
        self.key, self.docs, self.txns = key, list(docs), list(txns)
        self.contacts = contacts if contacts is not None else [{**ACME, "ContactStatus": "ACTIVE"}]
        self.writes: list[tuple] = []

    def get(self, path, **p):
        if path == "Organisation":
            return {"Organisations": [{"BaseCurrency": "GBP", "EndOfYearLockDate": "/Date(1782777600000+0000)/",
                                       "PeriodLockDate": "2026-09-30T00:00:00"}]}
        if path == "Accounts":
            return {"Accounts": [
                {"AccountID": "a-7100", "Code": "7100", "Name": "Hosting", "Status": "ACTIVE", "Type": "EXPENSE"},
                {"AccountID": "a-7999", "Code": "7999", "Name": "Old", "Status": "ARCHIVED", "Type": "EXPENSE"},
                {"AccountID": "bank-gbp", "Code": "090", "Name": "Main Bank", "Status": "ACTIVE",
                 "Type": "BANK", "CurrencyCode": "GBP"}]}
        if path == "TaxRates":
            return {"TaxRates": [
                {"TaxType": "NONE", "Name": "No VAT", "Status": "ACTIVE", "EffectiveRate": 0, "CanApplyToExpenses": True},
                {"TaxType": "XTAX", "Name": "Restricted", "Status": "ACTIVE", "EffectiveRate": 0, "CanApplyToExpenses": True},
                {"TaxType": "INPUT2", "Name": "20% (on Expenses)", "Status": "ACTIVE", "EffectiveRate": 20,
                 "CanApplyToExpenses": True},
                {"TaxType": "OUTPUT2", "Name": "20% (on Income)", "Status": "ACTIVE", "EffectiveRate": 20,
                 "CanApplyToExpenses": False}]}
        if path.startswith("Contacts/"):
            return {"Contacts": [c for c in self.contacts if c["ContactID"] == path.split("/")[1]]}
        if path == "Invoices":
            num = p["where"].split('InvoiceNumber=="')[1].rstrip('"')
            return {"Invoices": [d for d in self.docs if d.get("InvoiceNumber") == num and d["Type"] == "ACCPAY"]}
        raise AssertionError(f"unexpected GET {path}")

    def get_all(self, path, **p):
        if path == "Contacts":
            t = p["searchTerm"].lower()
            return [c for c in self.contacts if t in c["Name"].lower()]
        return {"Payments": [], "BankTransactions": self.txns, "Invoices": self.docs, "CreditNotes": []}[path]

    def put(self, path, body, idempotency_key=None, **p):
        self.writes.append(("PUT", path, body, idempotency_key))
        if path == "Contacts":
            return {"Contacts": [{"ContactID": "c0ffee00-0000-0000-0000-000000000009",
                                  "Name": body["Contacts"][0]["Name"]}]}
        if path == "Invoices":
            b = body["Invoices"][0]
            total = round(sum(l["Quantity"] * l["UnitAmount"] for l in b["LineItems"]), 2)
            return {"Invoices": [{**b, "InvoiceID": "b111ab11-0000-0000-0000-00000000000b", "Total": total,
                                  "AmountDue": total, "HasErrors": False}]}
        raise AssertionError(f"unexpected PUT {path}")

    def upload_attachment(self, endpoint, guid, filename, data, content_type="", include_online=False):
        self.writes.append(("ATTACH", f"{endpoint}/{guid}/{filename}", len(data), None))
        return {"Attachments": [{"FileName": filename}]}


def quiet(fn, *a, **kw):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        r = fn(*a, **kw)
    return r, buf.getvalue()


# ------------------------------------------------------------ the write gate

fake = FakeXero("ALPHA")
try:
    quiet(pb.WriteGate(fake, "ALPHA", live=False).put, "Invoices", {"Invoices": [{}]}, idempotency_key="k")
    check("a dry-run write raises before sending", False)
except pb.DryRun:
    check("a dry-run write raises before sending", fake.writes == [])
live = pb.WriteGate(fake, "ALPHA", live=True)
for what, call in [("an update (POST)", lambda: live.post("Invoices/x", {})),
                   ("spend money", lambda: live.put("BankTransactions", {})),
                   ("a manual journal", lambda: live.put("ManualJournals", {})),
                   ("a raw DELETE", lambda: live.request("DELETE", "Invoices/x")),
                   ("a credit note allocation", lambda: live.put("CreditNotes/" + "1" * 36 + "/Allocations", {}))]:
    try:
        quiet(call)
        check(f"the gate refuses {what}", False)
    except RuntimeError:
        check(f"the gate refuses {what}", fake.writes == [])
_, out = quiet(live.put, "Invoices", {"Invoices": [{"Type": "ACCPAY", "LineItems": []}]}, idempotency_key="k1")
check("a live write names the entity in full and prints the payload",
      "Alpha Holdings Limited (Alpha)" in out and '"Type":"ACCPAY"' in out and "key k1" in out)
os.environ["AGENT_READONLY"] = "1"
n_writes = len(fake.writes)
try:
    quiet(live.put, "Invoices", {"Invoices": [{}]}, idempotency_key="k2")
    check("the gate refuses a live write in a read-only session", False)
except readonly.ReadOnlyError:
    check("the gate refuses a live write in a read-only session", len(fake.writes) == n_writes)
os.environ.pop("AGENT_READONLY")


# ------------------------------------------------------------ the guard

def window(docs=(), txns=()):
    return {"base": "GBP", "accounts": {"bank-gbp": ("Main Bank", "GBP")}, "payments": [],
            "txns": list(txns), "docs": list(docs), "rates": {"USD": 1.30}}


def inv(number, total, d, contact=ACME, ccy="GBP", status="AUTHORISED", rate=None):
    return {"Type": "ACCPAY", "InvoiceID": f"{abs(hash(number)) % 10**8:08d}-0000-0000-0000-000000000000",
            "InvoiceNumber": number, "Contact": contact, "DateString": d + "T00:00:00", "Total": total,
            "CurrencyCode": ccy, "CurrencyRate": rate, "Status": status, "LineItems": []}


S = pb.load_spec(spec_file([bill()]))[0]
CID = ACME["ContactID"]
OWN = {"ALPHA": window()}
hits, _ = pb.guard(S, CID, OWN, [], [], [])
check("nothing in Xero: guard clear", hits == [])
hits, _ = pb.guard(S, CID, OWN, [inv("ACME-06OCT2026", 18.35, "2026-10-06")], [], [])
check("the same number in the target entity blocks", [h["why"] for h in hits] == ["same number"])
hits, _ = pb.guard(S, CID, OWN, [inv("ACME-06OCT2026", 18.35, "2026-10-06", status="VOIDED")], [], [])
check("a voided copy is not a duplicate", hits == [])
hits, _ = pb.guard(S, CID, {"ALPHA": window([inv("COPY-123", 18.35, "2026-10-08")])}, [], [], [])
check("the same supplier and amount two days apart blocks", len(hits) == 1 and "amount" in hits[0]["why"])
hits, _ = pb.guard(S, CID, {"ALPHA": window([inv("ACME-20SEP2026", 18.35, "2026-09-20")])}, [], [], [])
check("the same supplier and amount sixteen days apart does not block", hits == [])
spend = {"Type": "SPEND", "BankTransactionID": "t1", "Contact": ACME, "DateString": "2026-10-07T00:00:00",
         "Total": 18.35, "CurrencyCode": "GBP", "Status": "AUTHORISED", "BankAccount": {"Name": "Main Bank"},
         "LineItems": []}
hits, _ = pb.guard(S, CID, {"ALPHA": window(txns=[spend])}, [], [], [])
check("spend money for the same purchase blocks", len(hits) == 1 and "spend money" in hits[0]["why"])
other = {"Name": "Acme Hosting Co", "ContactID": "other"}
hits, _ = pb.guard(S, CID, {"ALPHA": window(), "BETA": window([inv("ACME-06OCT2026", 18.35, "2026-10-06",
                                                                   contact=other)])}, [], [], [])
check("the same number in another entity blocks", len(hits) == 1 and hits[0]["entity"] == "BETA")
hits, _ = pb.guard(S, CID, {"ALPHA": window(), "BETA": window([inv("R-77", 18.35, "2026-10-05")])}, [], [], [])
check("the supplier and amount in another entity blocks", len(hits) == 1 and "another entity" in hits[0]["why"])
usd = pb.load_spec(spec_file([bill(number="CI-1", currency="USD", total=23.18, lines=[line(amount=23.18)])]))[0]
hits, _ = pb.guard(usd, CID, {"ALPHA": window([inv("other-number", 18.35, "2026-10-06")])}, [], [], [])
check("the same purchase held in another currency blocks", any("named" in h["why"] for h in hits),
      str([h["why"] for h in hits]))
bank_side = {"Date": "/Date(1791244800000+0000)/", "Amount": 18.35, "Status": "AUTHORISED", "PaymentType": "ACCPAYPAYMENT",
             "PaymentID": "p1", "Account": {"AccountID": "bank-gbp"}, "CurrencyRate": 1.0,
             "Invoice": {"InvoiceNumber": "EARLIER-1", "Contact": ACME, "Total": 18.35, "CurrencyCode": "GBP"}}
hits, leads = pb.guard(S, CID, {"ALPHA": {**window(), "payments": [bank_side]}}, [], [], [])
check("a payment of the same bank amount to the supplier is found by the matching module",
      bool(hits or leads), str((hits, leads)))
hits, _ = pb.guard(S, CID, OWN, [], [], [{**S, "number": "ACME-06OCT2026-B", "id": ""}])
check("the same purchase twice in one batch blocks", len(hits) == 1 and "batch" in hits[0]["why"])
acked = pb.load_spec(spec_file([bill(ack={"COPY-123": "a copy of a different receipt"})]))[0]
hits, _ = pb.guard(acked, CID, {"ALPHA": window([inv("COPY-123", 18.35, "2026-10-08")])}, [], [], [])
check("an acknowledged hit is matched to its ack", bool(hits) and pb.acknowledged(acked, hits[0]) is not None)
blocking, out = quiet(pb.print_hits, acked, hits, [])
check("an acknowledged hit is printed with its reason and does not block",
      blocking == [] and "acknowledged: a copy" in out and "DUPLICATE?" not in out)
blocking, out = quiet(pb.print_hits, S, hits, [])
check("the same hit without an ack blocks", len(blocking) == 1 and "DUPLICATE?" in out)
cr = pb.load_spec(spec_file([bill(type="credit", number="REFUND-1", due=None)]))[0]
hits, _ = pb.guard(cr, CID, {}, [], [{"CreditNoteID": "c1", "Type": "ACCPAYCREDIT", "Reference": "REFUND-1",
                                      "Contact": ACME, "DateString": "2026-10-06T00:00:00", "Total": 18.35,
                                      "CurrencyCode": "GBP", "Status": "AUTHORISED"}], [])
check("a credit note with the same reference blocks", len(hits) == 1)


# ------------------------------------------------------------ whole runs

def run_with(fakes: dict, bills, name: str, **kw):
    pb.connect = lambda k: fakes[k]
    spec = spec_file(bills, name)
    results = spec.with_suffix(".results.jsonl")
    rc, out = quiet(pb.run, pb.load_spec(spec), results=results, cross=True, **kw)
    return rc, out, results


def fresh(**over):
    return {k: over.get(k) or FakeXero(k) for k in config_keys()}


def config_keys():
    return ["ALPHA", "BETA", "GAMMA_EU"]


rc, out = quiet(pb.run, pb.load_spec(spec_file([bill(), bill(number="INV-1", contact="Example Legal", new_contact=True,
                                                            files=[INVOICE])])),
                live=False, offline=True, cross=True, results=TMP / "x.jsonl")
check("an offline check reads nothing and prints both payloads",
      rc == 0 and out.count("payload") == 2 and "Nothing was read or sent" in out, out[-300:])

fakes = fresh()
rc, out, results = run_with(fakes, [bill()], "dry.json", live=False, offline=False)
check("a dry run reads every entity in scope and sends nothing",
      rc == 0 and all(f.writes == [] for f in fakes.values()) and "dry run: not sent" in out, out[-400:])
check("a dry run names accounts by name and code", "Hosting (7100)" in out)
check("a dry run writes no results file", not results.exists())

fakes = fresh()
rc, out, _ = run_with(fakes, [bill(lines=[line(account="7999")]), bill(number="ACME-2", date="2026-10-08",
                                                                       due="2026-10-08", total=3.10,
                                                                       lines=[line(amount=3.10)])],
                      "archived.json", live=True, offline=False)
w = fakes["ALPHA"].writes
check("an archived account refuses that document and the rest go on",
      rc == 4 and "REFUSED" in out and "ARCHIVED" in out and len(w) == 2
      and w[0][2]["Invoices"][0]["InvoiceNumber"] == "ACME-2")
rc, out, _ = run_with(fresh(), [bill(lines=[line(account="090")])], "bank_account.json", live=False, offline=False)
check("a bank account on a line is refused", rc == 4 and "is a bank account" in out)
rc, out, _ = run_with(fresh(), [bill(lines=[line(tax="NOSUCH")])], "no_tax.json", live=False, offline=False)
check("an unknown tax type is refused", rc == 4 and "no tax type NOSUCH" in out)
rc, out, _ = run_with(fresh(), [bill(lines=[line(account="9999")])], "no_account.json", live=False, offline=False)
check("an unknown account is refused", rc == 4 and "no account 9999" in out)
rc, out, _ = run_with(fresh(), [bill(total=18.35, lines=[line(tax="INPUT2")])], "wrong_total.json",
                      live=False, offline=False)
check("a total that differs from what Xero computes is refused", rc == 4 and "Xero will compute" in out, out[-300:])
rc, out, _ = run_with(fresh(), [bill(lines=[line(tax="OUTPUT2", amount=18.35)], total=22.02)], "sales_tax.json",
                      live=False, offline=False)
check("a tax type not enabled for expenses is a warning, Xero rules on it",
      rc == 0 and "WARN" in out and "not enabled for expenses" in out)
rc, out, _ = run_with(fresh(), [bill(date="2026-06-01", due="2026-06-01")], "locked.json", live=True, offline=False)
check("a date inside the end-of-year lock is refused", rc == 4 and "end-of-year lock date" in out)
rc, out, _ = run_with(fresh(), [bill(date="2026-09-15", due="2026-09-15")], "period.json", live=False, offline=False)
check("a date inside the period lock is a warning", rc == 0 and "inside the period lock" in out)

fakes = fresh()
two = [bill(), bill(number="INV-105071598", contact="Example Legal", new_contact=True, files=[INVOICE, PLUSNAME],
                    lines=[line(description="legal", tracking={"Region": "South"})])]
rc, out, results = run_with(fakes, two, "live.json", live=True, offline=False)
w = fakes["ALPHA"].writes
kinds = [(x[0], x[1].split("/")[0]) for x in w]
check("a live run posts the bills, the new contact and the files, nothing else",
      rc == 0 and kinds == [("PUT", "Invoices"), ("ATTACH", "Invoices"), ("PUT", "Contacts"), ("PUT", "Invoices"),
                            ("ATTACH", "Invoices"), ("ATTACH", "Invoices")], f"{kinds} {out[-600:]}")
check("no other entity is written to", all(f.writes == [] for k, f in fakes.items() if k != "ALPHA"))
b0 = w[0][2]["Invoices"][0]
check("the bill is AUTHORISED with its due date, number, currency and an idempotency key",
      b0["Status"] == "AUTHORISED" and b0["DueDate"] == "2026-10-06" and b0["InvoiceNumber"] == "ACME-06OCT2026"
      and b0["CurrencyCode"] == "GBP" and w[0][3].startswith("postbills-ALPHA-bill-"))
check("a file name Xero rejects is uploaded under a safe name", any(x[1].endswith("Rent_Invoice.pdf") for x in w))
rows = [json.loads(l) for l in results.read_text().splitlines()]
check("each settled document is in the results file", [r["status"] for r in rows] == ["posted", "posted"])
check("the amount due is checked and recorded", all(r["amount_due"] == r["total"] for r in rows))
n_before = len(w)
rc, out, _ = run_with(fakes, two, "live.json", live=True, offline=False)
check("a re-run skips what the results file shows posted",
      rc == 0 and len(fakes["ALPHA"].writes) == n_before and out.count("posted by an earlier --live") == 2)
first = pb.load_spec(spec_file(two, "live.json"))
second = pb.load_spec(spec_file(two, "live.json"))
check("the same spec gives the same keys on a re-run", [d["key"] for d in first] == [d["key"] for d in second])

fakes = fresh(**{"ALPHA": FakeXero("ALPHA", docs=[inv("ACME-06OCT2026", 18.35, "2026-10-06")])})
rc, out, results = run_with(fakes, [bill()], "dup.json", live=True, offline=False)
check("a duplicate is skipped and nothing is sent", rc == 3 and fakes["ALPHA"].writes == [] and "SKIPPED" in out)
check("the skip is in the results file", json.loads(results.read_text().splitlines()[-1])["status"] == "skipped")
fakes = fresh(**{"ALPHA": FakeXero("ALPHA", docs=[inv("COPY-123", 18.35, "2026-10-08")])})
rc, out, _ = run_with(fakes, [bill(ack={"COPY-123": "a copy of a different receipt"})], "ack.json",
                      live=True, offline=False)
check("an acknowledged hit does not stop the post", rc == 0 and len(fakes["ALPHA"].writes) == 2, out[-400:])

fakes = fresh(**{"BETA": FakeXero("BETA", docs=[inv("ACME-06OCT2026", 18.35, "2026-10-06")])})
rc, out, _ = run_with(fakes, [bill()], "cross.json", live=True, offline=False)
check("a duplicate in another entity skips the document", rc == 3 and fakes["ALPHA"].writes == [], out[-300:])
rc, out, _ = run_with(fresh(**{"GAMMA_EU": FakeXero("GAMMA_EU", docs=[inv("ACME-06OCT2026", 18.35, "2026-10-06")])}),
                      [bill()], "cross_off.json", live=False, offline=False)
check("an entity kept out of scope (reconcile_check off) is not searched", rc == 0, out[-300:])

fakes = fresh()
rc, out, _ = run_with(fakes, [bill(contact="Nobody Ltd", number="N-1")], "nocontact.json", live=True, offline=False)
check("an unknown contact without new_contact is refused",
      rc == 4 and fakes["ALPHA"].writes == [] and "new_contact" in out)
two_acme = [{**ACME, "ContactStatus": "ACTIVE"}, {**ACME, "ContactID": "db12e776-0000-0000-0000-000000000002",
                                                 "ContactStatus": "ACTIVE"}]
fakes = fresh(**{"ALPHA": FakeXero("ALPHA", contacts=two_acme)})
rc, out, _ = run_with(fakes, [bill()], "twin.json", live=True, offline=False)
check("two contacts with one name are refused until one is pinned",
      rc == 4 and fakes["ALPHA"].writes == [] and "pin one with contact_id" in out)
fakes = fresh(**{"ALPHA": FakeXero("ALPHA", contacts=two_acme)})
rc, out, _ = run_with(fakes, [bill(contact_id="db12e776-0000-0000-0000-000000000002")], "pin.json",
                      live=False, offline=False)
check("a pinned contact_id picks the one contact", rc == 0 and "(db12e776)" in out and "dry run: not sent" in out,
      out[-300:])

# the feed line is removed through bankfeed.py's own command, by the entity's slug
calls = []


class _Done:
    returncode, stdout, stderr = 0, "removed from x\n  line\n", ""


real_run = pb.subprocess.run
pb.subprocess.run = lambda cmd, **kw: (calls.append(cmd), _Done())[1]
fakes = fresh()
rc, out, _ = run_with(fakes, [bill(feed={"entity": "ALPHA", "match": "rev:abc"})], "feed.json", live=True, offline=False)
pb.subprocess.run = real_run
check("a posted bill with a feed field removes its line via bankfeed.py remove",
      rc == 0 and len(calls) == 1 and calls[0][2:5] == ["remove", "alpha", "rev:abc"], str(calls))
check("a dry run never removes a feed line",
      "feed " not in run_with(fresh(), [bill(number="F-2", feed={"entity": "ALPHA", "match": "rev:abc"})],
                              "feed2.json", live=False, offline=False)[1])

# ------------------------------------------------------------ the paying entity

check("the paying entity is read for the report", pb.load_spec(spec_file([bill(paid="parent")]))[0]["paid"] == "ALPHA")
check("an unknown paying entity is refused", refused([bill(paid="NOWHERE")], "unknown entity"))

# ------------------------------------------------------------ settling

def settle_spec(bills, settle, name):
    p = TMP / name
    p.write_text(json.dumps({"docs": str(TMP), "bills": bills, "settle": settle}))
    return p


def settle_refused(settle, needle: str) -> bool:
    p = settle_spec([bill()], settle, "settle_bad.json")
    try:
        pb.load_settle(p, pb.load_spec(p))
    except pb.SpecError as e:
        return needle in str(e)
    return False


LEDGER_OK = {"when": "2026-10-06 10:54", "source": "slack", "sender": "Sam Okafor", "seen": 1,
             "counterparty": "Acme Hosting", "ref": "GBP 18.35, 6 Oct", "entity": "Alpha",
             "outcome": "posted, 7100"}
check("a settle action names one kind", settle_refused([{"close": "K", "email": "abcdef12"}], "exactly one"))
check("after names only documents in the spec", settle_refused([{"close": "K", "after": ["NOPE-1"]}],
                                                               "not documents in this spec"))
check("a ledger field with a pipe is refused",
      settle_refused([{"ledger": {**LEDGER_OK, "ref": "a | b"}}], "pipe"))
check("a ledger line without its outcome is refused",
      settle_refused([{"ledger": {k: v for k, v in LEDGER_OK.items() if k != "outcome"}}], "missing"))
check("an email action takes a Gmail thread id", settle_refused([{"email": "not a thread"}], "thread id"))
check("a feed action names a configured entity",
      settle_refused([{"feed": {"entity": "NOWHERE", "match": "rev:1a2b3c4d", "why": "x"}}], "unknown entity"))

real_ledger = pb.LEDGER
pb.LEDGER = TMP / "LEDGER.md"
pb.LEDGER.write_text("# ledger\n\n## Entries\n\n## Run 2026-01-01\nold line\n")
CLOSED, MOVED, REMOVED = [], [], []
real_settlers = (pb.close_register, pb.move_email, pb.remove_feed)
pb.close_register = lambda pairs: (CLOSED.extend(pairs), {k: f"OI-1 closed: {r}" for k, r in pairs})[1]
pb.move_email = lambda tid: (MOVED.append(tid), "labelled Processed, out of the inbox")[1]
pb.remove_feed = lambda e, m, w: (REMOVED.append((e, m)), (True, "removed"))[1]

fakes = fresh(**{"ALPHA": FakeXero("ALPHA", docs=[inv("ACME-DUP", 5.00, "2026-10-06")])})
bills2 = [bill(), bill(number="ACME-DUP", date="2026-10-07", due="2026-10-07", total=5.00, lines=[line(amount=5.00)])]
acts = [{"ledger": LEDGER_OK, "after": ["ACME-06OCT2026"]},
        {"close": "acme-receipt", "reason": "the receipt arrived", "after": ["ACME-06OCT2026"]},
        {"email": "18f2a3b4c5d6e7f8", "after": ["ACME-06OCT2026"]},
        {"feed": {"entity": "BETA", "match": "rev:1a2b3c4d", "why": "already in Xero"}},
        {"close": "acme-dup", "after": ["ACME-DUP"]}]
sp = settle_spec(bills2, acts, "settle_live.json")
pb.connect = lambda k: fakes[k]
res = sp.with_suffix(".results.jsonl")
rc, out = quiet(pb.run, pb.load_spec(sp), live=True, offline=False, cross=True, results=res,
                settle=pb.load_settle(sp, pb.load_spec(sp)))
led = pb.LEDGER.read_text()
check("one live call posts, then settles what the post answers",
      CLOSED == [("acme-receipt", "the receipt arrived")] and MOVED == ["18f2a3b4c5d6e7f8"]
      and REMOVED == [("BETA", "rev:1a2b3c4d")], out[-800:])
check("the ledger line goes under today's run heading, with the posted id",
      led.rstrip().splitlines()[-2].startswith("## Run ") and "| 1/1 |" in led and "(xid " in led, led[-300:])
check("an action waiting on a skipped document is held, and the exit says so",
      rc == 3 and "HELD settle 5" in out and ("acme-dup", "") not in CLOSED, out[-600:])
check("a clean post prints one line and no payload on a live run",
      "POSTED Alpha · Acme Hosting · ACME-06OCT2026" in out and "payload" not in out and "SKIPPED" in out,
      out[-800:])
rows = [json.loads(l) for l in res.read_text().splitlines()]
check("every payload sent is kept in the results file",
      any(r.get("status") == "posted" and r.get("sent") and "ACCPAY" in r["sent"][0]["payload"] for r in rows))
n_led = len(led.splitlines())
rc, out = quiet(pb.run, pb.load_spec(sp), live=True, offline=False, cross=True, results=res,
                settle=pb.load_settle(sp, pb.load_spec(sp)))
check("a re-run settles nothing twice",
      len(pb.LEDGER.read_text().splitlines()) == n_led and len(CLOSED) == 1 and len(MOVED) == 1
      and "HELD settle 5" in out, out[-500:])
rc, out = quiet(pb.run, pb.load_spec(sp), live=False, offline=False, cross=True, results=TMP / "none.jsonl",
                settle=pb.load_settle(sp, pb.load_spec(sp)))
check("a dry run says what it would settle and does nothing", "would close acme-receipt" in out and len(CLOSED) == 1)
pb.LEDGER = real_ledger
pb.close_register, pb.move_email, pb.remove_feed = real_settlers

# ------------------------------------------------------------ a read-only session

os.environ["AGENT_READONLY"] = "1"
fakes = fresh()
try:
    run_with(fakes, [bill(number="RO-1")], "ro.json", live=True, offline=False)
    check("a live run is refused in a read-only session", False)
except readonly.ReadOnlyError:
    check("a live run is refused in a read-only session", all(f.writes == [] for f in fakes.values()))
rc, out = quiet(pb.main, [str(spec_file([bill(number="RO-2")], "ro2.json")), "--live"])
check("the command line refuses --live in a read-only session", rc == 1 and "read-only" in out, out)
rc, out = quiet(pb.main, [str(spec_file([bill(number="RO-3")], "ro3.json")), "--offline"])
check("an offline check still works in a read-only session", rc == 0, out)
os.environ.pop("AGENT_READONLY")

# ------------------------------------------------------------ the payment hook

src = (REPO / "scripts" / "post_bills.py").read_text()
payload = json.dumps({"tool_name": "Write", "tool_input": {"file_path": str(REPO / "scripts" / "post_bills.py"),
                                                           "content": src}})
hook = subprocess.run(["bash", str(REPO / ".claude" / "hooks" / "guard-payments.sh")], input=payload,
                      capture_output=True, text=True)
check("the payment guard hook passes post_bills.py", hook.returncode == 0 and hook.stdout.strip() == "",
      hook.stdout[:300])

print(f"\n{'FAIL' if FAILS else 'PASS'}: {len(FAILS)} failed")
sys.exit(1 if FAILS else 0)
