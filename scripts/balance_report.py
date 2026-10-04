#!/usr/bin/env python
"""The engine behind the balance-account reports: prepayments, accruals,
deposits. One report per runner script, one workbook each, this file shared.

Each report answers the same question about a different balance-sheet account:
what is sitting in it, which invoice put it there, and what has been posted
against it month by month. So the machinery is one machine with a spec:

    scripts/prepayments_report.py   Prepayments and friends   ASSET
    scripts/accruals_report.py      Accruals                  LIABILITY
    scripts/deposits_report.py      Deposits held             ASSET

    data/reports/<Workbook>.xlsx        the single copy, overwritten
    data/reports/<report>_store.json    the store, accumulates

The group's shape comes from config/group.toml: the entities (key, Xero name,
short name, base currency), and under [reports] the year the reports show from,
how far back a first build reads, and per report the words that find its
accounts in each chart of accounts or an explicit list of codes per entity.

HOW IT IS BUILT. There is no accounting.journals.read scope and no
account-transactions report, so each account's ledger is reassembled from the
documents that moved it: posted manual journals, bank transactions, bills and
invoices, credit notes, and payments settled against the account. The same
reassembly the intercompany reconciliation uses, which ties to the Balance
Sheet movement, with one addition: the document's other lines are kept, so the
contra account (the expense being deferred, accrued or refunded) is known.

    ROW SIDE     the posting that creates the item: a debit for an asset
                 (prepayment, deposit), a credit for a liability (accrual).
                 Becomes a ROW: one row per invoice.
    CONTRA SIDE  everything posted against it afterwards: the monthly release,
                 the reversal, the refund. Becomes a CELL in a month column.

Month columns hold what POSTED journals did, never a plan or an expectation.
A prepayment whose releases stop halfway shows as a gap, which is the point.

ATTRIBUTION. A movement is pointed at the row it belongs to by, in order: an
explicit link stored by `link`; the invoice number in the narration; the
supplier name with exactly one candidate row open that month; the supplier name
with exactly one open row at all. Anything left is UNATTRIBUTED: it goes to
the Checks sheet, never guessed into a row, and `link` settles it for good.

One item posted twice is one row: `combine` folds a row into another, so a
prepayment raised in two journal lines, or a rounding top-up of a premium
already deferred, reads as the single item that clears to nil. Each posting
keeps the month it was posted in, so the balance still ties to the Balance
Sheet.

PERIODS (prepayments only) are read from the line description, which is where
the group's deferral rule (rules/EXPENSES.md) says to put them. What cannot be
parsed is left blank and listed in Checks; `set-period` fixes it and the store
keeps it.

REFRESH IS INCREMENTAL. Documents are pulled from the last sync less a
lookback, so a routine refresh is cheap. They are pulled by DOCUMENT date, so
an edit to an older journal is not seen: the difference against the Balance
Sheet is what catches that, and `--full` is the fix. Read-only throughout: this
engine never writes to Xero.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from accounting_agent import config  # noqa: E402

OUT_DIR = ROOT / "data" / "reports"

# How far back a refresh re-reads documents it has already seen. Long enough to
# catch a prior-month journal posted late, short enough to stay cheap.
LOOKBACK_DAYS = 95
# Rounding tolerance below which a difference is not a difference.
TOLERANCE = 0.02


# ------------------------------------------------------------------- the group

def entity_keys() -> list[str]:
    """Entity keys in config order, which is the order rows and blocks sort in."""
    return config.entity_keys()


def short(entity: str) -> str:
    return config.entity(entity).short


def base_ccy(entity: str) -> str:
    return config.entity(entity).base_currency


def xero_name(entity: str) -> str:
    return config.entity(entity).xero_name


def first_build_from() -> str:
    """First build with no store: how far back to reassemble the account.
    [reports] first_build_from, else the start of the previous financial year."""
    return str(config.report_settings().get("first_build_from")
               or config.default_history_start().isoformat())


def report_from_year() -> int:
    """A status report (prepayments, accruals, deposits) carries every row from
    this financial year on (named by the calendar year it ends in), active or
    inactive; an inactive row from before it is left out and carried by "rows
    not shown". Month columns start at its first month. Default: the current
    financial year."""
    return int(config.report_settings().get("from_year")
               or config.fy_year_for(dt.date.today()))


def report_from_month() -> str:
    """YYYY-MM of the first month of the from_year financial year (January of
    from_year with the default 12-31 year end)."""
    return f"{config.fy_start(report_from_year()):%Y-%m}"


def resolve_entity(name: str) -> str:
    try:
        return config.entity(name).key
    except config.ConfigError as exc:
        sys.exit(str(exc))


def label_of(entity: str) -> str:
    """The entity's short name, or its key if config no longer knows it."""
    try:
        return short(entity)
    except config.ConfigError:
        return entity


def ccy_of(entity: str, store: dict | None = None) -> str:
    try:
        return base_ccy(entity)
    except config.ConfigError:
        return ((store or {}).get("accounts", {}).get(entity) or {}).get("base", "")


# --------------------------------------------------------------------- spec

@dataclass(frozen=True)
class Spec:
    """One report. The differences between the three, and nothing else."""
    key: str                      # "prepayments"; also its [reports.<key>] table
    title: str                    # sheet and workbook title
    workbook: str                 # file name of the single copy
    account_class: str            # ASSET | LIABILITY
    account_words: tuple[str, ...]        # matched against the account name
    account_not_words: tuple[str, ...] = ()   # ... and these exclude it
    row_side: int = 1             # +1 the row is a debit, -1 the row is a credit
    amount_label: str = "Amount"  # the row-side posting
    moved_label: str = "Moved"    # what has been posted against it since
    open_label: str = "Open"      # what is left
    row_noun: str = "item"
    movement_noun: str = "movement"
    open_only: bool = False       # carry only rows that still hold a balance
    # status: every row from the from_year on, with a Status column reading
    # active (still holds a balance) or inactive (released, reversed, refunded,
    # settled), and the workbook's filter set so only the active rows show
    # when opened. All three reports use it.
    status: bool = False
    # publish: the workbook on the server is the master; every build puts a
    # copy in the Drive publish folder, "reports" subfolder
    # (accounting_agent.publish holds the routing).
    publish: bool = False
    schedule: bool = False        # a covered period and a monthly release
    stale_months: int | None = None   # flag a row still open after this long
    # "movements": month cells hold what was posted AGAINST the row, positive
    #              (a prepayment release). The row's own posting is a column.
    # "all":       month cells hold every posting including the row's own, in
    #              row-side orientation, so the row sums to what is still open.
    grid: str = "movements"
    blurb: str = ""

    @property
    def store_path(self) -> Path:
        return OUT_DIR / f"{self.key}_store.json"

    @property
    def workbook_path(self) -> Path:
        return OUT_DIR / self.workbook

    # The discovery words and explicit codes live in config/group.toml under
    # [reports.<key>]; the values on the spec are the defaults when it is silent.
    def settings(self) -> dict:
        return config.report_settings(self.key) or {}

    def words(self) -> tuple[str, ...]:
        return tuple(w.lower() for w in self.settings().get("account_words", self.account_words))

    def not_words(self) -> tuple[str, ...]:
        return tuple(w.lower() for w in
                     self.settings().get("account_not_words", self.account_not_words))

    def explicit_codes(self, entity: str) -> list[str]:
        accounts = self.settings().get("accounts") or {}
        return [str(c) for c in accounts.get(entity, [])]


PREPAYMENTS = Spec(
    key="prepayments",
    title="Prepayments",
    workbook="Prepayments.xlsx",
    account_class="ASSET",
    account_words=("prepay", "prepaid"),
    row_side=1,
    amount_label="Prepaid",
    moved_label="Released",
    open_label="Remaining",
    row_noun="prepaid invoice",
    movement_noun="release",
    open_only=False,
    status=True,
    publish=True,
    schedule=True,
    grid="movements",
    blurb=("Every prepaid invoice still active, and every one raised or moved "
           "since {since}, with the amount that posted journals released in each "
           "month. Status is active while a balance remains; the filter shows "
           "active rows, clear it for the rest."),
)

ACCRUALS = Spec(
    key="accruals",
    title="Accruals",
    workbook="Accruals.xlsx",
    account_class="LIABILITY",
    account_words=("accrual", "accrued"),
    account_not_words=("income", "receivable", "revenue"),
    row_side=-1,
    amount_label="Accrued",
    moved_label="Reversed",
    open_label="Open",
    row_noun="accrual",
    movement_noun="reversal",
    open_only=False,
    status=True,
    publish=True,
    schedule=False,
    stale_months=4,
    grid="all",
    blurb=("Every accrual still active, and every one raised or reversed since "
           "{since}, in the month it was posted. Status is active while a balance "
           "remains; the filter shows active rows, clear it for the rest."),
)

DEPOSITS = Spec(
    key="deposits",
    title="Deposits",
    workbook="Deposits.xlsx",
    account_class="ASSET",
    account_words=("deposit",),
    account_not_words=("customer", "client"),   # those are a liability anyway
    row_side=1,
    amount_label="Deposit",
    moved_label="Returned",
    open_label="Open",
    row_noun="deposit",
    movement_noun="refund",
    open_only=False,
    status=True,
    publish=True,
    schedule=False,
    grid="all",
    blurb=("Every deposit still held, and every one paid or refunded since "
           "{since}, in the month it was paid. Status is active while a balance "
           "remains; the filter shows active rows, clear it for the rest."),
)

SPECS = {s.key: s for s in (PREPAYMENTS, ACCRUALS, DEPOSITS)}


# ------------------------------------------------------------------- months

def month_of(date: str) -> str:
    return (date or "")[:7]


def month_label(month: str) -> str:
    y, m = month.split("-")
    return f"{dt.date(int(y), int(m), 1):%b %y}"


