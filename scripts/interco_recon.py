#!/usr/bin/env python
"""Intercompany reconciliation across every entity in config/group.toml.

Builds, for every intercompany account pair configured under [[intercompany]],
both sides' transaction detail and closing balances, then matches them and
reports the breaks. Add an entity or a pair to the config and it is
reconciled on the next run; nothing structural is hard-coded here.

There is no accounting.journals.read scope, so the GL is reassembled from
documents: posted manual journals, bank transactions, bills/invoices, credit
notes, and payments settled against a payments-enabled loan account. This
reassembly must tie exactly to the Balance Sheet movement.

Every figure that compares two ledgers is translated to the group's
reporting currency (config [company] reporting_currency).

Usage:
    .venv/bin/python scripts/interco_recon.py --from 2026-07-01 --to 2026-09-30
    .venv/bin/python scripts/interco_recon.py --pair HOLDCO:OPCO_US
    .venv/bin/python scripts/interco_recon.py --pair HOLDCO:OPCO_US:recharge --json /tmp/r.json
"""
from __future__ import annotations

import argparse
import datetime
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from accounting_agent import config  # noqa: E402

# The Xero library is imported where it is used, so --help, the workbook
# layout and the matcher all run without credentials.

# ---------------------------------------------------------------- group map
#
# Everything structural is read from config/group.toml on every call, so a
# long-lived process that reloads the config sees the new structure.


def entity_map() -> dict[str, str]:
    """{entity key: Xero organisation name}, in config order."""
    return {e.key: e.xero_name for e in config.entities()}


def base_ccy() -> dict[str, str]:
    """{entity key: base currency}."""
    return {e.key: e.base_currency for e in config.entities()}


def reporting() -> str:
    return config.reporting_currency().upper()


def settings() -> dict:
    return config.intercompany_settings()


def setting(key: str, default):
    """A key under [intercompany_settings] that the config module does not
    default itself."""
    return config.section("intercompany_settings").get(key, default)


def select_pairs(spec: str | None) -> list:
    """The configured pairs, optionally narrowed by --pair A:B[:flavour].

    Entities may be given as key, slug, alias or short name; A:B matches the
    two entities in either order, every flavour; A:B:flavour one pair only.
    """
    pairs = config.intercompany_pairs()
    if not spec:
        return pairs
    parts = [p.strip() for p in spec.split(":")]
    if len(parts) not in (2, 3):
        raise SystemExit(f"--pair wants A:B or A:B:flavour, got {spec!r}")
    want = {config.entity(parts[0]).key, config.entity(parts[1]).key}
    out = [p for p in pairs if {p.a, p.b} == want and (len(parts) == 2 or p.flavour == parts[2])]
    if not out:
        names = ", ".join(p.name for p in pairs)
        raise SystemExit(f"no configured intercompany pair matches {spec!r}; configured: {names}")
    return out


def pair_title(p) -> str:
    return f"{p.a}<>{p.b} / {p.flavour}"


# --------------------------------------------------------------- GL rebuild

_MS = re.compile(r"/Date\((-?\d+)")


def _d(v) -> str:
    if not v:
        return ""
    m = _MS.match(str(v))
    if m:
        return datetime.datetime.fromtimestamp(int(m.group(1)) / 1000, datetime.UTC).date().isoformat()
    return str(v)[:10]


def month_end(d: str) -> str:
    """The last day of d's month. The Balance Sheet only reports month-end
    columns, so a rebuild proved against it must run to the month end too:
    a journal dated forward to the 30th (an accrual, a mirror, a recharge)
    is in the balance already and would otherwise read as unexplained."""
    dt = datetime.date.fromisoformat(d)
    nxt = (dt.replace(day=28) + datetime.timedelta(days=4)).replace(day=1)
    return (nxt - datetime.timedelta(days=1)).isoformat()


def to_base(amount: float, rate) -> float:
    """Document-currency amount -> entity base currency.

    Xero's CurrencyRate is stored INVERTED (foreign units per 1 base unit),
    so base = amount / rate. A EUR 10,000 document @0.8000 in a USD-base
    entity ties to the Balance Sheet only as 10,000/0.8 = USD 12,500, never
    as 10,000*0.8. Prove this once against your own Balance Sheet.
    """
    try:
        r = float(rate)
        return amount / r if r else float(amount)
    except (TypeError, ValueError, ZeroDivisionError):
        return float(amount)


@dataclass
class Txn:
    code: str
    date: str
    base: float        # signed, entity base currency: + = debit, - = credit
    doc_amount: float  # signed, document currency
    currency: str | None
    rate: float | None
    source: str
    party: str
    desc: str
    ref: str
    doc_id: str
    entity: str = ""
    entity_base: str = ""
    matched: bool = field(default=False, compare=False)
    reversed: bool = field(default=False, compare=False)   # a posting undone by its own reversal


def _payment_codes(entity_key: str, codes: set[str]) -> set[str]:
    """The subset of `codes` whose Xero payments must be read: those in a
    configured pair marked payments_enabled, plus any code no pair names."""
    enabled, disabled = set(), set()
    for p in config.intercompany_pairs(entity_key):
        code = p.account(entity_key)
        if code:
            (enabled if p.payments_enabled else disabled).add(str(code))
    return {c for c in codes if c in enabled or c not in disabled}


