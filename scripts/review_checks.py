#!/usr/bin/env python
"""The mechanical checks of the bookkeeping review, as one read-only script.

Skill: xero-review. A review used to start by writing its own harness; this is
that harness, committed. It pulls each entity once, runs every check the
skill's "Standard checks" list makes mechanical, and prints one line per
finding. The session reads the summary, opens only the findings it
investigates, and does the checks that need judgement itself.

WHAT IT PULLS, per entity, once (a cached pull from today is reused):

    Organisation, Accounts                       1 call each
    Trial Balance today + the last 3 month ends  4 calls
    Invoices (bills and sales) from the baseline 1 call per 1,000
    open Invoices dated before the baseline      1 call
    BankTransactions from the baseline           1 call per 1,000
    unreconciled BankTransactions before it      1 call
    ManualJournals from the baseline             1 call per 100
    DRAFT ManualJournals dated before it         1 call
    Contacts (for the AP/AR balances)            1 call per 1,000
    Fixed Assets API: AssetTypes and the REGISTERED, DRAFT and DISPOSED
    assets (200 a page)                          4 calls or so

About 15 to 25 calls an entity against the 5,000-a-day cap. No per-document
GET anywhere, and nothing but GET.

WINDOWS
    window    from the start of the financial quarter that held the day 30
              days ago (the quarter under review, or the one just closed),
              to today. Quarters follow [company] financial_year_end.
    baseline  the three months before the window: the supplier -> account
              map is built from it (check S08)
    new       a record updated since the last review started (read from
              data/logs/review-*.log, or --since). Event findings that are not
              new are counted, not printed; --all prints them too.

CHECK IDS follow the skill's numbered "Standard checks" (S01 .. S21), with S00
the lock date. Letters after a number are sub-checks (S08W the account
whitelist, S08C capitalisation). Checks the script does not attempt are listed
in the skill. Every group-specific rule comes from [review_checks] in
config/group.toml or from rules/EXPENSES.md (the account tables are the
whitelist); with neither, the group-neutral checks still run.

OUTPUT
    stdout                                         the compact summary
    data/review/checks/<date>/summary.txt          the same
    data/review/checks/<date>/findings.json        every finding with its detail
    data/review/checks/state.json                  the lock dates last seen
    data/review/cache/<ENTITY>-<date>.json         the raw pull, for follow-up
                                                   reads without more API calls

Usage:
    .venv/bin/python scripts/review_checks.py all
    .venv/bin/python scripts/review_checks.py opco-us [--since 2026-10-04T10:00]
                     [--from 2026-07-01] [--all] [--refresh] [--no-write]
    .venv/bin/python scripts/review_checks.py show OPCO_US-S05-2     one finding
    .venv/bin/python scripts/review_checks.py show OPCO_US-S05       one check

Entities: any key, slug, alias or short name in config/group.toml, or all.

READ-ONLY against Xero, always. A daily rate limit (429) stops the pull of that
entity and exits 3, so the run can say so rather than work around it.
`AGENT_ROOT` points the script at a repo other than its own.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import statistics
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = (Path(os.environ["AGENT_ROOT"]).resolve() if os.environ.get("AGENT_ROOT")
        else Path(__file__).resolve().parents[1])
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from accounting_agent import config  # noqa: E402
from interco_recon import _d, to_base  # noqa: E402

OUT = ROOT / "data" / "review"
CACHE = OUT / "cache"
CHECKS = OUT / "checks"
EXPENSES_MD = ROOT / "rules" / "EXPENSES.md"
INTERCO_LATEST = ROOT / "data" / "interco" / "latest.json"
UNRECONCILED = ROOT / "data" / "bankfeed" / "unreconciled.json"
LOGS = ROOT / "data" / "logs"
ASSETS_URL = "https://api.xero.com/assets.xro/1.0/"
PULL_VERSION = 1

# Tests put a fake client factory here; None means the real XeroClient.
CLIENT_FACTORY = None

# Checks describing a standing position rather than a document event: always
# printed, new or not.
STATE_CHECKS = {"S00", "S01", "S02", "S03", "S04D", "S06", "S14", "S16", "S19", "S20", "S21"}

CHECK_NAMES = {
    "S00": "lock date", "S01": "trial balance sanity", "S02": "unreconciled bank transactions",
    "S03": "aged payables/receivables", "S04D": "drafts", "S04V": "voided journals",
    "S04P": "journals posted or edited", "S05": "duplicate-looking documents",
    "S06": "suspense / clearing balances", "S06L": "lines on suspense / clearing",
    "S07": "blank references", "S08": "supplier -> account", "S08W": "off the account whitelist",
    "S08C": "capitalisation threshold", "S08S": "subscriptions vs IT costs",
    "S08T": "travel on a welfare account", "S09": "transaction type", "S10": "sales tax",
    "S11": "tracking", "S12": "revenue shape", "S13": "quarter boundary", "S14": "payroll controls",
    "S14M": "payroll mapping drift", "S15": "per-entity account routing",
    "S16": "known-bad / archived accounts", "S16P": "postings to known-bad accounts",
    "S17": "dated cutovers", "S18": "bills without a document", "S19": "intercompany",
    "S20": "bank truth", "S21": "GL vs fixed asset register",
}

FXRE = re.compile(r"gain|loss|revaluation|currency|exchange|\bfx\b|rounding", re.I)
SUSPENSE_NAME = re.compile(r"suspense|clearing|unknown|ask my|uncategori", re.I)
SMALL_SPEND = ("cafe|caf\u00e9|coffee|restaurant|takeaway|pizza|bakery|bistro|kitchen|grill|sushi|burger|"
               r"deli\b|supermarket|canteen|lunch|taxi|ride share|parking|fuel|petrol|\bbar\b|\bpub\b")
INVOICELESS = (r"bank fee|plan fee|card fee|fx fee|interest|conversion|exchanged? to|transfer|"
               r"bank charges?")
TRAVEL = (r"hotel|lodging|airbnb|flight|airline|airways|train ticket|railway|\btaxi\b|car hire|ferry|"
          r"ride share")
HARDWARE = (r"monitor|laptop|notebook|desktop|tablet|phone|\bdesk\b|chair|server|screen|display|printer|"
            r"furniture|keyboard|workstation|\bgpu\b|camera|television|\btv\b")
SOFTWAREISH = re.compile(r"licen[cs]e|subscription|saas|cloud|hosting|support|renewal|annual|"
                         r"monthly|plan\b|seat", re.I)
PAYROLL_NARRATION = re.compile(r"payroll|salar|pay ?run|wages|payslip", re.I)
PLACEHOLDER_CONTACT = re.compile(r"^\W*$|unknown|placeholder|\btest\b|no name|\btbc\b|\btbd\b|^n/?a$", re.I)
EXCEPTION_ACCOUNT_NAME = re.compile(
    r"bank fee|interest|\bfx\b|currency|exchange|suspense|wages|paye|nic\b|pension|social security|"
    r"vat|gst|tva|payroll|lease|right of use|deposit|tax", re.I)


def _alt(words, base: str = "") -> re.Pattern:
    """One case-insensitive pattern from the built-in words and the group's own."""
    parts = ([base] if base else []) + [str(w) for w in words or []]
    return re.compile("|".join(f"(?:{p})" for p in parts) if parts else r"(?!x)x", re.I)


class Cfg:
    """[review_checks] with its patterns compiled, built once per run."""

    def __init__(self, raw: dict | None = None):
        r = raw if raw is not None else config.review_checks()
        self.raw = r
        self.cap = float(r["capitalisation_threshold"])
        self.not_dup = _alt(r["not_duplicates"])
        self.twin_ref = re.compile(r["twin_reference_pattern"], re.I) if r["twin_reference_pattern"] else None
        self.card_ref = re.compile(r["card_charge_number_pattern"], re.I) if r["card_charge_number_pattern"] else None
        self.small = _alt(r["small_spend_words"], SMALL_SPEND)
        self.invoiceless = _alt(r["invoiceless_words"], INVOICELESS)
        self.travel = _alt(r["travel_words"], TRAVEL)
        self.hardware = _alt(r["hardware_words"], HARDWARE)
        self.quarter_suppliers = _alt(r["quarter_exempt_suppliers"])
        self.quarter_accounts = _alt(r["quarter_exempt_accounts"])
        self.capitalisable = [re.compile(p, re.I) for p in r["capitalisable_accounts"]]
        self.subscription = [re.compile(p, re.I) for p in r["subscription_accounts"]]
        self.welfare = _alt(r["welfare_accounts"])
        self.payroll_sources = _alt(r["payroll_sources"])
        self.num = {k: r[k] for k in (
            "movement_ratio", "movement_min", "suspense_threshold", "drift_min", "spend_line_min",
            "spend_money_min", "welfare_small", "subscription_max", "round_balance_min", "far_tolerance",
            "aged_days", "stale_journal_days", "stale_bill_draft_days", "unreconciled_old_days")}

    def table(self, name: str, entity: str | None = None) -> list[dict]:
        return [row for row in self.raw[name] if entity is None or row.get("entity") in (None, entity)]


# ---------------------------------------------------------------- the group

def order() -> list[str]:
    return config.entity_keys()


def base_of(key: str) -> str:
    return config.entity(key).base_currency


def short_of(key: str) -> str:
    return config.entity(key).short


def feed_key(key: str) -> str:
    return config.entity(key).slug


def interco_codes(key: str) -> set[str]:
    return {p.account(key) for p in config.intercompany_pairs(key) if p.account(key)}


def _interco_name() -> re.Pattern:
    pat = config.intercompany_settings().get("account_name_pattern") or "intercompany|interco"
    return re.compile(pat + r"|\bloan\b", re.I)


# Reporting-currency value of one unit of each currency, filled from the last
# intercompany run and [intercompany_settings] default_rates; a currency with
# no rate is counted at 1.0 and named in the summary.
RATES: dict[str, float] = {}
MISSING_RATES: set[str] = set()


def load_rates() -> None:
    RATES.clear()
    MISSING_RATES.clear()
    for k, v in (config.section("intercompany_settings").get("default_rates") or {}).items():
        RATES[str(k).upper()] = float(v)
    try:
        r = json.loads(INTERCO_LATEST.read_text()).get("rates") or {}
        RATES.update({str(k).upper(): float(v) for k, v in r.items() if v})
    except (OSError, ValueError):
        pass
    RATES[config.reporting_currency()] = 1.0


def rep(amount: float, ccy: str) -> float:
    """`amount` in `ccy`, in the reporting currency."""
    ccy = (ccy or config.reporting_currency()).upper()
    if ccy not in RATES:
        MISSING_RATES.add(ccy)
        return amount
    return amount * RATES[ccy]