def month_add(month: str, n: int) -> str:
    y, m = (int(x) for x in month.split("-"))
    total = y * 12 + (m - 1) + n
    return f"{total // 12:04d}-{total % 12 + 1:02d}"


def month_range(first: str, last: str) -> list[str]:
    out, cur = [], first
    while cur <= last:
        out.append(cur)
        cur = month_add(cur, 1)
    return out


def months_between(first: str, last: str) -> int:
    y1, m1 = (int(x) for x in first.split("-"))
    y2, m2 = (int(x) for x in last.split("-"))
    return (y2 * 12 + m2) - (y1 * 12 + m1) + 1


def this_month() -> str:
    return f"{dt.date.today():%Y-%m}"


def last_closed_month() -> str:
    return month_add(this_month(), -1)


def parse_month(text: str) -> str | None:
    """"Jan 26" / "January 2026" / "2026-01" / "30 Sep 2026" -> "2026-01"."""
    text = (text or "").strip()
    m = re.fullmatch(r"(\d{4})-(\d{1,2})(?:-\d{1,2})?", text)
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}"
    m = re.search(r"([A-Za-z]{3,9})\.?\s*'?(\d{2,4})", text)
    if not m:
        return None
    try:
        mon = dt.datetime.strptime(m.group(1)[:3].title(), "%b").month
    except ValueError:
        return None
    year = int(m.group(2))
    year += 2000 if year < 100 else 0
    return f"{year:04d}-{mon:02d}"


# A period as it is written on an invoice line: "Jan 26 - Dec 26",
# "31 May 26 - 30 May 27", "2026-01-01 to 2026-12-31", "1 Jan 26 to 31 Dec 26".
# The separator also accepts the en and em dash characters people paste in.
_SEP = r"\s*(?:-|\u2013|\u2014|to|until|through|thru)\s*"
_DATE = (r"(?:\d{4}-\d{2}-\d{2}"
         r"|(?:\d{1,2}(?:st|nd|rd|th)?\s+)?[A-Za-z]{3,9}\.?\s*'?\d{2,4}"
         r"|\d{1,2}[/.]\d{1,2}[/.]\d{2,4})")
_PERIOD = re.compile(rf"({_DATE}){_SEP}({_DATE})", re.I)


def parse_period(text: str) -> tuple[str, str] | None:
    for m in _PERIOD.finditer(text or ""):
        start, end = parse_month(m.group(1)), parse_month(m.group(2))
        if start and end and start <= end:
            return start, end
    return None


# -------------------------------------------------------------------- store

def load_store(spec: Spec) -> dict:
    if spec.store_path.exists():
        return json.loads(spec.store_path.read_text())
    return {"report": spec.key, "version": 1, "accounts": {}, "synced": {},
            "rows": {}, "moves": {}, "xero": {}, "notes": {}}


def save_store(spec: Spec, store: dict) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    spec.store_path.write_text(json.dumps(store, indent=1, sort_keys=True))


# ---------------------------------------------------------------- GL rebuild

_MS = re.compile(r"/Date\((-?\d+)")


def _d(v) -> str:
    """A Xero date ("/Date(1767225600000+0000)/" or ISO) -> "YYYY-MM-DD"."""
    if not v:
        return ""
    m = _MS.match(str(v))
    if m:
        return dt.datetime.fromtimestamp(int(m.group(1)) / 1000, dt.UTC).date().isoformat()
    return str(v)[:10]


def to_base(amount: float, rate) -> float:
    """Document-currency amount -> entity base currency.

    Xero's CurrencyRate is INVERTED (foreign units per 1 base unit), so
    base = amount / rate, never amount * rate. Check it once against the
    Balance Sheet on a foreign-currency document when adopting: only the
    division ties.
    """
    try:
        r = float(rate)
        return amount / r if r else float(amount)
    except (TypeError, ValueError, ZeroDivisionError):
        return float(amount)


@dataclass
class Move:
    """One posting on one of the report's accounts, base currency, debit +."""
    entity: str
    code: str
    account: str
    date: str
    base: float
    doc_amount: float
    currency: str
    source: str
    party: str
    desc: str
    ref: str
    doc_id: str
    contra: str = ""
    seq: int = field(default=1)


def _contra(lines: list[dict], hit_amount: float, names: dict[str, str],
            codes: set[str]) -> str:
    """The account on the other side of the document: the biggest line whose
    sign is opposite the hit and which is not one of the report's own."""
    best, best_abs = "", 0.0
    for ln in lines:
        code = str(ln.get("AccountCode") or "")
        amount = float(ln.get("LineAmount") or 0)
        if not code or code in codes or amount == 0:
            continue
        if (amount > 0) == (hit_amount > 0):
            continue
        if abs(amount) > best_abs:
            best, best_abs = names.get(code, code), abs(amount)
    return best


def pull_moves(entity: str, codes: set[str], names: dict[str, str],
               date_from: str, date_to: str) -> list[Move]:
    """Every posted movement on `codes` in one entity, between two dates.

    The five sources the intercompany reconciliation uses (that set ties to
    the Balance Sheet movement), with the document's other lines read for the
    contra account.
    """
    from accounting_agent.xero import XeroClient
    from accounting_agent.xero.banking import get_bank_transactions, get_payments
    from accounting_agent.xero.journals import get_manual_journals
    from accounting_agent.xero.purchases import get_bills, get_supplier_credit_notes
    from accounting_agent.xero.sales import get_credit_notes, get_invoices

    c = XeroClient(xero_name(entity))
    codes = {str(x) for x in codes if x}
    if not codes:
        return []
    out: list[Move] = []
    seen: dict[str, int] = {}
    ccy = base_ccy(entity)

    def add(code, date, doc_amt, rate, cur, source, party, desc, ref, doc_id, contra):
        doc_id = doc_id or f"{source}:{date}"
        seen[doc_id] = seen.get(doc_id, 0) + 1
        out.append(Move(entity, str(code), names.get(str(code), str(code)), date,
                        to_base(doc_amt, rate), doc_amt, cur or ccy,
                        source, (party or "")[:60], (desc or "")[:160],
                        (ref or "")[:40], doc_id, contra, seen[doc_id]))

    for mj in get_manual_journals(c, status="POSTED", date_from=date_from, date_to=date_to):
        lines = mj.get("JournalLines") or []
        for ln in lines:
            if str(ln.get("AccountCode")) in codes:
                amount = float(ln.get("LineAmount") or 0)
                add(ln["AccountCode"], _d(mj.get("DateString") or mj.get("Date")),
                    amount, None, None, "MJ", mj.get("Narration"),
                    ln.get("Description") or mj.get("Narration"), "",
                    mj.get("ManualJournalID"), _contra(lines, amount, names, codes))

    for bt in get_bank_transactions(c, from_date=date_from, to_date=date_to):
        if bt.get("Status") in ("DELETED", "VOIDED"):
            continue
        sign = 1.0 if bt.get("Type", "").startswith("SPEND") else -1.0
        lines = bt.get("LineItems") or []
        for ln in lines:
            if str(ln.get("AccountCode")) in codes:
                add(ln["AccountCode"], _d(bt.get("DateString") or bt.get("Date")),
                    sign * float(ln.get("LineAmount") or 0), bt.get("CurrencyRate"),
                    bt.get("CurrencyCode"), "BANK", (bt.get("Contact") or {}).get("Name"),
                    ln.get("Description"), bt.get("Reference"),
                    bt.get("BankTransactionID"), "")

    for kind, docs, sgn in (
        ("BILL", get_bills(c, date_from=date_from, date_to=date_to), 1.0),
        ("INV", get_invoices(c, date_from=date_from, date_to=date_to), -1.0),
        ("SUPP_CN", get_supplier_credit_notes(c, date_from=date_from, date_to=date_to), -1.0),
        ("CN", get_credit_notes(c, date_from=date_from, date_to=date_to), 1.0),
    ):
        for doc in docs:
            if doc.get("Status") in ("DELETED", "VOIDED", "DRAFT"):
                continue
            lines = doc.get("LineItems") or []
            for ln in lines:
                if str(ln.get("AccountCode")) in codes:
                    add(ln["AccountCode"], _d(doc.get("DateString") or doc.get("Date")),
                        sgn * float(ln.get("LineAmount") or 0), doc.get("CurrencyRate"),
                        doc.get("CurrencyCode"), kind, (doc.get("Contact") or {}).get("Name"),
                        ln.get("Description"),
                        doc.get("InvoiceNumber") or doc.get("CreditNoteNumber"),
                        doc.get("InvoiceID") or doc.get("CreditNoteID"),
                        _contra(lines, sgn * float(ln.get("LineAmount") or 0), names, codes))

    # A bill settled FROM one of these accounts moves it with no line item.
    for p in get_payments(c, from_date=date_from, to_date=date_to, status="AUTHORISED"):
        if str((p.get("Account") or {}).get("Code")) in codes:
            inv = p.get("Invoice") or {}
            sgn = -1.0 if inv.get("Type") == "ACCPAY" else 1.0
            add((p.get("Account") or {})["Code"], _d(p.get("Date")),
                sgn * float(p.get("Amount") or 0), p.get("CurrencyRate"),
                p.get("CurrencyCode"), "PAY", (inv.get("Contact") or {}).get("Name"),
                "settlement of " + (inv.get("InvoiceNumber") or ""),
                p.get("Reference"), p.get("PaymentID"), "")

    out.sort(key=lambda m: (m.date, m.doc_id, m.desc))
    return out


def account_names(client) -> dict[str, str]:
    return {str(a["Code"]): f"{a['Code']} {a['Name']}"
            for a in client.get("Accounts")["Accounts"] if a.get("Code")}