def pull_txns(entity_key: str, codes: set[str], date_from: str, date_to: str) -> list[Txn]:
    """Every posted movement on `codes` in one entity, between two dates."""
    codes = {str(x) for x in codes if x}
    if not codes:
        return []
    from accounting_agent.xero import XeroClient
    from accounting_agent.xero.banking import get_bank_transactions, get_payments
    from accounting_agent.xero.journals import get_manual_journals
    from accounting_agent.xero.purchases import get_bills, get_supplier_credit_notes
    from accounting_agent.xero.sales import get_credit_notes, get_invoices

    ent = config.entity(entity_key)
    c = XeroClient(ent.xero_name)
    out: list[Txn] = []

    def add(code, date, doc_amt, rate, cur, source, party, desc, ref, doc_id):
        out.append(Txn(str(code), date, to_base(doc_amt, rate), doc_amt, cur,
                       float(rate) if rate else None, source, (party or "")[:44],
                       (desc or "")[:100], (ref or "")[:28], doc_id or "",
                       ent.key, ent.base_currency))

    for mj in get_manual_journals(c, status="POSTED", date_from=date_from, date_to=date_to):
        for ln in mj.get("JournalLines") or []:
            if str(ln.get("AccountCode")) in codes:
                add(ln["AccountCode"], _d(mj.get("DateString") or mj.get("Date")),
                    float(ln.get("LineAmount") or 0), None, None, "MJ",
                    mj.get("Narration"), ln.get("Description") or mj.get("Narration"),
                    "", mj.get("ManualJournalID"))

    def net(ln, doc):
        """What the account actually receives from a line: the line amount
        less its tax when the document is tax-inclusive (the tax goes to the
        tax account, not the loan). Exclusive and NoTax lines are as stated."""
        amt = float(ln.get("LineAmount") or 0)
        if (doc.get("LineAmountTypes") or "").lower() == "inclusive":
            amt -= float(ln.get("TaxAmount") or 0)
        return amt

    for bt in get_bank_transactions(c, from_date=date_from, to_date=date_to):
        if bt.get("Status") in ("DELETED", "VOIDED"):
            continue
        # SPEND coded to a loan = Dr loan; RECEIVE coded to a loan = Cr loan.
        sign = 1.0 if bt.get("Type", "").startswith("SPEND") else -1.0
        for ln in bt.get("LineItems") or []:
            if str(ln.get("AccountCode")) in codes:
                add(ln["AccountCode"], _d(bt.get("DateString") or bt.get("Date")),
                    sign * net(ln, bt), bt.get("CurrencyRate"),
                    bt.get("CurrencyCode"), "BANK:" + bt.get("Type", ""),
                    (bt.get("Contact") or {}).get("Name"), ln.get("Description"),
                    bt.get("Reference"), bt.get("BankTransactionID"))

    for kind, docs, sgn in (
        ("BILL", get_bills(c, date_from=date_from, date_to=date_to), 1.0),
        ("INV", get_invoices(c, date_from=date_from, date_to=date_to), -1.0),
        ("SUPP_CN", get_supplier_credit_notes(c, date_from=date_from, date_to=date_to), -1.0),
        ("CN", get_credit_notes(c, date_from=date_from, date_to=date_to), 1.0),
    ):
        for doc in docs:
            if doc.get("Status") in ("DELETED", "VOIDED", "DRAFT"):
                continue
            for ln in doc.get("LineItems") or []:
                if str(ln.get("AccountCode")) in codes:
                    add(ln["AccountCode"], _d(doc.get("DateString") or doc.get("Date")),
                        sgn * net(ln, doc), doc.get("CurrencyRate"),
                        doc.get("CurrencyCode"), kind,
                        (doc.get("Contact") or {}).get("Name"), ln.get("Description"),
                        doc.get("InvoiceNumber") or doc.get("CreditNoteNumber"),
                        doc.get("InvoiceID") or doc.get("CreditNoteID"))

    # A bill/invoice settled FROM a payments-enabled loan account moves the
    # loan without any line item: easy to miss, and common on interco loans.
    # Payments are read only for codes that may carry them: a configured pair
    # with payments_enabled = true, or a code no configured pair names (found
    # by account name, so its setting is unknown). A configured pair with
    # payments_enabled = false is skipped, which saves the API calls.
    pay_codes = _payment_codes(ent.key, codes)
    payments = (get_payments(c, from_date=date_from, to_date=date_to, status="AUTHORISED")
                if pay_codes else [])
    for p in payments:
        if str((p.get("Account") or {}).get("Code")) in pay_codes:
            inv = p.get("Invoice") or {}
            sgn = -1.0 if inv.get("Type") == "ACCPAY" else 1.0
            add((p.get("Account") or {})["Code"], _d(p.get("Date")),
                sgn * float(p.get("Amount") or 0), p.get("CurrencyRate"),
                p.get("CurrencyCode"), "PAY:" + (inv.get("Type") or "?"),
                (inv.get("Contact") or {}).get("Name"),
                "settlement of " + (inv.get("InvoiceNumber") or ""),
                p.get("Reference"), p.get("PaymentID"))

    out.sort(key=lambda t: (t.code, t.date))
    return out


# Account classes that sit on the Balance Sheet. An interco account of any
# other class is a P&L account: postings land in the P&L and the Balance
# Sheet report never shows it (break class G).
BS_CLASSES = ("ASSET", "LIABILITY", "EQUITY")


def pull_balances(entity_key: str, as_at: str, periods: int = 8,
                  meta: dict | None = None) -> dict[str, dict[str, float]]:
    """{account_code: {period_label: closing_balance}} from the Balance Sheet.

    Balance Sheet sign convention: the value is the balance as PRESENTED in
    its section, so an asset row is + when debit and a liability row is + when
    credit. Normalised here to DEBIT-POSITIVE so both sides of a pair are
    directly comparable: a matched pair should sum to zero.

    When `meta` is a dict it is filled with {code: {name, class, type,
    status}} for the whole chart, so the caller can check that every mapped
    interco account exists and is balance-sheet typed.
    """
    from accounting_agent.xero import XeroClient
    from accounting_agent.xero.reports import balance_sheet

    c = XeroClient(config.entity(entity_key).xero_name)
    rep = balance_sheet(c, date=as_at, periods=periods, timeframe="MONTH")["Reports"][0]
    hdr = [cell["Value"] for cell in rep["Rows"][0]["Cells"]][1:]
    accounts = {a["AccountID"]: a for a in c.get("Accounts")["Accounts"]}
    if meta is not None:
        for a in accounts.values():
            if a.get("Code"):
                meta[str(a["Code"])] = {"name": a.get("Name", ""), "class": a.get("Class", ""),
                                        "type": a.get("Type", ""), "status": a.get("Status", "")}

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