# ---------------------------------------------------------------- helpers

def _num(v) -> float:
    try:
        return float(str(v).replace(",", "")) if v not in (None, "") else 0.0
    except ValueError:
        return 0.0


def _dt_expr(iso: str) -> str:
    y, m, d = (int(p) for p in iso.split("-"))
    return f"DateTime({y},{m},{d})"


def _upd(x: dict) -> str:
    """UpdatedDateUTC as 'YYYY-MM-DDTHH:MM', '' when absent."""
    v = x.get("UpdatedDateUTC")
    m = re.search(r"/Date\((-?\d+)", str(v or ""))
    if m:
        return dt.datetime.fromtimestamp(int(m.group(1)) / 1000, dt.UTC).strftime("%Y-%m-%dT%H:%M")
    s = x.get("UpdatedDateUTCString") or (v if isinstance(v, str) else "")
    return str(s)[:16]


def doc_date(x: dict) -> str:
    return (x.get("DateString") or "")[:10] or _d(x.get("Date"))


def contact(x: dict) -> str:
    return ((x.get("Contact") or {}).get("Name") or "").strip()


def lines(x: dict) -> list[dict]:
    return x.get("LineItems") or x.get("JournalLines") or []


def live_doc(x: dict) -> bool:
    return x.get("Status") not in ("VOIDED", "DELETED")


def money(ccy: str, amount: float) -> str:
    return f"{ccy} {amount:,.2f}"


def add_months(day: dt.date, n: int) -> dt.date:
    m = day.month - 1 + n
    return dt.date(day.year + m // 12, m % 12 + 1, 1)


def month_ends(today: dt.date, n: int = 3) -> list[str]:
    """The last n month ends before today, oldest first."""
    out, d = [], today.replace(day=1) - dt.timedelta(days=1)
    for _ in range(n):
        out.append(d.isoformat())
        d = d.replace(day=1) - dt.timedelta(days=1)
    return list(reversed(out))


def fin_quarter(iso: str) -> tuple[int, int]:
    """(quarter 1-4, the calendar year the financial year ends in)."""
    return config.fy_quarter_label(dt.date.fromisoformat(iso))


# ---------------------------------------------------------------- pull

class _Counting:
    """Wraps a Xero client and counts every request it sends."""

    def __init__(self, entity_key: str):
        if CLIENT_FACTORY is not None:
            self.c = CLIENT_FACTORY(entity_key)
        else:
            from accounting_agent.xero import client_for
            self.c = client_for(entity_key)
        self.calls = 0
        if hasattr(self.c, "request"):
            orig = self.c.request

            def counted(*a, **k):
                self.calls += 1
                return orig(*a, **k)
            self.c.request = counted

    def assets(self, path: str) -> dict:
        """GET on the Fixed Assets API (the Xero client targets the accounting API)."""
        if hasattr(self.c, "assets_get"):
            self.calls += 1
            return self.c.assets_get(path)
        from accounting_agent import xero_auth
        for attempt in range(3):
            self.calls += 1
            req = urllib.request.Request(ASSETS_URL + path, method="GET", headers={
                "Authorization": f"Bearer {xero_auth.get_access_token()}",
                "xero-tenant-id": self.c.tenant_id, "Accept": "application/json"})
            try:
                with xero_auth._urlopen(req) as r:
                    return json.loads(r.read() or b"{}")
            except urllib.error.HTTPError as e:
                body = e.read().decode(errors="replace")[:300]
                if e.code == 429 and attempt < 2:
                    time.sleep(min(int(e.headers.get("Retry-After", "5")) + 1, 65))
                    continue
                return {"__error__": f"{e.code} {body}"}
        return {"__error__": "429 after retries"}


def pull_far(cc: _Counting) -> dict:
    types = cc.assets("AssetTypes")
    if not isinstance(types, list):
        return {"error": (types or {}).get("__error__") or f"AssetTypes returned {type(types).__name__}"}
    out = {"types": types or [], "assets": {}}
    if not types:
        return out
    for status in ("REGISTERED", "DRAFT", "DISPOSED"):
        items, page = [], 1
        while True:
            d = cc.assets(f"Assets?status={status}&page={page}&pageSize=200")
            if d.get("__error__"):
                out["error"] = d["__error__"]
                break
            items += d.get("items") or []
            if page >= (d.get("pagination") or {}).get("pageCount", 1):
                break
            page += 1
        out["assets"][status] = items
    return out


def pull(key: str, win: dict) -> dict:
    from accounting_agent.xero.reports import report_to_rows, trial_balance
    cc = _Counting(key)
    c = cc.c
    base = _dt_expr(win["baseline_from"])
    out = {"v": PULL_VERSION, "entity": key,
           "pulled": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M"), "window": win}
    out["org"] = c.get("Organisation")["Organisations"][0]
    out["accounts"] = c.get("Accounts").get("Accounts", [])
    out["tb"] = {d: report_to_rows(trial_balance(c, date=d)) for d in win["tb_dates"]}
    out["invoices"] = c.get_all("Invoices", where=f"Date>={base}", order="Date ASC")
    out["invoices_open_old"] = c.get_all("Invoices", where=f'Status=="AUTHORISED" AND Date<{base}')
    out["bank_txns"] = c.get_all("BankTransactions", where=f"Date>={base}")
    out["bank_unrec_old"] = c.get_all(
        "BankTransactions", where=f'Status=="AUTHORISED" AND IsReconciled==false AND Date<{base}')
    out["mj"] = c.get_all("ManualJournals", page_size=100, where=f"Date>={base}")
    out["mj_draft_old"] = c.get_all("ManualJournals", page_size=100, where=f'Status=="DRAFT" AND Date<{base}')
    out["contacts"] = [{"ContactID": ct.get("ContactID"), "Name": ct.get("Name"), "Balances": ct.get("Balances")}
                       for ct in c.get_all("Contacts")
                       if any(_num((ct.get("Balances") or {}).get(s, {}).get("Outstanding"))
                              for s in ("AccountsPayable", "AccountsReceivable"))]
    out["far"] = pull_far(cc)
    out["calls"] = cc.calls
    return out


def cache_path(key: str, today: str) -> Path:
    return CACHE / f"{key}-{today}.json"


def load_or_pull(key: str, win: dict, refresh: bool, write: bool, cache_file: str | None) -> dict:
    p = Path(cache_file) if cache_file else cache_path(key, win["today"])
    if (cache_file or not refresh) and p.exists():
        d = json.loads(p.read_text())
        if d.get("v") == PULL_VERSION:
            d["from_cache"] = str(p)
            return d
        if cache_file:
            raise ValueError(f"{p} is not a review_checks pull (v {PULL_VERSION})")
    d = pull(key, win)
    if write:
        CACHE.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(d, default=str))
    return d


# ---------------------------------------------------------------- context

class Ctx:
    """One entity's pulled data, indexed for the checks."""

    def __init__(self, key: str, data: dict, win: dict, since: str, cfg: Cfg):
        self.key, self.d, self.win, self.since, self.cfg = key, data, win, since, cfg
        self.base = base_of(key)
        self.today = win["today"]
        self.accounts = data.get("accounts") or []
        self.by_code = {a["Code"]: a for a in self.accounts if a.get("Code")}
        self.by_id = {a["AccountID"]: a for a in self.accounts if a.get("AccountID")}
        inv = data.get("invoices") or []
        self.bills_all = [b for b in inv if b.get("Type") == "ACCPAY"]
        self.sales_all = [b for b in inv if b.get("Type") == "ACCREC"]
        self.bills = [b for b in self.bills_all if live_doc(b)]
        self.sales = [b for b in self.sales_all if live_doc(b)]
        self.bank = [t for t in (data.get("bank_txns") or []) if t.get("Status") == "AUTHORISED"]
        self.mj = data.get("mj") or []
        self.posted = [j for j in self.mj if j.get("Status") == "POSTED"]
        self.ic_codes = interco_codes(key)
        self.ic_name = _interco_name()
        self.findings: list[dict] = []
        self.lock = ("", "")
        # A record edited since the last review is reviewed whatever its date:
        # a correction journal dated back a quarter is still this week's work.
        self.wbills = [b for b in self.bills if doc_date(b) >= win["from"] or self.new(b)]
        self.wbank = [t for t in self.bank if doc_date(t) >= win["from"] or self.new(t)]
        self.wposted = [j for j in self.posted if doc_date(j) >= win["from"] or self.new(j)]

    # account helpers
    def acct(self, line: dict) -> dict:
        return self.by_code.get(str(line.get("AccountCode") or "")) or self.by_id.get(line.get("AccountID") or "") or {}

    def aname(self, code: str) -> str:
        a = self.by_code.get(code)
        return f"{a['Name']} ({code})" if a else f"({code})"

    def is_pl(self, a: dict) -> bool:
        return a.get("Class") in ("EXPENSE", "REVENUE")

    def is_interco(self, a: dict) -> bool:
        return a.get("Code") in self.ic_codes or bool(self.ic_name.search(a.get("Name") or ""))

    def new(self, x: dict) -> bool:
        return _upd(x) >= self.since

    def tb_rows(self, date: str) -> list[dict]:
        return [r for r in (self.d.get("tb") or {}).get(date) or [] if r.get("RowType") == "Row"]

    def tb_balance(self, date: str) -> dict[str, float]:
        """AccountID -> cumulative balance (debit positive) at date."""
        out = {}
        for r in self.tb_rows(date):
            if r.get("AccountID"):
                if "YTD Debit" in r or "YTD Credit" in r:
                    out[r["AccountID"]] = round(_num(r.get("YTD Debit")) - _num(r.get("YTD Credit")), 2)
                else:
                    out[r["AccountID"]] = round(_num(r.get("Debit")) - _num(r.get("Credit")), 2)
        return out

    def tb_movement(self, date: str) -> dict[str, float]:
        """AccountID -> movement in date's month (the TB's period columns)."""
        return {r["AccountID"]: round(_num(r.get("Debit")) - _num(r.get("Credit")), 2)
                for r in self.tb_rows(date) if r.get("AccountID")}

    def add(self, check: str, text: str, *, date: str = "", who: str = "", ccy: str = "",
            amount: float | None = None, refs: list[str] | None = None, new: bool = False,
            issue: str = "", detail: dict | None = None) -> None:
        self.findings.append({
            "entity": self.key, "check": check, "issue": issue, "date": date, "who": who,
            "ccy": ccy, "amount": None if amount is None else round(amount, 2), "text": text,
            "refs": [r for r in (refs or []) if r], "new": bool(new) or check in STATE_CHECKS,
            "detail": detail or {}})

    def docs(self, window: bool = True):
        """(kind, doc) over bills, bank transactions and posted journals."""
        for b in (self.wbills if window else self.bills):
            yield "BILL", b
        for t in (self.wbank if window else self.bank):
            yield "BANK", t
        for j in (self.wposted if window else self.posted):
            yield "MJ", j


def brief(x: dict) -> dict:
    """The fields of a document worth keeping in the detail file."""
    keep = ("InvoiceID", "BankTransactionID", "ManualJournalID", "InvoiceNumber", "Reference", "Type",
            "Status", "CurrencyCode", "CurrencyRate", "Total", "AmountDue", "HasAttachments",
            "IsReconciled", "Narration")
    out = {k: x[k] for k in keep if k in x}
    out["date"] = doc_date(x)
    out["contact"] = contact(x)
    out["lines"] = [{k: ln.get(k) for k in ("Description", "AccountCode", "LineAmount", "TaxType", "TaxAmount",
                                            "Tracking") if k in ln} for ln in lines(x)]
    return out


def ref_of(x: dict) -> str:
    return (x.get("InvoiceNumber") or x.get("Reference") or (x.get("Narration") or "")[:50]
            or (x.get("InvoiceID") or x.get("BankTransactionID") or x.get("ManualJournalID") or "")[:8])


def total(x: dict) -> float:
    if "Total" in x:
        return _num(x.get("Total"))
    return sum(_num(ln.get("LineAmount")) for ln in lines(x) if _num(ln.get("LineAmount")) > 0)


def doc_ccy(ctx: Ctx, x: dict) -> str:
    return x.get("CurrencyCode") or ctx.base


def in_base(x: dict, amount) -> float:
    return to_base(_num(amount), x.get("CurrencyRate"))


# ---------------------------------------------------------------- checks

def s00_lock(ctx: Ctx, state: dict) -> None:
    org = ctx.d.get("org") or {}
    lock, eoy = _d(org.get("PeriodLockDate")), _d(org.get("EndOfYearLockDate"))
    ctx.lock = (lock, eoy)
    prev = state.get(ctx.key) or {}
    if prev and (prev.get("lock"), prev.get("eoy")) != (lock, eoy):
        ctx.add("S00", f"lock date moved: period lock {prev.get('lock') or 'none'} -> {lock or 'none'}, "
                       f"end of year {prev.get('eoy') or 'none'} -> {eoy or 'none'} (last seen {prev.get('seen')})",
                date=ctx.today, detail={"previous": prev})


def s01_tb(ctx: Ctx) -> None:
    n = ctx.cfg.num
    bal = ctx.tb_balance(ctx.today)
    for aid, b in bal.items():
        a = ctx.by_id.get(aid) or {}
        name = f"{a.get('Name')} ({a.get('Code')})" if a.get("Code") else a.get("Name", aid)
        if a.get("Type") == "BANK" and a.get("BankAccountType") != "CREDITCARD" and b < -0.01:
            ctx.add("S01", f"negative bank balance on {name}", ccy=ctx.base, amount=b, date=ctx.today)
        elif a.get("Class") == "EXPENSE" and b < -0.01 and not FXRE.search(a.get("Name") or ""):
            ctx.add("S01", f"credit balance on expense account {name}", ccy=ctx.base, amount=b, date=ctx.today)
        elif a.get("Class") == "REVENUE" and b > 0.01 and not FXRE.search(a.get("Name") or ""):
            ctx.add("S01", f"debit balance on revenue account {name}", ccy=ctx.base, amount=b, date=ctx.today)
    ends = ctx.win["month_ends"]
    if len(ends) < 3:
        return
    mv = [ctx.tb_movement(e) for e in ends]          # oldest first
    for aid in set(mv[-1]):
        a = ctx.by_id.get(aid) or {}
        if not ctx.is_pl(a) or FXRE.search(a.get("Name") or ""):
            continue
        last, prior = mv[-1].get(aid, 0.0), [abs(m.get(aid, 0.0)) for m in mv[:-1]]
        avg = statistics.mean(prior)
        if (abs(last) > n["movement_ratio"] * max(avg, 0.01)
                and rep(abs(abs(last) - avg), ctx.base) >= n["movement_min"]):
            ctx.add("S01", f"{a.get('Name')} ({a.get('Code')}) moved {last:,.2f} in the month to {ends[-1]} "
                           f"against {', '.join(f'{m.get(aid, 0.0):,.2f}' for m in mv[:-1])} in the two before",
                    ccy=ctx.base, amount=last, date=ends[-1])


def s02_unreconciled(ctx: Ctx) -> None:
    days = ctx.cfg.num["unreconciled_old_days"]
    rows = [t for t in ctx.bank if not t.get("IsReconciled")] + list(ctx.d.get("bank_unrec_old") or [])
    cutoff = (dt.date.fromisoformat(ctx.today) - dt.timedelta(days=days)).isoformat()
    by = defaultdict(list)
    for t in rows:
        by[(t.get("BankAccount") or {}).get("Name") or "?"].append(t)

    def sign(t):
        return -_num(t.get("Total")) if str(t.get("Type", "")).startswith("SPEND") else _num(t.get("Total"))

    for acct, ts in sorted(by.items()):
        old = sorted((t for t in ts if doc_date(t) < cutoff), key=doc_date)
        ccy = ts[0].get("CurrencyCode") or ctx.base
        ctx.add("S02", f"{acct}: {len(ts)} unreconciled, net {money(ccy, sum(sign(t) for t in ts))}, "
                       f"{len(old)} older than {days} days", date=ctx.today,
                detail={"items": [brief(t) for t in sorted(ts, key=doc_date)]})
        for t in old:
            ctx.add("S02", f"unreconciled {t.get('Type')} on {acct}, older than {days} days",
                    date=doc_date(t), who=contact(t), ccy=ccy, amount=sign(t), refs=[ref_of(t)],
                    detail=brief(t))


def s03_aged(ctx: Ctx) -> None:
    n = ctx.cfg.num
    for ct in ctx.d.get("contacts") or []:
        b = ct.get("Balances") or {}
        ap = _num((b.get("AccountsPayable") or {}).get("Outstanding"))
        ar = _num((b.get("AccountsReceivable") or {}).get("Outstanding"))
        if ap < -0.005:
            ctx.add("S03", "negative payable balance (an overpayment or credit left unallocated)",
                    who=ct.get("Name"), ccy=ctx.base, amount=ap)
        if ar < -0.005:
            ctx.add("S03", "negative receivable balance (credit left unallocated)", who=ct.get("Name"),
                    ccy=ctx.base, amount=ar)
        for side, v in (("payable", ap), ("receivable", ar)):
            if abs(v) >= n["round_balance_min"] and abs(v) % 1000 < 0.005:
                ctx.add("S03", f"round-number {side} balance", who=ct.get("Name"), ccy=ctx.base, amount=v)
    cutoff = (dt.date.fromisoformat(ctx.today) - dt.timedelta(days=int(n["aged_days"]))).isoformat()
    opened = [x for x in ctx.bills_all + ctx.sales_all + list(ctx.d.get("invoices_open_old") or [])
              if x.get("Status") == "AUTHORISED" and _num(x.get("AmountDue")) > 0.005]
    seen = set()
    for x in sorted(opened, key=doc_date):
        if x.get("InvoiceID") in seen or doc_date(x) >= cutoff:
            continue
        seen.add(x.get("InvoiceID"))
        kind = "bill" if x.get("Type") == "ACCPAY" else "sales invoice"
        ctx.add("S03", f"open {kind} older than {int(n['aged_days'])} days", date=doc_date(x), who=contact(x),
                ccy=doc_ccy(ctx, x), amount=_num(x.get("AmountDue")), refs=[ref_of(x)], detail=brief(x))


MONTHS = {m: i + 1 for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep",
                                           "oct", "nov", "dec"])}