def report_codes(spec: Spec, client, entity: str, store: dict) -> list[str]:
    """The entity's accounts for this report.

    An explicit list in config/group.toml ([reports.<key>] accounts) wins
    outright. Otherwise they are discovered from the chart of accounts by class
    and name (the words in the same table), then cached. An account the
    discovery misses is added either to the config list or by hand to
    store["accounts"][entity]["codes"]; a cached list is never overwritten, so
    the addition survives every refresh.
    """
    explicit = spec.explicit_codes(entity)
    if explicit:
        store["accounts"][entity] = {"codes": sorted(explicit), "base": base_ccy(entity),
                                     "from": "config"}
        return sorted(explicit)
    cached_entry = store["accounts"].get(entity) or {}
    cached = cached_entry.get("codes")
    if cached and cached_entry.get("from") != "config":
        return [str(c) for c in cached]
    words, not_words = spec.words(), spec.not_words()
    codes = []
    for a in client.get("Accounts")["Accounts"]:
        name = (a.get("Name") or "").lower()
        if a.get("Class") != spec.account_class or a.get("Type") == "BANK":
            continue
        if not any(w in name for w in words):
            continue
        if any(w in name for w in not_words):
            continue
        codes.append(str(a["Code"]))
    if not codes:
        print(f"  {short(entity)}: no {spec.key} account in the chart of accounts")
        codes = []
    store["accounts"][entity] = {"codes": sorted(codes), "base": base_ccy(entity)}
    return sorted(codes)


# ------------------------------------------------------------------ refresh

def row_key(entity: str, m: Move) -> str:
    return f"{entity}|{m.doc_id}" if m.seq == 1 else f"{entity}|{m.doc_id}|{m.seq}"


def move_key(entity: str, m: Move) -> str:
    return f"{entity}|{m.doc_id}|m{m.seq}"


# Row fields `set-field` may set by hand that a refresh would otherwise rewrite
# from the document. The hand-set value is kept under "_<field>" and put back.
HAND_SET = ("supplier", "number")


def refresh_entity(spec: Spec, entity: str, store: dict, full: bool) -> None:
    from accounting_agent.xero import XeroClient

    client = XeroClient(xero_name(entity))
    names = account_names(client)
    codes = report_codes(spec, client, entity, store)
    if not codes:
        store["synced"][entity] = dt.date.today().isoformat()
        return

    today = dt.date.today()
    synced = store["synced"].get(entity)
    date_from = first_build_from() if (full or not synced) else (
        dt.date.fromisoformat(synced) - dt.timedelta(days=LOOKBACK_DAYS)).isoformat()
    # Releases are posted forward when a bill lands, so look past today.
    date_to = (today + dt.timedelta(days=400)).isoformat()

    moves = pull_moves(entity, set(codes), names, date_from, date_to)
    seen_rows, seen_moves = set(), set()
    for m in moves:
        is_row = (m.base > 0) if spec.row_side > 0 else (m.base < 0)
        if is_row:
            key = row_key(entity, m)
            seen_rows.add(key)
            row = store["rows"].get(key, {})
            fixed = row.get("fixed") or {}
            row.update({
                "entity": entity, "code": m.code, "account": m.account,
                "date": m.date, "supplier": m.party, "source": m.source,
                "doc_id": m.doc_id, "number": m.ref, "currency": m.currency,
                "doc_amount": round(abs(m.doc_amount), 2),
                "amount": round(m.base * spec.row_side, 2),
                "description": m.desc,
            })
            # A value set by hand is marked fixed and survives every refresh.
            for name in HAND_SET:
                if name in fixed and f"_{name}" in row:
                    row[name] = row[f"_{name}"]
            if m.contra and "expense_account" not in fixed:
                row["expense_account"] = m.contra
            if spec.schedule and "period" not in fixed:
                period = parse_period(f"{m.desc} {m.ref}")
                if period:
                    row["period_start"], row["period_end"] = period
            row.setdefault("period_start", None)
            row.setdefault("period_end", None)
            store["rows"][key] = row
        else:
            key = move_key(entity, m)
            seen_moves.add(key)
            mv = store["moves"].get(key, {})
            mv.update({
                "entity": entity, "code": m.code, "date": m.date,
                "month": month_of(m.date),
                "amount": round(-m.base * spec.row_side, 2),   # + reduces the row
                "narration": f"{m.party} {m.desc}".strip(),
                "contra": m.contra, "source": m.source, "doc_id": m.doc_id,
            })
            store["moves"][key] = mv

    # A document re-read in the window but no longer on the account was voided
    # or recoded: drop it rather than carry a balance Xero no longer has.
    for key, row in list(store["rows"].items()):
        if row["entity"] == entity and row["date"] >= date_from and key not in seen_rows:
            del store["rows"][key]
    for key, mv in list(store["moves"].items()):
        if mv["entity"] == entity and mv["date"] >= date_from and key not in seen_moves:
            del store["moves"][key]

    attribute(spec, entity, store)
    fill_expense_accounts(spec, entity, store)
    store["xero"][entity] = pull_xero_balances(spec, entity, codes)
    store["synced"][entity] = today.isoformat()


def pull_balances(entity: str, as_at: str, periods: int = 8) -> dict[str, dict[str, float]]:
    """{account_code: {period_label: closing_balance}} from the Balance Sheet.

    Balance Sheet sign convention: the value is the balance as PRESENTED in
    its section, so an asset row is + when debit and a liability row is + when
    credit. Normalised here to DEBIT-POSITIVE.
    """
    from accounting_agent.xero import XeroClient
    from accounting_agent.xero.reports import balance_sheet

    c = XeroClient(xero_name(entity))
    rep = balance_sheet(c, date=as_at, periods=periods, timeframe="MONTH")["Reports"][0]
    hdr = [cell["Value"] for cell in rep["Rows"][0]["Cells"]][1:]
    accounts = {a["AccountID"]: a for a in c.get("Accounts")["Accounts"]}

    def walk(rows):
        for r in rows:
            if r.get("RowType") == "Section":
                yield from walk(r.get("Rows", []))
            elif r.get("RowType") == "Row":
                yield r

    out: dict[str, dict[str, float]] = {}
    for r in walk(rep["Rows"]):
        cells = r["Cells"]
        aid = (cells[0].get("Attributes") or [{}])[0].get("Value")
        a = accounts.get(aid)
        if not a or not a.get("Code"):
            continue
        flip = 1.0 if a.get("Class") in ("ASSET", "EXPENSE") else -1.0
        vals = {}
        for label, cell in zip(hdr, cells[1:]):
            try:
                vals[label] = flip * float(cell.get("Value") or 0)
            except ValueError:
                vals[label] = 0.0
        out[str(a["Code"])] = vals
    return out


def pull_xero_balances(spec: Spec, entity: str, codes: list[str]) -> dict[str, float]:
    """The account balance at every month end the Balance Sheet will report.

    Twelve monthly columns per call, two calls, so two years are covered.
    Base currency, and signed the way the report reads: positive is a balance
    held, whether the account is an asset or a liability.
    """
    out: dict[str, float] = {}
    as_at = dt.date.today().replace(day=1) - dt.timedelta(days=1)
    for _ in range(2):
        balances = pull_balances(entity, as_at.isoformat(), periods=11)
        for code in codes:
            for label, value in (balances.get(code) or {}).items():
                month = parse_month(label)
                if month:
                    # pull_balances is debit-positive; a liability reads as the
                    # negative of the balance it holds.
                    out[month] = round(out.get(month, 0.0) + value * spec.row_side, 2)
        as_at = _minus_months(as_at, 12)
    return out


def _minus_months(date: dt.date, n: int) -> dt.date:
    """The month end n months before this one."""
    y, m = date.year, date.month - n
    while m <= 0:
        m, y = m + 12, y - 1
    first_next = dt.date(y + (m == 12), m % 12 + 1, 1)
    return first_next - dt.timedelta(days=1)


# -------------------------------------------------------------- attribution

# Words that appear in every second journal narration on these accounts. A row
# created by a journal carries its narration where a bill carries a contact, so
# without this every row shares "prepayment" with every movement and nothing can
# be told apart.
NOISE = {
    "prepayment", "prepayments", "prepaid", "amortisation", "amortization",
    "amortise", "amortised", "amortize", "amortized", "release", "released",
    "releases", "accrual", "accruals", "accrued", "accrue", "deposit",
    "deposits", "reversal", "reverse", "reversed", "adjustment", "adjustments",
    "adjust", "reclass", "reclassify", "restate", "correction", "journal",
    "entry", "monthly", "month", "annual", "annually", "yearly", "quarterly",
    "subscription", "subscriptions", "licence", "license", "invoice", "fee",
    "fees", "cost", "costs", "expense", "expenses", "the", "and", "for", "from",
    "into", "with", "was", "not", "per", "pro", "rata", "balance", "account",
    "clear", "cleared", "against", "full", "final", "residual", "instead",
    "ended", "period", "bill", "credit", "debit", "recognised", "recognized",
}
# Dates match everything: a narration saying "Apr 2026" would otherwise score
# against every row whose period or description mentions April or 2026.
NOISE |= {m.lower() for m in (
    "jan january feb february mar march apr april may jun june jul july "
    "aug august sep sept september oct october nov november dec december"
).split()}


def _tokens(text: str) -> set[str]:
    """Words worth matching on: long enough, not filler, and not a number."""
    words = {w for w in re.split(r"[^a-z0-9]+", (text or "").lower())
             if len(w) > 2 and not w.isdigit()}
    return words - NOISE


def combined_into(store: dict, key: str) -> str | None:
    return (store["rows"].get(key) or {}).get("combined_into")


def row_root(store: dict, key: str) -> str:
    """The row a combined row is shown on, following the chain to the end.

    One item posted twice (a prepayment raised in two lines of the same
    journal, a small rounding top-up of the same premium) is one row on the
    report, set by `combine`. Everything else reads the root and never the
    rows folded into it.
    """
    seen = {key}
    while True:
        parent = combined_into(store, key)
        if not parent or parent in seen or parent not in store["rows"]:
            return key
        key = parent
        seen.add(key)


def combined_children(store: dict, key: str) -> list[str]:
    return sorted(k for k in store["rows"]
                  if k != key and row_root(store, k) == key)