def account_problems(pairs, meta: dict[str, dict]) -> list[str]:
    """Structural findings on the chart: a mapped account that is not in the
    entity's chart, is archived, or is typed P&L (break class G)."""
    out = []
    for p in pairs:
        for ent in (p.a, p.b):
            code = p.account(ent)
            if not code or not meta.get(ent):
                continue
            m = meta[ent].get(code)
            if m is None:
                out.append(f"{pair_title(p)}: account {code} is not in {ent}'s chart of accounts")
            elif m.get("status") == "ARCHIVED":
                out.append(f"{pair_title(p)}: {ent} {code} '{m['name']}' is ARCHIVED")
            elif m.get("class") not in BS_CLASSES:
                out.append(f"{pair_title(p)}: {ent} {code} '{m['name']}' is typed {m.get('class')} "
                           f"(P&L); the Balance Sheet cannot see it, retype to balance sheet")
    return out


# --------------------------------------------------------------- matching

# Journals that revalue a foreign-currency intercompany balance are
# LEGITIMATELY ONE-SIDED: they restate one entity's local-currency carrying
# amount and have no counterpart in the other ledger. Never treat one as a
# break; report them separately. Override with fx_markers in config.
FX_ADJ_MARKERS = ("fx adjust", "fx adjustment", "revaluation", "reval",
                  "exchange difference", "fx difference", "fx recon")


def fx_markers() -> tuple[str, ...]:
    return tuple(str(m).lower() for m in setting("fx_markers", FX_ADJ_MARKERS))


def is_fx_adjustment(t: "Txn") -> bool:
    blob = f"{t.party} {t.desc}".lower()
    return t.source == "MJ" and any(m in blob for m in fx_markers())


# A consolidation or sweep journal: a periodic journal that moves one
# interco account's balance into another (clearing a recharge account into
# the pair's main loan, say; a duplicate-account consolidation reads the
# same way). Real movements, mirrored both sides, but not business with the
# counterparty, so categorised apart. Override with sweep_markers in config.
SWEEP_MARKERS = ("ic consolidation", "sweep to", "consolidat")


def sweep_markers() -> tuple[str, ...]:
    return tuple(str(m).lower() for m in setting("sweep_markers", SWEEP_MARKERS))


def is_sweep(t: "Txn") -> bool:
    blob = f"{t.party} {t.desc}".lower()
    return t.source == "MJ" and any(m in blob for m in sweep_markers())


def is_ct(t: "Txn", rates) -> bool:
    """Under the rounding tolerance (tolerance_abs, reporting currency):
    rounding true-ups and cents, categorised CT and filtered."""
    return abs(t.base * rate(rates, t.entity_base)) < float(settings()["tolerance_abs"])


# Words every intercompany narration carries; they say nothing about which
# item is which and would link everything to everything. The group's own
# entity names are added from config (stop_words()). Legal forms come from
# the one shared list, config.LEGAL_FORMS.
_STOP = config.LEGAL_FORMS | {
    "holdings", "holding", "group", "intercompany", "interco", "inter", "loan", "paid",
    "behalf", "payment", "settlement", "invoice", "from", "with", "transfer", "recharge",
    "operations"}


def stop_words() -> set[str]:
    words = set(_STOP)
    for e in config.entities():
        for s in (e.key, e.xero_name, e.short, *e.aliases):
            words |= {w for w in re.split(r"[^a-z0-9]+", s.lower()) if w}
    words |= {str(w).lower() for w in setting("stop_words", [])}
    return words


_STOP_CACHE: set[str] | None = None


def _tokens(s: str) -> set[str]:
    global _STOP_CACHE
    if _STOP_CACHE is None:
        _STOP_CACHE = stop_words()
    return {w for w in re.split(r"[^a-z0-9]+", (s or "").lower()) if len(w) > 3 and w not in _STOP_CACHE}


def same_party(a: "Txn", b: "Txn") -> bool:
    """Do the two sides name the same counterparty or document?"""
    if a.ref and b.ref and a.ref.strip().lower() == b.ref.strip().lower():
        return True
    return bool((_tokens(a.party) | _tokens(a.desc)) & (_tokens(b.party) | _tokens(b.desc)))


def days_apart(x: "Txn", y: "Txn") -> int:
    try:
        return abs((datetime.date.fromisoformat(x.date) - datetime.date.fromisoformat(y.date)).days)
    except ValueError:
        return 999


_REVERSAL = re.compile(r"revers|reclass", re.IGNORECASE)


def is_reversal_pair(a: "Txn", b: "Txn") -> bool:
    """Is an equal-and-opposite pair inside one ledger a posting and its own
    reversal, rather than two real items that each mirror across the pair?

    A contra is bookkeeping undoing bookkeeping: a journal that says it
    reverses something, or two documents sharing a reference. Two cash lines
    are never a contra: money that went out and came back is two transfers,
    each with its own mirror in the other entity. Nor is an
    attribution-then-sweep pair, or a settlement against one flavour's
    account and the sweep that clears it: those mirror line by line across
    the pair, and netting them here hides that and leaves the other side
    one-sided.
    """
    if a.source.startswith("BANK") and b.source.startswith("BANK"):
        return False
    if a.ref and b.ref and a.ref.strip().lower() == b.ref.strip().lower():
        return True
    return bool(_REVERSAL.search(f"{a.desc} {a.party}") or _REVERSAL.search(f"{b.desc} {b.party}"))


def find_contras(rows: list["Txn"], day_tol: int = 60) -> list[tuple["Txn", "Txn"]]:
    """Equal-and-opposite pairs WITHIN one side that are a posting and its
    own reversal (is_reversal_pair).

    These net to nothing, so they must be removed before the two sides are
    compared: otherwise each leg shows up as a phantom one-sided break.
    Anything equal-and-opposite that is NOT a reversal is left for the
    cross-entity passes, where each leg finds its own mirror.
    """
    out = []
    for i, a in enumerate(rows):
        if a.matched:
            continue
        for b in rows[i + 1:]:
            if b.matched or a.matched:
                continue
            if (abs(a.base + b.base) < 0.01 and abs(a.base) > 0.005
                    and days_apart(a, b) <= day_tol and is_reversal_pair(a, b)):
                a.matched = b.matched = True
                a.reversed = b.reversed = True
                out.append((a, b))
    return out