MONTH_RE = re.compile(r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
                      r"sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b[\s\-'.,]*(\d{4}|\d{2}\b)?", re.I)
QTR_RE = re.compile(r"\bQ([1-4])\b[\s\-']*(\d{4}|\d{2}\b)?", re.I)


def narration_family(text: str) -> str:
    """A journal narration with its dates taken out, so one month's payroll
    journal groups with the same journal in other months."""
    t = MONTH_RE.sub(" ", text.lower())
    t = re.sub(r"\b(may|q[1-4])\b|[\d/.\-]+", " ", t)
    return "journal: " + " ".join(t.split())[:60]


def s04_journals(ctx: Ctx) -> None:
    n = ctx.cfg.num
    today = dt.date.fromisoformat(ctx.today)
    drafts = [j for j in ctx.mj if j.get("Status") == "DRAFT"] + list(ctx.d.get("mj_draft_old") or [])
    for j in sorted(drafts, key=doc_date):
        upd = _upd(j)[:10] or doc_date(j)
        age = (today - dt.date.fromisoformat(upd)).days if upd else 0
        ctx.add("S04D", f"DRAFT journal, last touched {age} days ago"
                        + (" (stale)" if age > n["stale_journal_days"] else ""),
                date=doc_date(j), refs=[(j.get("Narration") or "")[:70]], detail=brief(j))
    for b in sorted((b for b in ctx.bills_all if b.get("Status") in ("DRAFT", "SUBMITTED")), key=doc_date):
        upd = _upd(b)[:10] or doc_date(b)
        age = (today - dt.date.fromisoformat(upd)).days if upd else 0
        if age > n["stale_bill_draft_days"]:
            ctx.add("S04D", f"{b.get('Status')} bill, last touched {age} days ago", date=doc_date(b),
                    who=contact(b), ccy=doc_ccy(ctx, b), amount=_num(b.get("Total")), refs=[ref_of(b)],
                    detail=brief(b))
    fams = defaultdict(list)
    for j in ctx.mj:
        if not ctx.new(j):
            continue
        if j.get("Status") == "VOIDED":
            ctx.add("S04V", "journal voided since the last review", date=doc_date(j), new=True,
                    refs=[(j.get("Narration") or "")[:70]], detail=brief(j))
        elif j.get("Status") == "POSTED":
            fams[narration_family(j.get("Narration") or "")].append(j)
    for js in fams.values():
        js.sort(key=doc_date)
        payroll = [j for j in js if PAYROLL_NARRATION.search(j.get("Narration") or "") and not j.get("HasAttachments")]
        note = f"; {len(payroll)} payroll journal(s) without the payroll report attached" if payroll else ""
        span = doc_date(js[0]) if len(js) == 1 else f"{doc_date(js[0])}..{doc_date(js[-1])}"
        ctx.add("S04P", (f"{len(js)} journals " if len(js) > 1 else "journal ") + f"posted or edited since the last "
                f"review, dated {span}{note}", date=doc_date(js[-1]), new=True, ccy=ctx.base,
                amount=sum(total(j) for j in js), refs=[(js[0].get("Narration") or "")[:70]],
                detail={"journals": [brief(j) for j in js]})