def attribute(spec: Spec, entity: str, store: dict) -> None:
    """Point every movement at the row it moves, or leave it unattributed."""
    rows = {k: r for k, r in store["rows"].items()
            if r["entity"] == entity and not combined_into(store, k)}
    for mv in store["moves"].values():
        if mv["entity"] != entity:
            continue
        if mv.get("unattributed"):                # set by `unlink`: belongs to no row
            mv["row"], mv["weak"] = None, False
            continue
        if mv.get("linked"):                      # set by `link`, never overridden
            mv["row"] = row_root(store, mv["linked"])
            mv["weak"] = False                    # a link is not a guess
            continue
        blob = (mv.get("narration") or "").lower()
        words = _tokens(blob)
        weak = False
        same_account = {k: r for k, r in rows.items() if r["code"] == mv["code"]}

        # The invoice number is the only unambiguous handle there is.
        hits = [k for k, r in same_account.items()
                if r.get("number") and len(r["number"]) > 3
                and r["number"].lower() in blob]

        if len(hits) != 1:
            # Otherwise the name, scored: the row sharing the most real words
            # wins outright, and a tie is broken by the period covering the
            # month. Still tied means genuinely ambiguous (two rows for the
            # same consultant and one reversal naming neither) and that is
            # left alone.
            scored: dict[str, int] = {}
            for k, r in same_account.items():
                text = [r["supplier"], r.get("description", "")]
                for ck in combined_children(store, k):   # a combined row answers
                    child = store["rows"][ck]            # to its own words too
                    text += [child["supplier"], child.get("description", "")]
                shared = set().union(*(_tokens(t) for t in text)) & words
                if shared:
                    scored[k] = len(shared)
            if scored:
                best = max(scored.values())
                hits = [k for k, n in scored.items() if n == best]
                if len(hits) > 1:
                    covering = [k for k in hits
                                if same_account[k].get("period_start")
                                and same_account[k]["period_start"] <= mv["month"]
                                <= same_account[k]["period_end"]]
                    hits = covering if len(covering) == 1 else hits
                if len(hits) > 1:
                    # Two rows for the same counterparty and nothing in the
                    # narration to tell them apart. Take the row that can
                    # actually absorb the movement, largest balance first, then
                    # the oldest (what a person does) and mark it, so the
                    # Checks sheet says the choice was made on the balance
                    # rather than on anything the journal said.
                    def fit(k: str) -> tuple:
                        left = remaining(store, k, same_account[k])
                        return (left + TOLERANCE < mv["amount"], -left,
                                same_account[k]["date"])
                    hits = sorted(hits, key=fit)[:1]
                    weak = True
            else:
                hits = []

        if len(hits) != 1:
            # One open row on the account and one movement: no ambiguity.
            open_rows = [k for k, r in same_account.items()
                         if abs(remaining(store, k, r)) > TOLERANCE]
            hits = open_rows if len(open_rows) == 1 else hits
        mv["row"] = hits[0] if len(hits) == 1 else None
        mv["weak"] = bool(weak and mv["row"])


def fill_expense_accounts(spec: Spec, entity: str, store: dict) -> None:
    """A prepayment's expense account is only known from its releases."""
    for key, row in store["rows"].items():
        if row["entity"] != entity or (row.get("fixed") or {}).get("expense_account"):
            continue
        if row.get("expense_account"):
            continue
        contras = [mv["contra"] for mv in store["moves"].values()
                   if mv.get("row") == key and mv.get("contra")]
        if contras:
            row["expense_account"] = max(set(contras), key=contras.count)