MISSING_RATES: set[str] = set()


def rate(rates: dict, ccy: str) -> float:
    """Reporting-currency value of one unit of `ccy`. A currency with no
    rate at all is translated at 1.0 and recorded, so the report can say so
    loudly instead of failing half-way; pass --rate CCY=... to fix it."""
    v = rates.get(ccy)
    if v is None:
        MISSING_RATES.add(ccy)
        return 1.0
    return v


def to_rep(t: "Txn", rates) -> float:
    """t in the reporting currency."""
    return t.base * rate(rates, t.entity_base)


def _within(ua: float, ub: float, tol: float) -> bool:
    """Opposite-signed and netting to within tol of the larger side."""
    if ua * ub >= 0:
        return False
    scale = max(abs(ua), abs(ub))
    return scale >= 0.01 and abs(ua + ub) / scale <= tol


def _amount_in(t: "Txn", ccy: str):
    """t's amount in `ccy` if it is stated in that currency (document currency,
    or base for a base-currency item); None otherwise."""
    if t.currency == ccy:
        return t.doc_amount
    if t.entity_base == ccy and (t.currency is None or t.currency == ccy):
        return t.base
    return None


def extend_fx_matches(matches, a_rows, b_rows, rates, day_tol: int = 10, max_extra: int = 4):
    """Turn a one-to-one cross-currency match that is really one-to-many into
    a group.

    One side books one line for a bill the other side split into several
    (one journal for 12,500.00 against three lines of 11,000.00, 1,375.00
    and 125.00). The fx pass, at 3%, pairs the journal with the big line and
    leaves the small ones one-sided. Here, for every fx match, the residual
    is measured in a currency both sides state the amount in, and if
    unmatched lines on the same account within `day_tol` days of the matched
    line sum to that residual to the cent, they join the match as a group.
    Returns (matches, groups).
    """
    from itertools import combinations
    kept, groups = [], []
    for a, b, why in matches:
        if why != "fx":
            kept.append((a, b, why))
            continue
        done = False
        for ccy in {a.currency, a.entity_base, b.currency, b.entity_base} - {None}:
            xa, xb = _amount_in(a, ccy), _amount_in(b, ccy)
            if xa is None or xb is None:
                continue
            r = xa + xb
            if abs(r) < 0.01:
                break                               # already exact in that currency
            for side_rows, anchor in ((b_rows, b), (a_rows, a)):
                cands = [t for t in side_rows if not t.matched and t.code == anchor.code
                         and days_apart(t, anchor) <= day_tol and _amount_in(t, ccy) is not None]
                for k in range(1, min(max_extra, len(cands)) + 1):
                    for sub in combinations(cands, k):
                        if abs(sum(_amount_in(t, ccy) for t in sub) + r) < 0.01:
                            for t in sub:
                                t.matched = True
                            ga = [a] + (list(sub) if side_rows is a_rows else [])
                            gb = [b] + (list(sub) if side_rows is b_rows else [])
                            groups.append((ga, gb, f"group {len(ga)}:{len(gb)}"))
                            done = True
                            break
                    if done:
                        break
                if done:
                    break
            if done:
                break
        if not done:
            kept.append((a, b, why))
    return kept, groups


def match_groups(a_rows, b_rows, rates, day_tol: int = 45, fx_tol: float = 0.03, max_side: int = 8,
                 loose: bool = False):
    """Many-to-many matches among what the one-to-one passes left behind.

    `loose` is the PROBABLE pass run last: the same supplier or subject on
    both sides within 10 days, opposite-signed, but the amounts up to 35%
    apart. Two payments to one supplier the same day for 1,250.00 and 980.00, or
    3,000 in one currency paid against a bill for 3,000 in another, are the
    same purchase with a difference to investigate, not two unrelated
    one-sided items. One-to-one is allowed here, every match is labelled
    "probable" and the gap is shown, never absorbed.

    One side books one journal for what the other side paid in several bank
    lines (a month's rent in four payments against one month-end journal), or
    one payment settles several bills. Each leg is real and each has its
    mirror, but no single line equals any single line on the other side, so
    the one-to-one passes leave all of them looking one-sided.

    Candidates are drawn together by what the narrations share (a supplier, a
    reference, a subject word) within `day_tol` days across the two sides, and
    a candidate cluster is a group match when its two sides net to within
    `fx_tol` in the reporting currency. When the whole cluster does not net,
    its subsets are tried: each single item on one side against the subsets
    of the other, then subsets against subsets, smallest first, so a real 1:4
    is found before an accidental 3:5. Clusters larger than `max_side` a side
    are not enumerated; they are left one-sided for a person to read.

    Returns [(a_items, b_items, label)], each item marked matched.
    """
    from itertools import combinations
    if loose:
        day_tol = min(day_tol, 10)
    ua_ = [t for t in a_rows if not t.matched]
    ub_ = [t for t in b_rows if not t.matched]
    if not ua_ or not ub_:
        return []

    # clusters: connected components over cross-side "same party, near in time" edges
    parent = {}

    def find(x):
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x, y):
        parent[find(x)] = find(y)

    for i, a in enumerate(ua_):
        for j, b in enumerate(ub_):
            if same_party(a, b) and days_apart(a, b) <= day_tol:
                union(("a", i), ("b", j))
    clusters = {}
    for i in range(len(ua_)):
        clusters.setdefault(find(("a", i)), ([], []))[0].append(ua_[i])
    for j in range(len(ub_)):
        clusters.setdefault(find(("b", j)), ([], []))[1].append(ub_[j])

    out = []
    for A, B in clusters.values():
        if not A or not B or len(A) > max_side or len(B) > max_side:
            continue
        all_same = all(same_party(a, b) for a in A for b in B)
        if loose and not all_same:
            continue
        tol = 0.35 if loose else (0.10 if all_same else fx_tol)
        label = "probable" if loose else "group"

        def try_take(sa, sb):
            if len(sa) == 1 and len(sb) == 1 and not loose:
                return False                       # one-to-one is the earlier passes' job
            if any(t.matched for t in sa + sb):
                return False
            if _within(sum(to_rep(t, rates) for t in sa), sum(to_rep(t, rates) for t in sb), tol):
                for t in sa + sb:
                    t.matched = True
                out.append((list(sa), list(sb), f"{label} {len(sa)}:{len(sb)}"))
                return True
            return False

        # whole cluster first
        if try_take(A, B):
            continue
        # then singles against subsets of the other side, smallest subsets first
        for one_side, other in ((A, B), (B, A)):
            for t in one_side:
                if t.matched:
                    continue
                done = False
                for k in range(2, len(other) + 1):
                    for sub in combinations([x for x in other if not x.matched], k):
                        ok = try_take([t], list(sub)) if one_side is A else try_take(list(sub), [t])
                        if ok:
                            done = True
                            break
                    if done:
                        break
        # then subsets against subsets, smallest total first
        restA = [t for t in A if not t.matched]
        restB = [t for t in B if not t.matched]
        if len(restA) >= 2 and len(restB) >= 2:
            sizes = sorted(((ka, kb) for ka in range(2, len(restA) + 1) for kb in range(2, len(restB) + 1)),
                           key=lambda x: x[0] + x[1])
            for ka, kb in sizes:
                taken = False
                for sa in combinations([t for t in restA if not t.matched], ka):
                    for sb in combinations([t for t in restB if not t.matched], kb):
                        if try_take(list(sa), list(sb)):
                            taken = True
                            break
                    if taken:
                        break
    return out


