#!/usr/bin/env python
"""Offline checks for scripts/review_checks.py: every check family, the cache,
`show` and the rate-limit exit, against a fake Xero client and fictional data.
No network, no credentials. The group is the fictional one in
config/group.example.toml plus a [review_checks] section written here.

    .venv/bin/python scripts/test_review_checks.py
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))

TMP = Path(tempfile.mkdtemp(prefix="review-checks-"))
(TMP / "rules").mkdir()
(TMP / "data" / "interco").mkdir(parents=True)
(TMP / "data" / "bankfeed").mkdir(parents=True)
os.environ["AGENT_ROOT"] = str(TMP)

REVIEW_SECTION = r'''
[review_checks]
not_duplicates = ["usage supplier"]
twin_reference_pattern = '\b(TRIP-\d{6})\b'
card_charge_number_pattern = '^CC\d{6}'
quarter_exempt_accounts = ["travel"]

[[review_checks.known_bad_accounts]]
entity = "HOLDCO"
code = "1999"
name_pattern = "old loan"
why = "redundant duplicate of the intercompany loan; never post"
archived = true

[[review_checks.clearing_accounts]]
entity = "HOLDCO"
code = "1250"

[[review_checks.cutovers]]
kind = "recognise_in"
entity = "OPCO_US"
from = "2026-09-01"
match = ["A\\. Person"]
why = "the US team's costs sit in OpCo US"

[[review_checks.cutovers]]
kind = "only_accounts"
entity = "OPCO_EU"
from = "2026-08-01"
match = ["example hosting"]
accounts = ["2620"]
mirror = { entity = "HOLDCO", account = "4000" }
why = "recognised in HoldCo from August"

[[review_checks.cutovers]]
kind = "not_accounts"
entity = "HOLDCO"
from = "2026-08-01"
match = ["recharge customer"]
accounts = ["4100"]
why = "the recharge stops"

[[review_checks.cutovers]]
kind = "no_bills"
entity = "OPCO_US"
from = "2026-08-01"
match = ["local payroll"]
exclude = ["provider a"]
why = "payroll runs through the control accounts"

[[review_checks.tracking]]
entity = "OPCO_EU"
category = "Region"
options = ["North", "South"]
mismatch = [{ option = "South", currency = "EUR", pattern = "northport" }]

[[review_checks.tax]]
entity = "OPCO_US"
no_tax = true

[[review_checks.tax]]
entity = "HOLDCO"
base_currency_only = true

[[review_checks.revenue]]
entity = "OPCO_US"
allowed_accounts = "management fee"
flag_sales = true

[[review_checks.routing]]
entity = "HOLDCO"
supplier_pattern = "example health"
account_pattern = "health"
must = "be_on"
why = "health care goes to the health care account"

[[review_checks.register_depreciated]]
entity = "HOLDCO"
assets = ["FA-0001"]
journal_pattern = "rou"
accounts = ["1590"]
why = "right of use assets are depreciated by the register"
'''

CFG = TMP / "group.toml"
CFG.write_text((REPO / "config" / "group.example.toml").read_text() + REVIEW_SECTION)
os.environ["AGENT_GROUP_CONFIG"] = str(CFG)

(TMP / "rules" / "EXPENSES.md").write_text("""# Expenses

| Cost category | HOLDCO | OPCO_US |
|---|---|---|
| Travel | 5100 Travel | 6100 Travel |
| Hosting | 5320 Hosting | n/a |
| Subscriptions | 5300 Subscriptions | 6300 Software |
| IT | 5310 IT Costs | 6310 IT Costs |
| Bank fees | 5600 Bank Fees | 6600 Bank Charges |
| Welfare | 5110 Staff Welfare | 6110 Welfare |
| Health | 6000 Health Care | n/a |