def s05_duplicates(ctx: Ctx) -> None:
    cfg = ctx.cfg

    def group(docs, label):
        g = defaultdict(list)
        for b in docs:
            g[((b.get("Contact") or {}).get("ContactID"), round(_num(b.get("Total")), 2), doc_date(b))].append(b)
        for (_, tot, day), v in g.items():
            if len(v) < 2:
                continue
            nums = [x.get("InvoiceNumber") or "" for x in v]
            if cfg.not_dup.search(contact(v[0])) and len(set(nums)) == len(nums):
                continue
            tag = ("booking-twin" if cfg.twin_ref and any(cfg.twin_ref.search(n) for n in nums) else
                   "card-charge-twin" if cfg.card_ref and any(cfg.card_ref.match(n) for n in nums) else "")
            ctx.add("S05", f"{len(v)} live {label} with the same contact, total and date",
                    date=day, who=contact(v[0]), ccy=doc_ccy(ctx, v[0]), amount=tot, refs=nums, issue=tag,
                    new=any(ctx.new(x) for x in v), detail={"docs": [brief(x) for x in v]})
    group(ctx.wbills, "bills")
    group([s for s in ctx.sales if doc_date(s) >= ctx.win["from"]], "sales invoices")

    by_num = defaultdict(list)
    for b in ctx.bills:
        num = (b.get("InvoiceNumber") or "").strip().lower()
        if num:
            by_num[((b.get("Contact") or {}).get("ContactID"), num)].append(b)
    for v in by_num.values():
        if len(v) > 1 and any(doc_date(x) >= ctx.win["from"] for x in v):
            ctx.add("S05", f"{len(v)} live bills from one contact with the same invoice number",
                    date=doc_date(v[-1]), who=contact(v[0]), ccy=doc_ccy(ctx, v[0]), amount=_num(v[-1].get("Total")),
                    refs=[v[0].get("InvoiceNumber")], new=any(ctx.new(x) for x in v),
                    detail={"docs": [brief(x) for x in v]})

    # A booking platform's invoice and the merchant's own share one reference
    if cfg.twin_ref:
        by_ref = defaultdict(list)
        for b in ctx.bills:
            text = " ".join([b.get("InvoiceNumber") or "", b.get("Reference") or ""]
                            + [ln.get("Description") or "" for ln in lines(b)])
            for ref in {m.upper() for m in cfg.twin_ref.findall(text)}:
                by_ref[ref].append(b)
        for ref, v in by_ref.items():
            ids = {x.get("InvoiceID") for x in v}
            if len(ids) > 1 and any(doc_date(x) >= ctx.win["from"] for x in v):
                ctx.add("S05", f"{len(ids)} live bills carry reference {ref}; open both documents",
                        issue="booking-twin", date=doc_date(v[-1]),
                        who=" / ".join(sorted({contact(x) for x in v})), ccy=doc_ccy(ctx, v[0]),
                        amount=_num(v[0].get("Total")), refs=[x.get("InvoiceNumber") for x in v],
                        new=any(ctx.new(x) for x in v), detail={"docs": [brief(x) for x in v]})

    # A card-charge bill plus the supplier's own bill for the same cost
    if cfg.card_ref:
        rb = [b for b in ctx.wbills if cfg.card_ref.match(b.get("InvoiceNumber") or "")]
        others = [b for b in ctx.bills if not cfg.card_ref.match(b.get("InvoiceNumber") or "")]
        for r in rb:
            rd = dt.date.fromisoformat(doc_date(r))
            twins = [o for o in others
                     if (o.get("Contact") or {}).get("ContactID") == (r.get("Contact") or {}).get("ContactID")
                     and abs(_num(o.get("Total")) - _num(r.get("Total"))) < 0.005
                     and abs((dt.date.fromisoformat(doc_date(o)) - rd).days) <= 45]
            if twins and doc_date(r) != doc_date(twins[0]):
                ctx.add("S05", "card-charge bill and a supplier-numbered bill for the same amount; open both documents",
                        issue="card-charge-twin", date=doc_date(r), who=contact(r), ccy=doc_ccy(ctx, r),
                        amount=_num(r.get("Total")),
                        refs=[r.get("InvoiceNumber")] + [t.get("InvoiceNumber") for t in twins],
                        new=ctx.new(r) or any(ctx.new(t) for t in twins),
                        detail={"docs": [brief(r)] + [brief(t) for t in twins]})


def suspense_accounts(ctx: Ctx) -> list[dict]:
    return [a for a in ctx.accounts if SUSPENSE_NAME.search(a.get("Name") or "") and a.get("Status") != "ARCHIVED"]


def clearing_codes(ctx: Ctx) -> set[str]:
    return {str(r["code"]) for r in ctx.cfg.table("clearing_accounts", ctx.key)}


def s06_suspense(ctx: Ctx) -> None:
    limit = ctx.cfg.num["suspense_threshold"]
    dates = ctx.win["month_ends"] + [ctx.today]
    bals = {d: ctx.tb_balance(d) for d in dates}
    clearing = clearing_codes(ctx)
    for a in suspense_accounts(ctx):
        series = {d: bals[d].get(a["AccountID"], 0.0) for d in dates}
        name = f"{a['Name']} ({a.get('Code')})"
        txt = ", ".join(f"{d[5:]} {v:,.2f}" for d, v in series.items())
        if a.get("Code") in clearing:
            last_end = ctx.win["month_ends"][-1]
            if abs(series[last_end]) > limit:
                ctx.add("S06", f"{name} clearing account left a month-end residual: {txt}", ccy=ctx.base,
                        amount=series[last_end], date=last_end, detail={"series": series})
        elif abs(series[ctx.today]) > limit:
            ctx.add("S06", f"{name} holds a balance: {txt}", ccy=ctx.base, amount=series[ctx.today],
                    date=ctx.today, detail={"series": series})
    codes = {a.get("Code") for a in suspense_accounts(ctx) if a.get("Code") not in clearing}
    codes.discard(None)
    for kind, x in ctx.docs():
        hit = [ln for ln in lines(x) if str(ln.get("AccountCode") or "") in codes]
        if hit:
            ctx.add("S06L", f"{kind} line(s) coded to {', '.join(sorted({ctx.aname(str(ln['AccountCode'])) for ln in hit}))}",
                    date=doc_date(x), who=contact(x), ccy=doc_ccy(ctx, x),
                    amount=sum(_num(ln.get("LineAmount")) for ln in hit), refs=[ref_of(x)], new=ctx.new(x),
                    detail=brief(x))


def s07_blanks(ctx: Ctx) -> None:
    for b in ctx.wbills:
        if not (b.get("InvoiceNumber") or "").strip():
            ctx.add("S07", "bill with no invoice number", date=doc_date(b), who=contact(b), ccy=doc_ccy(ctx, b),
                    amount=_num(b.get("Total")), new=ctx.new(b), detail=brief(b))
        if PLACEHOLDER_CONTACT.search(contact(b)):
            ctx.add("S07", "bill with a placeholder contact", date=doc_date(b), who=contact(b) or "(blank)",
                    ccy=doc_ccy(ctx, b), amount=_num(b.get("Total")), refs=[ref_of(b)], new=ctx.new(b),
                    detail=brief(b))
    nocontact = [t for t in ctx.wbank if not contact(t) and "TRANSFER" not in str(t.get("Type"))]
    if nocontact:
        ctx.add("S07", f"{len(nocontact)} spend/receive money in the window with no contact "
                       f"({sum(1 for t in nocontact if ctx.new(t))} new)", date=ctx.today,
                new=any(ctx.new(t) for t in nocontact),
                detail={"items": [brief(t) for t in nocontact]})


def _supplier_lines(ctx: Ctx, docs) -> dict:
    out = defaultdict(lambda: defaultdict(lambda: {"n": 0, "amt": 0.0, "docs": []}))
    for x in docs:
        if "TRANSFER" in str(x.get("Type")):
            continue
        cid = (x.get("Contact") or {}).get("ContactID") or contact(x)
        if not cid:
            continue
        for ln in lines(x):
            code = str(ln.get("AccountCode") or "")
            if code:
                slot = out[cid][code]
                slot["n"] += 1
                slot["amt"] += in_base(x, ln.get("LineAmount"))
                slot["docs"].append(x)
    return out


def s08_supplier(ctx: Ctx) -> None:
    w0 = ctx.win["from"]
    base_docs = [x for x in ctx.bills + ctx.bank if doc_date(x) < w0]
    cur_docs = [x for x in ctx.wbills + ctx.wbank]
    base, cur = _supplier_lines(ctx, base_docs), _supplier_lines(ctx, cur_docs)
    for cid, accs in cur.items():
        if cid not in base:
            continue
        prior = sorted(base[cid], key=lambda c: -abs(base[cid][c]["amt"]))
        for code, s in accs.items():
            if code in base[cid] or rep(abs(s["amt"]), ctx.base) < ctx.cfg.num["drift_min"]:
                continue
            docs = s["docs"]
            ctx.add("S08", f"now on {ctx.aname(code)}; before the window on "
                           f"{', '.join(ctx.aname(p) for p in prior[:3])}", issue="supplier-wrong-account",
                    date=doc_date(docs[-1]), who=contact(docs[-1]), ccy=ctx.base, amount=s["amt"],
                    refs=[ref_of(x) for x in docs[:4]], new=any(ctx.new(x) for x in docs),
                    detail={"docs": [brief(x) for x in docs]})


def parse_whitelists(path: Path | None = None) -> dict[str, set[str]]:
    """{entity key: account codes} from the account tables in rules/EXPENSES.md.

    A table whose header row names entity keys (| Cost category | HOLDCO | ...)
    lists, in each entity's column, `<code> <name>` or n/a. Quoted lines
    (the fictional illustrations) and tables with no entity key are skipped,
    so a file still carrying its FILL IN placeholders yields nothing and the
    whitelist check skips cleanly."""
    path = path or EXPENSES_MD
    try:
        text = path.read_text()
    except OSError:
        return {}
    keys = set(config.entity_keys())
    out: dict[str, set[str]] = {}
    cols: dict[int, str] = {}
    for raw in text.splitlines():
        ln = raw.strip()
        if not ln.startswith("|"):
            cols = {}
            continue
        cells = [c.strip() for c in ln.strip("|").split("|")]
        if not cols:
            cols = {i: c for i, c in enumerate(cells) if i and c in keys}
            continue
        if set(ln) <= set("|-: "):
            continue
        for i, key in cols.items():
            if i < len(cells):
                m = re.match(r"^(\d[\w.\-]*)\s+\S", cells[i])
                if m:
                    out.setdefault(key, set()).add(m.group(1))
    return out