# Lines the matcher cannot pair on its own but that are one transaction, most
# often a correction dated a month end against a line weeks earlier. Recorded
# by `scripts/interco_breaks.py confirm` on the server, with the evidence.
CONFIRMED = ROOT / "data" / "interco" / "confirmed_matches.json"


def load_confirmed() -> list[dict]:
    try:
        return json.loads(CONFIRMED.read_text())
    except (OSError, ValueError):
        return []


def match_confirmed(a_rows, b_rows) -> list[tuple[list, list, str]]:
    """Pair the lines a confirmed entry names, before any other pass. An
    entry applies only when every one of its documents is on this account."""
    out = []
    for entry in load_confirmed():
        ids = set(entry.get("doc_ids") or [])
        ga = [t for t in a_rows if not t.matched and t.doc_id in ids]
        gb = [t for t in b_rows if not t.matched and t.doc_id in ids]
        if not ids or {t.doc_id for t in ga + gb} != ids:
            continue
        for t in ga + gb:
            t.matched = True
        out.append((ga, gb, entry.get("reason", "confirmed")))
    return out


def match_sides(a_rows, b_rows, rates, day_tol: int = 35, fx_tol: float = 0.03):
    """Pair up the two sides of one intercompany account.

    A correct pair is equal and OPPOSITE, so a is compared against -b. Passes
    run tightest first and each transaction is consumed by the first pass that
    claims it:

      0. fx_adjustment  one-sided revaluation journals, set aside
      1. contra         posting + its own reversal within the SAME side
                        (is_reversal_pair: never two cash lines), netted off
      2. exact          same document currency, equal and opposite doc amount
      3. base           both entities share a base currency, equal and opposite
      4. fx             cross-currency, converted via `rates`, inside fx_tol
                        (relaxed to 10% when the counterparty/reference agrees)
      5. group          an fx match whose residual equals leftover lines on
                        the same account to the cent grows into a group
                        (extend_fx_matches); then many-to-many among the
                        leftovers (match_groups): one journal against several
                        payments, one payment against several bills
      6. probable       same supplier or subject within 10 days, amounts up
                        to 35% apart: paired and shown with the gap to
                        investigate

      -  confirmed      lines recorded as one transaction by
                        interco_breaks.py confirm
                        (data/interco/confirmed_matches.json), paired
                        before everything else

    Whatever is left is genuinely one-sided: a real break.
    """
    fx_a = [t for t in a_rows if is_fx_adjustment(t)]
    fx_b = [t for t in b_rows if is_fx_adjustment(t)]
    for t in fx_a + fx_b:
        t.matched = True
    confirmed = match_confirmed(a_rows, b_rows)

    contra_a, contra_b = find_contras(a_rows), find_contras(b_rows)
    matches = []

    def sweep(pred, label):
        # Widen the date window in steps. Interco transfers repeat the same
        # round amount over and over, so a wide window on the first pass pairs
        # them arbitrarily; matching same-day first keeps the pairing honest.
        for window in (0, 3, 10, day_tol):
            for a in a_rows:
                if a.matched:
                    continue
                best = None
                for b in b_rows:
                    if b.matched or days_apart(a, b) > window:
                        continue
                    if pred(a, b) and (best is None or days_apart(a, b) < days_apart(a, best)):
                        best = b
                if best is not None:
                    a.matched = best.matched = True
                    matches.append((a, best, label))

    sweep(lambda a, b: a.currency and b.currency and a.currency == b.currency
          and abs(a.doc_amount + b.doc_amount) < 0.01, "exact")

    sweep(lambda a, b: a.entity_base == b.entity_base
          and abs(a.base + b.base) < 0.01 and abs(a.base) > 0.005, "base")

    def fx_ok(a, b):
        ua, ub = to_rep(a, rates), to_rep(b, rates)
        if ua * ub >= 0:                      # a real pair is opposite-signed
            return False
        scale = max(abs(ua), abs(ub))
        if scale < 0.01:
            return False
        return abs(ua + ub) / scale <= (0.10 if same_party(a, b) else fx_tol)

    sweep(fx_ok, "fx")

    matches, extended = extend_fx_matches(matches, a_rows, b_rows, rates)
    groups = extended + match_groups(a_rows, b_rows, rates, fx_tol=fx_tol)
    probable = match_groups(a_rows, b_rows, rates, fx_tol=fx_tol, loose=True)

    return {
        "matched": matches,
        "groups": groups,
        "probable": probable,
        "confirmed": confirmed,
        "fx_adjustments": {"a": fx_a, "b": fx_b},
        "contras": {"a": contra_a, "b": contra_b},
        "unmatched_a": [t for t in a_rows if not t.matched],
        "unmatched_b": [t for t in b_rows if not t.matched],
    }