> | Cost category | HOLDCO |
> |---|---|
> | Illustration | 9999 Quoted |
""")

import review_checks as rc  # noqa: E402
from accounting_agent import config  # noqa: E402
from accounting_agent.xero import XeroError  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, what: str) -> None:
    print(("ok    " if cond else "FAIL  ") + what)
    if not cond:
        FAILS.append(what)


# ---------------------------------------------------------------- fake data

TODAY = "2026-10-07"
SINCE = "2026-10-01T00:00"
NEW = "2026-10-05T09:00:00"
OLD = "2026-08-01T09:00:00"
_ids = iter(range(1, 10_000))


def acct(code, name, cls, typ=None, status="ACTIVE"):
    return {"AccountID": f"A{code}", "Code": code, "Name": name, "Class": cls,
            "Type": typ or {"EXPENSE": "EXPENSE", "REVENUE": "REVENUE", "ASSET": "CURRENT",
                            "LIABILITY": "CURRLIAB"}.get(cls, "CURRENT"),
            "Status": status}


def line(code, desc, amount, tax=0.0, tax_type="NONE", tracking=None, unit=None):
    return {"Description": desc, "AccountCode": code, "LineAmount": amount,
            "UnitAmount": unit if unit is not None else amount, "TaxAmount": tax, "TaxType": tax_type,
            "Tracking": tracking or []}


def doc(kind, number, who, date, lines_, status="AUTHORISED", att=True, ccy=None, upd=NEW, cid=None,
        total=None, reconciled=False, bank="Main", due=None, ref=""):
    n = next(_ids)
    d = {"Contact": {"ContactID": cid or f"C-{who}", "Name": who}, "DateString": f"{date}T00:00:00",
         "LineItems": lines_, "Status": status, "HasAttachments": att, "UpdatedDateUTC": upd,
         "Total": total if total is not None else sum(ln["LineAmount"] for ln in lines_),
         "CurrencyCode": ccy, "Reference": ref}
    if kind in ("ACCPAY", "ACCREC"):
        d.update({"InvoiceID": f"I{n}", "InvoiceNumber": number, "Type": kind,
                  "AmountDue": d["Total"] if due is None else due})
    else:
        d.update({"BankTransactionID": f"B{n}", "Type": kind, "IsReconciled": reconciled,
                  "BankAccount": {"Name": bank}})
    return d


def journal(narration, date, lines_, status="POSTED", upd=NEW, att=False):
    n = next(_ids)
    return {"ManualJournalID": f"J{n}", "Narration": narration, "DateString": f"{date}T00:00:00",
            "Status": status, "UpdatedDateUTC": upd, "HasAttachments": att,
            "JournalLines": [{"AccountCode": c, "LineAmount": a, "Description": narration} for c, a in lines_]}


def tb_report(rows):
    """rows: (code, ytd_debit, ytd_credit, period_debit, period_credit)"""
    cells = [{"Value": v} for v in ("Account", "Debit", "Credit", "YTD Debit", "YTD Credit")]
    out = [{"RowType": "Header", "Cells": cells}]
    body = []
    for code, yd, yc, pd, pc in rows:
        body.append({"RowType": "Row", "Cells": [
            {"Value": code, "Attributes": [{"Id": "account", "Value": f"A{code}"}]},
            {"Value": str(pd)}, {"Value": str(pc)}, {"Value": str(yd)}, {"Value": str(yc)}]})
    out.append({"RowType": "Section", "Title": "Accounts", "Rows": body})
    return {"Reports": [{"Rows": out}]}


class FakeXero:
    """The few GETs the pull makes, answered from a dict."""

    calls = 0

    def __init__(self, key, data):
        self.key, self.d = key, data
        self.tenant_id = "tenant"

    def request(self, method, path, **kw):
        FakeXero.calls += 1
        return 200, b"", {}

    def get(self, path, **params):
        self.request("GET", path)
        if self.d.get("rate_limited"):
            raise XeroError("429 daily limit", 429)
        if path == "Organisation":
            return {"Organisations": [self.d["org"]]}
        if path == "Accounts":
            return {"Accounts": self.d["accounts"]}
        if path == "Reports/TrialBalance":
            return tb_report(self.d["tb"][params["date"]])
        raise AssertionError(path)

    def get_all(self, path, collection=None, page_size=1000, **params):
        self.request("GET", path)
        where = params.get("where", "")
        if path == "Invoices":
            return self.d["invoices_open_old"] if "Status==" in where else self.d["invoices"]
        if path == "BankTransactions":
            return self.d["bank_unrec_old"] if "IsReconciled" in where else self.d["bank_txns"]
        if path == "ManualJournals":
            return self.d["mj_draft_old"] if "DRAFT" in where else self.d["mj"]
        if path == "Contacts":
            return self.d["contacts"]
        raise AssertionError(path)

    def assets_get(self, path):
        if path == "AssetTypes":
            return self.d["far_types"]
        status = path.split("status=")[1].split("&")[0]
        return {"items": self.d["far_assets"].get(status, []), "pagination": {"pageCount": 1}}


def empty(key, base):
    return {"org": {"Name": f"{key} org", "PeriodLockDate": "2026-06-30", "EndOfYearLockDate": "2025-12-31"},
            "accounts": [], "tb": {d: [] for d in ("2026-07-31", "2026-08-31", "2026-09-30", TODAY)},
            "invoices": [], "invoices_open_old": [], "bank_txns": [], "bank_unrec_old": [], "mj": [],
            "mj_draft_old": [], "contacts": [], "far_types": [], "far_assets": {}}


def holdco():
    d = empty("HOLDCO", "GBP")
    d["accounts"] = [
        acct("090", "Main Bank", "ASSET", "BANK"), acct("1500", "Computer Equipment", "ASSET", "FIXED"),
        acct("5100", "Travel", "EXPENSE"), acct("5110", "Staff Welfare", "EXPENSE"),
        acct("5300", "Subscriptions", "EXPENSE"), acct("5310", "IT Costs", "EXPENSE"),
        acct("5320", "Hosting", "EXPENSE"), acct("5600", "Bank Fees", "EXPENSE"),
        acct("5700", "Miscellaneous Costs", "EXPENSE"), acct("6000", "Health Care", "EXPENSE"),
        acct("1998", "Suspense", "ASSET"), acct("1250", "Card Clearing", "ASSET"),
        acct("1999", "Old Loan", "LIABILITY"), acct("814", "Wages Payable", "LIABILITY"),
        acct("825", "Payroll Taxes Payable", "LIABILITY"), acct("4000", "Management Fee", "REVENUE"),
        acct("4100", "Recharges", "REVENUE"), acct("1610", "Intercompany Loan", "ASSET"),
    ]
    base = [("090", 0, 100, 0, 0), ("5310", 0, 30, 0, 0), ("1998", 250, 0, 0, 0), ("814", 0, 500, 0, 0),
            ("1999", 10, 0, 0, 0), ("1500", 1500, 0, 0, 0)]
    d["tb"][TODAY] = base
    d["tb"]["2026-09-30"] = base + [("1250", 40, 0, 0, 0), ("5320", 30200, 0, 30000, 0)]
    d["tb"]["2026-08-31"] = [("5320", 200, 0, 100, 0)]
    d["tb"]["2026-07-31"] = [("5320", 100, 0, 100, 0)]
    inv = d["invoices"]
    inv += [
        # S05 duplicates, a usage supplier left alone, a shared trip number, a card-charge twin
        doc("ACCPAY", "INV-1", "Acme Hosting", "2026-09-10", [line("5320", "Hosting", 120)]),
        doc("ACCPAY", "INV-2", "Acme Hosting", "2026-09-10", [line("5320", "Hosting", 120)]),
        doc("ACCPAY", "U-1", "Usage Supplier Ltd", "2026-09-11", [line("5320", "Usage", 50)]),
        doc("ACCPAY", "U-2", "Usage Supplier Ltd", "2026-09-11", [line("5320", "Usage", 50)]),
        doc("ACCPAY", "TRIP-123456", "Booking Platform", "2026-09-12", [line("5100", "Trip", 300)]),
        doc("ACCPAY", "H-9", "Example Hotel Group", "2026-09-13", [line("5100", "Stay TRIP-123456", 280)]),
        doc("ACCPAY", "CC000001", "Card Merchant", "2026-09-14", [line("5300", "Plan", 77)]),
        doc("ACCPAY", "M-77", "Card Merchant", "2026-09-20", [line("5300", "Plan", 77)]),
        # S07 blanks
        doc("ACCPAY", "", "Stationery Co", "2026-09-15", [line("5700", "Pens", 20)]),
        doc("ACCPAY", "PH-1", "Unknown", "2026-09-15", [line("5700", "Misc", 20)]),
        # S08 drift (earlier on 5300, now 5700) and S08W (5700 is not in the account table)
        doc("ACCPAY", "E-1", "Example Supplier Ltd", "2026-05-10", [line("5300", "Licence", 500)], upd=OLD),
        doc("ACCPAY", "E-2", "Example Supplier Ltd", "2026-09-16", [line("5700", "Licence", 500)]),
        # S08C both directions, S08S steady subscription on IT Costs
        doc("ACCPAY", "L-1", "Laptop Shop", "2026-09-17", [line("5310", "Laptop for a new starter", 1200)]),
        doc("ACCPAY", "C-1", "Cable Shop", "2026-09-17", [line("1500", "Cable", 100)]),
        *[doc("ACCPAY", f"S-{m}", "Example Cloud Co", f"2026-{m:02d}-05", [line("5310", "Seats", 40)], upd=OLD)
          for m in (6, 7, 8, 9)],
        # S08T travel on welfare
        doc("ACCPAY", "W-1", "Example Hotel Group", "2026-09-18", [line("5110", "Hotel stay", 90)]),
        # S09 bank fees billed
        doc("ACCPAY", "BF-1", "Example Bank", "2026-09-19", [line("5600", "Bank fee", 10)]),
        # S10 tax on a foreign-currency line (HOLDCO is GBP)
        doc("ACCPAY", "T-1", "US Vendor Inc", "2026-09-19", [line("5300", "Subscription", 100, tax=20, tax_type="INPUT2")],
            ccy="USD"),
        # S13 quarter: dated Q3 but describes November; the travel account is exempt
        doc("ACCPAY", "Q-1", "Example Supplier Ltd", "2026-09-20", [line("5320", "Hosting for November", 60)]),
        doc("ACCPAY", "Q-2", "Booking Platform", "2026-09-20", [line("5100", "Flights for November", 60)]),
        # S14M payroll mapping drift: the provider's bills move to a new account
        doc("ACCPAY", "P-7", "Payroll Provider", "2026-07-25", [line("5300", "Payroll", 1000)], status="PAID",
            upd=OLD, due=0),
        doc("ACCPAY", "P-8", "Payroll Provider", "2026-08-25", [line("5300", "Payroll", 1000)], status="PAID",
            upd=OLD, due=0),
        doc("ACCPAY", "P-9", "Payroll Provider", "2026-09-25", [line("5300", "Payroll", 900), line("5700", "Fee", 100)],
            upd=OLD),
        # S15 routing: health care supplier on the wrong account
        doc("ACCPAY", "HC-1", "Example Health Ltd", "2026-09-21", [line("5700", "Cover", 200)]),
        # S16P posting to a known-bad account
        doc("ACCPAY", "KB-1", "Example Supplier Ltd", "2026-09-22", [line("1999", "Oops", 30)]),
        # S17: a person's cost in the wrong entity, and a sale on a barred account
        doc("ACCPAY", "TX-1", "Taxi Co", "2026-09-15", [line("5100", "A. Person taxi", 30)]),
        doc("ACCREC", "R-1", "Recharge Customer", "2026-09-10", [line("4100", "Recharge", 400)]),
        # S18 no document (the interco-coded one is exempt)
        doc("ACCPAY", "ND-1", "No Doc Ltd", "2026-09-23", [line("5700", "Misc", 45)], att=False),
        doc("ACCPAY", "ND-2", "Group Loan Desk", "2026-09-23", [line("1610", "Loan", 45)], att=False),
    ]
    d["invoices_open_old"] = [doc("ACCPAY", "OLD-1", "Slow Supplier", "2026-03-01", [line("5700", "Old", 80)], upd=OLD)]
    bt = d["bank_txns"]
    bt += [
        doc("SPEND", "", "Example Consulting", "2026-09-24", [line("5700", "Advice", 400)], bank="Main"),
        doc("SPEND", "", "Corner Cafe", "2026-09-24", [line("5110", "Lunch", 400)], bank="Main"),
        doc("SPEND", "", "Example Bank", "2026-08-01", [line("5600", "Fee", 5)], bank="Main"),
    ]
    d["bank_unrec_old"] = [doc("SPEND", "", "Example Bank", "2026-05-02", [line("5600", "Fee", 5)], upd=OLD)]
    d["mj"] = [
        journal("Payroll September", "2026-09-30", [("5300", 100), ("814", -100)]),
        journal("Stale draft", "2026-09-01", [("5700", 1), ("1998", -1)], status="DRAFT", upd="2026-08-20T00:00:00"),
        journal("Wrong one", "2026-09-02", [("5700", 1), ("1998", -1)], status="VOIDED"),
        journal("ROU depreciation", "2026-09-30", [("1590", -50), ("5700", 50)]),
    ]
    d["contacts"] = [{"ContactID": "C1", "Name": "Overpaid Ltd", "Balances": {"AccountsPayable": {"Outstanding": -25}}},
                     {"ContactID": "C2", "Name": "Round Ltd", "Balances": {"AccountsPayable": {"Outstanding": 3000}}}]
    d["far_types"] = [{"assetTypeId": "T1", "assetTypeName": "Computers", "fixedAssetAccountId": "A1500"}]
    d["far_assets"] = {"REGISTERED": [{"assetTypeId": "T1", "purchasePrice": 1000, "assetNumber": "FA-0001",
                                       "bookDepreciationSetting": {"depreciationMethod": "NoDepreciation"}}],
                       "DRAFT": [{"assetTypeId": "T1", "purchasePrice": 100, "assetNumber": "FA-0002",
                                  "assetName": "Cable", "purchaseDate": "2026-09-17"}]}
    return d


def opco_us():
    d = empty("OPCO_US", "USD")
    d["accounts"] = [acct("6100", "Travel", "EXPENSE"), acct("4100", "Sales", "REVENUE"),
                     acct("4200", "Management Fee", "REVENUE"), acct("6300", "Software", "EXPENSE"),
                     acct("2100", "Payroll Liabilities", "LIABILITY")]
    d["invoices"] = [
        doc("ACCPAY", "U-1", "Software Co", "2026-09-10", [line("6300", "Tool", 100, tax=10, tax_type="TAX")]),
        doc("ACCPAY", "LP-1", "Local Payroll Ltd", "2026-09-25", [line("6300", "Payroll", 100)]),
        doc("ACCPAY", "LP-2", "Provider A Payroll", "2026-09-25", [line("6300", "Payroll", 100)]),
        doc("ACCREC", "S-1", "Customer", "2026-09-12", [line("4100", "Services", 500)]),
    ]
    d["bank_txns"] = [doc("RECEIVE", "", "Customer", "2026-09-14", [line("4100", "Services", 50)])]
    return d


def opco_eu():
    d = empty("OPCO_EU", "EUR")
    d["accounts"] = [acct("6100", "Hosting", "EXPENSE"), acct("2620", "Intercompany Loan", "LIABILITY")]
    d["invoices"] = [
        doc("ACCPAY", "H-1", "Example Hosting", "2026-09-10", [line("6100", "Hosting", 80)]),
        doc("ACCPAY", "T-1", "Northport Printer", "2026-09-11", [
            line("6100", "Printing", 10, tracking=[{"Name": "Region", "Option": "South"}]),
            line("6100", "More printing", 10)], ccy="EUR"),
    ]
    return d


DATA = {"HOLDCO": holdco(), "OPCO_US": opco_us(), "OPCO_EU": opco_eu()}
CREATED: list[str] = []


def factory(key):
    CREATED.append(key)
    return FakeXero(key, DATA[key])


rc.CLIENT_FACTORY = factory
rc.RATES.update({"USD": 1.0, "GBP": 1.25, "EUR": 1.1})
(TMP / "data" / "interco" / "latest.json").write_text(json.dumps({
    "as_at": TODAY, "reporting_currency": "USD", "rates": {"GBP": 1.25, "EUR": 1.1},
    "pairs": [
        {"pair": "HOLDCO:OPCO_US:loan", "flavour": "loan", "break": 120.5, "status": "BREAK (2 unmatched)",
         "a": {"entity": "HOLDCO"}, "b": {"entity": "OPCO_US"}},
        {"pair": "HOLDCO:OPCO_EU:loan", "flavour": "loan", "break": 0.0, "status": "AGREED",
         "a": {"entity": "HOLDCO"}, "b": {"entity": "OPCO_EU"}}]}))
(TMP / "data" / "bankfeed" / "unreconciled.json").write_text(json.dumps({
    "built": f"{TODAY}T06:00:00Z",
    "entities": {"holdco": {"accounts": {
        "Main GBP": {"difference": 5.0, "ties": False, "verified": False, "bank_balance": 100.0,
                     "xero_balance": 95.0, "open_sum": 0.0, "unreconciled_sum": 0.0, "currency": "GBP"},
        "Spare GBP": {"why": "no Xero account mapped"}}}}}))
(TMP / "data" / "review" / "checks").mkdir(parents=True)
(TMP / "data" / "review" / "checks" / "state.json").write_text(json.dumps(
    {"HOLDCO": {"lock": "2026-05-31", "eoy": "2025-12-31", "seen": "2026-10-01"}}))


def run(*args):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = rc.main(["--today", TODAY, "--since", SINCE, *args])
    return code, buf.getvalue()


def findings():
    return json.loads((TMP / "data" / "review" / "checks" / TODAY / "findings.json").read_text())["findings"]


def has(entity, check, text="", issue=None):
    return any(f["entity"] == entity and f["check"] == check and text.lower() in f["text"].lower()
               and (issue is None or f["issue"] == issue) for f in findings())


# ---------------------------------------------------------------- the run

code, out = run("all", "--all")
check(code == 0, "all entities pulled and checked, exit 0")
check(len(CREATED) == 3, "each entity pulled once")
check("REVIEW CHECKS 2026-10-07" in out and "window 2026-07-01..2026-10-07" in out,
      "the window is the financial quarter that held the day 30 days ago")
check("baseline from 2026-04-01" in out, "the baseline is three months before the window")

# state and balance checks
check(has("HOLDCO", "S00", "lock date moved"), "S00 lock date moved since the last run")
check(has("HOLDCO", "S01", "negative bank balance"), "S01 negative bank balance")
check(has("HOLDCO", "S01", "credit balance on expense"), "S01 credit balance on an expense account")
check(has("HOLDCO", "S01", "Hosting (5320) moved"), "S01 a P&L account that moved far above its prior months")
check(has("HOLDCO", "S02", "1 unreconciled") or has("HOLDCO", "S02", "unreconciled"), "S02 unreconciled per bank account")
check(has("HOLDCO", "S02", "older than 30 days"), "S02 an old unreconciled line")
check(has("HOLDCO", "S03", "negative payable"), "S03 negative payable balance")
check(has("HOLDCO", "S03", "round-number payable"), "S03 round-number balance")
check(has("HOLDCO", "S03", "older than 90 days"), "S03 an open bill older than 90 days")
check(has("HOLDCO", "S04D", "DRAFT journal") and has("HOLDCO", "S04D", "(stale)"), "S04D a stale draft journal")
check(has("HOLDCO", "S04V", "voided"), "S04V a journal voided since the last review")
check(has("HOLDCO", "S04P", "without the payroll report"), "S04P journals posted since the last review, payroll flagged")

# documents
check(has("HOLDCO", "S05", "same contact, total and date"), "S05 same contact, total and date")
check(not any(f["entity"] == "HOLDCO" and f["check"] == "S05" and "Usage Supplier" in f["who"] for f in findings()),
      "S05 leaves a configured not-duplicate supplier alone")
check(has("HOLDCO", "S05", "carry reference TRIP-123456", issue="booking-twin"), "S05 a booking number shared by two bills")
check(has("HOLDCO", "S05", "card-charge bill", issue="card-charge-twin"), "S05 a card-charge bill and the supplier's own")
check(has("HOLDCO", "S06", "Suspense") and has("HOLDCO", "S06", "Card Clearing", ), "S06 suspense balance and a clearing residual")
check(any(f["check"] == "S06" and "month-end residual" in f["text"] for f in findings()),
      "S06 a configured clearing account is judged at month end")
check(has("HOLDCO", "S07", "no invoice number") and has("HOLDCO", "S07", "placeholder contact"), "S07 blank number and placeholder contact")
check(has("HOLDCO", "S08", "now on", issue="supplier-wrong-account"), "S08 supplier on an account it was not on in the baseline")
check(has("HOLDCO", "S08W", "Miscellaneous Costs (5700)"), "S08W an account outside the rules/EXPENSES.md table")
check(not has("OPCO_EU", "S08W"), "S08W skips an entity with no account-table column")
check(has("HOLDCO", "S08C", "expensed", issue="capitalisation-threshold") and has("HOLDCO", "S08C", "capitalised"),
      "S08C the capitalisation threshold, both directions")
check(has("HOLDCO", "S08S", "steady", issue="subscription-vs-it-costs"), "S08S a steady monthly amount on IT Costs")
check(has("HOLDCO", "S08T", "travel-shaped", issue="welfare-vs-travel"), "S08T travel on a welfare account")
check(has("HOLDCO", "S09", "spend money to", issue="wrong-transaction-type"), "S09 spend money to a supplier")
check(not any(f["check"] == "S09" and "Corner Cafe" in f["who"] for f in findings()), "S09 leaves small card spend out")
check(has("HOLDCO", "S09", "bill raised for bank fees"), "S09 a bill for bank fees")
check(has("HOLDCO", "S10", "tax is recognised only on GBP lines"), "S10 tax on a foreign-currency line, from [[review_checks.tax]]")
check(has("OPCO_US", "S10", "no recoverable tax"), "S10 tax in an entity configured with none")
check(has("OPCO_EU", "S11", "no Region") and has("OPCO_EU", "S11", "implausible Region"), "S11 tracking from [[review_checks.tracking]]")
check(has("OPCO_US", "S12", "sales invoice") and has("OPCO_US", "S12", "revenue account Sales"), "S12 revenue shape")
check(has("HOLDCO", "S13", "Q3 2026 describes Q4 2026", issue="wrong-quarter"), "S13 a bill naming another financial quarter")
check(not any(f["check"] == "S13" and f["who"] == "Booking Platform" for f in findings()), "S13 exempt accounts are left out")
check(has("HOLDCO", "S14", "Wages Payable (814) is not zero"), "S14 payroll control account with a balance")
check(has("HOLDCO", "S14", "bill cleared by journal is still open"), "S14 a journal-cleared bill still open")
check(has("HOLDCO", "S14M", "new account(s)", issue="payroll-consistency"), "S14M payroll mapping drift")
check(has("HOLDCO", "S15", "is not on an account matching"), "S15 routing from [[review_checks.routing]]")
check(has("HOLDCO", "S16", "expected archived") and has("HOLDCO", "S16", "carries a balance"), "S16 known-bad account status and balance")
check(has("HOLDCO", "S16P", "posts to Old Loan (1999)"), "S16P a posting to a known-bad account")
check(has("HOLDCO", "S17", "A. Person cost", issue="dated-cutover"), "S17 recognise_in cutover")
check(has("OPCO_EU", "S17", "coded off 2620") and has("HOLDCO", "S17", "no journal on 4000"), "S17 only_accounts cutover and its mirror")
check(has("HOLDCO", "S17", "on 4100 after 2026-08-01"), "S17 not_accounts cutover")
check(has("OPCO_US", "S17", "bill dated on or after 2026-08-01")
      and not any(f["check"] == "S17" and f["who"] == "Provider A Payroll" for f in findings()),
      "S17 no_bills cutover, with the excluded contact left out")
check(has("HOLDCO", "S18", "no document attached") and not any(f["check"] == "S18" and f["who"] == "Group Loan Desk" for f in findings()),
      "S18 a bill without a document, interco-coded exempt")
check(has("HOLDCO", "S19", "HOLDCO:OPCO_US:loan: BREAK (2 unmatched), break USD 120.50")
      and not has("OPCO_US", "S19"), "S19 reads the intercompany reconciliation, one line per pair")
check(not has("OPCO_EU", "S19"), "S19 leaves an agreed pair out")
check(has("HOLDCO", "S20", "off by 5.00") and has("HOLDCO", "S20", "not checked"), "S20 reads the bank reconcile check")
check(has("HOLDCO", "S21", "GL 1,500.00 against registered 1,000.00"), "S21 GL against the register")
check(has("HOLDCO", "S21", "1 DRAFT asset"), "S21 draft assets")
check(has("HOLDCO", "S21", "FA-0001 is on NoDepreciation") and has("HOLDCO", "S21", "manual depreciation journal"),
      "S21 register_depreciated assets and a manual journal")
check(all("check failed" not in f["text"] for f in findings()), "no check crashed")

# output: new, hidden, ids, show
code, out = run("holdco")
check("older in the window, not printed" in out, "event findings that are not new are counted, not printed")
check(" *  " in out or "*  " in out, "new event findings carry a star")
check("HOLDCO-S05-1" in findings()[0]["id"] or any(f["id"].startswith("HOLDCO-S05-") for f in findings()),
      "findings carry entity-check-number ids")
check("cache " in out and len(CREATED) == 3, "a second run the same day reads the cache, not Xero")
code, out = run("holdco", "--refresh")
check(len(CREATED) == 4, "--refresh pulls again")
fid = next(f["id"] for f in findings() if f["check"] == "S05")
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rc_code = rc.main(["show", fid])
check(rc_code == 0 and fid in buf.getvalue() and '"detail"' in buf.getvalue(), "show opens one finding")
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rc_code = rc.main(["show", "HOLDCO-S05"])
check(rc_code == 0 and buf.getvalue().count('"id"') >= 2, "show opens a whole check by prefix")
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rc_code = rc.main(["show", "HOLDCO-S99"])
check(rc_code == 1, "show of an unknown id exits 1")
check(json.loads((TMP / "data" / "review" / "checks" / "state.json").read_text())["HOLDCO"]["lock"] == "2026-06-30",
      "the lock dates seen are saved for the next run")
check((TMP / "data" / "review" / "cache" / f"HOLDCO-{TODAY}.json").exists(), "the pull is cached per day")

# --no-write and the rate limit
before = (TMP / "data" / "review" / "checks" / TODAY / "summary.txt").read_text()
code, out = run("opco-eu", "--no-write")
check(code == 0 and (TMP / "data" / "review" / "checks" / TODAY / "summary.txt").read_text() == before,
      "--no-write prints without writing")
DATA["OPCO_US"]["rate_limited"] = True
for p in (TMP / "data" / "review" / "cache").glob("OPCO_US-*"):
    p.unlink()
code, out = run("opco-us")
check(code == 3 and "daily rate limit" in out, "a 429 exits 3 and says so")
DATA["OPCO_US"]["rate_limited"] = False

# --cache-file reads one entity from a file
cache_file = TMP / "data" / "review" / "cache" / f"HOLDCO-{TODAY}.json"
before_calls = len(CREATED)
code, out = run("holdco", "--cache-file", f"holdco={cache_file}", "--no-write")
check(code == 0 and len(CREATED) == before_calls, "--cache-file reads a saved pull")

# ---------------------------------------------------------------- units

wl = rc.parse_whitelists(TMP / "rules" / "EXPENSES.md")
check(wl.get("HOLDCO") == {"5100", "5320", "5300", "5310", "5600", "5110", "6000"}, "whitelist: HOLDCO column of the account tables")
check("9999" not in wl.get("HOLDCO", set()), "whitelist: quoted illustrations are skipped")
check("OPCO_EU" not in wl and "6100" in wl["OPCO_US"], "whitelist: n/a and absent columns skipped")
placeholder = TMP / "placeholder.md"
placeholder.write_text("| Cost category | <entity key> |\n|---|---|\n| <cost> | <code> <name> |\n")
check(rc.parse_whitelists(placeholder) == {}, "whitelist: FILL IN placeholders yield nothing, the check skips")
check(rc.parse_whitelists(TMP / "missing.md") == {}, "whitelist: a missing file yields nothing")

qs = rc.mentioned_quarters("Hosting for November and Q1 2027", "2026-09-20")
check(qs == {(4, 2026), (1, 2027)}, "financial quarters named by month and by Qn")
check(rc.mentioned_quarters("Sep 30 payment", "2026-09-20") == {(3, 2026)}, "a number after a month is a day, not a year")
check(rc.narration_family("Payroll September 2026").startswith("journal: payroll"), "narration family drops the month")

# no [review_checks] section at all: the neutral checks still run
neutral = TMP / "neutral.toml"
neutral.write_text((REPO / "config" / "group.example.toml").read_text())
os.environ["AGENT_GROUP_CONFIG"] = str(neutral)
config.reload()
cfg = rc.Cfg()
check(cfg.cap == 500.0 and cfg.raw["cutovers"] == [] and cfg.twin_ref is None, "no section: defaults and empty rule tables")
ctxs = rc.run_checks({"HOLDCO": json.loads(cache_file.read_text())}, rc.windows(rc.dt.date.fromisoformat(TODAY), None), SINCE, {}, cfg)
fs = ctxs["HOLDCO"].findings
check(any(f["check"] == "S08C" for f in fs) and not any(f["check"] in ("S16", "S16P", "S17", "S10", "S15") for f in fs),
      "no section: neutral checks run, group rules do not")
check(all("check failed" not in f["text"] for f in fs), "no section: no check crashed")
os.environ["AGENT_GROUP_CONFIG"] = str(CFG)
config.reload()

print()
if FAILS:
    print(f"{len(FAILS)} FAILED")
    sys.exit(1)
print("all review_checks checks passed")