WHITELISTS: dict[str, set[str]] = {}


def s08_whitelist(ctx: Ctx) -> None:
    wl = WHITELISTS.get(ctx.key)
    if not wl:
        return
    agg = defaultdict(list)
    for kind, x in ctx.docs():
        for ln in lines(x):
            code = str(ln.get("AccountCode") or "")
            a = ctx.by_code.get(code) or {}
            if code and ctx.is_pl(a) and code not in wl and not FXRE.search(a.get("Name") or ""):
                agg[code].append((x, ln))
    for code, hits in agg.items():
        docs = list({id(x): x for x, _ in hits}.values())
        ctx.add("S08W", f"{len(hits)} line(s) on {ctx.aname(code)}, which is not in the rules/EXPENSES.md account table",
                date=doc_date(docs[-1]), who=", ".join(sorted({contact(x) for x in docs if contact(x)}))[:60],
                ccy=ctx.base, amount=sum(in_base(x, ln.get("LineAmount")) for x, ln in hits),
                refs=[ref_of(x) for x in docs[:4]], new=any(ctx.new(x) for x in docs),
                detail={"docs": [brief(x) for x in docs]})


def s08_capitalisation(ctx: Ctx) -> None:
    cfg = ctx.cfg
    for b in ctx.wbills:
        for ln in lines(b):
            a = ctx.acct(ln)
            name = (a.get("Name") or "").strip()
            unit = in_base(b, ln.get("UnitAmount") or ln.get("LineAmount"))
            unit_rep = rep(abs(unit), ctx.base)
            text = f"{contact(b)} {ln.get('Description') or ''}"
            if (any(p.fullmatch(name) for p in cfg.capitalisable) and unit_rep >= cfg.cap
                    and cfg.hardware.search(text) and not SOFTWAREISH.search(text)):
                ctx.add("S08C", f"item of about {config.reporting_currency()} {unit_rep:,.0f} expensed to "
                                f"{ctx.aname(a.get('Code'))}; {cfg.cap:,.0f} or more per item is capitalised",
                        issue="capitalisation-threshold", date=doc_date(b), who=contact(b),
                        ccy=doc_ccy(ctx, b), amount=_num(ln.get("LineAmount")),
                        refs=[ref_of(b), (ln.get("Description") or "")[:50]], new=ctx.new(b), detail=brief(b))
            elif (a.get("Type") == "FIXED" and 0 < unit_rep < cfg.cap
                  and not re.search(r"depreciation|right of use|rou\b", name, re.I)):
                ctx.add("S08C", f"item of about {config.reporting_currency()} {unit_rep:,.0f} capitalised to "
                                f"{ctx.aname(a.get('Code'))}; under {cfg.cap:,.0f} per item is expensed",
                        issue="capitalisation-threshold", date=doc_date(b), who=contact(b),
                        ccy=doc_ccy(ctx, b), amount=_num(ln.get("LineAmount")),
                        refs=[ref_of(b), (ln.get("Description") or "")[:50]], new=ctx.new(b), detail=brief(b))


def s08_subscriptions(ctx: Ctx) -> None:
    months = defaultdict(lambda: defaultdict(float))
    docs = defaultdict(list)
    for x in ctx.bills + ctx.bank:
        for ln in lines(x):
            name = (ctx.acct(ln).get("Name") or "").strip()
            if any(p.fullmatch(name) for p in ctx.cfg.subscription):
                who = contact(x)
                if who:
                    months[who][doc_date(x)[:7]] += in_base(x, ln.get("LineAmount"))
                    docs[who].append(x)
    for who, by_m in months.items():
        vals = [abs(v) for v in by_m.values() if abs(v) > 0.005]
        if len(vals) < 3 or not any(doc_date(x) >= ctx.win["from"] for x in docs[who]):
            continue
        med = statistics.median(vals)
        if rep(med, ctx.base) < ctx.cfg.num["subscription_max"] and all(abs(v - med) <= 0.2 * med for v in vals):
            ctx.add("S08S", f"paid on a cost account in {len(vals)} months at a steady {med:,.2f}: a "
                            "subscription by the review method's test", issue="subscription-vs-it-costs",
                    who=who, ccy=ctx.base, amount=med, date=max(by_m), new=any(ctx.new(x) for x in docs[who]),
                    detail={"months": dict(sorted(by_m.items()))})


def s08_welfare_travel(ctx: Ctx) -> None:
    for kind, x in ctx.docs():
        for ln in lines(x):
            a = ctx.acct(ln)
            if ctx.cfg.welfare.search(a.get("Name") or ""):
                text = f"{contact(x)} {ln.get('Description') or ''}"
                if ctx.cfg.travel.search(text):
                    ctx.add("S08T", f"travel-shaped line on {ctx.aname(a.get('Code'))}; hotels and transport are Travel",
                            issue="welfare-vs-travel", date=doc_date(x), who=contact(x), ccy=doc_ccy(ctx, x),
                            amount=_num(ln.get("LineAmount")), refs=[ref_of(x), (ln.get("Description") or "")[:50]],
                            new=ctx.new(x), detail=brief(x))


def payroll_control_codes(key: str) -> list:
    return config.payroll_controls(key).get(key, [])


def _exception_account(ctx: Ctx, a: dict, controls: set[str]) -> bool:
    return (a.get("Code") in controls or ctx.is_interco(a)
            or a.get("Type") in ("BANK", "CURRLIAB", "LIABILITY", "TERMLIAB", "EQUITY")
            or bool(EXCEPTION_ACCOUNT_NAME.search(a.get("Name") or "")))


def s09_transaction_type(ctx: Ctx) -> None:
    cfg, n = ctx.cfg, ctx.cfg.num
    controls = {c.code for c in payroll_control_codes(ctx.key)}
    agg = defaultdict(list)
    for t in ctx.wbank:
        if t.get("Type") != "SPEND":
            continue
        who = contact(t)
        if cfg.invoiceless.search(who) or cfg.small.search(who):
            continue
        for ln in lines(t):
            a = ctx.acct(ln)
            amt = abs(_num(ln.get("LineAmount")))
            if _exception_account(ctx, a, controls) or rep(in_base(t, amt), ctx.base) < n["spend_line_min"]:
                continue
            if cfg.welfare.search(a.get("Name") or "") and rep(amt, doc_ccy(ctx, t)) < n["welfare_small"]:
                continue
            agg[(who, a.get("Code"))].append((t, ln))
    for (who, code), hits in agg.items():
        amt = sum(in_base(t, ln.get("LineAmount")) for t, ln in hits)
        if rep(abs(amt), ctx.base) < n["spend_money_min"]:
            continue
        docs = list({id(t): t for t, _ in hits}.values())
        ctx.add("S09", f"{len(docs)} spend money to {ctx.aname(code)} with no bill; a supplier invoice is a bill",
                issue="wrong-transaction-type", date=doc_date(docs[-1]), who=who or "(no contact)", ccy=ctx.base,
                amount=amt, refs=[ref_of(t) for t in docs[:4]], new=any(ctx.new(t) for t in docs),
                detail={"docs": [brief(t) for t in docs]})
    for b in ctx.wbills:
        accts = [ctx.acct(ln) for ln in lines(b)]
        if accts and all(re.search(r"bank fee", a.get("Name") or "", re.I) for a in accts):
            ctx.add("S09", "bill raised for bank fees, which are invoice-less spend money", issue="wrong-transaction-type",
                    date=doc_date(b), who=contact(b), ccy=doc_ccy(ctx, b), amount=_num(b.get("Total")),
                    refs=[ref_of(b)], new=ctx.new(b), detail=brief(b))


def s10_tax(ctx: Ctx) -> None:
    rules = ctx.cfg.table("tax", ctx.key)
    if not rules:
        return
    for kind, x in ctx.docs():
        ccy = doc_ccy(ctx, x)
        for ln in lines(x):
            ta, tt = _num(ln.get("TaxAmount")), str(ln.get("TaxType") or "")
            if abs(ta) <= 0.004:
                continue
            a = ctx.acct(ln)
            code = str(ln.get("AccountCode") or "")
            why = None
            for r in rules:
                if r.get("no_tax"):
                    why = "tax amount in an entity with no recoverable tax: every tax line is part of the cost"
                elif r.get("base_currency_only") and ccy != ctx.base:
                    why = f"{tt} tax on a {ccy} line: tax is recognised only on {ctx.base} lines"
                elif r.get("no_tax_accounts") and re.search(r["no_tax_accounts"], a.get("Name") or "", re.I):
                    why = f"tax on {ctx.aname(code)}, which carries none"
                elif r.get("allowed_types") and tt not in r["allowed_types"]:
                    why = f"tax type {tt} is not one of {', '.join(r['allowed_types'])}"
                if why:
                    break
            if why:
                ctx.add("S10", why, date=doc_date(x), who=contact(x), ccy=ccy, amount=ta,
                        refs=[ref_of(x), ctx.aname(code)], new=ctx.new(x), detail=brief(x))


def s11_tracking(ctx: Ctx) -> None:
    for rule in ctx.cfg.table("tracking", ctx.key):
        cat, options = rule["category"], rule.get("options") or []
        for kind, x in ctx.docs():
            missing, odd = [], []
            for ln in lines(x):
                a = ctx.acct(ln)
                if not ctx.is_pl(a) or FXRE.search(a.get("Name") or ""):
                    continue
                opt = next((t.get("Option") for t in ln.get("Tracking") or [] if t.get("Name") == cat), None)
                text = f"{ln.get('Description') or ''} {contact(x)}"
                if not opt:
                    missing.append(ln)
                elif options and opt not in options:
                    odd.append(ln)
                elif any(m.get("option") == opt and (
                        doc_ccy(ctx, x) == m.get("currency")
                        or (m.get("pattern") and re.search(m["pattern"], text, re.I)))
                        for m in rule.get("mismatch") or []):
                    odd.append(ln)
            if missing:
                ctx.add("S11", f"{kind}: {len(missing)} cost line(s) with no {cat}", issue="tracking",
                        date=doc_date(x), who=contact(x), ccy=doc_ccy(ctx, x),
                        amount=sum(_num(ln.get("LineAmount")) for ln in missing),
                        refs=[ref_of(x)] + sorted({ctx.aname(str(ln.get("AccountCode"))) for ln in missing}),
                        new=ctx.new(x), detail=brief(x))
            if odd:
                ctx.add("S11", f"{kind}: {len(odd)} line(s) with an implausible {cat}", issue="tracking",
                        date=doc_date(x), who=contact(x), ccy=doc_ccy(ctx, x),
                        amount=sum(_num(ln.get("LineAmount")) for ln in odd), refs=[ref_of(x)], new=ctx.new(x),
                        detail=brief(x))


