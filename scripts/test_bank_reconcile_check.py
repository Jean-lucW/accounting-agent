"""Offline checks for scripts/bank_reconcile_check.py: the bank-account mapping
from config, one-to-one pairing, the tolerances, ambiguity, wrong matches, a
pasted reconcile screen and the per-account tie-out. A fictional four-entity
group, fake bank and Xero data, a fake agent; no network, nothing written
outside a temporary folder.

    .venv/bin/python scripts/test_bank_reconcile_check.py
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
from datetime import date, timedelta

REPO = pathlib.Path(__file__).resolve().parents[1]
TMP = pathlib.Path(tempfile.mkdtemp(prefix="test_bank_reconcile_check_"))
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "src"))

(TMP / "group.toml").write_text('''
[company]
name = "Test Group"
reporting_currency = "GBP"
timezone = "UTC"

[[entities]]
key = "ALPHA"
xero_name = "Alpha Ltd"
short = "Alpha"
base_currency = "GBP"
  [[entities.bank_accounts]]
  label = "Revolut"
  provider = "revolut"
  slug = "alpha"
  currency = "GBP"
  xero_account_code = "090"
  [[entities.bank_accounts]]
  label = "Revolut"
  provider = "revolut"
  slug = "alpha"
  currency = "USD"
  xero_account_code = "091"

[[entities]]
key = "BETA"
xero_name = "Beta Inc"
short = "Beta"
base_currency = "USD"
  [[entities.bank_accounts]]
  label = "Mercury"
  provider = "mercury"
  slug = "beta"
  statement_name = "Checking xx1"
  xero_account = "Mercury Checking xx1"
  currency = "USD"
  [[entities.bank_accounts]]
  label = "Mercury"
  provider = "mercury"
  slug = "beta"
  statement_name = "Savings xx2"
  xero_account_code = "1001"
  currency = "USD"

[[entities]]
key = "GAMMA"
xero_name = "Gamma BV"
short = "Gamma"
base_currency = "GBP"

[[entities]]
key = "DELTA"
xero_name = "Delta Ltd"
short = "Delta"
base_currency = "GBP"
reconcile_check = "off"
''')
os.environ["AGENT_GROUP_CONFIG"] = str(TMP / "group.toml")
os.environ["BRC_OUT_DIR"] = str(TMP / "out")
import bank_reconcile_check as brc  # noqa: E402

brc.FEED = TMP / "feed"                    # no native feed: nothing to merge in
FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("ok   " if cond else "FAIL ") + name + (f" - {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def d(n: int) -> str:
    return (date.today() - timedelta(days=n)).isoformat()


# ---- the configuration drives who is read -------------------------------------
check("entities read in full come from config", brc.ENTITIES == ["alpha", "beta"], str(brc.ENTITIES))
check("an entity with no bank account is read for documents only", brc.DOC_ONLY == ["gamma"], str(brc.DOC_ONLY))
check("an entity set to off is read by nobody", "delta" not in brc.ENTITIES + brc.DOC_ONLY)

# ---- the bank account mapping: by config, refused when unclaimed -----------------
rev_rows = [
    {"day": d(5), "heading": "GBP Main", "currency": "GBP", "amount": -10.0, "id": "rev:1:GBP Main", "payee": "x",
     "alt": "", "who": "", "reference": "", "orig_amount": None, "orig_currency": "", "source": "revolut api",
     "settled_at": ""},
    {"day": d(5), "heading": "USD Main", "currency": "USD", "amount": -20.0, "id": "rev:2:USD Main", "payee": "y",
     "alt": "", "who": "", "reference": "", "orig_amount": None, "orig_currency": "", "source": "revolut api",
     "settled_at": ""},
    {"day": d(5), "heading": "EUR Main", "currency": "EUR", "amount": -30.0, "id": "rev:3:EUR Main", "payee": "z",
     "alt": "", "who": "", "reference": "", "orig_amount": None, "orig_currency": "", "source": "revolut api",
     "settled_at": ""},
]
rev_accts = [{"heading": "GBP Main", "currency": "GBP", "ledger": 100.0, "pending": 0.0},
             {"heading": "USD Main", "currency": "USD", "ledger": 200.0, "pending": 0.0},
             {"heading": "EUR Main", "currency": "EUR", "ledger": 300.0, "pending": 0.0}]
brc._revolut = lambda slug, frm: ([dict(r) for r in rev_rows], list(rev_accts))
got = brc.load_bank("alpha", d(30), None)
check("a GBP row lands on the GBP block, a USD row on the USD block",
      {m["id"]: m["bank"] for m in got["movements"]} == {"rev:1:GBP Main": "Revolut GBP", "rev:2:USD Main": "Revolut USD"},
      str(got["movements"]))
check("a row on an account no block claims is refused, not guessed",
      [u["id"] for u in got["unmapped"]] == ["rev:3:EUR Main"] and "no configured" in got["unmapped"][0]["why"], str(got["unmapped"]))
check("balances are kept per configured account, the unclaimed one dropped",
      set(got["balances"]) == {"Revolut GBP", "Revolut USD"} and got["balances"]["Revolut USD"]["ledger"] == 200.0)

mer_rows = [{"day": d(3), "heading": "Checking xx1", "currency": "USD", "amount": -5.0, "id": "mer:1:Checking xx1",
             "payee": "p", "alt": "", "who": "", "reference": "", "orig_amount": None, "orig_currency": "",
             "source": "mercury api", "settled_at": ""},
            {"day": d(3), "heading": "Other xx9", "currency": "USD", "amount": -6.0, "id": "mer:2:Other xx9",
             "payee": "q", "alt": "", "who": "", "reference": "", "orig_amount": None, "orig_currency": "",
             "source": "mercury api", "settled_at": ""}]
brc._mercury = lambda slug, frm: ([dict(r) for r in mer_rows], [])
got = brc.load_bank("beta", d(30), None)
check("statement_name tells two accounts of one login apart",
      [m["bank"] for m in got["movements"]] == ["Checking xx1"] and [u["id"] for u in got["unmapped"]] == ["mer:2:Other xx9"],
      str(got))

chart_a = [{"AccountID": "A90", "Code": "090", "Name": "Revolut GBP Main", "Type": "BANK", "CurrencyCode": "GBP"},
           {"AccountID": "A91", "Code": "091", "Name": "Revolut USD Main", "Type": "BANK", "CurrencyCode": "USD"}]
check("accounts map to the chart by code", brc.set_bank_map("alpha", chart_a) == []
      and brc.BANK_MAP["alpha"] == {"Revolut GBP": "Revolut GBP Main", "Revolut USD": "Revolut USD Main"})
probs = brc.set_bank_map("alpha", chart_a[:1])
check("an account the chart does not hold is a problem, not a guess", len(probs) == 1 and "091" in probs[0], str(probs))
chart_b = [{"AccountID": "B1", "Code": "1000", "Name": "Mercury Checking xx1", "Type": "BANK", "CurrencyCode": "USD"},
           {"AccountID": "B2", "Code": "1001", "Name": "Mercury Savings xx2", "Type": "BANK", "CurrencyCode": "USD"}]
check("a Xero account is found by its configured name as well as by code",
      brc.set_bank_map("beta", chart_b) == [] and brc.BANK_MAP["beta"]["Checking xx1"] == "Mercury Checking xx1")

# ---- tolerances ---------------------------------------------------------------
rates = brc.Rates([], "2026-01-01")
m = {"currency": "GBP", "amount": -100.0, "orig_currency": "", "orig_amount": None}
check("one currency agrees to the cent", brc.fit(m, "GBP", 100.0, rates) == "exact"
      and brc.fit(m, "GBP", 100.01, rates) is None)
doc = {"currency": "EUR", "base": "GBP", "rate": 1.25}          # EUR 125.00 is GBP 100.00 at its own rate
check("another currency fits within the tolerance at the document's own rate",
      brc.fit(m, "EUR", 125.0, rates, doc) == "converted" and brc.fit(m, "EUR", 125.9, rates, doc) == "converted"
      and brc.fit(m, "EUR", 130.0, rates, doc) is None)
card = {"currency": "GBP", "amount": -100.0, "orig_currency": "EUR", "orig_amount": 118.0}
check("the card's own charge in the bill's currency is exact", brc.fit(card, "EUR", 118.0, rates) == "exact")

# ---- the whole run over fake Xero and bank data ----------------------------------
GBP_ACCT = "Revolut GBP Main"


def mv(n, acct_ident, amount, payee, day, ccy="GBP", entity="alpha", xero=None):
    return {"id": f"rev:{n:08d}:{acct_ident}", "entity": entity, "bank": acct_ident, "heading": "GBP Main",
            "date": day, "currency": ccy, "amount": amount, "payee": payee, "alt": "", "who": "", "reference": "",
            "orig_amount": None, "orig_currency": "", "source": "revolut api", "settled_at": f"{day}T10:00"}


def pay(pid, amount, day, contact, number, acct="A90", reconciled=True, ccy="GBP"):
    return {"PaymentID": pid, "PaymentType": "ACCPAYPAYMENT", "Amount": amount, "BankAmount": amount,
            "Date": f"/Date({int(date.fromisoformat(day).strftime('%s')) * 1000 + 43200000})/", "Status": "AUTHORISED",
            "IsReconciled": reconciled, "Account": {"AccountID": acct}, "Reference": "",
            "Invoice": {"InvoiceID": "i-" + pid, "InvoiceNumber": number, "CurrencyCode": ccy,
                        "Contact": {"Name": contact}}}


def inv(iid, total, day, contact, number, ccy="GBP"):
    return {"InvoiceID": iid, "Type": "ACCPAY", "Status": "AUTHORISED", "InvoiceNumber": number, "Reference": "",
            "Contact": {"Name": contact}, "Date": f"/Date({int(date.fromisoformat(day).strftime('%s')) * 1000 + 43200000})/",
            "CurrencyCode": ccy, "CurrencyRate": 1.0, "Total": total, "AmountDue": total, "LineItems": []}


def summary(rows):
    return {"Reports": [{"Rows": [{"Rows": [{"RowType": "Row", "Cells": [
        {"Value": n}, {"Value": "0"}, {"Value": "0"}, {"Value": "0"}, {"Value": str(c)}]} for n, c in rows]}]}]}


XERO = {
    "alpha": {"pulled": "x", "since": d(30), "org": {"BaseCurrency": "GBP", "ShortCode": "a"}, "accounts": chart_a,
              "invoices": [inv("b-bakery", 200.0, d(9), "Corner Bakery", "CB-1"),
                           inv("b-gadget", 10.01, d(9), "Gadget Store", "GS-1")],
              "credit_notes": [],
              "payments": [pay("p-acme", 75.50, d(9), "Acme Hosting", "AH-1"),
                           pay("p-mkt", 30.0, d(5), "Daily Market", "DM-1")],
              "bank_transactions": [], "bank_summary": summary([(GBP_ACCT, -105.50)])},
    "beta": {"pulled": "x", "since": d(30), "org": {"BaseCurrency": "USD", "ShortCode": "b"}, "accounts": chart_b,
             "invoices": [], "credit_notes": [], "payments": [], "bank_transactions": [],
             "bank_summary": summary([("Mercury Checking xx1", 0), ("Mercury Savings xx2", 0)])},
    "gamma": {"pulled": "x", "since": d(30), "org": {"BaseCurrency": "GBP", "ShortCode": "g"}, "accounts": [],
              "invoices": [inv("b-travel", 500.0, d(5), "Wayfarer Travel Ltd", "WT-1")], "credit_notes": []},
}
BANK = {
    "alpha": {"pulled": "x", "lo": d(40), "unmapped": [
        {**mv(99, "", -9.0, "Stray", d(2)), "bank": "", "why": "no configured bank account claims it"}],
        "balances": {"Revolut GBP": {"currency": "GBP", "ledger": -1005.5, "pending": 0.0},
                     "Revolut USD": {"currency": "USD", "ledger": -300.0, "pending": 0.0}},
        "movements": [
            mv(1, "Revolut GBP", -120.0, "Harbour Cafe", d(10)),
            mv(2, "Revolut GBP", -75.5, "Acme Hosting", d(9)),
            mv(3, "Revolut GBP", -40.0, "Pet Supplies", d(8)),
            mv(4, "Revolut GBP", -200.0, "Corner Bakery", d(7)),
            mv(5, "Revolut GBP", -30.0, "Daily Market", d(6)),
            mv(6, "Revolut GBP", -30.0, "Daily Market", d(4)),
            mv(7, "Revolut GBP", -10.0, "Gadget Store", d(6)),
            mv(8, "Revolut GBP", -500.0, "Wayfarer Travel", d(3)),
            {**mv(9, "Revolut USD", -300.0, "Foreign Vendor", d(5), ccy="USD"), "heading": "USD Main"}]},
    "beta": {"pulled": "x", "lo": d(40), "unmapped": [],
             "balances": {"Checking xx1": {"currency": "USD", "ledger": -50.0, "pending": None},
                          "Savings xx2": {"currency": "USD", "ledger": 150.0, "pending": None}},
             "movements": [
                 {**mv(11, "Checking xx1", -50.0, "Mystery Vendor", d(2), ccy="USD", entity="beta"), "heading": "Checking xx1"},
                 {**mv(12, "Savings xx2", 100.0, "Interest", d(2), ccy="USD", entity="beta"), "heading": "Savings xx2"}]},
}
brc.pull_xero = lambda key, since, cache, docs_only=False: XERO[key]
brc.load_bank = lambda key, lo, cache: BANK[key]

res = brc.run_check(None, since=d(30), write=True, agent=False, quiet=True)
alpha = res["entities"]["alpha"]
open_ = {m["payee"] + ("2" if m["id"].startswith("rev:00000006") else ""): m for m in alpha["movements"]}
check("a movement of the amount to the cent, the right payee, is reconciled and not listed",
      "Acme Hosting" not in open_, str(sorted(open_)))
check("the unreconciled-record and open-document counts reach the result",
      alpha["reconciled_count"] == 2, str(alpha["reconciled_count"]))
check("an open bill of the exact amount from the named supplier is TO MATCH, by code",
      open_["Corner Bakery"]["status"] == "TO MATCH" and open_["Corner Bakery"]["match_by"] == "code"
      and open_["Corner Bakery"]["goes_to"]["documents"][0]["number"] == "CB-1")
check("a line no document comes near is NO DOCUMENT",
      open_["Pet Supplies"]["status"] == "NO DOCUMENT" and open_["Harbour Cafe"]["status"] == "NO DOCUMENT")
check("a bill one cent out is not a match: awaiting information, never TO MATCH",
      open_["Gadget Store"]["status"] == "NO DOCUMENT" and brc._label(open_["Gadget Store"]) == "AWAITING INFORMATION",
      str(open_["Gadget Store"]))
check("a bill in another entity is INTERCO, not TO MATCH",
      open_["Wayfarer Travel"]["status"] == "INTERCO" and open_["Wayfarer Travel"]["goes_to"]["documents"][0]["entity"] == "gamma",
      str(open_["Wayfarer Travel"]))
mk = [m for m in alpha["movements"] if m["payee"] == "Daily Market"]
check("two lines, one reconciled record: one is reconciled, the other is open and ambiguous",
      len(mk) == 1 and mk[0]["status"] == "AMBIGUOUS" and len(mk[0]["siblings"]) == 1, str(mk))
check("a refused line is reported, with the reason", [r["bank"] for r in res["refused"]] == ["GBP Main"]
      and "no configured" in res["refused"][0]["why"], str(res["refused"]))

ties = alpha["accounts"]
check("an account whose lines and records explain the balances ties and is verified",
      ties[GBP_ACCT]["ties"] and ties[GBP_ACCT]["verified"] and ties[GBP_ACCT]["difference"] == 0, str(ties[GBP_ACCT]))
check("the tie-out sums the open lines and the unreconciled records",
      ties[GBP_ACCT]["open_sum"] == -900.0 and ties[GBP_ACCT]["unreconciled_sum"] == 0.0, str(ties[GBP_ACCT]))
beta = res["entities"]["beta"]["accounts"]
check("an account that does not tie and has no paste is UNVERIFIED",
      beta["Mercury Savings xx2"]["verified"] is False and beta["Mercury Savings xx2"]["difference"] == 50.0
      and beta["Mercury Checking xx1"]["verified"], str(beta))
check("lines of an unverified account are flagged so no one is asked to reconcile them",
      all(m["verified"] is False for m in res["entities"]["beta"]["movements"] if m["xero"] == "Mercury Savings xx2"))
check("an entity read for documents only has no bank result", "gamma" not in res["entities"])
check("the not-yet-reconciled files are written", (TMP / "out" / "unreconciled.json").exists()
      and (TMP / "out" / "unreconciled" / "alpha.md").exists())
saved = json.loads((TMP / "out" / "unreconciled.json").read_text())
check("the saved list holds the open lines for the reconcile list", len(saved["entities"]["alpha"]["movements"]) == len(alpha["movements"]))

# ---- the window: from the oldest open line less the pad, never beyond the cap -------
since, why = brc.window_since("alpha")
oldest = min(m["date"] for m in alpha["movements"])
check("the next window opens the pad before the oldest open line, inside the cap",
      since == brc._shift(oldest, -brc.WATERMARK_PAD) or since == brc._shift(date.today().isoformat(), -brc.WINDOW_CAP_DAYS),
      f"{since} {why}")
check("a line the last run left open is carried", len(brc.previous_open("alpha")) == len(alpha["movements"]))

# ---- a pasted reconcile screen is the last word up to its moment -----------------------
paste = TMP / "paste.txt"
def screen(day, payee, direction, amount):
    dd = date.fromisoformat(day)
    return f"{dd.day} {dd:%b} {dd.year}\n{payee}\n{direction}\n{amount}\n"
paste.write_text("Reconcile (2)\n1,000.00 Statement Balance 1,005.50 Balance in Xero\n"
                 + screen(d(10), "Harbour Cafe", "Spent", "120.00") + screen(d(8), "Pet Supplies", "Spent", "40.00"))
parsed = brc.parse_paste(paste.read_text())
check("a pasted screen is read: lines, direction, amount, balances",
      len(parsed["lines"]) == 2 and parsed["lines"][0]["direction"] == "spent" and parsed["lines"][0]["amount"] == 120.0
      and parsed["statement_balance"] == 1000.0)
try:
    brc.parse_paste("Reconcile (3)\n" + screen(d(10), "Harbour Cafe", "Spent", "120.00"))
    check("a paste short of the screen's own count is refused", False)
except ValueError:
    check("a paste short of the screen's own count is refused", True)
import io, contextlib  # noqa: E402
with contextlib.redirect_stdout(io.StringIO()):
    brc.cmd_confirm("alpha", "Revolut GBP", str(paste), (date.today() + timedelta(days=1)).isoformat() + "T12:00:00Z")
res2 = brc.run_check(["alpha"], since=d(30), write=False, agent=False, quiet=True)
a2 = res2["entities"]["alpha"]
left = sorted(m["payee"] for m in a2["movements"] if m["xero"] == GBP_ACCT)
check("after a paste only the lines on the screen are open on that account", left == ["Harbour Cafe", "Pet Supplies"], str(left))
check("every disagreement between the paste and the inference is reported",
      any(x["kind"] == "inferred open, paste reconciled" for x in a2["disagreements"]), str(a2["disagreements"]))
check("an account with a paste is verified whether or not it ties", a2["accounts"][GBP_ACCT]["verified"])

# ---- --sync-feed adds the NO DOCUMENT lines the native feed lacks, through bankfeed.py --------
import bankfeed  # noqa: E402
bankfeed.FEED = TMP / "feed"
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    brc.run_check(["beta"], since=d(30), write=True, agent=False, quiet=True, sync_feed=True)
print(buf.getvalue())
fed = (TMP / "feed" / "beta.md").read_text() if (TMP / "feed" / "beta.md").exists() else ""
check("--sync-feed adds a verified NO DOCUMENT line and holds back an unverified account's",
      "Mystery Vendor" in fed and "Interest" not in fed, fed)

# ---- the printed forms -----------------------------------------------------------------
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    brc.print_summary(res)
    brc.cmd_show("alpha")
text = buf.getvalue()
check("the summary names each account, its tie-out and its open lines",
      "Revolut GBP Main" in text and "TIES" in text and "UNVERIFIED" in text and "Harbour Cafe" in text, text[:400])
check("the not-yet-reconciled list says the account does not tie where it does not",
      "OFF BY USD 50.00" in brc.entity_md("beta", res["entities"]["beta"], res["built"]))

print(f"\n{len(FAILS)} failed" if FAILS else "\nall passed")
sys.exit(1 if FAILS else 0)