def wrong_flavour_hints(results: dict, rates, day_tol: int = 10) -> dict[int, str]:
    """Break class B: an item left one-sided on one flavour whose mirror sits,
    one-sided too, on a DIFFERENT flavour of the same two entities.

    `results` is [(pair, match_sides result)] for every reconciled pair. Two
    one-sided items, one in each entity, on two flavours of the same entity
    pair, opposite in sign and within 3% in the reporting currency, are
    almost always one posting routed to the wrong flavour on one side.
    Returns {id(txn): note} for both legs. The items stay one-sided: the fix
    is a reclass in whichever side is wrong, and rules/INTERCOMPANY.md says
    which flavour the item belongs in.
    """
    hints: dict[int, str] = {}
    by_ents: dict[frozenset, list] = {}
    for p, res in results:
        by_ents.setdefault(frozenset((p.a, p.b)), []).append((p, res))
    for group in by_ents.values():
        if len(group) < 2:
            continue
        loose = []                                   # (pair, txn)
        for p, res in group:
            loose += [(p, t) for t in res["unmatched_a"] + res["unmatched_b"]]
        for i, (p1, t1) in enumerate(loose):
            for p2, t2 in loose[i + 1:]:
                if p1 is p2 or t1.entity == t2.entity or id(t1) in hints or id(t2) in hints:
                    continue
                if days_apart(t1, t2) <= day_tol and _within(to_rep(t1, rates), to_rep(t2, rates), 0.03):
                    hints[id(t1)] = f"WRONG FLAVOUR? mirror in {t2.entity} on {p2.flavour} [{t2.code}]"
                    hints[id(t2)] = f"WRONG FLAVOUR? mirror in {t1.entity} on {p1.flavour} [{t1.code}]"
    return hints