def s12_revenue(ctx: Ctx) -> None:
    for rule in ctx.cfg.table("revenue", ctx.key):
        allowed = re.compile(rule.get("allowed_accounts") or r"(?!x)x", re.I)
        if rule.get("flag_sales"):
            for s in ctx.sales:
                if doc_date(s) < ctx.win["from"]:
                    continue
                accs = sorted({ctx.aname(str(ln.get("AccountCode"))) for ln in lines(s)})
                ctx.add("S12", f"sales invoice to {', '.join(accs)}", issue="revenue-shape", date=doc_date(s),
                        who=contact(s), ccy=doc_ccy(ctx, s), amount=_num(s.get("Total")), refs=[ref_of(s)],
                        new=ctx.new(s), detail=brief(s))
        agg = defaultdict(list)
        for kind, x in list(ctx.docs()):
            for ln in lines(x):
                a = ctx.acct(ln)
                if (a.get("Class") == "REVENUE" and not allowed.search(a.get("Name") or "")
                        and not FXRE.search(a.get("Name") or "")):
                    agg[a.get("Code")].append((x, ln))
        for code, hits in agg.items():
            docs = list({id(x): x for x, _ in hits}.values())
            ctx.add("S12", f"{len(hits)} line(s) on revenue account {ctx.aname(code)}, outside this entity's revenue shape",
                    issue="revenue-shape", date=doc_date(docs[-1]), ccy=ctx.base,
                    amount=sum(in_base(x, ln.get("LineAmount")) for x, ln in hits),
                    refs=[ref_of(x) for x in docs[:4]], new=any(ctx.new(x) for x in docs),
                    detail={"docs": [brief(x) for x in docs]})


def _year(token: str, y0: int) -> int | None:
    """A year written after a month, or None where the number is more likely a
    day ("Sep 30"): two digits count only within a year of the document's."""
    if not token:
        return None
    y = int(token) + (2000 if len(token) == 2 else 0)
    return y if abs(y - y0) <= 1 else None


def mentioned_quarters(text: str, on: str) -> set[tuple[int, int]]:
    """Financial quarters (quarter, financial year) a text names, as months
    or as Q1..Q4, read relative to the document date `on`."""
    y0, m0 = int(on[:4]), int(on[5:7])
    fy0 = config.fy_year_for(dt.date.fromisoformat(on))
    out = set()
    for mon, yr in MONTH_RE.findall(text):
        m = MONTHS[mon[:3].lower()]
        y = _year(yr, y0)
        if mon.lower() == "may" and y is None:
            continue
        if y is None:
            y = y0 - 1 if m - m0 > 6 else y0 + 1 if m0 - m > 6 else y0
        out.add(config.fy_quarter_label(dt.date(y, m, 1)))
    for q, yr in QTR_RE.findall(text):
        out.add((int(q), _year(yr, fy0) or fy0))
    return out


def s13_quarter(ctx: Ctx) -> None:
    cfg = ctx.cfg
    for b in ctx.wbills:
        if cfg.quarter_suppliers.search(contact(b)):
            continue
        q = fin_quarter(doc_date(b))
        other = set()
        for ln in lines(b):
            a = ctx.acct(ln)
            if ctx.is_pl(a) and not cfg.quarter_accounts.search(a.get("Name") or ""):
                other |= {x for x in mentioned_quarters(ln.get("Description") or "", doc_date(b)) if x != q}
        if other:
            ctx.add("S13", f"bill dated Q{q[0]} {q[1]} describes "
                           f"{', '.join(f'Q{x[0]} {x[1]}' for x in sorted(other, key=lambda t: (t[1], t[0])))}",
                    issue="wrong-quarter", date=doc_date(b), who=contact(b), ccy=doc_ccy(ctx, b),
                    amount=_num(b.get("Total")), refs=[ref_of(b)], new=ctx.new(b), detail=brief(b))


def journal_cleared_patterns() -> list[tuple[re.Pattern, str]]:
    """Bills settled by journal, not by the bank: scripts/check_payroll_controls.py's list."""
    try:
        import check_payroll_controls as cpc
        return cpc.journal_cleared_patterns()
    except Exception:  # the list is optional context
        return []


def s14_payroll(ctx: Ctx) -> None:
    dates = ctx.win["month_ends"] + [ctx.today]
    bals = {d: ctx.tb_balance(d) for d in dates}
    controls = payroll_control_codes(ctx.key)
    for ctl in controls:
        a = ctx.by_code.get(ctl.code)
        if not a:
            continue
        series = {d: bals[d].get(a["AccountID"], 0.0) for d in dates}
        if abs(series[ctx.today]) > 0.05:
            ctx.add("S14", f"{a['Name']} ({ctl.code}) is not zero: "
                           f"{', '.join(f'{d[5:]} {v:,.2f}' for d, v in series.items())}. "
                           f"Legitimate only as: {ctl.residual or 'nothing (no residual configured)'}; "
                           "check the bank it is paid from",
                    ccy=ctx.base, amount=series[ctx.today], date=ctx.today, detail={"series": series})
    patterns = journal_cleared_patterns()
    if controls and patterns:
        month = ctx.today[:7]
        for b in ctx.bills_all + list(ctx.d.get("invoices_open_old") or []):
            if (b.get("Status") == "AUTHORISED" and _num(b.get("AmountDue")) > 0.005 and doc_date(b)[:7] < month
                    and any(p.search(f"{contact(b)} {b.get('Reference') or ''}") for p, _ in patterns)):
                ctx.add("S14", "bill cleared by journal is still open past its month (clear it from the control "
                               "accounts, or they cannot clear)", date=doc_date(b), who=contact(b),
                        ccy=doc_ccy(ctx, b), amount=_num(b.get("AmountDue")), refs=[ref_of(b)], detail=brief(b))
    # line -> account mapping drift on payroll sources, month on month
    source = _alt([p.pattern for p, _ in patterns] + list(ctx.cfg.raw["payroll_sources"]))
    srcs = defaultdict(lambda: defaultdict(set))
    docs = defaultdict(list)
    for kind, x in ctx.docs(window=False):
        label = None
        if kind == "BILL" and source.search(contact(x)):
            label = contact(x)
        elif kind == "MJ" and PAYROLL_NARRATION.search(x.get("Narration") or ""):
            label = narration_family(x.get("Narration") or "")
        if not label:
            continue
        m = doc_date(x)[:7]
        srcs[label][m] |= {str(ln.get("AccountCode")) for ln in lines(x) if ln.get("AccountCode")}
        docs[label].append(x)
    for label, by_m in srcs.items():
        months = sorted(m for m in by_m if m < ctx.today[:7])     # complete months only
        if len(months) < 3 or months[-1] < ctx.win["from"][:7]:
            continue
        last, prior = by_m[months[-1]], by_m[months[-2]] | by_m[months[-3]]
        added, dropped = last - prior, (by_m[months[-2]] & by_m[months[-3]]) - last
        if added or dropped:
            ctx.add("S14M", f"{label} {months[-1]} " + "; ".join(filter(None, [
                f"new account(s) {', '.join(ctx.aname(c) for c in sorted(added))}" if added else "",
                f"no longer on {', '.join(ctx.aname(c) for c in sorted(dropped))}" if dropped else ""])),
                    issue="payroll-consistency", who=label, date=months[-1],
                    new=any(ctx.new(x) for x in docs[label] if doc_date(x)[:7] == months[-1]),
                    detail={"by_month": {m: sorted(v) for m, v in sorted(by_m.items())}})


def s15_routing(ctx: Ctx) -> None:
    for rule in ctx.cfg.table("routing", ctx.key):
        sup, acc = re.compile(rule["supplier_pattern"], re.I), re.compile(rule["account_pattern"], re.I)
        for kind, x in ctx.docs():
            if not sup.search(contact(x)):
                continue
            for ln in lines(x):
                a = ctx.acct(ln)
                if not ctx.is_pl(a):
                    continue
                on = bool(acc.search(a.get("Name") or ""))
                if on != (rule["must"] == "be_on"):
                    verb = "is not on" if rule["must"] == "be_on" else "is on"
                    ctx.add("S15", f"line {verb} an account matching {rule['account_pattern']!r}"
                                   + (f": {rule['why']}" if rule.get("why") else ""),
                            issue="account-routing", date=doc_date(x), who=contact(x), ccy=doc_ccy(ctx, x),
                            amount=_num(ln.get("LineAmount")), refs=[ref_of(x), ctx.aname(str(ln.get("AccountCode")))],
                            new=ctx.new(x), detail=brief(x))


def s16_known_bad(ctx: Ctx) -> None:
    bal = ctx.tb_balance(ctx.today)
    for rule in ctx.cfg.table("known_bad_accounts", ctx.key):
        code, why = str(rule["code"]), rule["why"]
        a = ctx.by_code.get(code)
        if not a or not re.search(rule.get("name_pattern") or ".", a.get("Name") or "", re.I):
            continue
        label = f"{a['Name']} ({code})"
        if rule.get("archived") and a.get("Status") != "ARCHIVED":
            ctx.add("S16", f"{label} is {a.get('Status')}, expected archived: {why}", date=ctx.today)
        if abs(bal.get(a["AccountID"], 0.0)) > 0.005:
            ctx.add("S16", f"{label} carries a balance: {why}", ccy=ctx.base, amount=bal[a["AccountID"]],
                    date=ctx.today)
        for kind, x in ctx.docs():
            hit = [ln for ln in lines(x) if str(ln.get("AccountCode") or "") == code]
            if hit:
                ctx.add("S16P", f"{kind} posts to {label}: {why}", date=doc_date(x), who=contact(x),
                        ccy=doc_ccy(ctx, x), amount=sum(_num(ln.get("LineAmount")) for ln in hit),
                        refs=[ref_of(x)], new=ctx.new(x), detail=brief(x))


def staff_register_names(location: str) -> list[str]:
    """Names in the optional staff register ([bank_reconcile] staff_register,
    JSON {"employees": [{"name": ..., "location": ...}]}) at one location."""
    path = config.bank_reconcile().get("staff_register") or ""
    if not path or not location:
        return []
    try:
        reg = json.loads((ROOT / path).read_text())
        return [e["name"] for e in reg.get("employees", []) if e.get("name") and e.get("location") == location]
    except (OSError, ValueError, KeyError, AttributeError):
        return []