def moved_by_month(store: dict, key: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for mv in store["moves"].values():
        if mv.get("row") == key:
            out[mv["month"]] = round(out.get(mv["month"], 0.0) + mv["amount"], 2)
    return out


def unattributed_by_month(store: dict, entity: str) -> dict[str, float]:
    """Movements on the account that belong to no row on the sheet.

    They are real ledger movements whatever the report makes of them, so they
    carry into the balance and show as their own line. Leaving them out is what
    turns the check against the Balance Sheet into noise.
    """
    out: dict[str, float] = {}
    for mv in store["moves"].values():
        if mv["entity"] == entity and mv.get("row") is None:
            out[mv["month"]] = round(out.get(mv["month"], 0.0) + mv["amount"], 2)
    return out


def hidden_net_by_month(spec: Spec, store: dict, entity: str,
                        shown: set[str]) -> dict[str, float]:
    """Net movement of the rows the sheet does not show.

    A prepayment added and released to nil inside the window, an accrual raised
    and reversed, a deposit paid and refunded: the sheet leaves them out, but
    the ledger had them, so the balance moves. Without this line the sheet
    disagrees with Xero in exactly those months for no real reason.
    """
    out: dict[str, float] = {}
    for key, row in store["rows"].items():
        if row["entity"] != entity or key in shown or combined_into(store, key):
            continue
        for month, amount in row_additions(store, key, row).items():
            out[month] = round(out.get(month, 0.0) + amount, 2)
        for month, amount in moved_by_month(store, key).items():
            out[month] = round(out.get(month, 0.0) - amount, 2)
    return out


def row_additions(store: dict, key: str, row: dict) -> dict[str, float]:
    """The row-side postings the row carries, in the month each was posted.

    Its own, plus every row combined into it. The month is each posting's own:
    a top-up posted a year after the invoice it corrects moved the account in
    the month it was posted, whatever row the report shows it on.
    """
    out = {month_of(row["date"]): row["amount"]}
    for ck in combined_children(store, key):
        child = store["rows"][ck]
        month = month_of(child["date"])
        out[month] = round(out.get(month, 0.0) + child["amount"], 2)
    return out


def row_amount(store: dict, key: str, row: dict) -> float:
    return round(sum(row_additions(store, key, row).values()), 2)


def remaining(store: dict, key: str, row: dict) -> float:
    return round(row_amount(store, key, row)
                 - sum(moved_by_month(store, key).values()), 2)


def cleared_month(spec: Spec, store: dict, entity: str) -> str | None:
    """The last month end at which Xero says the account held nothing.

    An account cleared to nil has nothing open in it, whatever the report's
    own attribution made of the postings: the balance is the truth and a row
    still showing as open on or before that month is a reversal the report
    could not tie back, not a liability anyone still owes. So that month is a
    line under the account: everything raised up to it is settled.
    """
    xero = store["xero"].get(entity) or {}
    nil = [m for m, v in xero.items() if abs(v) <= TOLERANCE]
    return max(nil) if nil else None


def is_settled(spec: Spec, store: dict, row: dict) -> bool:
    """Nothing left in it: cleared by hand, or raised before the account was nil."""
    if row.get("cleared"):
        return True
    cut = cleared_month(spec, store, row["entity"])
    return bool(cut) and month_of(row["date"]) <= cut


def remaining_today(store: dict, key: str, row: dict) -> float:
    """What the row holds as at this month: postings dated after this month
    are already in Xero but have not happened yet, so they do not count."""
    now = this_month()
    return round(row_amount(store, key, row)
                 - sum(a for m, a in moved_by_month(store, key).items() if m <= now), 2)


def is_active(spec: Spec, store: dict, key: str, row: dict) -> bool:
    """Still holds a balance today and nobody has settled it: the row is live.

    As at this month, not after every posted movement: a prepayment whose
    twelve releases are all posted ahead still has money in the account until
    the last of them falls due.
    """
    return (abs(remaining_today(store, key, row)) > TOLERANCE
            and not is_settled(spec, store, row))


def status_of(spec: Spec, store: dict, key: str, row: dict) -> str:
    return "active" if is_active(spec, store, key, row) else "inactive"


def last_month_touched(store: dict, key: str, row: dict) -> str:
    """The latest month (YYYY-MM) the row was posted or moved in."""
    months = set(row_additions(store, key, row)) | set(moved_by_month(store, key))
    return max(months)


# ------------------------------------------------------------------ shaping

def visible_rows(spec: Spec, store: dict, entity: str | None = None) -> list[tuple[str, dict]]:
    """Which rows the workbook carries.

    A status report carries every active row, and every inactive row that was
    raised or moved in the from_year or later, marked active or inactive in
    its own column. An inactive row older than that is left out. open_only
    reports carry what still holds a balance and nothing else: a deposit
    refunded in full is gone. Any other report carries anything open plus
    anything that moved this financial year.

    A row raised on or before the month the account was last cleared to nil
    holds nothing, whatever the attribution made of it, and is inactive. What
    leaves is carried by "rows not shown", so the balance is still the whole
    account.
    """
    fy_year = config.fy_year_for(dt.date.today())
    fy_lo, fy_hi = config.fy_start(fy_year).isoformat(), config.fy_end(fy_year).isoformat()
    since = report_from_month()
    out = []
    for key, row in store["rows"].items():
        if entity and row["entity"] != entity:
            continue
        if combined_into(store, key):     # shown on the row it was combined into
            continue
        if spec.status:
            if (is_active(spec, store, key, row)
                    or last_month_touched(store, key, row) >= since):
                out.append((key, row))
            continue
        if row.get("cleared"):            # settled by hand, carried in "rows not shown"
            continue
        if is_active(spec, store, key, row):
            out.append((key, row))
        elif not spec.open_only:
            moved = (fy_lo <= row["date"] <= fy_hi
                     or any(fy_lo[:7] <= m <= fy_hi[:7] for m in moved_by_month(store, key)))
            if moved:
                out.append((key, row))
    order = entity_keys()

    def rank(e: str) -> int:
        # An entity removed from config keeps its rows in the store; sort it last.
        return order.index(e) if e in order else len(order)

    out.sort(key=lambda kv: (rank(kv[1]["entity"]),
                             kv[1]["supplier"].lower(), kv[1]["date"]))
    return out


def column_months(spec: Spec, store: dict, rows: list[tuple[str, dict]]) -> list[str]:
    months = set()
    for key, row in rows:
        months.update(row_additions(store, key, row))
        if row.get("period_start"):
            months.update(month_range(row["period_start"], row["period_end"]))
        months.update(moved_by_month(store, key))
    if not months:
        return []
    # Columns start at the first month of the from_year: what a row did before that is
    # carried in one "Before" column, and the totals block opens from Xero's
    # own balance at the month before.
    first = max(min(months), report_from_month())
    last = max(months)
    return month_range(first, max(last, month_add(this_month(), 1)))


def period_text(row: dict) -> str:
    if not row.get("period_start"):
        return ""
    return f"{month_label(row['period_start'])} - {month_label(row['period_end'])}"


def monthly_release(store: dict, key: str, row: dict) -> float | None:
    if not row.get("period_start"):
        return None
    n = months_between(row["period_start"], row["period_end"])
    if not n:
        return None
    # Where part of the invoice was a one-off cost recognised at once, only the
    # rest is scheduled (the deferral rule in rules/EXPENSES.md):
    # `set-field <row> scheduled <amt>`.
    base = row.get("scheduled")
    return round((base if base is not None else row_amount(store, key, row)) / n, 2)


def row_cells(spec: Spec, store: dict, key: str, row: dict) -> dict[str, float]:
    """What goes in the month columns for one row."""
    cells = moved_by_month(store, key)
    if spec.grid == "movements":
        return cells
    out = dict(row_additions(store, key, row))
    for month, amount in cells.items():
        out[month] = round(out.get(month, 0.0) - amount, 2)
    return out


def balances_by_month(spec: Spec, store: dict, entity: str,
                      rows: list[tuple[str, dict]]) -> dict[str, float]:
    """Closing balance per month from the rows the workbook shows: the same
    arithmetic the workbook's balance row does in formulas.

    Carried across every month Xero reports as well, not just the months the
    report has movement in: a month where the report does nothing and Xero
    moves is exactly the month a difference has to surface.
    """
    net: dict[str, float] = {}
    for key, row in rows:
        if row["entity"] != entity:
            continue
        for month, amount in row_additions(store, key, row).items():
            net[month] = round(net.get(month, 0.0) + amount, 2)
        for month, amount in moved_by_month(store, key).items():
            net[month] = round(net.get(month, 0.0) - amount, 2)
    for month, amount in unattributed_by_month(store, entity).items():
        net[month] = round(net.get(month, 0.0) - amount, 2)
    shown = {k for k, r in rows if r["entity"] == entity}
    for month, amount in hidden_net_by_month(spec, store, entity, shown).items():
        net[month] = round(net.get(month, 0.0) + amount, 2)
    months = sorted(set(net) | set(store["xero"].get(entity) or {}))
    if not months:
        return {}
    out: dict[str, float] = {}
    balance = opening_balance(spec, store, entity, months[0])
    for month in month_range(months[0], months[-1]):
        balance += net.get(month, 0.0)
        out[month] = round(balance, 2)
    return out


def opening_balance(spec: Spec, store: dict, entity: str, first_month: str) -> float:
    """What the account held before the first month the sheet shows.

    Xero's own balance at the month before is the answer where it has one: it
    includes whatever was brought forward from before the rebuild window, which
    the documents cannot show. Derived from the rows only as a fallback.
    """
    before = month_add(first_month, -1)
    xero = store["xero"].get(entity) or {}
    if before in xero:
        return round(xero[before], 2)
    total = 0.0
    for key, row in store["rows"].items():
        if row["entity"] != entity or combined_into(store, key):
            continue
        for month, amount in row_additions(store, key, row).items():
            if month < first_month:
                total += amount
        for month, amount in moved_by_month(store, key).items():
            if month < first_month:
                total -= amount
    return round(total, 2)


def additions_by_month(spec: Spec, store: dict, entity: str,
                       rows: list[tuple[str, dict]]) -> dict[str, float]:
    out: dict[str, float] = {}
    for key, row in rows:
        if row["entity"] != entity:
            continue
        for month, amount in row_additions(store, key, row).items():
            out[month] = round(out.get(month, 0.0) + amount, 2)
    return out


# ------------------------------------------------------------------ workbook

def build_workbook(spec: Spec, store: dict) -> Path:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter as col

    rows = visible_rows(spec, store)
    months = column_months(spec, store, rows)
    head = ["Entity"]
    if spec.status:
        head += ["Status"]
    head += ["Supplier", "Description", "Account", "Expense account",
             "Invoice number", "Invoice date", "Posted", "CCY", "Invoice amount",
             spec.amount_label]
    if spec.schedule:
        head += ["Period", "Months", "Monthly release"]
    # Everything the row did before the first month column, in one cell, so
    # the row's own total still adds up when the early months are not shown.
    before_label = f"Before {month_label(months[0])}" if months else "Before"
    # ... and what the row holds as at this month, which is what Status reads:
    # postings dated ahead are in Xero already but have not fallen due.
    today_label = f"{spec.open_label} at {month_label(this_month())}"
    head += [before_label, spec.moved_label, spec.open_label, today_label]
    fixed = len(head)
    at_col = {name: i + 1 for i, name in enumerate(head)}
    amount_col = at_col[spec.amount_label]
    before_col = at_col[before_label]
    moved_col = at_col[spec.moved_label]
    open_col = at_col[spec.open_label]
    today_col = at_col[today_label]
    status_col = at_col.get("Status")

    wb = Workbook()
    ws = wb.active
    ws.title = spec.title
    bold = Font(bold=True)
    head_fill = PatternFill("solid", fgColor="E8EDF3")
    total_fill = PatternFill("solid", fgColor="F3F0E8")
    thin = Side(style="thin", color="BFBFBF")
    money = "#,##0.00;(#,##0.00);-"

    ws["A1"] = f"{config.company_name()} · {spec.title}"
    ws["A1"].font = Font(bold=True, size=14)
    ws["A2"] = (spec.blurb.format(year=dt.date.today().year, since=report_from_year())
                + f"  ·  built {dt.date.today():%d %b %Y}")

    HEADER = 4
    for i, name in enumerate(head, start=1):
        c = ws.cell(HEADER, i, name)
        c.font, c.fill, c.border = bold, head_fill, Border(bottom=thin)
    for j, month in enumerate(months):
        c = ws.cell(HEADER, fixed + 1 + j, month_label(month))
        c.font, c.fill, c.border = bold, head_fill, Border(bottom=thin)
        c.alignment = Alignment(horizontal="right")

    first_data = HEADER + 1
    r = first_data
    spans: dict[str, tuple[int, int]] = {}
    inactive_rows: list[int] = []
    for key, row in rows:
        cells = row_cells(spec, store, key, row)
        ws.cell(r, at_col["Entity"], label_of(row["entity"]))
        if status_col:
            status = status_of(spec, store, key, row)
            ws.cell(r, status_col, status)
            if status != "active":
                inactive_rows.append(r)
        ws.cell(r, at_col["Supplier"], row["supplier"])
        folded = len(combined_children(store, key))
        desc = (row.get("type") or row.get("description") or "")[:120]
        ws.cell(r, at_col["Description"],
                f"{desc} · combined {folded + 1} postings" if folded else desc)
        ws.cell(r, at_col["Account"], row.get("account") or "")
        ws.cell(r, at_col["Expense account"], row.get("expense_account") or "")
        ws.cell(r, at_col["Invoice number"], row.get("number") or "")
        ws.cell(r, at_col["Invoice date"], row["date"])
        ws.cell(r, at_col["Posted"], month_label(month_of(row["date"])))
        ws.cell(r, at_col["CCY"], row.get("currency") or "")
        ws.cell(r, at_col["Invoice amount"], row.get("doc_amount"))
        ws.cell(r, amount_col, row_amount(store, key, row))
        if spec.schedule:
            ws.cell(r, amount_col + 1, period_text(row))
            ws.cell(r, amount_col + 2,
                    months_between(row["period_start"], row["period_end"])
                    if row.get("period_start") else "")
            ws.cell(r, amount_col + 3, monthly_release(store, key, row))
        grid = f"{col(fixed + 1)}{r}:{col(fixed + len(months))}{r}"
        before = round(sum(a for m, a in cells.items() if months and m < months[0]), 2)
        ws.cell(r, before_col, before)
        if spec.grid == "movements":
            ws.cell(r, moved_col, f"=SUM({grid})+{col(before_col)}{r}")
            ws.cell(r, open_col, f"={col(amount_col)}{r}-{col(moved_col)}{r}")
        else:
            ws.cell(r, open_col, f"=SUM({grid})+{col(before_col)}{r}")
            ws.cell(r, moved_col, f"={col(amount_col)}{r}-{col(open_col)}{r}")
        ws.cell(r, today_col, remaining_today(store, key, row))
        for j, month in enumerate(months):
            if month in cells:
                ws.cell(r, fixed + 1 + j, cells[month])
        for i in [at_col["Invoice amount"], amount_col, before_col, moved_col, open_col,
                  today_col, *range(fixed + 1, fixed + 1 + len(months))]:
            ws.cell(r, i).number_format = money
        if spec.schedule:
            ws.cell(r, amount_col + 3).number_format = money
        start, _ = spans.get(row["entity"], (r, r))
        spans[row["entity"]] = (start, r)
        r += 1

    if rows:
        ws.auto_filter.ref = f"A{HEADER}:{col(fixed + len(months))}{r - 1}"
        if status_col:
            # Opened, the sheet shows the active rows only: the filter on the
            # Status column is set to "active" and the inactive rows are hidden,
            # which is how Excel stores an applied filter. Clearing the filter
            # brings the inactive rows back. The totals SUM every row, hidden
            # or not, so the balance is still the whole account.
            ws.auto_filter.add_filter_column(status_col - 1, ["active"])
            for hr in inactive_rows:
                ws.row_dimensions[hr].hidden = True
    ws.freeze_panes = ws.cell(first_data, at_col["Supplier"] + 1)

    # Totals, one block per entity in that entity's own base currency: adding
    # one currency to another down a column would be meaningless.
    r += 2
    for entity, (start, end) in spans.items():
        # The title and the line labels sit in the frozen columns A and C so
        # they stay in view while scrolling across the months.
        opening = opening_balance(spec, store, entity, months[0]) if months else 0.0
        ws.cell(r, 1, f"{label_of(entity)} · {ccy_of(entity, store)}").font = bold
        ws.cell(r, 3, "opening" + (f" at {month_label(month_add(months[0], -1))}"
                                   if months else "")).font = bold
        ws.cell(r, open_col, opening).number_format = money
        labels = ([(spec.amount_label, "add"), (spec.moved_label, "moved")]
                  if spec.grid == "movements" else [("Movement", "net")])
        labels += [("Unattributed", "unatt"), ("Rows not shown", "hidden"),
                   ("Balance", "bal"), ("Balance in Xero", "xero"),
                   ("Difference", "diff")]
        at = {kind: r + 1 + i for i, (_, kind) in enumerate(labels)}
        for label, kind in labels:
            ws.cell(at[kind], 3, label).font = bold
        additions = additions_by_month(spec, store, entity, rows)
        unattributed = unattributed_by_month(store, entity)
        hidden = hidden_net_by_month(spec, store, entity,
                                     {k for k, r in rows if r["entity"] == entity})
        xero = store["xero"].get(entity) or {}
        for j, month in enumerate(months):
            cc = fixed + 1 + j
            letter = col(cc)
            if spec.grid == "movements":
                ws.cell(at["add"], cc, additions.get(month, 0.0))
                ws.cell(at["moved"], cc, f"=SUM({letter}{start}:{letter}{end})")
                movement = f"{letter}{at['add']}-{letter}{at['moved']}"
            else:
                ws.cell(at["net"], cc, f"=SUM({letter}{start}:{letter}{end})")
                movement = f"{letter}{at['net']}"
            # Movements the report could not tie to a row are still movements:
            # they belong in the balance, named, not quietly dropped.
            ws.cell(at["unatt"], cc, unattributed.get(month, 0.0))
            movement += f"-{letter}{at['unatt']}"
            # Items that opened and closed inside the window are not on the
            # sheet, but the ledger had them: this is why it still ties.
            ws.cell(at["hidden"], cc, hidden.get(month, 0.0))
            movement += f"+{letter}{at['hidden']}"
            prev = f"{col(cc - 1)}{at['bal']}" if j else f"{col(open_col)}{r}"
            ws.cell(at["bal"], cc, f"={prev}+{movement}")
            if month in xero:
                ws.cell(at["xero"], cc, xero[month])
                ws.cell(at["diff"], cc,
                        f"=ROUND({letter}{at['bal']}-{letter}{at['xero']},2)")
            for kind in at.values():
                ws.cell(kind, cc).number_format = money
                ws.cell(kind, cc).fill = total_fill
        r = max(at.values()) + 2

    if spans:
        ws.cell(r, 1, "The balance row is the whole account, so it agrees with "
                      "Xero at every month end; \"Balance in Xero\" is the "
                      "account's closing balance on the Balance Sheet. The block "
                      "opens from Xero's balance at the month before the first "
                      f"column, and each row's \"{before_label}\" cell carries what "
                      "it did before that. \"Rows not shown\" carries the items "
                      "not on the sheet; \"unattributed\" carries movements no row "
                      "could be found for, which are listed in Checks.")
    widths = {"Entity": 10, "Status": 9, "Supplier": 26, "Description": 34,
              "Account": 24, "Expense account": 24, "Invoice number": 16,
              "Invoice date": 12, "Posted": 10, "CCY": 6, "Invoice amount": 14}
    for name, w in widths.items():
        if name in at_col:
            ws.column_dimensions[col(at_col[name])].width = w
    for i in range(at_col["Invoice amount"] + 1, fixed + 1):
        ws.column_dimensions[col(i)].width = 14
    for j in range(len(months)):
        ws.column_dimensions[col(fixed + 1 + j)].width = 12

    build_checks(spec, wb, store, rows)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    wb.save(spec.workbook_path)
    return spec.workbook_path


# --------------------------------------------------------------------- checks

def collect_checks(spec: Spec, store: dict,
                   rows: list[tuple[str, dict]] | None = None) -> list[tuple[str, str, str]]:
    """(entity, check, detail) for everything that needs a person to look."""
    rows = rows if rows is not None else visible_rows(spec, store)
    out: list[tuple[str, str, str]] = []
    now, closed = this_month(), last_closed_month()
    since = report_from_month()

    # A decision a person has recorded: an item the ledger is right about and
    # nobody should correct, an open point waiting on something outside Xero.
    # It is not a check the engine can raise or settle, and it is not a
    # document, so it lives in the store and reports itself every run.
    for nid, note in sorted((store.get("notes") or {}).items()):
        out.append((label_of(note["entity"]), note["kind"],
                    f"{note['detail']} · note {nid}"))

    for key, row in rows:
        if is_settled(spec, store, row):
            continue
        ent = label_of(row["entity"])
        moved = moved_by_month(store, key)
        left = remaining(store, key, row)
        amount = row_amount(store, key, row)
        who = f"{row['supplier']} · {row['currency']} {amount:,.2f}" \
              + (f" · {row['number']}" if row.get("number") else "")

        # A check an admin has looked at and accepted is not raised again. It
        # still reports itself, once, as the decision it is: a check that has
        # quietly stopped being raised is a check nobody can audit.
        def add(kind: str, detail: str) -> None:
            why = (row.get("accepted") or {}).get(kind)
            out.append((ent, "accepted", f"{why} · {kind} no longer raised · {who}")
                       if why else (ent, kind, detail))

        if left < -TOLERANCE:
            add(f"over-{spec.moved_label.lower()}",
                f"{spec.moved_label.lower()} {amount - left:,.2f} against "
                f"{amount:,.2f}, balance {left:,.2f} · {who}")
        if spec.schedule:
            if not row.get("period_start"):
                add("period not stated",
                    f"no period on the invoice line, so no schedule · {who}"
                    f" · set-period {key}")
                continue
            expired = row["period_end"] < now
            if expired and left > TOLERANCE:
                add("expired with a balance",
                    f"period ended {month_label(row['period_end'])}, "
                    f"{left:,.2f} still held · {who}")
            late = sorted(m for m in moved if m > row["period_end"])
            if late:
                add("released after the period",
                    f"release in {', '.join(month_label(m) for m in late)}, "
                    f"period ended {month_label(row['period_end'])} · {who}")
            due = sorted(m for m in month_range(row["period_start"],
                                                min(row["period_end"], closed))
                         if m not in moved)
            if due:
                add("release missing",
                    f"nothing released in "
                    f"{', '.join(month_label(m) for m in due)} · {who}")
        elif spec.stale_months is not None:
            # A deposit is meant to sit there for years; an accrual is not.
            age = months_between(month_of(row["date"]), now) - 1
            if age >= spec.stale_months:
                add("open a long time",
                    f"posted {month_label(month_of(row['date']))}, "
                    f"{age} months ago, {left:,.2f} still open · {who}")

    # Why a history is missing: an account cleared to nil has no open items
    # before that month, so the rows before it are gone from the sheet. One
    # line, so nobody goes looking for them.
    for entity in sorted(store["xero"]):
        cut = cleared_month(spec, store, entity)
        if not cut:
            continue
        shown = {key for key, row in rows if row["entity"] == entity}
        gone = sum(1 for key, row in store["rows"].items()
                   if row["entity"] == entity and key not in shown
                   and not combined_into(store, key)
                   and not row.get("cleared")
                   and is_settled(spec, store, row)
                   and abs(remaining(store, key, row)) > TOLERANCE)
        if gone:
            what = (f"{gone} {spec.row_noun} raised up to then is" if gone == 1
                    else f"{gone} {spec.row_noun}s raised up to then are")
            out.append((label_of(entity), "cleared to nil",
                        f"the account is nil at {month_label(cut)}, so {what} "
                        f"settled and off the report"))

    # A row taken off the report by hand is a decision, not a check, but the
    # balance it leaves behind is only explicable if the decision is on the sheet.
    shown_keys = {key for key, _ in rows}
    for key, row in sorted(store["rows"].items()):
        if not row.get("cleared") or combined_into(store, key):
            continue
        left = remaining(store, key, row)
        where = ("shown inactive" if key in shown_keys
                 else f"off the report, {left:,.2f} carried in rows not shown")
        out.append((label_of(row["entity"]), "cleared by hand",
                    f"{row['cleared']} · {where} · {row['supplier']} · "
                    f"{row['currency']} {row_amount(store, key, row):,.2f}"))

    # Weak matches are collapsed per row: fourteen monthly releases against the
    # same counterparty are one thing to look at, not fourteen.
    weak: dict[str, list[dict]] = {}
    for mv in store["moves"].values():
        if mv.get("weak") and mv.get("row"):
            weak.setdefault(mv["row"], []).append(mv)
    for key, group in sorted(weak.items()):
        row = store["rows"].get(key) or {}
        if row and is_settled(spec, store, row):
            continue
        months = sorted(m["month"] for m in group)
        out.append((label_of(group[0]["entity"]), f"{spec.movement_noun}s matched on balance",
                    f"{len(group)} against {row.get('supplier', key)}, "
                    f"{sum(m['amount'] for m in group):,.2f} over "
                    f"{month_label(months[0])} to {month_label(months[-1])}, put on this "
                    f"row because the narration named no invoice and this "
                    f"counterparty has more than one · link them to settle it · {key}"))

    for key, mv in sorted(store["moves"].items()):
        if mv.get("row") is None:
            cut = cleared_month(spec, store, mv["entity"])
            if cut and mv["month"] <= cut:
                continue
            # Before the first month column it is inside Xero's opening
            # balance and nothing to look at.
            if mv["month"] < since:
                continue
            out.append((label_of(mv["entity"]), f"unattributed {spec.movement_noun}",
                        f"{mv['amount']:,.2f} in {month_label(mv['month'])}, "
                        f"\"{(mv.get('narration') or '')[:70]}\" · link {key} <row>"))

    for entity in sorted(set(store["xero"]) | {r["entity"] for _, r in rows}):
        xero = store["xero"].get(entity) or {}
        balances = balances_by_month(spec, store, entity, rows)
        # Every month end is checked, open-only reports included: what the
        # sheet leaves out is carried by the "rows not shown" line, so the
        # balance is the whole account whichever report this is.
        for month in sorted(set(xero) & set(balances)):
            diff = round(balances[month] - xero[month], 2)
            if abs(diff) > TOLERANCE:
                out.append((label_of(entity), "does not agree with Xero",
                            f"{month_label(month)}: report {balances[month]:,.2f}, "
                            f"Xero {xero[month]:,.2f}, difference {diff:,.2f}"))
    return out


def build_checks(spec: Spec, wb, store: dict, rows) -> None:
    from openpyxl.styles import Font, PatternFill
    ws = wb.create_sheet("Checks")
    ws["A1"] = "Checks"
    ws["A1"].font = Font(bold=True, size=14)
    ws["A2"] = ("Everything the report cannot settle by itself. "
                "Nothing here is posted or corrected by the report.")
    for i, name in enumerate(("Entity", "Check", "Detail"), start=1):
        c = ws.cell(4, i, name)
        c.font = Font(bold=True)
        c.fill = PatternFill("solid", fgColor="E8EDF3")
    r = 5
    for entity, kind, detail in collect_checks(spec, store, rows):
        ws.cell(r, 1, entity)
        ws.cell(r, 2, kind)
        ws.cell(r, 3, detail)
        r += 1
    if r == 5:
        ws.cell(5, 1, "nothing outstanding")
    for letter, width in (("A", 10), ("B", 28), ("C", 120)):
        ws.column_dimensions[letter].width = width


# ------------------------------------------------------------------ commands

def publish(spec: Spec, path: Path) -> str:
    """The workbook on the server is the master; Drive holds a copy of it, put
    there after every build. One line saying what happened, kept in the store
    so the Slack summary can carry it. Never raises."""
    if not spec.publish:
        return ""
    try:
        from accounting_agent.publish import publish_to_drive
        line = publish_to_drive(path, kind="reports")
    except Exception as exc:  # noqa: BLE001  a build must not be lost to Drive
        line = f"NOT published: {exc}"
    store = load_store(spec)
    store["published"] = {"at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                          "line": line}
    save_store(spec, store)
    return line


def count_status(spec: Spec, store: dict, rows) -> tuple[int, int]:
    active = sum(1 for key, row in rows if is_active(spec, store, key, row))
    return active, len(rows) - active


def cmd_refresh(spec: Spec, targets: list[str], full: bool, do_publish: bool) -> int:
    store = load_store(spec)
    for entity in targets:
        print(f"{short(entity)}: reading Xero ...", flush=True)
        refresh_entity(spec, entity, store, full)
        save_store(spec, store)
    path = build_workbook(spec, store)
    rows = visible_rows(spec, store)
    checks = collect_checks(spec, store, rows)
    active, inactive = count_status(spec, store, rows)
    print(f"\n{active} active · {inactive} inactive · {len(checks)} checks · {path}")
    for entity, kind, detail in checks:
        print(f"  {entity} · {kind} · {detail}")
    if do_publish:
        print("Drive: " + (publish(spec, path) or "not a published report"))
    return 1 if any(k == "does not agree with Xero" for _, k, _ in checks) else 0


def cmd_build(spec: Spec, do_publish: bool) -> int:
    path = build_workbook(spec, load_store(spec))
    print(path)
    if do_publish:
        print("Drive: " + (publish(spec, path) or "not a published report"))
    return 0


# Checks sheet lines that record a decision a person already made, or explain
# why a history is missing. They stay on the sheet; the Slack summary counts
# them and carries only what still needs a person.
DECISION_KINDS = ("accepted", "cleared by hand", "cleared to nil")

# A finding whose fix is a step only a person can take rather than a question
# waiting on an answer. docs/COMMS.md keeps the two apart in every message:
# `queries` is a question, `manual` is a pair of hands.
MANUAL_KINDS = ("period not stated",)


def summary_lines(spec: Spec, store: dict, limit: int = 25) -> list[str]:
    """The report's findings as one Slack section in the COMMS shape: a
    headline bullet, then one bullet per open check, references trailing.
    Built from the store, no Xero call; the checks are the collect_checks
    lines less the recorded decisions and notes, which are counted."""
    rows = visible_rows(spec, store)
    checks = collect_checks(spec, store, rows)
    active, inactive = count_status(spec, store, rows)
    note_kinds = {n["kind"] for n in (store.get("notes") or {}).values()}
    findings = [c for c in checks if c[1] not in DECISION_KINDS and c[1] not in note_kinds]
    recorded = len(checks) - len(findings)
    synced = sorted(set(store["synced"].values()))
    when = synced[-1] if synced else "never"
    head = f"- {active} active · {inactive} inactive · {len(findings)} to look at"
    if recorded:
        head += f" · {recorded} recorded decisions on the Checks sheet"
    lines = [head + f" · refreshed {when} · {spec.workbook}"]
    for entity, kind, detail in findings[:limit]:
        lines.append(f"- {kind}: {detail}, {entity}")
    if len(findings) > limit:
        lines.append(f"- and {len(findings) - limit} more on the Checks sheet")
    return lines


def summary_sections(spec: Spec, store: dict,
                     limit: int = 25) -> tuple[str, list[str], list[str]]:
    """(headline, queries, manual) for one report, in the COMMS shape.

    The same findings `summary_lines` prints, split the way every message
    splits them: a question waiting on an answer against an action waiting on a
    pair of hands (docs/COMMS.md, "queries and manual"). Built from the store,
    no Xero call."""
    rows = visible_rows(spec, store)
    checks = collect_checks(spec, store, rows)
    active, inactive = count_status(spec, store, rows)
    note_kinds = {n["kind"] for n in (store.get("notes") or {}).values()}
    findings = [c for c in checks if c[1] not in DECISION_KINDS and c[1] not in note_kinds]
    recorded = len(checks) - len(findings)
    synced = sorted(set(store["synced"].values()))
    when = synced[-1] if synced else "never"

    head = f"{active} active · {inactive} inactive · {len(findings)} to look at"
    if recorded:
        head += f" · {recorded} recorded decisions on the Checks sheet"
    head += f" · refreshed {when} · {spec.workbook}"

    def bullets(found: list[tuple[str, str, str]]) -> list[str]:
        out = [f"• {kind}: {detail}, {entity}" for entity, kind, detail in found[:limit]]
        if len(found) > limit:
            out.append(f"• and {len(found) - limit} more on the Checks sheet")
        return out

    manual = [f for f in findings if f[1] in MANUAL_KINDS]
    queries = [f for f in findings if f[1] not in MANUAL_KINDS]
    return head, bullets(queries), bullets(manual)


def cmd_show(spec: Spec, entity: str | None) -> int:
    store = load_store(spec)
    rows = visible_rows(spec, store, entity)
    print(f"{'entity':9} {'supplier':24} {'posted':8} "
          f"{spec.amount_label:>13} {spec.moved_label:>13} {spec.open_label:>13}"
          + ("  status" if spec.status else ""))
    for key, row in rows:
        moved = sum(moved_by_month(store, key).values())
        amount = row_amount(store, key, row)
        print(f"{label_of(row['entity'])[:9]:9} {row['supplier'][:24]:24} "
              f"{month_label(month_of(row['date'])):8} {amount:13,.2f} "
              f"{moved:13,.2f} {amount - moved:13,.2f}"
              + (f"  {status_of(spec, store, key, row)}" if spec.status else ""))
    if spec.status:
        active, inactive = count_status(spec, store, rows)
        print(f"\n{active} active · {inactive} inactive")
    else:
        print(f"\n{len(rows)} {spec.row_noun}s")
    return 0


def cmd_checks(spec: Spec) -> int:
    for entity, kind, detail in collect_checks(spec, load_store(spec)):
        print(f"{entity} · {kind} · {detail}")
    return 0


def cmd_rows(spec: Spec, entity: str | None) -> int:
    store = load_store(spec)
    for key, row in visible_rows(spec, store, entity):
        extra = period_text(row) or "no period" if spec.schedule else row["account"]
        folded = len(combined_children(store, key))
        print(f"{key}  {row['supplier']} · {row['date']} · "
              f"{row['currency']} {row_amount(store, key, row):,.2f} · {extra}"
              + (f" · combined {folded + 1} postings" if folded else "")
              + (f" · {status_of(spec, store, key, row)}" if spec.status else ""))
    return 0


def cmd_set_period(spec: Spec, key: str, start: str, end: str) -> int:
    store = load_store(spec)
    if key not in store["rows"]:
        sys.exit(f"no such row {key!r}; list them with `rows`")
    s, e = parse_month(start), parse_month(end)
    if not s or not e or s > e:
        sys.exit(f"cannot read the period {start!r} - {end!r}")
    row = store["rows"][key]
    row["period_start"], row["period_end"] = s, e
    row.setdefault("fixed", {})["period"] = True
    save_store(spec, store)
    print(f"{key}: {period_text(row)} ({months_between(s, e)} months)")
    return 0


CHECK_KINDS = ("release missing", "released after the period",
               "expired with a balance", "period not stated",
               "over-released", "over-reversed", "over-returned",
               "open a long time")


def cmd_accept(spec: Spec, key: str, kind: str, why: str) -> int:
    """Stop raising one check on one row, because a person has accepted it.

    The row stays on the report with everything it holds: only the check
    goes, and it is replaced by a line saying who accepted what and why. For
    `clear`, where the whole row is settled, see below.
    """
    store = load_store(spec)
    if key not in store["rows"]:
        sys.exit(f"no such row {key!r}; list them with `rows`")
    if kind not in CHECK_KINDS:
        sys.exit("kind: " + " | ".join(CHECK_KINDS))
    if not why.strip():
        sys.exit("say why it is accepted: it goes on the Checks sheet")
    row = store["rows"][key]
    row.setdefault("accepted", {})[kind] = why.strip()
    row.setdefault("fixed", {})["accepted"] = True
    save_store(spec, store)
    print(f"{key}: {kind!r} accepted · {why.strip()}")
    return 0


def cmd_unaccept(spec: Spec, key: str, kind: str) -> int:
    store = load_store(spec)
    if key not in store["rows"]:
        sys.exit(f"no such row {key!r}; list them with `rows`")
    (store["rows"][key].get("accepted") or {}).pop(kind, None)
    save_store(spec, store)
    print(f"{key}: {kind!r} raised again")
    return 0


def cmd_clear(spec: Spec, key: str, why: str) -> int:
    """Take a row off the report because a person has settled it.

    For an item the ledger cannot show as closed by itself: a prepayment
    released against a different account, a balance written back to accruals,
    a reversal posted in a month the rebuild window no longer reaches. The row
    leaves the sheet and its checks; whatever it still holds is carried by
    "rows not shown", so the balance is still the whole account.
    """
    store = load_store(spec)
    if key not in store["rows"]:
        sys.exit(f"no such row {key!r}; list them with `rows`")
    if not why.strip():
        sys.exit("say why it is cleared: it goes on the Checks sheet")
    row = store["rows"][key]
    row["cleared"] = why.strip()
    row.setdefault("fixed", {})["cleared"] = True
    save_store(spec, store)
    print(f"{key} cleared: {why.strip()}")
    return 0


def cmd_unclear(spec: Spec, key: str) -> int:
    store = load_store(spec)
    if key not in store["rows"]:
        sys.exit(f"no such row {key!r}; list them with `rows`")
    store["rows"][key].pop("cleared", None)
    (store["rows"][key].get("fixed") or {}).pop("cleared", None)
    save_store(spec, store)
    print(f"{key} is back on the report")
    return 0


def cmd_note(spec: Spec, entity: str, kind: str, detail: str) -> int:
    """Put a standing line on the Checks sheet and keep it there.

    For what the report cannot work out and no correction will settle: an item
    accepted as it stands, an open point waiting on a board decision or a
    refund. It survives every refresh, including `--full`, and is removed by
    hand with `unnote`.
    """
    ent = resolve_entity(entity)
    if not kind.strip() or not detail.strip():
        sys.exit("a note is an entity, what kind of line it is, and what it says")
    store = load_store(spec)
    notes = store.setdefault("notes", {})
    used = [int(k[1:]) for k in notes if re.fullmatch(r"n\d+", k)]
    nid = f"n{max(used, default=0) + 1}"
    notes[nid] = {"entity": ent, "kind": kind.strip(), "detail": detail.strip()}
    save_store(spec, store)
    print(f"{nid}: {short(ent)} · {kind.strip()} · {detail.strip()}")
    return 0


def cmd_unnote(spec: Spec, nid: str) -> int:
    store = load_store(spec)
    if nid not in (store.get("notes") or {}):
        sys.exit(f"no such note {nid!r}; list them with `notes`")
    gone = store["notes"].pop(nid)
    save_store(spec, store)
    print(f"{nid} removed: {gone['kind']} · {gone['detail']}")
    return 0


def cmd_notes(spec: Spec) -> int:
    store = load_store(spec)
    notes = store.get("notes") or {}
    if not notes:
        print("no notes")
    for nid, note in sorted(notes.items()):
        print(f"{nid}  {label_of(note['entity']):<9} {note['kind']:<22} {note['detail']}")
    return 0


def cmd_set_field(spec: Spec, key: str, field_name: str, value: str) -> int:
    if field_name not in ("type", "expense_account", "supplier", "number", "scheduled"):
        sys.exit("field: type | expense_account | supplier | number | scheduled")
    store = load_store(spec)
    if key not in store["rows"]:
        sys.exit(f"no such row {key!r}; list them with `rows`")
    row = store["rows"][key]
    row[field_name] = (
        round(float(value.replace(",", "")), 2) if field_name == "scheduled" else value)
    if field_name in HAND_SET:
        # A refresh rewrites these from the document; this copy is what it
        # puts back.
        row[f"_{field_name}"] = value
    row.setdefault("fixed", {})[field_name] = True
    save_store(spec, store)
    print(f"{key}: {field_name} = {value}")
    return 0


def cmd_combine(spec: Spec, key: str, into: str) -> int:
    """Show two postings of the same item as one row.

    The same thing posted twice (one journal raising a subscription in two
    lines, a rounding top-up against a premium already deferred) reads as two
    rows, each half-released, when it is one item that clears to nil. Combining
    puts both postings, and every movement attributed to either, on one row.
    Each posting keeps its own month, so the balance still ties to Xero.
    """
    store = load_store(spec)
    for k in (key, into):
        if k not in store["rows"]:
            sys.exit(f"no such row {k!r}; list them with `rows`")
    root = row_root(store, into)
    if root == key:
        sys.exit(f"{into!r} is already combined into {key!r}")
    a, b = store["rows"][key], store["rows"][root]
    if a["entity"] != b["entity"] or a["code"] != b["code"]:
        sys.exit("only two rows on the same account of the same entity combine")
    a["combined_into"] = root
    a.setdefault("fixed", {})["combined"] = True
    attribute(spec, a["entity"], store)
    save_store(spec, store)
    print(f"{key} -> {root} · {b['currency']} {row_amount(store, root, b):,.2f} "
          f"over {len(combined_children(store, root)) + 1} postings")
    return 0


def cmd_uncombine(spec: Spec, key: str) -> int:
    store = load_store(spec)
    if key not in store["rows"]:
        sys.exit(f"no such row {key!r}; list them with `rows`")
    row = store["rows"][key]
    row.pop("combined_into", None)
    (row.get("fixed") or {}).pop("combined", None)
    attribute(spec, row["entity"], store)
    save_store(spec, store)
    print(f"{key} is its own row again")
    return 0


def cmd_link(spec: Spec, move: str, row: str) -> int:
    store = load_store(spec)
    if move not in store["moves"]:
        sys.exit(f"no such {spec.movement_noun} {move!r}; list them with `checks`")
    if row not in store["rows"]:
        sys.exit(f"no such row {row!r}; list them with `rows`")
    store["moves"][move]["linked"] = row
    store["moves"][move]["row"] = row_root(store, row)
    store["moves"][move]["weak"] = False
    store["moves"][move].pop("unattributed", None)
    save_store(spec, store)
    print(f"{move} -> {row}")
    return 0


def cmd_unlink(spec: Spec, move: str) -> int:
    """Say that a movement belongs to no row on the sheet, and mean it.

    A release whose own prepayment was never posted to the account: the
    attribution will always find it the least-bad row otherwise, and put a
    counterparty's balance out by it. Unlinked, it stands in the unattributed
    line where the balance still carries it.
    """
    store = load_store(spec)
    if move not in store["moves"]:
        sys.exit(f"no such {spec.movement_noun} {move!r}")
    mv = store["moves"][move]
    mv.pop("linked", None)
    mv["unattributed"] = True
    mv["row"], mv["weak"] = None, False
    save_store(spec, store)
    print(f"{move} belongs to no row")
    return 0


def main(spec: Spec, argv: list[str], doc: str) -> int:
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(doc)
        return 0 if argv else 2
    full = "--full" in argv
    do_publish = "--no-publish" not in argv
    cmd, rest = argv[0], [a for a in argv[1:] if a not in ("--full", "--no-publish")]
    if cmd == "refresh":
        targets = entity_keys() if (not rest or rest[0] == "all") \
            else [resolve_entity(a) for a in rest]
        return cmd_refresh(spec, targets, full, do_publish)
    if cmd == "build":
        return cmd_build(spec, do_publish)
    if cmd == "summary":
        print("\n".join(summary_lines(spec, load_store(spec))))
        return 0
    if cmd == "show":
        return cmd_show(spec, resolve_entity(rest[0]) if rest else None)
    if cmd == "checks":
        return cmd_checks(spec)
    if cmd == "rows":
        return cmd_rows(spec, resolve_entity(rest[0]) if rest else None)
    if cmd == "set-period" and len(rest) == 3 and spec.schedule:
        return cmd_set_period(spec, *rest)
    if cmd == "notes" and not rest:
        return cmd_notes(spec)
    if cmd == "note" and len(rest) >= 3:
        return cmd_note(spec, rest[0], rest[1], " ".join(rest[2:]))
    if cmd == "unnote" and len(rest) == 1:
        return cmd_unnote(spec, rest[0])
    if cmd == "set-field" and len(rest) >= 3:
        return cmd_set_field(spec, rest[0], rest[1], " ".join(rest[2:]))
    if cmd == "accept" and len(rest) >= 3:
        return cmd_accept(spec, rest[0], rest[1], " ".join(rest[2:]))
    if cmd == "unaccept" and len(rest) == 2:
        return cmd_unaccept(spec, *rest)
    if cmd == "clear" and len(rest) >= 2:
        return cmd_clear(spec, rest[0], " ".join(rest[1:]))
    if cmd == "unclear" and len(rest) == 1:
        return cmd_unclear(spec, rest[0])
    if cmd == "combine" and len(rest) == 2:
        return cmd_combine(spec, *rest)
    if cmd == "uncombine" and len(rest) == 1:
        return cmd_uncombine(spec, rest[0])
    if cmd == "link" and len(rest) == 2:
        return cmd_link(spec, *rest)
    if cmd == "unlink" and len(rest) == 1:
        return cmd_unlink(spec, rest[0])
    print(doc)
    return 2