def derive_rates(txns, as_at: str | None = None, window_days: int = 45) -> dict[str, float]:
    """Cross rates to the reporting currency from Xero's own document
    CurrencyRates: {currency: reporting units per 1 unit}.

    A document in currency C on an entity of base B carries CurrencyRate r =
    C units per 1 B (stored inverted, see to_base). So once either side is
    known the other follows: C = B / r, and B = C * r. Starting from the
    reporting currency at 1.0, rates propagate entity by entity until no new
    currency can be reached. The median of the observations shrugs off
    outliers. Anything still unreached falls back to default_rates in
    config [intercompany_settings], then to --rate.

    A BALANCE is translated at the rate on its balance date, so when `as_at` is
    given only observations within `window_days` before it are used, widening
    only if that finds nothing. Taking the median across a whole year instead
    silently values an August balance at January's rate, which alone can move
    a reported cross-currency break by thousands.
    """
    rep = reporting()

    def median(vals):
        vals = sorted(vals)
        return vals[len(vals) // 2]

    def solve(cutoff):
        known = {rep: 1.0}
        while True:
            obs: dict[str, list[float]] = {}
            for rows in txns.values():
                for t in rows:
                    if not (t.currency and t.rate) or t.currency == t.entity_base:
                        continue
                    if cutoff and t.date < cutoff:
                        continue
                    c, b = t.currency, t.entity_base
                    if b in known and c not in known:
                        obs.setdefault(c, []).append(known[b] / t.rate)
                    elif c in known and b not in known:
                        obs.setdefault(b, []).append(known[c] * t.rate)
            if not obs:
                return known
            for ccy, vals in obs.items():
                known[ccy] = median(vals)

    cutoff = None
    if as_at:
        cutoff = (datetime.date.fromisoformat(as_at) - datetime.timedelta(days=window_days)).isoformat()
    rates = {str(k).upper(): float(v) for k, v in setting("default_rates", {}).items()}
    rates.update(solve(None))                # widen only where the window is empty
    rates.update(solve(cutoff))
    rates[rep] = 1.0
    return rates


# -------------------------------------------------------------- reporting

def fmt(t: "Txn") -> str:
    fx = f"{t.currency} {t.doc_amount:,.2f} @{t.rate:.4f}" if t.rate and t.currency else ""
    return (f"{t.date} {t.source:<13} {t.entity_base} {t.base:>14,.2f} | "
            f"{t.party[:26]:<26} | {t.desc[:46]:<46} {fx}")


def status_of(brk: float, av: float, bv: float, a_rows, b_rows, n_un: int) -> str:
    tol = float(settings()["tolerance_abs"])
    small = float(setting("small_break", 500.0))
    if av == 0 and bv == 0 and not a_rows and not b_rows:
        return "nil both sides"
    if abs(brk) < tol:
        return "AGREED"
    if abs(brk) < small:
        return f"small diff ({n_un} unmatched)"
    return f"BREAK ({n_un} unmatched)"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    today = datetime.date.today()
    q_start = config.fy_quarter_start(today).isoformat()
    ap.add_argument("--from", dest="date_from", default=q_start,
                    help="window start (default: first day of the current financial quarter, "
                         "from [company] financial_year_end)")
    ap.add_argument("--to", dest="date_to", default=today.isoformat())
    ap.add_argument("--as-at", dest="as_at", default=None, help="balance date (default --to)")
    ap.add_argument("--periods", type=int, default=6)
    ap.add_argument("--rate", action="append", default=[], metavar="CCY=REPORTING_PER_UNIT",
                    help="override a cross rate to the reporting currency, e.g. --rate EUR=1.10")
    ap.add_argument("--pair", default=None,
                    help="only this pair: A:B (every flavour) or A:B:flavour, entity keys from config")
    ap.add_argument("--json", dest="json_out", default=None,
                    help="full detail (default data/interco/interco_recon.json)")
    args = ap.parse_args()
    as_at = args.as_at or args.date_to
    # both ends of the window are month ends (docs/INTERCOMPANY_RECON.md §2):
    # the transaction pull runs to the month end of --to so forward-dated
    # journals the Balance Sheet column already holds are in the rebuild
    args.date_to = month_end(args.date_to)

    ENTITY, BASE, REP = entity_map(), base_ccy(), reporting()
    pairs = select_pairs(args.pair)
    non_group = config.non_group_accounts()

    codes: dict[str, set[str]] = {k: set() for k in ENTITY}
    for p in pairs:
        for ent in (p.a, p.b):
            if p.account(ent):
                codes[ent].add(p.account(ent))
    if not args.pair:
        for ent, extra in non_group.items():
            codes.setdefault(ent, set()).update(extra)
    involved = [e for e in ENTITY if codes.get(e)]

    meta: dict[str, dict] = {e: {} for e in involved}
    with ThreadPoolExecutor(5) as ex:
        tf = {e: ex.submit(pull_txns, e, codes[e], args.date_from, args.date_to) for e in involved}
        bf = {e: ex.submit(pull_balances, e, as_at, args.periods, meta[e]) for e in involved}
        txns = {e: f.result() for e, f in tf.items()}
        bals = {e: f.result() for e, f in bf.items()}
    for e in ENTITY:
        txns.setdefault(e, [])
        bals.setdefault(e, {})

    rates = derive_rates(txns, as_at)
    for spec in args.rate:
        ccy, val = spec.split("=")
        rates[ccy.strip().upper()] = float(val)

    # Xero labels a part-month column with that month's end date.
    label = next((list(v)[0] for e in ENTITY for v in bals[e].values() if v), None)

    print(f"\nINTERCOMPANY RECONCILIATION   {config.company_name()}   transactions "
          f"{args.date_from}..{args.date_to}   balances as at {as_at} (column '{label}')")
    print(f"cross rates to {REP}: " + ", ".join(f"{k}={v:.6g}" for k, v in sorted(rates.items())))
    print("=" * 118)
    print(f'{"pair / flavour":<40}{"A side":>25}{"B side":>25}{"break " + REP:>14}  status')
    print("-" * 118)

    report = {"window": [args.date_from, args.date_to], "as_at": as_at, "reporting_currency": REP,
              "rates": rates, "pairs": [], "structural": []}
    details, grand, results = [], 0.0, []

    for p in pairs:
        a_ent, b_ent, flavour = p.a, p.b, p.flavour
        a_code, b_code = p.account(a_ent), p.account(b_ent)
        name = pair_title(p)
        a_rows = [t for t in txns[a_ent] if t.code == a_code] if a_code else []
        b_rows = [t for t in txns[b_ent] if t.code == b_code] if b_code else []
        av = (bals[a_ent].get(a_code) or {}).get(label, 0.0) if a_code else 0.0
        bv = (bals[b_ent].get(b_code) or {}).get(label, 0.0) if b_code else 0.0

        if a_code is None or b_code is None:
            # Structural gap: the mirror account is not configured (or not
            # opened yet). Never matched; reported under its own heading.
            missing = a_ent if a_code is None else b_ent
            other_v = bv if a_code is None else av
            other_rows = b_rows if a_code is None else a_rows
            extra = (f" (other side holds {other_v:,.2f}, {len(other_rows)} txns in window)"
                     if other_v or other_rows else "")
            print(f'{name:<40}{"-- none --" if a_code is None else format(av, ",.2f"):>25}'
                  f'{"-- none --" if b_code is None else format(bv, ",.2f"):>25}'
                  f'{"n/a":>14}  NO MIRROR ACCOUNT in {missing}{extra}')
            report["pairs"].append({"pair": name, "flavour": flavour, "status": "no_mirror_account",
                                    "a_entity": a_ent, "b_entity": b_ent, "missing_in": missing,
                                    "other_balance": other_v,
                                    "other_rows": [t.__dict__ for t in other_rows]})
            report["structural"].append(f"{name}: no mirror account in {missing}{extra}")
            continue

        res = match_sides(a_rows, b_rows, rates)
        results.append((p, res))
        brk = av * rate(rates, BASE[a_ent]) + bv * rate(rates, BASE[b_ent])
        grand += abs(brk)
        n_un = len(res["unmatched_a"]) + len(res["unmatched_b"])
        residual = (sum(to_rep(a, rates) + to_rep(b, rates) for a, b, _ in res["matched"])
                    + sum(sum(to_rep(t, rates) for t in ga) + sum(to_rep(t, rates) for t in gb)
                          for ga, gb, _ in res["groups"] + res["confirmed"]))
        status = status_of(brk, av, bv, a_rows, b_rows, n_un)

        print(f'{name:<40}{a_code + " " + format(av, ",.2f") + " " + BASE[a_ent]:>25}'
              f'{b_code + " " + format(bv, ",.2f") + " " + BASE[b_ent]:>25}'
              f'{brk:>14,.2f}  {status}')

        report["pairs"].append({
            "pair": name, "flavour": flavour, "break": brk, "status": status,
            "a": {"entity": a_ent, "code": a_code, "base": BASE[a_ent], "balance": av},
            "b": {"entity": b_ent, "code": b_code, "base": BASE[b_ent], "balance": bv},
            "matched": [(x.__dict__, y.__dict__, w) for x, y, w in res["matched"]],
            "groups": [([t.__dict__ for t in ga], [t.__dict__ for t in gb], w) for ga, gb, w in res["groups"]],
            "probable": [([t.__dict__ for t in ga], [t.__dict__ for t in gb], w) for ga, gb, w in res["probable"]],
            "confirmed": [([t.__dict__ for t in ga], [t.__dict__ for t in gb], w) for ga, gb, w in res["confirmed"]],
            "matched_residual": residual,
            "fx_adjustments": {k: [t.__dict__ for t in v] for k, v in res["fx_adjustments"].items()},
            "contras": {k: [(x.__dict__, y.__dict__) for x, y in v] for k, v in res["contras"].items()},
            "unmatched_a": [t.__dict__ for t in res["unmatched_a"]],
            "unmatched_b": [t.__dict__ for t in res["unmatched_b"]],
        })

        if (n_un or abs(residual) > 1 or res["groups"] or res["probable"] or res["confirmed"]
                or res["fx_adjustments"]["a"] or res["fx_adjustments"]["b"]):
            details.append((name, a_ent, b_ent, res, rates))

    print("-" * 118)
    print(f'{"TOTAL ABSOLUTE BREAK (" + REP + ")":<90}{grand:>28,.2f}')

    hints = wrong_flavour_hints(results, rates)
    report["structural"] += account_problems(pairs, meta)
    if report["structural"]:
        print("\nSTRUCTURAL GAPS (break class G: fix the chart or config/group.toml before first use)")
        for s in report["structural"]:
            print("  " + s)

    if not args.pair and non_group:
        print("\nNON-GROUP intercompany-looking accounts (reported, never matched)")
        report["non_group"] = []
        for ent, accts in non_group.items():
            for code, lbl in accts.items():
                v = (bals.get(ent, {}).get(code) or {}).get(label, 0.0)
                n = sum(1 for t in txns.get(ent, []) if t.code == code)
                print(f"  {ent:<10} {code:<8} {lbl[:50]:<50} {v:>16,.2f} {BASE.get(ent, '')}"
                      f"  ({n} txns in window)")
                report["non_group"].append({"entity": ent, "code": code, "label": lbl, "balance": v})

    for name, a_ent, b_ent, res, rates_ in details:
        print("\n" + "=" * 118 + f"\n{name}\n" + "=" * 118)
        if res["unmatched_a"]:
            print(f"  ONE-SIDED in {a_ent} (no counterpart in {b_ent}):")
            for t in res["unmatched_a"]:
                print("    " + fmt(t) + (f"   << {hints[id(t)]}" if id(t) in hints else ""))
        if res["unmatched_b"]:
            print(f"  ONE-SIDED in {b_ent} (no counterpart in {a_ent}):")
            for t in res["unmatched_b"]:
                print("    " + fmt(t) + (f"   << {hints[id(t)]}" if id(t) in hints else ""))
        for ga, gb, w in res["groups"]:
            d = sum(to_rep(t, rates_) for t in ga) + sum(to_rep(t, rates_) for t in gb)
            print(f"  GROUP MATCH {w}: {len(ga)} in {a_ent} against {len(gb)} in {b_ent}, "
                  f"translation gap {REP} {d:,.2f}")
            for t in ga:
                print(f"    [{a_ent}] " + fmt(t))
            for t in gb:
                print(f"    [{b_ent}] " + fmt(t))
        for ga, gb, w in res["confirmed"]:
            d = sum(to_rep(t, rates_) for t in ga) + sum(to_rep(t, rates_) for t in gb)
            print(f"  CONFIRMED MATCH (recorded as one transaction, nets off; translation gap {REP} {d:,.2f}):"
                  f" {w[:90]}")
            for t in ga:
                print(f"    [{a_ent}] " + fmt(t))
            for t in gb:
                print(f"    [{b_ent}] " + fmt(t))
        for ga, gb, w in res["probable"]:
            d = sum(to_rep(t, rates_) for t in ga) + sum(to_rep(t, rates_) for t in gb)
            print(f"  PROBABLE MATCH {w}: same supplier or subject, amounts differ by {REP} {d:,.2f}"
                  " - investigate the difference")
            for t in ga:
                print(f"    [{a_ent}] " + fmt(t))
            for t in gb:
                print(f"    [{b_ent}] " + fmt(t))
        for side, ent in (("a", a_ent), ("b", b_ent)):
            for x, y in res["contras"][side]:
                print(f"  REVERSED in {ent} (posting and its reversal, nets to nil, filtered out):")
                print("    " + fmt(x))
                print("    " + fmt(y))
            for t in res["fx_adjustments"][side]:
                print(f"  FX REVALUATION in {ent} (one-sided by design):")
                print("    " + fmt(t))
        for a, b, _w in res["matched"]:
            d = to_rep(a, rates_) + to_rep(b, rates_)
            # If both ledgers recorded the SAME transaction-currency amount the
            # intercompany item agrees; any gap is a local-currency translation
            # difference, cleared by revaluation, never by a correcting journal.
            agreed_ccy = abs(a.doc_amount + b.doc_amount) < 0.01
            if abs(d) > 1 and not agreed_ccy:
                print(f"  AMOUNT / FX DIFFERENCE on a matched pair: {REP} {d:,.2f}"
                      f"  ({a.entity} {a.doc_amount:,.2f} {a.currency or a.entity_base}"
                      f" vs {b.entity} {b.doc_amount:,.2f} {b.currency or b.entity_base})")
                print(f"    [{a.entity}] " + fmt(a))
                print(f"    [{b.entity}] " + fmt(b))
            elif abs(d) > 1:
                print(f"  translation difference only (agrees at {abs(a.doc_amount):,.2f} "
                      f"transaction currency): {REP} {d:,.2f} carrying gap | {a.desc[:44]}")
            elif a.date != b.date:
                print(f"  TIMING DIFFERENCE: {a.entity} {a.date} vs {b.entity} {b.date}"
                      f"  ({a.base:,.2f} {a.entity_base}) {a.desc[:44]}")

    if MISSING_RATES:
        msg = (f"WARNING: no rate to {REP} for {', '.join(sorted(MISSING_RATES))}; translated at 1.0. "
               f"Re-run with --rate CCY=..., or set default_rates in [intercompany_settings].")
        print("\n" + msg)
        report["warnings"] = [msg]

    path = args.json_out or str(ROOT / "data" / "interco" / "interco_recon.json")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as fh:
        json.dump(report, fh, indent=1, default=str)
    print(f"\nfull detail: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