def _matchers(rule: dict) -> list[tuple[str, re.Pattern]]:
    """(label, pattern) for the rule's match patterns and the register's names
    at its register_location, less any name an exclude pattern covers."""
    exclude = [re.compile(p, re.I) for p in rule.get("exclude") or []]
    out = [(m, re.compile(m, re.I)) for m in rule.get("match") or []]
    for n in staff_register_names(rule.get("register_location", "")):
        if not any(e.search(n) for e in exclude):
            out.append((n, re.compile(r"\b" + re.escape(n) + r"\b", re.I)))
    return out


def s17_cutovers(ctx: Ctx) -> None:
    for rule in ctx.cfg.table("cutovers"):
        start, kind, why = str(rule["from"]), rule["kind"], rule["why"]
        pats = _matchers(rule)
        barred_who = [re.compile(p, re.I) for p in rule.get("exclude") or []]

        def hit(x, pats=pats, barred_who=barred_who):
            """The document's contact matches the rule and is not excluded."""
            return (any(p.search(contact(x)) for _, p in pats)
                    and not any(e.search(contact(x)) for e in barred_who))
        if kind == "recognise_in":
            if ctx.key == rule["entity"]:
                continue
            for dkind, x in ctx.docs():
                if doc_date(x) < start:
                    continue
                for ln in lines(x):
                    a = ctx.acct(ln)
                    if not ctx.is_pl(a):
                        continue
                    text = f"{contact(x)} {ln.get('Description') or ''} {x.get('Narration') or ''}"
                    who = next((n for n, p in pats if p.search(text)), None)
                    if who:
                        label = re.sub(r"\\(.)", r"\1", who)
                        ctx.add("S17", f"{label} cost on {ctx.aname(a.get('Code'))}, dated on or "
                                       f"after {start}: recognised in {short_of(rule['entity'])} ({why})",
                                issue="dated-cutover", date=doc_date(x), who=contact(x), ccy=doc_ccy(ctx, x),
                                amount=_num(ln.get("LineAmount")), refs=[ref_of(x)], new=ctx.new(x),
                                detail=brief(x))
                        break
        elif ctx.key != rule["entity"]:
            continue
        elif kind == "only_accounts":
            allowed = {str(c) for c in rule.get("accounts") or []}
            for b in ctx.bills:
                if doc_date(b) >= start and hit(b):
                    off = [ln for ln in lines(b) if str(ln.get("AccountCode")) not in allowed]
                    if off:
                        ctx.add("S17", f"bill coded off {', '.join(sorted(allowed))} after {start}: {why}",
                                issue="dated-cutover", date=doc_date(b), who=contact(b), ccy=doc_ccy(ctx, b),
                                amount=_num(b.get("Total")), refs=[ref_of(b)], new=ctx.new(b), detail=brief(b))
        elif kind == "not_accounts":
            barred = {str(c) for c in rule.get("accounts") or []}
            for dkind, x in list(ctx.docs()) + [("SALE", s) for s in ctx.sales if doc_date(s) >= ctx.win["from"]]:
                if doc_date(x) >= start and hit(x) and any(
                        str(ln.get("AccountCode")) in barred for ln in lines(x)):
                    ctx.add("S17", f"{dkind} on {', '.join(sorted(barred))} after {start}: {why}",
                            issue="dated-cutover", date=doc_date(x), who=contact(x), ccy=doc_ccy(ctx, x),
                            amount=total(x), refs=[ref_of(x)], new=ctx.new(x), detail=brief(x))
        elif kind == "no_bills":
            for b in ctx.wbills:
                if doc_date(b) >= start and hit(b):
                    ctx.add("S17", f"bill dated on or after {start}: {why}", issue="dated-cutover",
                            date=doc_date(b), who=contact(b), ccy=doc_ccy(ctx, b), amount=_num(b.get("Total")),
                            refs=[ref_of(b)], new=ctx.new(b), detail=brief(b))


def s17_mirrors(ctxs: dict[str, Ctx]) -> None:
    """A cutover rule with a mirror: every matching bill from the start date has
    a posted journal in its month in the mirror entity, on the mirror account."""
    for rule in next(iter(ctxs.values())).cfg.table("cutovers"):
        mirror = rule.get("mirror")
        src, dst = ctxs.get(rule["entity"]), ctxs.get((mirror or {}).get("entity"))
        if not (mirror and src and dst):
            continue
        pats = _matchers(rule)
        months = {doc_date(j)[:7] for j in dst.posted
                  if any(p.search(j.get("Narration") or "") for _, p in pats)
                  and any(str(ln.get("AccountCode")) == str(mirror["account"]) for ln in lines(j))}
        for b in src.wbills:
            if (any(p.search(contact(b)) for _, p in pats) and doc_date(b) >= str(rule["from"])
                    and doc_date(b)[:7] not in months):
                dst.add("S17", f"no journal on {mirror['account']} in {doc_date(b)[:7]} for {short_of(src.key)}'s "
                               f"bill: {rule['why']}", issue="dated-cutover", date=doc_date(b), who=contact(b),
                        ccy=doc_ccy(src, b), amount=_num(b.get("Total")), refs=[ref_of(b)], new=src.new(b),
                        detail=brief(b))


def s18_attachments(ctx: Ctx) -> None:
    for b in ctx.wbills:
        if b.get("HasAttachments") or b.get("Status") not in ("AUTHORISED", "PAID"):
            continue
        accts = [ctx.acct(ln) for ln in lines(b)]
        if accts and all(ctx.is_interco(a) for a in accts):
            continue
        ctx.add("S18", f"{b.get('Status')} bill with no document attached", date=doc_date(b), who=contact(b),
                ccy=doc_ccy(ctx, b), amount=_num(b.get("Total")), refs=[ref_of(b)], new=ctx.new(b), detail=brief(b))


def s19_interco(ctxs: dict[str, Ctx], today: str) -> None:
    """The last intercompany reconciliation (data/interco/latest.json, written
    by deploy/run-interco.sh from scripts/interco_recon.py): any pair that does not agree."""
    try:
        d = json.loads(INTERCO_LATEST.read_text())
    except (OSError, ValueError):
        d = {}
    first = ctxs[next(k for k in order() if k in ctxs)]
    if d.get("as_at") != today:
        first.add("S19", f"no intercompany reconciliation as at today (latest {d.get('as_at') or 'none'}): "
                         "run it per the xero-interco skill", date=today)
        return
    ccy = d.get("reporting_currency") or config.reporting_currency()
    for p in d.get("pairs", []):
        status = str(p.get("status") or "")
        brk = _num(p.get("break"))
        if status.upper().startswith(("AGREED", "NIL")) or (status != "no_mirror_account" and abs(brk) < 0.01):
            continue
        ents = {(p.get("a") or {}).get("entity"), (p.get("b") or {}).get("entity")}
        mine = [k for k in order() if k in ents and k in ctxs]
        if mine:     # one line per pair, on the first entity of it in config order
            ctxs[mine[0]].add("S19", f"{p.get('pair')}: {status}, break {ccy} {brk:,.2f}", date=d.get("as_at"),
                              ccy=ccy, amount=brk,
                              detail={k2: p.get(k2) for k2 in ("pair", "flavour", "status", "break", "a", "b")})


def s20_bank(ctxs: dict[str, Ctx], today: str) -> None:
    """The last bank reconcile check (data/bankfeed/unreconciled.json, written
    by scripts/bank_reconcile_check.py): every account must tie out and be verified."""
    try:
        d = json.loads(UNRECONCILED.read_text())
    except (OSError, ValueError):
        d = {}
    first = ctxs[next(k for k in order() if k in ctxs)]
    if (d.get("built") or "")[:10] != today:
        first.add("S20", f"no bank reconcile check from today (latest {(d.get('built') or 'none')[:16]}): run "
                         "scripts/bank_reconcile_check.py run all --dry-run and read its tie-out", date=today)
        return
    for key, c in ctxs.items():
        e = (d.get("entities") or {}).get(feed_key(key), {})
        for xname, t in (e.get("accounts") or {}).items():
            if "difference" not in t:
                c.add("S20", f"{xname}: not checked, {t.get('why')}", date=today)
            elif not t.get("ties") or not t.get("verified"):
                c.add("S20", f"{xname}: bank {t.get('bank_balance'):,.2f}, Xero {t.get('xero_balance'):,.2f}, "
                             f"open {t.get('open_sum'):,.2f}, unreconciled {t.get('unreconciled_sum'):,.2f}: "
                             + ("TIES but UNVERIFIED" if t.get("ties") else f"off by {t.get('difference'):,.2f}"),
                      date=today, ccy=t.get("currency"), amount=t.get("difference"), detail=t)


def s21_far(ctx: Ctx) -> None:
    far = ctx.d.get("far") or {}
    if far.get("error"):
        ctx.add("S21", f"fixed asset register not read: {far['error'][:120]}", date=ctx.today)
        return
    types = far.get("types") or []
    if not types:
        return
    tol = ctx.cfg.num["far_tolerance"]
    assets = far.get("assets") or {}
    gl = ctx.tb_balance(ctx.today)
    by_acct = defaultdict(lambda: {"reg": 0.0, "draft": 0.0, "types": []})
    type_acct = {t.get("assetTypeId"): t.get("fixedAssetAccountId") for t in types}
    for t in types:
        by_acct[t.get("fixedAssetAccountId")]["types"].append(t.get("assetTypeName"))
    for status, slot in (("REGISTERED", "reg"), ("DRAFT", "draft")):
        for a in assets.get(status) or []:
            by_acct[type_acct.get(a.get("assetTypeId"))][slot] += _num(a.get("purchasePrice"))
    for aid, s in by_acct.items():
        g = gl.get(aid, 0.0)
        acct = ctx.by_id.get(aid) or {}
        gap = round(g - s["reg"], 2)
        if abs(gap) > tol:
            note = " (the gap is the drafts)" if abs(gap - s["draft"]) <= tol else ""
            ctx.add("S21", f"{acct.get('Name')} ({acct.get('Code')}): GL {g:,.2f} against registered {s['reg']:,.2f}, "
                           f"drafts {s['draft']:,.2f}, gap {gap:,.2f}{note}", ccy=ctx.base, amount=gap, date=ctx.today,
                    detail={"types": s["types"], "gl": g, "registered": s["reg"], "draft": s["draft"]})
    drafts = assets.get("DRAFT") or []
    if drafts:
        ctx.add("S21", f"{len(drafts)} DRAFT asset(s) on the register", ccy=ctx.base,
                amount=sum(_num(a.get("purchasePrice")) for a in drafts), date=ctx.today,
                detail={"drafts": [{k: a.get(k) for k in ("assetNumber", "assetName", "purchaseDate", "purchasePrice")}
                                   for a in drafts]})
    for rule in ctx.cfg.table("register_depreciated", ctx.key):
        wanted = {str(n) for n in rule["assets"]}
        for a in assets.get("REGISTERED") or []:
            method = (a.get("bookDepreciationSetting") or {}).get("depreciationMethod")
            if a.get("assetNumber") in wanted and method in (None, "", "NoDepreciation"):
                ctx.add("S21", f"asset {a.get('assetNumber')} is on {method or 'no method'}; the register "
                               f"depreciates it ({rule['why']}), so set its method in the Xero UI",
                        date=ctx.today, refs=[a.get("assetNumber")])
        if rule.get("journal_pattern"):
            accounts = {str(c) for c in rule.get("accounts") or []}
            for j in ctx.mj:
                if j.get("Status") != "POSTED" or not re.search(rule["journal_pattern"], j.get("Narration") or "", re.I):
                    continue
                hit = [ln for ln in j.get("JournalLines") or []
                       if str(ln.get("AccountCode")) in accounts and _num(ln.get("LineAmount")) < 0]
                if hit:
                    ctx.add("S21", f"manual depreciation journal: the register depreciates these assets "
                                   f"({rule['why']}), so this double-counts it", date=doc_date(j), ccy=ctx.base,
                            amount=-sum(_num(ln.get("LineAmount")) for ln in hit),
                            refs=[(j.get("Narration") or "")[:70]], detail=brief(j))


ENTITY_CHECKS = [s01_tb, s02_unreconciled, s03_aged, s04_journals, s05_duplicates, s06_suspense, s07_blanks,
                 s08_supplier, s08_whitelist, s08_capitalisation, s08_subscriptions, s08_welfare_travel,
                 s09_transaction_type, s10_tax, s11_tracking, s12_revenue, s13_quarter, s14_payroll, s15_routing,
                 s16_known_bad, s17_cutovers, s18_attachments, s21_far]


def run_checks(data: dict[str, dict], win: dict, since: str, state: dict, cfg: Cfg | None = None) -> dict[str, Ctx]:
    cfg = cfg or Cfg()
    WHITELISTS.clear()
    WHITELISTS.update(parse_whitelists())
    ctxs = {k: Ctx(k, d, win, since, cfg) for k, d in data.items()}
    for ctx in ctxs.values():
        s00_lock(ctx, state)
        for fn in ENTITY_CHECKS:
            try:
                fn(ctx)
            except Exception as e:  # one broken check must not hide the others
                ctx.add(fn.__name__.split("_")[0].upper(), f"check failed: {type(e).__name__}: {e}", date=win["today"])
    if ctxs:
        for name, fn in (("S17", lambda: s17_mirrors(ctxs)), ("S19", lambda: s19_interco(ctxs, win["today"])),
                         ("S20", lambda: s20_bank(ctxs, win["today"]))):
            try:
                fn()
            except Exception as e:
                next(iter(ctxs.values())).add(name, f"check failed: {type(e).__name__}: {e}", date=win["today"])
    for ctx in ctxs.values():
        for i, f in enumerate(ctx.findings, 1):
            f["id"] = f"{ctx.key}-{f['check']}-{i}"
    return ctxs


# ---------------------------------------------------------------- output

def fmt(f: dict, year: str) -> str:
    parts = [f["date"][5:] if f["date"][:4] == year else f["date"]]
    if f["who"]:
        parts.append(f["who"][:40])
    if f["amount"] is not None:
        parts.append(money(f["ccy"] or "", f["amount"]))
    parts.append(f["text"])
    tail = " · ".join(str(r)[:40] for r in f["refs"][:4])
    tag = f" [{f['issue']}]" if f["issue"] else ""
    return f"  {f['id']}{' *' if f['new'] and f['check'] not in STATE_CHECKS else ''}  " + " · ".join(
        p for p in parts if p) + (f" · {tail}" if tail else "") + tag


def summary(ctxs: dict[str, Ctx], win: dict, since: str, show_all: bool) -> str:
    out = [f"REVIEW CHECKS {win['today']} · window {win['from']}..{win['today']} · baseline from "
           f"{win['baseline_from']} · new since {since} · * = new event finding"]
    for key in [k for k in order() if k in ctxs]:
        ctx = ctxs[key]
        org = ctx.d.get("org") or {}
        lock, eoy = ctx.lock
        src = f"cache {ctx.d['from_cache']}" if ctx.d.get("from_cache") else f"{ctx.d.get('calls', 0)} Xero calls"
        out.append(f"\n== {org.get('Name') or config.entity(key).xero_name} ({short_of(key)}) · base {ctx.base} · "
                   f"lock {lock or 'none'} / year end {eoy or 'none'} · pulled {ctx.d.get('pulled')} · {src}")
        counts = defaultdict(lambda: [0, 0])
        for f in ctx.findings:
            counts[f["check"]][0] += 1
            counts[f["check"]][1] += f["new"]
        out.append("   " + (" · ".join(f"{c} {n}" + (f" ({k} new)" if c not in STATE_CHECKS and k != n else "")
                                       for c, (n, k) in sorted(counts.items())) or "no findings"))
        hidden = defaultdict(int)
        for f in ctx.findings:
            if f["new"] or show_all:
                out.append(fmt(f, win["today"][:4]))
            else:
                hidden[f["check"]] += 1
        if hidden:
            out.append("   older in the window, not printed (--all, or show <id prefix>): "
                       + ", ".join(f"{c} {n}" for c, n in sorted(hidden.items())))
    if MISSING_RATES:
        out.append(f"\nno rate to {config.reporting_currency()} for {', '.join(sorted(MISSING_RATES))}: amounts in "
                   "those currencies are counted at 1.0 in the thresholds (run the intercompany reconciliation, "
                   "or set default_rates in [intercompany_settings])")
    return "\n".join(out)


def last_review_start(now: dt.datetime) -> str | None:
    best = None
    cutoff = (now - dt.timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for p in sorted(LOGS.glob("review-*.log")):
        try:
            for m in re.finditer(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ) \[review\] start", p.read_text(), re.M):
                if m.group(1) < cutoff and (best is None or m.group(1) > best):
                    best = m.group(1)
        except OSError:
            continue
    return best[:16] if best else None


def windows(today: dt.date, frm: str | None) -> dict:
    w0 = dt.date.fromisoformat(frm) if frm else config.fy_quarter_start(today - dt.timedelta(days=30))
    ends = month_ends(today)
    return {"today": today.isoformat(), "from": w0.isoformat(), "baseline_from": add_months(w0, -3).isoformat(),
            "month_ends": ends, "tb_dates": ends + [today.isoformat()]}


def cmd_show(ident: str) -> int:
    runs = sorted(CHECKS.glob("*/findings.json"))
    if not runs:
        print("no findings yet: run review_checks.py first")
        return 1
    found = [f for f in json.loads(runs[-1].read_text())["findings"]
             if f["id"] == ident or f["id"].startswith(ident + "-")]
    print(json.dumps(found, indent=1, default=str) if found else f"{ident}: not in {runs[-1]}")
    return 0 if found else 1


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("entity", help="an entity key, slug, alias or short name | all | show")
    p.add_argument("ident", nargs="?", help="with show: a finding id, or an id prefix such as OPCO_US-S05")
    p.add_argument("--since", help="ISO time; records updated from then are new (default: the last review start)")
    p.add_argument("--from", dest="frm", help="window start, YYYY-MM-DD")
    p.add_argument("--today", help="as-at date, YYYY-MM-DD (default: today, UTC)")
    p.add_argument("--all", action="store_true", help="print old event findings too")
    p.add_argument("--refresh", action="store_true", help="pull again even if today's cache exists")
    p.add_argument("--cache-file", action="append", default=[], metavar="ENTITY=PATH",
                   help="read one entity's pull from this file instead of Xero")
    p.add_argument("--no-write", action="store_true", help="print only: no cache, findings or state written")
    a = p.parse_args(argv)
    if a.entity == "show":
        return cmd_show(a.ident or "")
    keys = order() if a.entity == "all" else [config.entity(a.entity).key]
    now = dt.datetime.now(dt.UTC)
    today = dt.date.fromisoformat(a.today) if a.today else now.date()
    win = windows(today, a.frm)
    since = a.since or last_review_start(now) or (today - dt.timedelta(days=4)).isoformat() + "T00:00"
    load_rates()
    cfg = Cfg()
    files = dict(s.split("=", 1) for s in a.cache_file)
    files = {config.entity(k).key: v for k, v in files.items()}
    write = not a.no_write

    if CLIENT_FACTORY is None and (
            any(k not in files and not cache_path(k, win["today"]).exists() for k in keys) or a.refresh):
        from accounting_agent import xero_auth
        xero_auth.get_access_token()     # refresh once, before the threads share the token
    data, failed = {}, {}
    with ThreadPoolExecutor(min(5, len(keys))) as ex:
        futs = {k: ex.submit(load_or_pull, k, win, a.refresh, write, files.get(k)) for k in keys}
        for k, f in futs.items():
            try:
                data[k] = f.result()
            except Exception as e:
                failed[k] = e
    rate_limited = [k for k, e in failed.items() if getattr(e, "status", None) == 429]
    state = {}
    try:
        state = json.loads((CHECKS / "state.json").read_text())
    except (OSError, ValueError):
        pass
    ctxs = run_checks(data, win, since, state, cfg) if data else {}
    text = summary(ctxs, win, since, a.all) if ctxs else ""
    for k, e in failed.items():
        text += f"\n\n!! {short_of(k)}: pull failed, no checks run: {type(e).__name__}: {str(e)[:200]}"
    if rate_limited:
        text += ("\n!! Xero daily rate limit reached for " + ", ".join(short_of(k) for k in rate_limited)
                 + ": stop and say so in the report; the job re-runs when the cap resets")
    print(text.strip("\n"))
    if write and ctxs:
        day = CHECKS / win["today"]
        day.mkdir(parents=True, exist_ok=True)
        fpath = day / "findings.json"
        try:
            prev = json.loads(fpath.read_text())
        except (OSError, ValueError):
            prev = {"findings": []}
        keep = [f for f in prev.get("findings", []) if f["entity"] not in ctxs]
        allf = keep + [f for c in ctxs.values() for f in c.findings]
        fpath.write_text(json.dumps({"window": win, "since": since, "findings": allf}, indent=1, default=str))
        (day / "summary.txt").write_text(text + "\n")
        for k, c in ctxs.items():
            lock, eoy = c.lock
            state[k] = {"lock": lock, "eoy": eoy, "seen": win["today"]}
        (CHECKS / "state.json").write_text(json.dumps(state, indent=1))
        print(f"\ndetail: {fpath} (review_checks.py show <id>)")
    if rate_limited:
        return 3
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
