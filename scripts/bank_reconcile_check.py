#!/usr/bin/env python
"""Which bank lines are still open on Xero's reconcile screen, and what each
one goes to. Skill: xero-bank-reconcile-check.

Xero's API cannot read a statement line or whether it is reconciled, so the
answer is inferred, and every inference is checked against the money:

  1. Every SETTLED movement on every mapped bank account, from the sources the
     bills report uses (the saved statement CSV up to each account's
     csv_until, the read-only Revolut and Mercury APIs after). Pending,
     failed, cancelled and reversed movements are not cash and are dropped.
  2. Every bank-side record Xero holds on the matching Xero bank account:
     payments (at their BankAmount, never the bill-currency Amount), spend
     and receive money, transfer, prepayment and overpayment legs, with
     IsReconciled; and every open bill, invoice, expense claim and credit
     note in every entity.
  3. RECONCILED: movements are paired ONE-TO-ONE with reconciled records of the
     same bank-side amount to the cent within 5 days (a batch payment, or
     several payments of one day to one contact, may make up one movement),
     nearest date first, then the payee's name. A movement no reconciled
     record is left for is open.
  4. AMBIGUOUS: a repeated amount the dates cannot settle; the message says
     "one of these".
  5. Each open movement is given what it goes to, in three tiers: code when
     the exact amount is unique and the payee's name, who paid or the number
     agrees; code tie-breaks (who paid, a reference, merchant detail, a clear
     date) where the amount repeats; then one call of the bank-line-match
     agent (a small model, no tools, no repo context) for the rest, every
     answer verified by code. A verified "certain" pick is TO MATCH; anything
     else is AWAITING INFORMATION, the NO DOCUMENT path with its candidates
     noted. A document in another entity is INTERCO (bill-payments
     territory); nothing at all is NO DOCUMENT, the native bank feed's line.
  6. SUSPECT WRONG MATCH: a reconciled payment whose bank amount does not fit
     its bill, or an open bill that fits a line someone reconciled to another
     bill.
  7. A pasted Xero reconcile screen (`confirm`) overrides all of it for every
     line up to the moment it was pasted, and every disagreement between the
     paste and the inference is reported.
  8. Tie-out per account: (bank balance) - (Xero balance) must equal (open
     movements) - (unreconciled Xero records). An account that does not tie
     and has no paste is UNVERIFIED: its lines go to the admin, never to the
     bookkeeper as "reconcile this".

Which bank account is which Xero account comes from config/group.toml, the
`[[entities.bank_accounts]]` blocks: `xero_account_code` or `xero_account`
names the Xero bank account, `statement_name` tells two blocks of one bank
login apart, `currency` limits a block to one sub-account. A movement on an
account no block claims, or two blocks claim, is refused and listed, never
put on the nearest-looking account. An entity's `reconcile_check` says how it
is read: `full` (its banks and Xero), `documents` (open documents only, for
the cross-entity search; the default with no bank account) or `off`.
Tolerances and caps come from the optional `[bank_reconcile]` section.

Outputs (server only, data/ is gitignored):
    data/bankfeed/unreconciled/<entity>.md   per account: every open line, its
                                             status, what it goes to, the tie-out
    data/bankfeed/unreconciled.json          the same, for scripts/reconcile_list.py
    data/bankfeed/confirmed/<entity>.json    pasted reconcile screens

Usage:
    .venv/bin/python scripts/bank_reconcile_check.py run [entity|all] [--since YYYY-MM-DD]
                     [--dry-run] [--cache DIR] [--out DIR] [--sync-feed] [--no-agent]
    .venv/bin/python scripts/bank_reconcile_check.py confirm <entity> "<account>" --paste <file> [--at ISO]
    .venv/bin/python scripts/bank_reconcile_check.py show [entity]

READ-ONLY against Xero and the banks, always. Writes only its own files, and
none at all under --dry-run or AGENT_READONLY (unless --out names a folder).
`--sync-feed` edits the native bank feed through scripts/bankfeed.py: it adds
the NO DOCUMENT lines the feed is missing and removes only lines a paste, or a
one-to-one exact match with the payee's name, shows reconciled.
`AGENT_ROOT` points the script at a repo other than its own (testing from a
temporary folder); `--cache DIR` keeps the Xero and bank pulls for development.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import statistics
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from itertools import combinations
from pathlib import Path

ROOT = (Path(os.environ["AGENT_ROOT"]).resolve() if os.environ.get("AGENT_ROOT")
        else Path(__file__).resolve().parents[1])
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from accounting_agent import config                                  # noqa: E402
import bills_report as br                                            # noqa: E402

FEED = ROOT / "data" / "bankfeed"
OUT = Path(os.environ["BRC_OUT_DIR"]) if os.environ.get("BRC_OUT_DIR") else FEED


class _Short:
    """SHORT[slug]: how reports name an entity."""

    def get(self, key, default=""):
        try:
            return config.entity(key).short
        except config.ConfigError:
            return default or key

    def __getitem__(self, key):
        return self.get(key)


SHORT = _Short()
# Entities read in full (their banks and Xero) and entities whose open
# documents alone are read for the cross-entity search, from each entity's
# `reconcile_check` in config/group.toml. `off` is read by nobody.
ENTITIES = [e.slug for e in config.entities() if e.bank_check_mode == "full"]
DOC_ONLY = [e.slug for e in config.entities() if e.bank_check_mode == "documents"]
# Per entity, the configured bank account's name inside this script -> the
# Xero bank account it feeds. Filled from each entity's chart on every run by
# set_bank_map(); a movement on an account not in it is refused.
BANK_MAP: dict[str, dict[str, str]] = {}

_S = config.bank_reconcile()
SINCE = None                                    # the window is worked out per entity: window_since()
WINDOW_CAP_DAYS = int(_S["window_cap_days"])    # the bank is never read further back than this
WATERMARK_PAD = int(_S["watermark_pad_days"])   # the window opens this many days before the oldest line the last run left open
PAD_DAYS = int(_S["pad_days"])                  # movements read before the window as context: a record near the edge finds its own line
RECONCILED_GAP = int(_S["reconciled_gap_days"])             # a reconciled record and its statement line, days apart at most
RECONCILED_GAP_NAMED = int(_S["reconciled_gap_named_days"])  # ... where the payee's name agrees
RECORD_GAP = int(_S["record_gap_days"])         # an unreconciled record and an open line, without the payee's name
RECORD_GAP_NAMED = int(_S["record_gap_named_days"])  # ... with it (a claim dated at the receipt, paid weeks later)
DOC_BEFORE, DOC_AFTER = int(_S["doc_before_days"]), int(_S["doc_after_days"])
FX_TOLERANCE = float(_S["fx_tolerance"])        # a converted document fits a line within this; one currency is always to the cent
WRONG_FX = float(_S["wrong_fx"])                # a reconciled payment's implied rate further than this from the median is suspect
WRONG_FX_ANY = float(_S["wrong_fx_named"])      # ... even when the line names the payee
PART_WORDS = {"deposit", "balance", "instalment", "installment", "part", "partial", "advance"}
FEE_WORDS = {"fee", "fees", "charge", "charges"}
FEED_LAG_DAYS = int(_S["feed_lag_days"])        # a line settled this close before a paste may not have reached Xero yet
DOCS_BACK_DAYS = int(_S["docs_back_days"])      # documents of every status read this far before the window

# What an open line goes to, in three tiers: code when the exact amount is
# unique and named, code tie-breaks where it repeats, then one agent call per
# run for what is left, every answer verified by code.
DATE_CLEAR, DATE_FAR = 1, 3      # a date breaks a tie: this candidate within 1 day, every other 3 or more away
TRANSFER_GAP = 2                 # a transfer leg names no payee: exact, unique and this close is enough
NARROW_AMOUNT = float(_S["narrow_amount"])      # tier 3 candidates: the amount within this share ...
NARROW_NAMED_DAYS = int(_S["narrow_named_days"])  # ... or the name or who paid agreeing, dated within this many days
LINE_MATCH_CAP = int(_S["line_match_cap"])      # lines asked per run
LINE_MATCH_CANDIDATES = int(_S["line_match_candidates"])  # candidates per line, nearest first
LINE_MATCH_BUDGET_TOKENS = int(_S["line_match_budget_tokens"])  # the payload, estimated as chars / 4
# What an amount difference on an agent's pick may be (a tip, an estimate
# against the charge, card FX, a fee). In the line's own currency, or the
# card's: more than the bill by a tip or fee, less only against an estimate
ESTIMATE_WORDS = {"estimate", "estimated", "estimation", "approx", "approximate", "preauth", "hold"}
TIP_MAX = float(_S["tip_max"])                  # the line at most this share of the bill
ESTIMATE_MIN = float(_S["estimate_min"])        # the line at least this share of an estimate
FX_BAND = float(_S["fx_band"])                  # across currencies, this much either side before any tip
LINE_MATCH_AGENT = "bank-line-match"
LINE_MATCH_MODEL = str(_S["line_match_model"])
LINE_MATCH_EFFORT = str(_S["line_match_effort"])
LINE_MATCH_TIMEOUT = int(_S["line_match_timeout"])
LINE_MATCH_MAX_USD = str(_S["line_match_max_usd"])
CLAUDE_BIN = os.environ.get("AGENT_CLAUDE_BIN", "claude")
AGENT_FILE = ROOT / ".claude" / "agents" / f"{LINE_MATCH_AGENT}.md"

OUT_TYPES = {"ACCPAYPAYMENT", "ARCREDITPAYMENT", "AROVERPAYMENTPAYMENT", "ARPREPAYMENTPAYMENT"}
KIND = {"SPEND": "spend money", "RECEIVE": "receive money", "SPEND-TRANSFER": "transfer",
        "RECEIVE-TRANSFER": "transfer", "SPEND-PREPAYMENT": "prepayment", "RECEIVE-PREPAYMENT": "prepayment",
        "SPEND-OVERPAYMENT": "overpayment", "RECEIVE-OVERPAYMENT": "overpayment"}
DOC_DIRECTION = {"ACCPAY": "spent", "ACCREC": "received", "ACCPAYCREDIT": "received", "ACCRECCREDIT": "spent"}

# Words that say nothing about who was paid. br.GENERIC holds the words two
# unrelated names share; these never carry a name at all. The group's own words
# (entity names, short codes, aliases, currency codes) are added by noise().
_NOISE = {"revolut", "mercury", "transfer", "exchanged", "main", "fee", "fees",
          "from", "for", "expense", "claims", "claim", "receipt", "invoice", "lost", "paid", "pay",
          "refund", "debit", "credit", "purchase", "pos", "visa", "mastercard", "www", "http", "https",
          "uk", "inc", "ltd", "card", "bill", "bills", "reimbursement",
          "balance", "expenses", "staff", "sep", "oct", "aug", "jul", "jun", "nov", "dec", "jan", "feb",
          "mar", "apr", "may", "receipts", "fwd", "via", "trip", "travel"}
_NOISE_EXTRA: set[str] | None = None


def noise() -> set[str]:
    """_NOISE plus the group's own words and every currency code in the config."""
    global _NOISE_EXTRA
    if _NOISE_EXTRA is None:
        from accounting_agent.xero.matching import group_words
        codes = {e.base_currency.lower() for e in config.entities()}
        codes |= {b.currency.lower() for e in config.entities() for b in e.bank_accounts if b.currency}
        codes |= {config.reporting_currency().lower(), "usd", "gbp", "eur"}
        _NOISE_EXTRA = set(group_words()) | codes
    return _NOISE | _NOISE_EXTRA


# ------------------------------------------------------------------ helpers

def _today() -> str:
    return date.today().isoformat()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")


def _shift(iso: str, days: int) -> str:
    return (date.fromisoformat(iso) + timedelta(days=days)).isoformat()


def _iso_ts(value: str) -> str:
    """Any timestamp the sources give -> 'YYYY-MM-DDTHH:MM' (UTC), or ''."""
    s = str(value or "")
    m = re.search(r"/Date\((\d+)", s)
    if m:
        return datetime.fromtimestamp(int(m.group(1)) / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M")
    m = re.match(r"(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})", s)
    if m:
        return f"{m.group(1)}T{m.group(2)}"
    m = re.match(r"(\d{2})-(\d{2})-(\d{4}) (\d{2}:\d{2})", s)            # Mercury CSV
    if m:
        return f"{m.group(3)}-{m.group(1)}-{m.group(2)}T{m.group(4)}"
    return ""


def _gap(a: str, b: str) -> int:
    return abs((date.fromisoformat(a) - date.fromisoformat(b)).days)


def _c(x: float) -> int:
    """Cents, so amounts compare exactly."""
    return int(round(abs(x) * 100))


def words(*parts: str) -> set[str]:
    out = set()
    skip = noise()
    for t in br._tokens(" ".join(p or "" for p in parts)):
        if t in skip or not re.search(r"[a-z]", t) or re.fullmatch(r"\d+[a-z]{3}\d+", t):
            continue
        out.add(t)
    return out


def agree(a: set[str], b: set[str]) -> bool:
    """The two share a distinctive word (a truncation counts, br._shared)."""
    return bool(br._shared(a, b) - br.GENERIC)


# ------------------------------------------------------------- who paid

PARTICLES = {"de", "le", "la", "van", "von", "der", "den", "du", "da", "di", "del"}
# An optional pattern for the bill numbers the agent makes itself, with one
# group: the card holder's initials ([bank_reconcile] agent_bill_number_initials).
INITIALS_NUMBER = (re.compile(_S["agent_bill_number_initials"])
                   if _S["agent_bill_number_initials"] else None)
_PEOPLE: dict | None = None


def _ntok(s: str) -> list[str]:
    """Name tokens: two letters or more, so a two-letter name keeps both."""
    return [t for t in re.split(r"[^a-z]+", (s or "").lower()) if len(t) >= 2 and t not in PARTICLES]


def people() -> dict:
    """The staff register's names ([bank_reconcile] staff_register, a JSON file
    {"employees": [{"name": ...}]}; absent means no register and this signal is
    skipped), and the initials an agent-made bill number carries, where they
    belong to one person only."""
    global _PEOPLE
    if _PEOPLE is None:
        names = []
        if _S["staff_register"]:
            try:
                reg = json.loads((ROOT / _S["staff_register"]).read_text())
                names = [e["name"] for e in reg.get("employees", []) if e.get("name")]
            except (OSError, ValueError, KeyError, AttributeError):
                pass
        initials: dict[str, set] = defaultdict(set)
        for n in names:
            ini = "".join(p[0] for p in re.split(r"[\s\-]+", n.strip()) if p and p[0].isalpha()).upper()
            if len(ini) >= 2:
                initials[ini].add(n)
        _PEOPLE = {"names": {n: _ntok(n) for n in names if len(_ntok(n)) >= 2},
                   "initials": {k: next(iter(v)) for k, v in initials.items() if len(v) == 1}}
    return _PEOPLE


def _named_in(tset: set[str], toks: list[str]) -> bool:
    """Every surname token present, and the first name or a short form of it
    ("Rob" for Robert)."""
    first, rest = toks[0], toks[1:]
    if not all(r in tset for r in rest):
        return False
    return first in tset or (len(first) >= 3 and any(
        len(t) >= 3 and (first.startswith(t) or t.startswith(first)) for t in tset))


def people_in(*texts: str, number: str = "") -> set[str]:
    """The staff a document names: in full in its text, or by the initials in an
    agent-made number."""
    tset = set(_ntok(" ".join(t or "" for t in texts)))
    out = {n for n, toks in people()["names"].items() if _named_in(tset, toks)}
    m = INITIALS_NUMBER.match((number or "").strip().upper()) if INITIALS_NUMBER else None
    if m and m.group(1) in people()["initials"]:
        out.add(people()["initials"][m.group(1)])
    return out


def who_person(who: str) -> str:
    """The register's name for a cardholder, where exactly one person fits."""
    hits = people_in(who) if who else set()
    return next(iter(hits)) if len(hits) == 1 else ""


def who_agrees(m: dict, named: set | list, ntoks: set | list) -> bool | None:
    """True: the line's cardholder is a person the record names. False: the
    record names people and the cardholder is not one of them. None: nothing
    to compare."""
    p = m.get("person") or ""
    named = set(named or ())
    if p:
        return True if p in named else (False if named else None)
    wt = _ntok(m.get("who") or "")
    if len(wt) >= 2 and wt[0] in set(ntoks or ()) and wt[-1] in set(ntoks or ()):
        return True                       # first and last name on the record; banks add middle names
    return None


def _d(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{d.day} {d:%b}" if d.year == date.today().year else f"{d.day} {d:%b %Y}"


def _money(ccy: str, amount: float) -> str:
    return f"{ccy} {abs(amount):,.2f}"


def _local(iso_utc: str) -> str:
    """'2026-10-07T08:14Z' -> '7 Oct 2026, 09:14 UTC', in [company] timezone."""
    try:
        from zoneinfo import ZoneInfo
        t = datetime.fromisoformat(iso_utc.replace("Z", "+00:00")).astimezone(
            ZoneInfo(str(config.company("timezone", "UTC") or "UTC")))
        return f"{t.day} {t:%b %Y}, {t:%H:%M %Z}"
    except Exception:                                              # noqa: BLE001
        return iso_utc


# --------------------------------------------------------------- the banks

def _norm(s: str) -> str:
    """A bank account name for comparing: masked digits as xx, one space, lower case."""
    return " ".join(str(s or "").replace("••", "xx").replace("••", "xx").split()).lower()


def bank_ident(bank: config.BankAccount) -> str:
    """One configured bank account's name inside this script: its statement
    name, else its label and currency ("Revolut GBP"). Two blocks of one entity
    must not share it; set statement_name on one of them."""
    return bank.statement_name or " ".join(filter(None, (bank.label, bank.currency)))


def _accepts(bank: config.BankAccount, currency: str, name: str, *, feed: bool = False) -> bool:
    """Whether a statement row, bank account or feed heading called `name` in
    `currency` belongs to this configured account. The block's `currency`
    limits it to one sub-account; its `statement_name`, where given, must be
    in the name. A feed heading of a csv account is the label `bankfeed.py add`
    opens it under."""
    if bank.currency and currency and bank.currency.upper() != str(currency).upper():
        return False
    n, sn = _norm(name), _norm(bank.statement_name)
    if feed and bank.provider == "csv":
        return n in (_norm(bank.label), sn)
    if sn:
        return sn in n or n == _norm(bank.label)
    return True


def _csv_movements(key: str, bank: config.BankAccount, lo: str) -> tuple[list[dict], float | None]:
    """Settled rows of the account's saved statement export, from `lo` to its
    csv_until (to the end of the file for an account with no API). The schema
    is recognised from the header: Revolut, Mercury, or the generic one
    docs/BANKING.md defines. -> (movements, the file's closing balance where it
    carries one and the account has no API)."""
    if not bank.statement_csv or (bank.has_api and not bank.csv_until):
        return [], None
    path = br.STATEMENTS / bank.statement_csv
    if not path.exists():
        return [], None
    end = bank.csv_until or "9999-12-31"
    ident = bank_ident(bank)
    with path.open(newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    out: list[dict] = []
    seen: dict[str, int] = defaultdict(int)
    last_balance: tuple[str, float] | None = None
    base_ccy = config.entity(key).base_currency

    def uid(prefix: str, base: str) -> str:
        seen[base] += 1
        return f"{prefix}:{base}" + (f":{seen[base]}" if seen[base] > 1 else "")

    if rows and "Date completed (UTC)" in rows[0]:                       # Revolut
        for r in rows:
            if (r.get("State") or "").upper() != "COMPLETED":
                continue
            stamp = r.get("Date completed (UTC)") or r.get("Date started (UTC)") or ""
            day = stamp[:10]
            ccy = r.get("Payment currency") or ""
            acct = r.get("Account") or ""
            if not _accepts(bank, ccy, acct):
                continue
            try:
                amt = float(r.get("Amount") or 0)
                total = float(r.get("Total amount") or amt)
            except ValueError:
                continue
            if r.get("Balance"):
                try:
                    if last_balance is None or stamp >= last_balance[0]:
                        last_balance = (stamp, float(r["Balance"]))
                except ValueError:
                    pass
            if not (lo <= day <= end):
                continue
            oc, oa = r.get("Orig currency") or "", r.get("Orig amount") or ""
            txid = r.get("ID") or f"{day}-{r.get('Description')}-{amt}"
            payee = " ".join((r.get("Description") or "").split())
            heading = config.statement_account_name("revolut", ccy, acct, bank.label)
            base = {"entity": key, "bank": ident, "heading": heading, "date": day, "currency": ccy, "alt": "",
                    "who": r.get("Payer") or r.get("Card label") or "", "reference": r.get("Reference") or "",
                    "source": "statement csv", "settled_at": stamp}
            if amt:
                out.append({**base, "id": f"csv:{txid}:{ident}", "amount": amt, "payee": payee,
                            "orig_amount": float(oa) if oa and oc and oc != ccy else None,
                            "orig_currency": oc if oa and oc != ccy else ""})
            # Revolut's fee reaches Xero as a statement line of its own, on the
            # leg it was taken from: where the total differs from the amount
            if round(total - amt, 2):
                out.append({**base, "id": f"csv:{txid}:{ident}:fee", "amount": round(total - amt, 2),
                            "payee": f"Fee: {payee}", "orig_amount": None, "orig_currency": ""})
    elif rows and "Date (UTC)" in rows[0]:                                 # Mercury
        for r in rows:
            if (r.get("Status") or "").lower() != "sent":
                continue
            m = re.match(r"(\d{2})-(\d{2})-(\d{4})", r.get("Date (UTC)") or "")
            day = f"{m.group(3)}-{m.group(1)}-{m.group(2)}" if m else ""
            acct = r.get("Source Account") or ""
            if not day or not (lo <= day <= end) or not _accepts(bank, bank.currency, acct):
                continue
            try:
                amt = float(r.get("Amount") or 0)
            except ValueError:
                continue
            if not amt:
                continue
            out.append({"entity": key, "bank": ident, "heading": config.statement_account_name(
                            "mercury", "", acct, bank.label),
                        "date": day, "currency": bank.currency or "USD", "amount": amt,
                        "id": f"csv:{r.get('Tracking ID') or r.get('Timestamp')}:{ident}:{amt}",
                        "payee": " ".join((r.get("Description") or "").split()),
                        "alt": " ".join((r.get("Bank Description") or "").split()),
                        "who": r.get("Name On Card") or "",
                        "reference": " ".join(filter(None, (r.get("Reference"), r.get("Note")))),
                        "orig_amount": None, "orig_currency": "", "source": "statement csv",
                        "settled_at": r.get("Timestamp") or ""})
    else:                                                                  # generic (docs/BANKING.md)
        for r in rows:
            low = {(k or "").strip().lower(): (v or "").strip() for k, v in r.items()}
            if (low.get("status") or "").lower() in ("pending", "failed", "cancelled", "declined", "reverted"):
                continue
            day = (low.get("date") or "")[:10]
            ccy = (low.get("currency") or bank.currency or base_ccy).upper()
            acct = low.get("account") or ""
            if not day or not _accepts(bank, ccy, acct):
                continue
            amt = br._num(low.get("amount"))
            if low.get("balance"):
                try:
                    if last_balance is None or day >= last_balance[0]:
                        last_balance = (day, float(low["balance"].replace(",", "")))
                except ValueError:
                    pass
            if not amt or not (lo <= day <= end):
                continue
            oa = low.get("original amount") or ""
            payee = " ".join((low.get("description") or low.get("payee") or "").split())
            out.append({"entity": key, "bank": ident, "heading": acct or bank.label, "date": day,
                        "currency": ccy, "amount": amt, "id": uid("csv", f"{day}-{payee}-{amt}:{ident}"),
                        "payee": payee, "alt": "", "who": low.get("payer") or low.get("card") or "",
                        "reference": low.get("reference") or "",
                        "orig_amount": br._num(oa) if oa else None,
                        "orig_currency": (low.get("original currency") or "").upper(),
                        "source": "statement csv", "settled_at": day})
    balance = last_balance[1] if last_balance and not bank.has_api else None
    return out, balance


def _revolut(slug: str, from_date: str) -> tuple[list[dict], list[dict]]:
    """Settled legs from `from_date`, and per bank account the ledger balance
    now: the API balance is net of pending holds, so they are added back."""
    from accounting_agent.revolut import RevolutClient
    cl = RevolutClient(slug)
    accounts = cl.accounts()
    label = {a["id"]: config.statement_account_name("revolut", a.get("currency"), a.get("name"))
             for a in accounts}
    txns = cl.transactions(from_date=from_date)
    pending: dict[str, float] = defaultdict(float)
    out = []
    for t in txns:
        state = (t.get("state") or "").lower()
        for leg in t.get("legs") or []:
            lab = label.get(leg.get("account_id"), leg.get("account_id") or "")
            amt, fee = float(leg.get("amount") or 0), float(leg.get("fee") or 0)
            if state == "pending":
                pending[lab] += amt - fee
                continue
            if state != "completed":
                continue                      # declined, failed, reverted: not cash
            day = (t.get("completed_at") or t.get("created_at") or "")[:10]
            merchant = t.get("merchant") or {}
            card = t.get("card") or {}
            ccy = leg.get("currency") or ""
            bc, ba = leg.get("bill_currency") or "", leg.get("bill_amount")
            base = {"day": day, "heading": lab, "currency": ccy,
                    "who": " ".join(filter(None, (card.get("first_name"), card.get("last_name")))),
                    "reference": t.get("reference") or "", "source": "revolut api",
                    "settled_at": t.get("completed_at") or ""}
            payee = merchant.get("full_name") or merchant.get("name") or leg.get("description") or t.get("reference") or ""
            if amt:
                out.append({**base, "id": f"rev:{t.get('id')}:{lab}", "amount": amt,
                            "payee": " ".join(payee.split()), "alt": leg.get("description") or "",
                            "orig_amount": float(ba) if ba is not None and bc and bc != ccy else None,
                            "orig_currency": bc if bc and bc != ccy else ""})
            if fee:
                # the fee is a statement line of its own in Xero
                out.append({**base, "id": f"rev:{t.get('id')}:{lab}:fee", "amount": -abs(fee),
                            "payee": f"Fee: {' '.join(payee.split())}", "alt": "", "orig_amount": None,
                            "orig_currency": ""})
    balances = [{"heading": label[a["id"]], "currency": a.get("currency"),
                 "ledger": round(float(a.get("balance") or 0) - pending[label[a["id"]]], 2),
                 "pending": round(pending[label[a["id"]]], 2)} for a in accounts]
    return out, balances


def _mercury(slug: str, from_date: str) -> tuple[list[dict], list[dict]]:
    """Posted movements from `from_date`; Mercury's currentBalance is the posted ledger."""
    from accounting_agent.mercury import MercuryClient
    cl = MercuryClient(slug)
    accounts = cl.accounts()
    holder = _mercury_holders(cl, accounts)
    out = []
    for l in cl.statement_lines(from_date=from_date):
        if (l.get("state") or "").lower() != "sent":
            continue
        day = l.get("date") or ""
        amt = float(l.get("amount") or 0)
        if not amt:
            continue
        lab = config.statement_account_name("mercury", "", l.get("account_name"))
        out.append({"day": day, "heading": lab, "currency": l.get("currency") or "USD", "amount": amt,
                    "id": f"mer:{l.get('id')}:{lab}",
                    "payee": " ".join((l.get("merchant") or l.get("counterparty") or l.get("description") or "").split()),
                    "alt": l.get("description") or "",
                    "who": (l.get("cardholder") or holder.get(l.get("card_id") or "")
                            or holder.get((l.get("card_email") or "").lower()) or ""),
                    "reference": l.get("reference") or "", "orig_amount": None, "orig_currency": "",
                    "source": "mercury api", "settled_at": l.get("completed_at") or ""})
    balances = [{"heading": config.statement_account_name("mercury", "", a.get("name")), "currency": "USD",
                 "ledger": round(float(a.get("currentBalance") or 0), 2), "pending": None} for a in accounts]
    return out, balances


def _mercury_holders(cl, accounts: list[dict]) -> dict[str, str]:
    """Card id -> name on the card, and user email -> name, read-only. Mercury's
    transactions carry the card's id (debit) or the user's email (credit card),
    never the name. A failure here costs the who-paid signal, nothing else."""
    out: dict[str, str] = {}
    for a in accounts:
        try:
            for c in (cl.get(f"account/{a['id']}/cards") or {}).get("cards", []):
                if c.get("cardId") and c.get("nameOnCard"):
                    out[c["cardId"]] = c["nameOnCard"]
        except Exception:                                          # noqa: BLE001
            continue
    try:
        for u in (cl.get("users") or {}).get("users", []):
            if u.get("email"):
                out[u["email"].lower()] = " ".join(filter(None, (u.get("firstName"), u.get("lastName"))))
    except Exception:                                              # noqa: BLE001
        pass
    return out


def load_bank(key: str, lo: str, cache: Path | None) -> dict:
    """Every settled movement of one entity from `lo`, each on the configured
    bank account it came from, and each account's balance now. Rows on an API
    account that no block claims, or two claim, are returned apart as
    `unmapped`: they are refused, never guessed onto an account."""
    path = cache / f"{key}.bank.json" if cache else None
    if path and path.exists():
        return json.loads(path.read_text())
    ent = config.entity(key)
    moves: list[dict] = []
    balances: dict[str, dict] = {}
    unmapped: list[dict] = []
    for bank in ent.bank_accounts:
        got, bal = _csv_movements(key, bank, lo)
        moves += got
        if bal is not None:
            balances[bank_ident(bank)] = {"currency": bank.currency, "ledger": round(bal, 2), "pending": None}
    groups: dict[tuple[str, str], list[config.BankAccount]] = defaultdict(list)
    for bank in ent.bank_accounts:
        if bank.has_api:
            groups[(bank.provider, bank.slug)].append(bank)
    for (provider, slug), banks in groups.items():
        # the API takes over the day after each account's csv_until, and never reads before the window
        start = min(max(lo, b.csv_until) if b.csv_until else lo for b in banks)
        raw, accounts = (_revolut if provider == "revolut" else _mercury)(slug, _shift(start, -5))
        for r in raw:
            day = r.pop("day")
            owners = [b for b in banks if _accepts(b, r["currency"], r["heading"])]
            if day < lo or any(b.csv_until and day <= b.csv_until for b in owners):
                continue
            row = {"entity": key, "date": day, **r}
            if len(owners) == 1:
                moves.append({**row, "bank": bank_ident(owners[0])})
            else:
                unmapped.append({**row, "bank": "", "why": "no configured bank account claims it"
                                 if not owners else "more than one configured bank account claims it"})
        for a in accounts:
            owners = [b for b in banks if _accepts(b, a["currency"], a["heading"])]
            if len(owners) == 1 and bank_ident(owners[0]) not in balances:
                balances[bank_ident(owners[0])] = {k: a[k] for k in ("currency", "ledger", "pending")}
    data = {"pulled": _now(), "lo": lo, "movements": moves, "unmapped": unmapped, "balances": balances}
    if path:
        path.write_text(json.dumps(data))
    return data


def set_bank_map(key: str, chart: list[dict]) -> list[str]:
    """Fill BANK_MAP[key] from the entity's chart: each configured bank account
    to the Xero bank account `xero_account` names, else the one whose code is
    `xero_account_code`. -> the problems found (an account the chart does not
    hold, two blocks with one name)."""
    by_name = {_norm(a.get("Name")): a for a in chart if a.get("Type") == "BANK"}
    by_code = {str(a.get("Code")): a for a in chart if a.get("Type") == "BANK" and a.get("Code")}
    problems: list[str] = []
    mapping: dict[str, str] = {}
    for bank in config.entity(key).bank_accounts:
        ident = bank_ident(bank)
        acct = by_name.get(_norm(bank.xero_account)) if bank.xero_account else None
        if acct is None and bank.xero_account_code:
            acct = by_code.get(str(bank.xero_account_code))
        if ident in mapping:
            problems.append(f"{ident}: two configured bank accounts share this name; set statement_name on one")
        elif acct is None:
            problems.append(f"{ident}: Xero has no bank account "
                            f"{bank.xero_account or bank.xero_account_code or '(none configured: set xero_account_code or xero_account)'}")
        else:
            mapping[ident] = acct["Name"]
    BANK_MAP[key] = mapping
    return problems


# ---------------------------------------------------------------- Xero pull

def _where_date(field: str, iso: str) -> str:
    y, m, d = iso.split("-")
    return f"{field}>=DateTime({int(y)},{int(m)},{int(d)})"


def pull_xero(key: str, since: str, cache: Path | None, docs_only: bool = False) -> dict:
    """One pull per entity, read-only: the chart, every payment and every live
    bank transaction (all dates, for the balances), documents of every status
    from DOCS_BACK_DAYS before the window plus every open one, credit notes,
    transfers and today's Bank Summary."""
    path = cache / f"{key}.xero.json" if cache else None
    if path and path.exists():
        return json.loads(path.read_text())
    c = br._client(key)
    org = (c.get("Organisation").get("Organisations") or [{}])[0]
    docs_from = _shift(since, -DOCS_BACK_DAYS)
    data = {"pulled": _now(), "since": since, "org": {"BaseCurrency": org.get("BaseCurrency"),
                                                       "ShortCode": org.get("ShortCode")}}
    data["accounts"] = c.get("Accounts").get("Accounts", [])
    data["invoices"] = c.get_all("Invoices", where=_where_date("Date", docs_from))
    seen = {i["InvoiceID"] for i in data["invoices"]}
    data["invoices"] += [i for i in c.get_all("Invoices", where='Status=="AUTHORISED"') if i["InvoiceID"] not in seen]
    data["credit_notes"] = c.get_all("CreditNotes", where=f'Status=="AUTHORISED" OR {_where_date("Date", docs_from)}')
    if not docs_only:
        data["payments"] = c.get_all("Payments")
        data["bank_transactions"] = c.get_all("BankTransactions", where='Status=="AUTHORISED"')
        data["bank_summary"] = c.get("Reports/BankSummary", fromDate=_today(), toDate=_today())
    if path:
        path.write_text(json.dumps(data))
    return data


def bank_summary(report: dict) -> dict[str, float]:
    """Closing balance per Xero bank account, in the org's base currency."""
    out = {}
    for sec in (report.get("Reports") or [{}])[0].get("Rows", []):
        for row in sec.get("Rows", []) or []:
            cells = row.get("Cells") or []
            # five columns in a USD-based org, six where an FX Gain column is added
            if len(cells) >= 5 and row.get("RowType") == "Row":
                try:
                    out[cells[0].get("Value")] = float(cells[-1].get("Value") or 0)
                except ValueError:
                    pass
    return out


# ------------------------------------------------------------ Xero records

def _doc_text(lines: list[dict]) -> str:
    return " ".join(" ".join((l.get("Description") or "").split()) for l in lines or [])


def _people_of(d: dict, *texts: str, number: str = "") -> None:
    """The staff a document or record names, and its name tokens, for who paid."""
    d["people"] = sorted(people_in(*texts, number=number))
    d["ntoks"] = sorted(set(_ntok(" ".join(t or "" for t in texts))))


def documents(key: str, x: dict) -> tuple[list[dict], dict[str, dict]]:
    """Open documents (AUTHORISED, something still due) and every document by id."""
    base = x["org"]["BaseCurrency"]
    open_docs, by_id = [], {}
    for i in x.get("invoices", []):
        if i.get("Status") in ("DELETED", "VOIDED"):
            continue
        d = {"id": i["InvoiceID"], "entity": key, "type": i.get("Type"), "number": i.get("InvoiceNumber") or "",
             "reference": i.get("Reference") or "", "contact": " ".join(((i.get("Contact") or {}).get("Name") or "").split()),
             "date": br._xdate(i.get("Date")), "currency": i.get("CurrencyCode") or base, "base": base,
             "rate": float(i.get("CurrencyRate") or 1) or 1.0, "total": float(i.get("Total") or 0),
             "due": float(i.get("AmountDue") or 0), "status": i.get("Status"), "text": _doc_text(i.get("LineItems"))}
        d["words"] = sorted(words(d["contact"], d["number"], d["reference"], d["text"]))
        _people_of(d, d["contact"], d["reference"], d["text"], number=d["number"])
        by_id[d["id"]] = d
        if d["status"] == "AUTHORISED" and d["due"] > 0.004 and d["type"] in DOC_DIRECTION:
            open_docs.append(d)
    for n in x.get("credit_notes", []):
        if n.get("Status") in ("DELETED", "VOIDED"):
            continue
        d = {"id": n["CreditNoteID"], "entity": key, "type": n.get("Type"), "number": n.get("CreditNoteNumber") or "",
             "reference": n.get("Reference") or "", "contact": " ".join(((n.get("Contact") or {}).get("Name") or "").split()),
             "date": br._xdate(n.get("Date")), "currency": n.get("CurrencyCode") or base, "base": base,
             "rate": float(n.get("CurrencyRate") or 1) or 1.0, "total": float(n.get("Total") or 0),
             "due": float(n.get("RemainingCredit") or 0), "status": n.get("Status"), "text": _doc_text(n.get("LineItems")),
             "credit_note": True}
        d["words"] = sorted(words(d["contact"], d["number"], d["reference"], d["text"]))
        _people_of(d, d["contact"], d["reference"], d["text"], number=d["number"])
        by_id[d["id"]] = d
        if d["status"] == "AUTHORISED" and d["due"] > 0.004 and d["type"] in DOC_DIRECTION:
            open_docs.append(d)
    return open_docs, by_id


def records(key: str, x: dict, docs_by_id: dict) -> tuple[list[dict], dict[str, dict]]:
    """Every live bank-side record on every Xero bank account, signed from the
    account's side, at its bank-side amount."""
    banks = {a["AccountID"]: a for a in x["accounts"] if a.get("Type") == "BANK"}
    names = {a.get("Code"): a.get("Name") for a in x["accounts"] if a.get("Code")}
    out = []
    for p in x.get("payments", []):
        if (p.get("Status") or "AUTHORISED") == "DELETED":
            continue
        acct = banks.get((p.get("Account") or {}).get("AccountID"))
        if not acct:
            continue
        doc = p.get("Invoice") or p.get("CreditNote") or p.get("Prepayment") or p.get("Overpayment") or {}
        did = doc.get("InvoiceID") or doc.get("CreditNoteID") or ""
        full = docs_by_id.get(did) or {}
        bank_amt = float(p.get("BankAmount") if p.get("BankAmount") is not None else p.get("Amount") or 0)
        out_ = (p.get("PaymentType") or "") in OUT_TYPES
        contact = " ".join(((doc.get("Contact") or {}).get("Name") or full.get("contact") or "").split())
        number = doc.get("InvoiceNumber") or doc.get("CreditNoteNumber") or full.get("number") or ""
        r = {"rid": p["PaymentID"], "kind": "payment", "xero": acct["Name"], "xero_id": acct["AccountID"],
             "currency": acct.get("CurrencyCode") or x["org"]["BaseCurrency"],
             "date": br._xdate(p.get("Date")), "amount": -abs(bank_amt) if out_ else abs(bank_amt),
             "reconciled": bool(p.get("IsReconciled")), "batch": p.get("BatchPaymentID") or "",
             "contact": contact, "number": number, "doc_id": did,
             "doc_currency": doc.get("CurrencyCode") or full.get("currency") or "", "doc_amount": float(p.get("Amount") or 0),
             "doc_date": full.get("date") or "", "doc_total": full.get("total"),
             "reference": p.get("Reference") or "", "updated": _iso_ts(p.get("UpdatedDateUTC"))}
        r["words"] = sorted(words(contact, number, r["reference"], full.get("reference", ""), full.get("text", "")))
        _people_of(r, contact, r["reference"], full.get("reference", ""), full.get("text", ""), number=number)
        out.append(r)
    for t in x.get("bank_transactions", []):
        if t.get("Status") != "AUTHORISED":
            continue
        acct = banks.get((t.get("BankAccount") or {}).get("AccountID"))
        if not acct:
            continue
        typ = t.get("Type") or ""
        total = float(t.get("Total") or 0)
        codes = []
        for li in t.get("LineItems") or []:
            if li.get("AccountCode") and li["AccountCode"] not in codes:
                codes.append(li["AccountCode"])
        contact = " ".join(((t.get("Contact") or {}).get("Name") or "").split())
        r = {"rid": t["BankTransactionID"], "kind": KIND.get(typ, "bank transaction"), "xero": acct["Name"],
             "xero_id": acct["AccountID"], "currency": t.get("CurrencyCode") or acct.get("CurrencyCode") or x["org"]["BaseCurrency"],
             "date": br._xdate(t.get("Date")), "amount": -abs(total) if typ.startswith("SPEND") else abs(total),
             "reconciled": bool(t.get("IsReconciled")), "batch": "", "contact": contact, "number": "",
             "doc_id": "", "doc_currency": "", "doc_amount": 0.0, "doc_date": "", "doc_total": None,
             "reference": t.get("Reference") or "", "updated": _iso_ts(t.get("UpdatedDateUTC")),
             "accounts": [f"{names.get(c_, c_)} ({c_})" for c_ in codes]}
        r["words"] = sorted(words(contact, r["reference"], _doc_text(t.get("LineItems"))))
        _people_of(r, contact, r["reference"], _doc_text(t.get("LineItems")))
        out.append(r)
    return out, {a["Name"]: a for a in banks.values()}


# --------------------------------------------------------------------- FX

class Rates:
    """Median rates per currency pair over the window, from the documents'
    own CurrencyRate in every entity (foreign units per unit of base)."""

    def __init__(self, docs: list[dict], lo: str):
        self.samples: dict[tuple, list[float]] = defaultdict(list)
        for d in docs:
            if d["currency"] == d["base"] or d["rate"] in (0, 1) or d["date"] < lo:
                continue
            self.samples[(d["base"], d["currency"])].append(d["rate"])
            self.samples[(d["currency"], d["base"])].append(1 / d["rate"])
        self._cache: dict = {}

    def rate(self, a: str, b: str) -> float | None:
        """Units of b one unit of a buys."""
        if a == b:
            return 1.0
        if (a, b) in self._cache:
            return self._cache[(a, b)]
        r = None
        if len(self.samples.get((a, b), [])) >= 2:
            r = statistics.median(self.samples[(a, b)])
        else:
            for via in ("USD", "GBP", "EUR"):
                if via in (a, b):
                    continue
                if len(self.samples.get((a, via), [])) >= 2 and len(self.samples.get((via, b), [])) >= 2:
                    r = statistics.median(self.samples[(a, via)]) * statistics.median(self.samples[(via, b)])
                    break
        self._cache[(a, b)] = r
        return r


def convert_options(amount: float, ccy: str, to: str, doc: dict | None, rates: Rates) -> list[float]:
    """The document amount in `to`: at the document's own rate and at the median."""
    out = []
    if doc and doc.get("rate") and doc["currency"] != doc["base"]:
        in_base = amount / doc["rate"]
        r = rates.rate(doc["base"], to)
        if r:
            out.append(in_base * r)
    r = rates.rate(ccy, to)
    if r:
        out.append(amount * r)
    return out


def fit(m: dict, ccy: str, amount: float, rates: Rates, doc: dict | None = None) -> str | None:
    """'exact' (same currency to the cent, or the card's own charge in that
    currency to the cent), 'converted' (another currency within the tolerance),
    or None."""
    if ccy == m["currency"] and _c(m["amount"]) == _c(amount):
        return "exact"
    if m.get("orig_currency") == ccy and m.get("orig_amount") is not None and _c(m["orig_amount"]) == _c(amount):
        return "exact"
    if ccy == m["currency"]:
        return None
    for v in convert_options(amount, ccy, m["currency"], doc, rates):
        if v and abs(abs(m["amount"]) - v) <= FX_TOLERANCE * v:
            return "converted"
    return None


# ----------------------------------------------------------- the paste

PASTE_DATE = re.compile(r"^(?:(\d{1,2}) ([A-Z][a-z]{2}) (\d{4})|([A-Z][a-z]{2}) (\d{1,2}), (\d{4}))$")
NUMBER = re.compile(r"^\(?-?[\d,]+\.\d{2}\)?$")
BALANCES = re.compile(r"(\(?-?[\d,]+\.\d{2}\)?)\s*(?:[A-Z]{3})?\s*Statement Balance\s*(\(?-?[\d,]+\.\d{2}\)?)\s*(?:[A-Z]{3})?\s*Balance in Xero")


def _num(s: str) -> float:
    s = s.strip()
    neg = s.startswith("(") or s.startswith("-")
    v = float(s.strip("()-").replace(",", ""))
    return -v if neg else v


def parse_paste(text: str) -> dict:
    """A pasted Xero reconcile screen -> balances and every statement line on it."""
    lines = [l.strip() for l in text.splitlines()]
    out = {"statement_balance": None, "xero_balance": None, "lines": [], "count_shown": None}
    m = BALANCES.search(text.replace("\n", " "))
    if m:
        out["statement_balance"], out["xero_balance"] = _num(m.group(1)), _num(m.group(2))
    m = re.search(r"Reconcile \((\d+)\)", text)
    if m:
        out["count_shown"] = int(m.group(1))
    starts = [i for i, l in enumerate(lines) if PASTE_DATE.match(l)]
    for n, i in enumerate(starts):
        block = lines[i:starts[n + 1] if n + 1 < len(starts) else len(lines)]
        g = PASTE_DATE.match(block[0]).groups()
        day = (datetime.strptime(f"{g[0]} {g[1]} {g[2]}", "%d %b %Y") if g[0]
               else datetime.strptime(f"{g[4]} {g[3]} {g[5]}", "%d %b %Y")).date().isoformat()
        payee = next((l for l in block[1:] if l), "")
        direction = amount = None
        for j, l in enumerate(block):
            if l in ("Spent", "Received") and j + 1 < len(block) and NUMBER.match(block[j + 1]):
                direction, amount = ("spent" if l == "Spent" else "received"), _num(block[j + 1])
                break
        if amount is None:
            raise ValueError(f"no Spent or Received amount for the {day} line ({payee})")
        staged = ""
        if "Who" in block and "What" in block:
            w, k = block.index("Who"), block.index("What")
            staged = " / ".join(filter(None, (block[w + 1] if w + 1 < len(block) else "",
                                              block[k + 1] if k + 1 < len(block) else "")))
        out["lines"].append({"date": day, "payee": payee, "direction": direction, "amount": round(abs(amount), 2),
                             "staged": staged})
    if out["count_shown"] is not None and out["count_shown"] != len(out["lines"]):
        raise ValueError(f"the screen says Reconcile ({out['count_shown']}) but {len(out['lines'])} lines were "
                         f"read: paste the whole list, every page")
    return out

# ----------------------------------------------------------- the paste (cont.)

def _resolve_account(key: str, name: str) -> tuple[str, config.BankAccount]:
    """A configured bank account from what a person calls it: its name here, its
    label, statement name, Xero account name or code."""
    n = _norm(name)
    banks = config.entity(key).bank_accounts

    def names(b: config.BankAccount) -> set[str]:
        return {_norm(x) for x in (bank_ident(b), b.label, b.statement_name, b.xero_account,
                                   b.xero_account_code, f"{b.label} {b.currency}") if x}
    hits = [b for b in banks if n in names(b)]
    if not hits:
        hits = [b for b in banks if any(n in x for x in names(b))]
    if len(hits) == 1:
        return bank_ident(hits[0]), hits[0]
    sys.exit(f"{key}: account {name!r} is not one of {', '.join(bank_ident(b) for b in banks) or '(no bank accounts)'}")


def load_confirmed(key: str) -> dict:
    path = OUT / "confirmed" / f"{key}.json"
    return json.loads(path.read_text()).get("accounts", {}) if path.exists() else {}


def cmd_confirm(key: str, account: str, paste: str, at: str | None) -> int:
    from accounting_agent import readonly
    if readonly.active() and not os.environ.get("BRC_OUT_DIR"):
        readonly.refuse("saving a confirmed reconcile screen")
    try:
        key = config.entity(key).slug
    except config.ConfigError as exc:
        sys.exit(str(exc))
    if key not in ENTITIES:
        sys.exit(f"{key} is not read in full; one of {', '.join(ENTITIES)}")
    lab, bank = _resolve_account(key, account)
    parsed = parse_paste(Path(paste).read_text())
    at_iso = at or _now()
    datetime.fromisoformat(at_iso.replace("Z", "+00:00"))
    path = OUT / "confirmed" / f"{key}.json"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    data = json.loads(path.read_text()) if path.exists() else {"accounts": {}}
    xero = bank.xero_account or bank.xero_account_code
    data["accounts"][lab] = {"at": at_iso, "xero_account": xero, "saved": _now(),
                             "statement_balance": parsed["statement_balance"], "xero_balance": parsed["xero_balance"],
                             "lines": parsed["lines"], "source_file": str(paste)}
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n")
    path.chmod(0o600)
    print(f"{SHORT[key]} · {lab} · {len(parsed['lines'])} open lines as at {at_iso}"
          + (f" · statement {parsed['statement_balance']:,.2f}, Xero {parsed['xero_balance']:,.2f}"
             if parsed["statement_balance"] is not None else ""))
    for l in parsed["lines"]:
        print(f"  {l['date']} | {l['payee']} | {l['direction']} | {l['amount']:,.2f}"
              + (f" | staged {l['staged']}" if l["staged"] else ""))
    print(f"saved {path}")
    return 0


# ---------------------------------------------------------- classification

def _units(recs: list[dict]) -> list[dict]:
    """What one statement line can be matched to: one record, one batch
    payment, or several records of one day to one contact together."""
    units, batches, loose = [], defaultdict(list), []
    for r in recs:
        (batches[r["batch"]] if r["batch"] else loose).append(r)
    for b, rs in batches.items():
        units.append({"records": rs, "batch": b})
    groups = defaultdict(list)
    for r in loose:
        units.append({"records": [r], "batch": ""})
        groups[(r["date"], r["amount"] < 0, r["contact"].lower())].append(r)
    for (day, out_, contact), rs in groups.items():
        if len(rs) >= 2 and contact:
            units.append({"records": rs, "batch": ""})
            if len(rs) <= 6:
                for k in range(2, len(rs)):
                    for combo in combinations(rs, k):
                        units.append({"records": list(combo), "batch": ""})
    for u in units:
        rs = u["records"]
        u["amount"] = round(sum(r["amount"] for r in rs), 2)
        u["date"] = min(r["date"] for r in rs)
        u["words"] = sorted(set().union(*(set(r["words"]) for r in rs)))
        u["rids"] = [r["rid"] for r in rs]
        u["people"] = sorted(set().union(*(set(r.get("people", ())) for r in rs)))
        u["ntoks"] = sorted(set().union(*(set(r.get("ntoks", ())) for r in rs)))
    return units


def _join(parts: list[dict]) -> dict:
    """Several records or atoms as one thing a line is matched to."""
    rs = [r for p in parts for r in (p.get("records") or [p])]
    batch = parts[0].get("batch", "") if len(parts) == 1 else ""
    return {"records": rs, "batch": batch, "amount": round(sum(r["amount"] for r in rs), 2),
            "date": min(r["date"] for r in rs), "words": sorted(set().union(*(set(r["words"]) for r in rs))),
            "rids": [r["rid"] for r in rs],
            "people": sorted(set().union(*(set(r.get("people", ())) for r in rs))),
            "ntoks": sorted(set().union(*(set(r.get("ntoks", ())) for r in rs)))}


def _atoms(recs: list[dict]) -> list[dict]:
    """A batch payment is one thing on the statement; every other record is one."""
    batches, out = defaultdict(list), []
    for r in recs:
        if r["batch"]:
            batches[r["batch"]].append(r)
        else:
            out.append(_join([r]))
    out += [{**_join(rs), "batch": b} for b, rs in batches.items()]
    return out


def _subset(atoms: list[dict], target: int) -> list[dict] | None:
    """Atoms whose amounts sum to `target` cents exactly (bitset subset sum)."""
    w = [_c(a["amount"]) for a in atoms]
    reach = [1]
    for x in w:
        reach.append(reach[-1] | (reach[-1] << x))
    if not (reach[-1] >> target) & 1:
        return None
    pick, t = [], target
    for i in range(len(w), 0, -1):
        if (reach[i - 1] >> t) & 1:
            continue
        pick.append(atoms[i - 1])
        t -= w[i - 1]
    return pick if t == 0 else None


def _is_fee(r: dict) -> bool:
    return r["kind"] != "payment" and (bool(FEE_WORDS & set(r["words"]))
                                       or any("fee" in a.lower() for a in r.get("accounts", [])))


def _combo_ok(m: dict, combo) -> bool:
    """Two or three records may make one line only as a payment plus the bank
    fees on it, or as records to one contact the line names."""
    rs = [r for a in combo for r in (a.get("records") or [a])]
    named = agree(set(m["words"]), set().union(*(set(r["words"]) for r in rs)))
    contacts = {r["contact"].lower() for r in rs}
    if named and len(contacts) == 1 and "" not in contacts:
        return True
    main = [r for r in rs if not _is_fee(r)]
    return len(main) == 1 and agree(set(m["words"]), set(main[0]["words"]))


def _shared_and_named(m: dict, pick) -> bool:
    """A line matched by sum to several records: they share one reference or
    one contact, and the line names it."""
    rs = [r for a in pick for r in (a.get("records") or [a])]
    refs = {(r.get("reference") or "").strip().lower() for r in rs}
    contacts = {r["contact"].lower() for r in rs}
    if len(refs) == 1 and "" not in refs and agree(set(m["words"]), words(next(iter(refs)))):
        return True
    return len(contacts) == 1 and "" not in contacts and agree(set(m["words"]), words(next(iter(contacts))))


def describe_unit(u: dict) -> str:
    rs = u["records"]
    r = rs[0]
    if r["kind"] == "payment":
        nums = ", ".join(x["number"] or x["contact"] for x in rs)
        what = "batch payment" if u["batch"] else ("payments" if len(rs) > 1 else "payment")
        docs = " + ".join(f"{x['doc_currency']} {x['doc_amount']:,.2f}" for x in rs if x["doc_currency"])
        return f"{what} on {nums} ({r['contact']}{', ' + docs if docs else ''}), {_d(u['date'])}"
    acc = ", ".join(r.get("accounts") or [])
    return f"{r['kind']} to {r['contact'] or 'no contact'}{' · ' + acc if acc else ''}{' · ' + r['reference'] if r['reference'] else ''}, {_d(u['date'])}"


def describe_doc(ds: list[dict]) -> str:
    d = ds[0]
    kind = "credit note" if d.get("credit_note") else ("bill" if d["type"] == "ACCPAY" else "invoice")
    if len(ds) == 1:
        return f"{kind} {d['contact']} · {_d(d['date'])} · {_money(d['currency'], d['due'])} · {d['number'] or d['reference']}"
    return (f"{len(ds)} {kind}s together {d['contact']} · {_money(d['currency'], sum(x['due'] for x in ds))} · "
            + ", ".join(x["number"] or x["reference"] for x in ds))


# ------------------------------------------------- what an open line goes to

def _line_text(m: dict) -> str:
    return " ".join(filter(None, (m.get("payee"), m.get("alt"), m.get("reference"))))


def _num_hit(m: dict, refs: list[str], text: str = "") -> bool:
    """A reference, invoice or order number on the bank line that is on the
    record: the record's number inside the line, or a numbered token of the
    line among the record's own words."""
    line = _line_text(m).lower()
    flat = re.sub(r"[^a-z0-9]", "", line)
    ltoks = set(re.findall(r"[a-z0-9]+", line))
    for r in refs:
        n = re.sub(r"[^a-z0-9]", "", (r or "").lower())
        if not re.search(r"\d", n) or re.fullmatch(r"(19|20)\d\d", n):
            continue
        if (len(n) >= 5 and n in flat) or (len(n) >= 4 and n in ltoks):
            return True
    mine = {t for t in ltoks if len(t) >= 5 and re.search(r"\d", t) and re.search(r"[a-z]", t) or
            (len(t) >= 6 and t.isdigit())}
    theirs = set(re.findall(r"[a-z0-9]+", " ".join(list(refs) + [text]).lower()))
    return bool(mine & theirs)


DOC_KIND = {"ACCPAY": "bill", "ACCREC": "invoice", "ACCPAYCREDIT": "credit note", "ACCRECCREDIT": "credit note"}


def _cand(m: dict, kind: str, target, f: str) -> dict:
    """One thing an open line may go to, with what agrees between the two:
    kind "unit" (unreconciled records on the account), "docs" (open documents
    of the entity) or "part" (a part payment of one document)."""
    if kind == "unit":
        u = target
        rs = u["records"]
        refs = [x for r in rs for x in (r["number"], r["reference"]) if x]
        c = {"cid": "rec:" + "+".join(sorted(u["rids"])), "res": {"r:" + x for x in u["rids"]},
             "date": u["date"], "gap": _gap(m["date"], u["date"]), "currency": rs[0]["currency"],
             "amount": abs(u["amount"]), "contact": ", ".join(sorted({r["contact"] for r in rs if r["contact"]})),
             "number": ", ".join(r["number"] for r in rs if r["number"]), "refs": refs,
             "words": set(u["words"]), "people": set(u["people"]), "ntoks": set(u["ntoks"]),
             "text": " ".join(filter(None, [rs[0]["kind"], rs[0]["reference"]] + rs[0].get("accounts", []))),
             "transfer": all(r["kind"] == "transfer" for r in rs), "entity": m["entity"],
             "direction": "spent" if u["amount"] < 0 else "received"}
    else:
        ds = target
        c = {"cid": ("part:" if kind == "part" else "doc:") + "+".join(sorted(d["id"] for d in ds)),
             "res": {"d:" + d["id"] for d in ds}, "date": ds[0]["date"],
             "gap": max(_gap(m["date"], d["date"]) for d in ds), "currency": ds[0]["currency"],
             "amount": round(sum(d["due"] for d in ds), 2), "contact": ds[0]["contact"],
             "number": ", ".join(d["number"] or d["reference"] for d in ds if d["number"] or d["reference"]),
             "refs": [x for d in ds for x in (d["number"], d["reference"]) if x],
             "words": set().union(*(set(d["words"]) for d in ds)),
             "people": set().union(*(set(d.get("people", ())) for d in ds)),
             "ntoks": set().union(*(set(d.get("ntoks", ())) for d in ds)),
             "text": " ".join(d["text"] for d in ds), "transfer": False, "entity": ds[0]["entity"],
             "direction": ds[0].get("direction", "")}
    c.update(kind=kind, target=target, fit=f)
    c["named"] = agree(set(m["words"]), c["words"])
    c["who"] = who_agrees(m, c["people"], c["ntoks"])
    c["ref"] = _num_hit(m, c["refs"], c["text"])
    c["shared"] = frozenset(br._shared(set(m["words"]), c["words"]) - br.GENERIC)
    # tier 1 needs the payee's name, who paid or the record's number on the
    # line; a transfer leg names no payee, so its account, amount and a date
    # within 2 days are its whole identity
    c["agrees"] = bool(c["named"] or c["who"] is True or c["ref"]
                       or (c["transfer"] and c["gap"] <= TRANSFER_GAP))
    return c


def _doc_pool(m: dict, docs: list[dict]) -> list[dict]:
    direction = "spent" if m["amount"] < 0 else "received"
    lo, hi = _shift(m["date"], -DOC_BEFORE), _shift(m["date"], DOC_AFTER)
    return [d for d in docs if d["direction"] == direction and lo <= d["date"] <= hi]


def _apply(m: dict, c: dict, by: str, how: str, used_records: set, used_docs: set) -> None:
    """The line goes to this candidate: TO MATCH, high."""
    kind, target = c["kind"], c["target"]
    if kind == "unit":
        used_records.update(target["rids"])
        m["goes_to"] = {"kind": "record", "text": describe_unit(target),
                        "records": [{"kind": r["kind"], "contact": r["contact"], "number": r["number"],
                                     "date": r["date"], "amount": r["amount"], "currency": r["currency"],
                                     "doc_currency": r["doc_currency"], "doc_amount": r["doc_amount"],
                                     "reference": r["reference"], "accounts": r.get("accounts", [])}
                                    for r in target["records"]]}
    elif kind == "part":
        d = target[0]
        used_docs.add(d["id"])
        m["goes_to"] = {"kind": "document", "part": True, "text": "part payment of " + describe_doc(target),
                        "documents": [{k: d[k] for k in ("entity", "type", "number", "reference", "contact",
                                                         "date", "currency", "due", "total")}]}
        m["note"] = (m.get("note") or "") + (f"part payment: {_money(m['currency'], abs(m['amount']))} of "
                                             f"{_money(d['currency'], d['due'])} due")
    else:
        used_docs.update(d["id"] for d in target)
        m["goes_to"] = {"kind": "document", "text": describe_doc(target),
                        "documents": [{k: d[k] for k in ("entity", "type", "number", "reference", "contact",
                                                         "date", "currency", "due", "total")} for d in target]}
    m["confidence"] = "high"
    m["match_by"], m["match_how"] = by, how
    if not m["status"]:
        m["status"] = "TO MATCH"


def _discriminate(m: dict, cs: list[dict]) -> tuple[dict, str] | None:
    """Tier 2: one candidate among several of the same amount, told apart by
    who paid, a reference, merchant detail or a clear date, in that order.
    Each must point to exactly one; a candidate naming another person than the
    cardholder is never picked by a weaker rule."""
    if not cs:
        return None
    if m.get("person") or len(set(_ntok(m.get("who") or ""))) >= 2:
        hits = [c for c in cs if c["who"] is True]
        if len(hits) == 1:
            return hits[0], "who paid"
    ok = [c for c in cs if c["who"] is not False]
    hits = [c for c in cs if c["ref"]]
    if len(hits) == 1 and hits[0] in ok:
        return hits[0], "reference"
    for c in ok:
        if c["shared"] and all(c["shared"] > o["shared"] for o in cs if o is not c):
            return c, "merchant detail" if len(cs) > 1 else "the payee's name"
    near = [c for c in ok if c["gap"] <= DATE_CLEAR and c["agrees"]]
    if len(near) == 1 and all(o["gap"] >= DATE_FAR for o in cs if o is not near[0]):
        return near[0], "date"
    return None


def _settle(lines: list[dict], cands: dict[str, list[dict]], free) -> tuple[list[tuple], dict[str, list[dict]]]:
    """Tiers 1 and 2 over one group of open lines sharing exact candidates.
    -> (pairs (line, candidate, how), the lines left tied with their candidates)"""
    taken: set = set()

    def live(mid: str) -> list[dict]:
        return [c for c in cands.get(mid, []) if free(c) and not (c["res"] & taken)]
    if len(lines) == 1:
        cs = live(lines[0]["id"])
        if len(cs) == 1 and cs[0]["agrees"] and cs[0]["who"] is not False:
            return [(lines[0], cs[0], "unique")], {}
    left = {m["id"]: m for m in lines}
    pairs: list[tuple] = []
    while left:
        props = {}
        for mid, m in left.items():
            hit = _discriminate(m, live(mid))
            if hit:
                props[mid] = hit
        wanted: dict[str, set] = defaultdict(set)
        for mid, (c, _) in props.items():
            for r in c["res"]:
                wanted[r].add(mid)
        moved = False
        for mid, (c, how) in props.items():
            # one candidate two lines point to goes to neither
            if any(len(wanted[r]) > 1 for r in c["res"]) or c["res"] & taken:
                continue
            pairs.append((left[mid], c, how))
            taken |= c["res"]
            moved = True
        for m, _, _ in pairs:
            left.pop(m["id"], None)
        if not moved:
            break
    rest = {mid: cs for mid in left if (cs := live(mid))}
    if pairs and len(rest) == 1:
        mid, cs = next(iter(rest.items()))
        if len(cs) == 1 and cs[0]["agrees"] and cs[0]["who"] is not False:
            pairs.append((left[mid], cs[0], "the only one left"))
            rest = {}
    return pairs, rest


def _dev(m: dict, ccy: str, amount: float, rates: "Rates", doc: dict | None = None) -> float | None:
    """How far a document's amount is from the line, as a share of the document."""
    if not amount:
        return None
    if ccy == m["currency"]:
        return abs(abs(m["amount"]) - abs(amount)) / abs(amount)
    if m.get("orig_currency") == ccy and m.get("orig_amount") is not None:
        return abs(abs(m["orig_amount"]) - abs(amount)) / abs(amount)
    devs = [abs(abs(m["amount"]) - v) / v for v in convert_options(abs(amount), ccy, m["currency"], doc, rates) if v]
    return min(devs) if devs else None


def _narrow(m: dict, mine: list[dict], units: list[dict], rates: "Rates", free, first: list[dict]) -> list[dict]:
    """Tier 3's candidates, gathered narrowly: the same entity and direction, a
    document dated 45 days before the line to 7 after, and either the amount
    within 5% or the name or who paid agreeing within 7 days. Tied and part
    payment candidates first, then nearest first, 6 at most."""
    rest = []
    for d in _doc_pool(m, mine):
        c = _cand(m, "docs", [d], "near")
        if not free(c):
            continue
        dev = _dev(m, d["currency"], d["due"], rates, d)
        if (dev is not None and dev <= NARROW_AMOUNT) or ((c["named"] or c["who"] is True) and c["gap"] <= NARROW_NAMED_DAYS):
            c["dev"] = dev
            rest.append(c)
    lo, hi = _shift(m["date"], -DOC_BEFORE), _shift(m["date"], DOC_AFTER)
    for u in units:
        if len(u["records"]) != 1 or (u["amount"] < 0) != (m["amount"] < 0) or not (lo <= u["date"] <= hi):
            continue
        c = _cand(m, "unit", u, "near")
        if not free(c):
            continue
        dev = abs(abs(m["amount"]) - abs(u["amount"])) / abs(u["amount"]) if u["amount"] else None
        if (dev is not None and dev <= NARROW_AMOUNT) or ((c["named"] or c["who"] is True) and c["gap"] <= NARROW_NAMED_DAYS):
            c["dev"] = dev
            rest.append(c)
    rest.sort(key=lambda c: (c["gap"], 9 if c["dev"] is None else c["dev"]))
    out, seen = [], set()
    for c in first:
        c.setdefault("dev", 0.0 if c["fit"] == "exact" else _dev(m, c["currency"], c["amount"], rates))
    for c in first + rest:
        if c["cid"] not in seen:
            seen.add(c["cid"])
            out.append(c)
    return out[:LINE_MATCH_CANDIDATES]


def classify(key: str, moves: list[dict], recs: list[dict], open_docs: dict[str, list[dict]],
             docs_by_id: dict[str, dict], rates: Rates, confirmed: dict, since: str, lo: str) -> dict:
    """-> {movements: [...], suspects: [...], disagreements: [...], unreconciled_records: [...]}"""
    for m in moves:
        m["words"] = sorted(words(m["payee"], m.get("alt", ""), m.get("reference", "")))
        m["person"] = who_person(m.get("who") or "")
        m.update(state="OPEN", status="", source="inferred", matched_to=None, goes_to=None,
                 confidence="", siblings=[], note="",
                 origin=m.get("source"),
                 in_window=m["date"] >= since or m.get("source") in ("paste", "native feed", "carried"))
    by_acct = defaultdict(list)
    for m in moves:
        by_acct[m["xero"]].append(m)
    recs_by_acct = defaultdict(list)
    for r in recs:
        recs_by_acct[r["xero"]].append(r)
    alloc: dict[str, dict] = {}            # movement id -> reconciled unit
    used_records: set[str] = set()
    ambiguous: dict[str, set] = defaultdict(set)

    # a. reconciled: one-to-one, nearest date, then the payee's name
    for acct, ms in by_acct.items():
        taken: set[str] = set()

        def take(m, u, key):
            taken.add(m["id"])
            used_records.update(u["rids"])
            alloc[m["id"]] = u
            u["_key"], u["_move"] = key, m

        def free_recs():
            return [r for r in recs_by_acct.get(acct, []) if r["reconciled"] and r["rid"] not in used_records]

        # a1. one record, one batch payment, or one day's records to one
        # contact, of the line's amount to the cent; within 5 days, or 10
        # where the payee's name agrees
        units = _units(free_recs())
        by_amt = defaultdict(list)
        for u in units:
            by_amt[(u["amount"] < 0, _c(u["amount"]))].append(u)
        cands = []
        for m in ms:
            for u in by_amt.get((m["amount"] < 0, _c(m["amount"])), []):
                g = _gap(m["date"], u["date"])
                # the payee's name, or the cardholder being the person the
                # record's document names
                miss = 0 if (agree(set(m["words"]), set(u["words"]))
                             or who_agrees(m, u["people"], u["ntoks"]) is True) else 1
                if g <= RECONCILED_GAP or (not miss and g <= RECONCILED_GAP_NAMED):
                    cands.append((g, miss, len(u["rids"]), m["id"], m, u))
        cands.sort(key=lambda c_: c_[:4])
        for g, miss, size, _, m, u in cands:
            if m["id"] not in taken and not any(r in used_records for r in u["rids"]):
                take(m, u, (g, miss))

        # a2. two or three records of any contacts within 5 days (a payment
        # plus the bank-fee spend money added in the reconcile screen); a
        # batch payment counts as one record
        atoms = _atoms(free_recs())
        combo_cands = []
        for m in ms:
            if m["id"] in taken:
                continue
            near = sorted((a for a in atoms if (a["amount"] < 0) == (m["amount"] < 0)
                           and _gap(m["date"], a["date"]) <= RECONCILED_GAP),
                          key=lambda a: _gap(m["date"], a["date"]))[:20]
            for k in (2, 3):
                for combo in combinations(near, k):
                    if _c(sum(a["amount"] for a in combo)) == _c(m["amount"]) and _combo_ok(m, combo):
                        g = max(_gap(m["date"], a["date"]) for a in combo)
                        combo_cands.append((g, k, m["id"], m, _join(combo)))
        combo_cands.sort(key=lambda c_: c_[:3])
        for g, k, _, m, u in combo_cands:
            if m["id"] not in taken and not any(r in used_records for r in u["rids"]):
                take(m, u, (g, 9))

        # No approximate pass: a record a few per cent off a line is not a
        # match. One currency agrees to the cent or the line stays open, and
        # the tie-out shows the difference to the admin

        # a4. one line matched to many records of one or two days (a payroll
        # collection split over the people it covers): an exact subset sum of
        # the records left within 2 days, for lines of 50 or more
        for m in sorted(ms, key=lambda m_: -abs(m_["amount"])):
            if m["id"] in taken or abs(m["amount"]) < 50:
                continue
            pool = [a for a in _atoms(free_recs()) if (a["amount"] < 0) == (m["amount"] < 0)
                    and _gap(m["date"], a["date"]) <= 2 and abs(a["amount"]) <= abs(m["amount"])]
            if len(pool) < 2 or len(pool) > 24:
                continue
            pick = _subset(pool, _c(m["amount"]))
            if pick and len(pick) >= 2 and _shared_and_named(m, pick):
                take(m, _join(pick), (2, 9))
                m["note"] = f"matched by sum to {len(pick)} records of the same days"

        # Nor a far pass: a record further than the named window from a line
        # is not its match, whatever the amount and the name

        # b. a leftover that would have suited the same record just as well
        for g, miss, size, _, m, u in cands:
            if m["id"] in taken or u.get("_move") is None or u["_move"]["id"] == m["id"]:
                continue
            if (g, miss) == u["_key"]:
                ambiguous[m["id"]].add(u["_move"]["id"])
                ambiguous[u["_move"]["id"]].add(m["id"])
    move_by_id = {m["id"]: m for m in moves}
    for m in moves:
        u = alloc.get(m["id"])
        if u:
            m["state"], m["status"] = "RECONCILED", "RECONCILED"
            m["matched_to"] = describe_unit(u)
            m["_unit"] = u

    for mid, sibs in ambiguous.items():
        m = move_by_id[mid]
        m["siblings"] = sorted(sibs)
        if m["state"] == "OPEN":
            m["status"] = "AMBIGUOUS"

    # e. a pasted reconcile screen decides every line up to its moment
    disagreements = []
    for lab, conf in confirmed.items():
        xname = BANK_MAP[key].get(lab)
        at = conf["at"]
        at_day = at[:10]
        lag_floor = _shift(at_day, -FEED_LAG_DAYS)
        ms = [m for m in by_acct.get(xname, []) if m["date"] <= at_day]
        free = {m["id"] for m in ms}
        for pl in conf["lines"]:
            best = None
            for m in ms:
                if m["id"] not in free or (m["amount"] < 0) != (pl["direction"] == "spent"):
                    continue
                if _c(m["amount"]) != _c(pl["amount"]) or _gap(m["date"], pl["date"]) > 3:
                    continue
                if best is None or _gap(m["date"], pl["date"]) < _gap(best["date"], pl["date"]):
                    best = m
            if best is None:
                synth = {"id": f"paste:{lab}:{pl['date']}:{pl['amount']}:{pl['direction']}", "entity": key,
                         "bank": lab, "xero": xname, "date": pl["date"], "currency": None,
                         "amount": -pl["amount"] if pl["direction"] == "spent" else pl["amount"],
                         "payee": pl["payee"], "alt": "", "who": "", "reference": "", "orig_amount": None,
                         "orig_currency": "", "source": "paste", "settled_at": "", "words": sorted(words(pl["payee"])),
                         "state": "OPEN", "status": "", "matched_to": None, "goes_to": None, "confidence": "",
                         "siblings": [], "note": "on the pasted screen, not in the bank data read",
                         "origin": "paste", "in_window": True}
                moves.append(synth)
                by_acct[xname].append(synth)
                move_by_id[synth["id"]] = synth
                disagreements.append({"account": xname, "kind": "paste only",
                                      "line": f"{pl['date']} {pl['payee']} {pl['direction']} {pl['amount']:,.2f}"})
                synth["source"] = "confirmed"
                continue
            free.discard(best["id"])
            if best["state"] == "RECONCILED":
                u = best.get("_unit") or {}
                stamps = [r.get("updated") or "" for r in u.get("records", [])]
                if stamps and all(s[:16] > at[:16].replace("Z", "") for s in stamps):
                    # every record behind the match was saved after the paste:
                    # somebody reconciled the line since, and the inference stands
                    disagreements.append({"account": xname, "kind": "reconciled after the paste",
                                          "line": _mline(best), "was": best["matched_to"]})
                    continue
                disagreements.append({"account": xname, "kind": "inferred reconciled, paste open",
                                      "line": _mline(best), "was": best["matched_to"]})
                u = best.pop("_unit", None)
                if u:
                    used_records.difference_update(u["rids"])
                best["matched_to"] = None
            best["state"], best["status"], best["source"] = "OPEN", "", "confirmed"
            best["in_window"] = True
            if pl.get("staged"):
                best["note"] = f"staged in Xero: {pl['staged']}"
        for m in ms:
            if m["id"] not in free:
                continue
            settled = _iso_ts(m.get("settled_at") or "")
            after = settled[:16] > at[:16] if settled else m["date"] >= at_day
            if after or m["date"] > lag_floor and m["state"] == "OPEN":
                continue                      # may not have reached Xero by the paste: inference stands
            if m["state"] == "OPEN" and m["in_window"]:
                disagreements.append({"account": xname, "kind": "inferred open, paste reconciled", "line": _mline(m)})
            m["state"], m["status"], m["source"] = "RECONCILED", "RECONCILED", "confirmed"
            m["matched_to"] = m["matched_to"] or "reconciled (not on the pasted screen)"
    for m in moves:
        if m["id"] in ambiguous and m["source"] == "confirmed" and m["state"] == "OPEN":
            m["status"] = ""

    # c. what each open line goes to, in three tiers.
    # Tier 1, code: the exact amount, one line and one candidate, and the
    # payee's name, who paid or the bill's number on the line agreeing.
    # Tier 2, code: a repeated amount separated by who paid, a reference,
    # merchant detail or a clear date, each pointing to exactly one.
    # Tier 3: what is left goes to the bank-line-match agent (run_check)
    open_moves = [m for m in moves if m["state"] == "OPEN" and m["in_window"]]
    mine = open_docs.get(key, [])
    used_docs: set[str] = set()
    exact: dict[str, list[dict]] = defaultdict(list)
    parts: dict[str, list[dict]] = defaultdict(list)
    units_of: dict[str, list[dict]] = {}
    for acct, ms in by_acct.items():
        units = _units([r for r in recs_by_acct.get(acct, []) if not r["reconciled"] and r["rid"] not in used_records])
        units_of[acct] = units
        for m in ms:
            if m["state"] != "OPEN" or not m["in_window"]:
                continue
            for u in units:
                if (u["amount"] < 0) != (m["amount"] < 0) or _c(u["amount"]) != _c(m["amount"]):
                    continue
                c = _cand(m, "unit", u, "exact")
                if c["gap"] <= RECORD_GAP or (c["agrees"] and c["gap"] <= RECORD_GAP_NAMED):
                    exact[m["id"]].append(c)
    for m in open_moves:
        pool = _doc_pool(m, mine)
        for d in pool:
            f = fit(m, d["currency"], d["due"], rates, d)
            if not f:
                continue
            c = _cand(m, "docs", [d], f)
            # converted, it needs the payee's or the person's name as well
            if f == "exact" or c["named"] or c["who"] is True:
                exact[m["id"]].append(c)
        # a part payment: one currency, less than what is due, the payee
        # named, and the line itself saying so (its number, or a word like
        # deposit: the word alone is left to tier 3)
        mw = set(m["words"])
        for d in pool:
            if d["currency"] != m["currency"] or not (0 < abs(m["amount"]) < d["due"] - 0.005):
                continue
            if not agree(mw, set(d["words"])):
                continue
            c = _cand(m, "part", [d], "part")
            if c["ref"] or (PART_WORDS & mw):
                parts[m["id"]].append(c)
        # several documents of one supplier paid by one line
        groups = defaultdict(list)
        for d in pool:
            if agree(mw, set(d["words"])):
                groups[(d["contact"].lower(), d["currency"])].append(d)
        for (_, ccy), ds in groups.items():
            if len(ds) < 2 or len(ds) > 10:
                continue
            for k in range(2, min(4, len(ds)) + 1):
                for combo in combinations(ds, k):
                    f = fit(m, ccy, round(sum(x["due"] for x in combo), 2), rates, combo[0])
                    if f:
                        exact[m["id"]].append(_cand(m, "docs", list(combo), f))

    def free(c: dict) -> bool:
        return not any((x[2:] in used_records) if x[0] == "r" else (x[2:] in used_docs) for x in c["res"])

    def take(m: dict, c: dict, by: str, how: str) -> None:
        _apply(m, c, by, how, used_records, used_docs)

    # lines that share a candidate are settled together
    parent: dict = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for mid, cs in exact.items():
        for c in cs:
            for r in c["res"]:
                parent[find(("m", mid))] = find(("r", r))
    comps: dict = defaultdict(list)
    for m in open_moves:
        if exact.get(m["id"]):
            comps[find(("m", m["id"]))].append(m)
    tied: dict[str, list[dict]] = {}
    for group in comps.values():
        pairs, left = _settle(group, exact, free)
        for m, c, how in pairs:
            take(m, c, "code", how)
        tied.update(left)
    for m in open_moves:
        if m["goes_to"]:
            continue
        hits = [c for c in parts.get(m["id"], []) if c["ref"] and free(c)]
        if len(hits) == 1 and not tied.get(m["id"]):
            take(m, hits[0], "code", "part payment, its number on the line")

    # d. cross-entity: a document in another entity is bill-payments territory
    for m in open_moves:
        if m["goes_to"]:
            continue
        direction = "spent" if m["amount"] < 0 else "received"
        lo, hi = _shift(m["date"], -DOC_BEFORE), _shift(m["date"], DOC_AFTER)
        best = None
        for other, ds in open_docs.items():
            if other == key:
                continue
            for d in ds:
                if d["direction"] != direction or not (lo <= d["date"] <= hi) or d["id"] in used_docs:
                    continue
                if not agree(set(m["words"]), set(d["words"])):
                    continue
                if fit(m, d["currency"], d["due"], rates, d):
                    g = _gap(m["date"], d["date"])
                    if best is None or g < best[0]:
                        best = (g, d)
        if best:
            used_docs.add(best[1]["id"])
            m["goes_to"] = {"kind": "interco", "text": f"{SHORT[best[1]['entity']]} {describe_doc([best[1]])}",
                            "documents": [{k: best[1][k] for k in ("entity", "type", "number", "reference", "contact",
                                                                   "date", "currency", "due", "total")}]}
            m["confidence"] = "high"
            if not m["status"]:
                m["status"] = "INTERCO"

    # d. suspect wrong matches
    suspects = []
    move_of_record = {}
    for m in moves:
        u = m.get("_unit")
        if u:
            for r in u["records"]:
                move_of_record[r["rid"]] = m
    wrongly_paid, rate_notes = [], []
    by_line: dict[str, dict] = {}
    for r in recs:
        if r["kind"] != "payment" or not r["reconciled"] or r["date"] < since or not r["doc_currency"]:
            continue
        why, dev = "", 0.0
        if r["doc_currency"] == r["currency"]:
            if _c(r["doc_amount"]) != _c(r["amount"]):
                why, dev = (f"bank amount {_money(r['currency'], r['amount'])} against a "
                            f"{_money(r['doc_currency'], r['doc_amount'])} payment"), 1.0
        else:
            med = rates.rate(r["currency"], r["doc_currency"])
            if med and abs(r["amount"]) > 0:
                dev = abs(r["doc_amount"]) / abs(r["amount"]) / med - 1
                if abs(dev) > WRONG_FX:
                    why = (f"{_money(r['doc_currency'], r['doc_amount'])} paid with {_money(r['currency'], r['amount'])}, "
                           f"a rate {dev:+.0%} off the window's median")
        if not why:
            continue
        m = move_of_record.get(r["rid"])
        record = f"payment on {r['number'] or r['contact']} ({r['contact']}), {_d(r['date'])}"
        if not m or (abs(dev) <= WRONG_FX_ANY and agree(set(m["words"]), set(r["words"]))):
            # the line names the payment's own payee and the rate is within
            # 25%: the right bill at a rate somebody typed, a note for the admin
            rate_notes.append({"xero": r["xero"], "line": _mline(m) if m else "no line found",
                               "record": record, "why": why})
            continue
        wrongly_paid.append((r, m))
        s_ = by_line.get(m["id"])
        if s_:
            s_["records"].append(record)
            s_["why"] += f"; {why}"
            continue
        by_line[m["id"]] = s_ = {"kind": "payment does not fit its bill", "why": why, "xero": r["xero"],
                                 "line": _mline(m), "line_id": m["id"], "records": [record], "record": record,
                                 "bill": r["number"], "contact": r["contact"], "doc_currency": r["doc_currency"],
                                 "doc_amount": r["doc_amount"], "entity": key, "date": m["date"],
                                 "amount": m["amount"], "currency": m.get("currency"), "payee": m["payee"]}
        suspects.append(s_)
        m["suspect"] = why
    # an open line whose bill another line's wrong match has taken: one line
    # per bill, the nearest date first
    wp_cands = []
    for m in moves:
        if m["state"] != "OPEN" or m["goes_to"] or not m["in_window"]:
            continue
        for r, other in wrongly_paid:
            d = docs_by_id.get(r["doc_id"]) or {}
            if (r["amount"] < 0) != (m["amount"] < 0) or not d:
                continue
            if not (_shift(m["date"], -DOC_BEFORE) <= d["date"] <= _shift(m["date"], DOC_AFTER)):
                continue
            if fit(m, d["currency"], d["total"], rates, d) and agree(set(m["words"]), set(d["words"])):
                wp_cands.append((_gap(m["date"], d["date"]), m["id"], m, r, other, d))
    wp_cands.sort(key=lambda c_: c_[:2])
    wp_taken = set()
    for g, _, m, r, other, d in wp_cands:
        if m["id"] in wp_taken or d["id"] in wp_taken:
            continue
        wp_taken.update((m["id"], d["id"]))
        m["status"] = "SUSPECT WRONG MATCH"
        m["goes_to"] = {"kind": "wrong match", "text": f"{describe_doc([{**d, 'due': d['total']}])}, marked paid by "
                        f"the {r['xero']} {_mline(other) if other else 'payment'}",
                        "documents": [{"entity": d["entity"], "type": d["type"], "number": d["number"],
                                       "reference": d["reference"], "contact": d["contact"], "date": d["date"],
                                       "currency": d["currency"], "due": d["total"], "total": d["total"]}],
                        "taken_by": {"xero": r["xero"], "line": _mline(other) if other else ""}}
        m["confidence"] = "high"
    # a reconciled line an open bill fits better than the bill it was matched to
    for m in moves:
        u = m.get("_unit")
        if m["state"] != "RECONCILED" or not u:
            continue
        direction = "spent" if m["amount"] < 0 else "received"
        mw = set(m["words"])
        own_fit = False
        for r in u["records"]:
            if r["kind"] == "payment" and r["doc_currency"]:
                d = docs_by_id.get(r["doc_id"])
                late = d and d["date"] > _shift(m["date"], DOC_AFTER)
                if fit(m, r["doc_currency"], r["doc_amount"], rates, d) and not late:
                    own_fit = True
            elif r["kind"] != "payment" and agree(mw, set(r["words"])):
                own_fit = True
        if own_fit or (u["records"][0]["kind"] != "payment" and not u["records"][0]["contact"]):
            continue
        for d in mine:
            if d["direction"] != direction or d["id"] in used_docs:
                continue
            if not (_shift(m["date"], -DOC_BEFORE) <= d["date"] <= _shift(m["date"], DOC_AFTER)):
                continue
            if fit(m, d["currency"], d["due"], rates, d) and agree(mw, set(d["words"])):
                used_docs.add(d["id"])
                why = f"reconciled to the {describe_unit(u)}, which does not fit the line; {describe_doc([d])} does"
                suspects.append({"kind": "line matched to the wrong bill", "why": why, "xero": m["xero"],
                                 "entity": key, "date": m["date"], "amount": m["amount"],
                                 "currency": m.get("currency"), "payee": m["payee"],
                                 "bill": ", ".join(r["number"] for r in u["records"] if r["number"]),
                                 "line": _mline(m), "line_id": m["id"], "record": describe_unit(u),
                                 "likely": describe_doc([d]), "likely_doc": {k: d[k] for k in (
                                     "entity", "number", "contact", "date", "currency", "due")}})
                m["suspect"] = why
                break

    # tier 3: the narrow candidates for every line still without one; the
    # bank-line-match agent is asked in run_check, once for the whole run
    for m in open_moves:
        if m["goes_to"] or m["status"]:
            continue
        first = [c for c in tied.get(m["id"], []) if free(c)]
        first += [c for c in parts.get(m["id"], []) if free(c)]
        cands = _narrow(m, mine, units_of.get(m["xero"], []), rates, free, first)
        if cands:
            m["_t3"] = {"candidates": cands, "tied": len([c for c in first if c["kind"] != "part"]) > 1}

    for m in moves:
        if m["state"] == "OPEN" and not m["status"]:
            m["status"] = "NO DOCUMENT"
        m.pop("_unit", None)
    unrec = [r for r in recs if not r["reconciled"] and r["rid"] not in used_records and r["date"] <= _today()]
    loose = [r for r in recs if r["reconciled"] and r["rid"] not in used_records and r["date"] >= since]
    return {"movements": moves, "suspects": suspects, "disagreements": disagreements,
            "unmatched_records": unrec, "rate_notes": rate_notes, "reconciled_without_line": loose,
            "_used": (used_records, used_docs), "_rates": rates}


def _mline(m: dict | None) -> str:
    if not m:
        return ""
    what = "payment" if m["amount"] < 0 else "receipt"
    ccy = m.get("currency") or ""
    return f"{_d(m['date'])} {ccy} {abs(m['amount']):,.2f} {what} ({m['payee']})".replace("  ", " ")


# ----------------------------------------------------------- the tie-out

def tie_out(key: str, x: dict, bank_balances: dict, moves: list[dict], recs: list[dict],
            banks: dict[str, dict], rates: Rates, confirmed: dict) -> dict[str, dict]:
    """(bank balance) - (Xero balance) must equal (open movements) - (unreconciled
    records), per account. The Xero balance is the Bank Summary's closing
    balance for a base-currency account; for any other currency, the sum of
    every live record on the account, checked against the Bank Summary at the
    median rate."""
    base = x["org"]["BaseCurrency"]
    summary = bank_summary(x.get("bank_summary") or {})
    today = _today()
    out = {}
    for lab, xname in BANK_MAP[key].items():
        acct = banks.get(xname)
        bal = bank_balances.get(lab)
        mine = [m for m in moves if m["xero"] == xname]
        if not acct or bal is None:
            if mine or bal:
                out[xname] = {"bank": lab, "ties": False, "verified": False,
                              "why": "no Xero account" if not acct else "no bank balance"}
            continue
        ccy = acct.get("CurrencyCode") or base
        live = [r for r in recs if r["xero"] == xname and r["date"] <= today]
        records_sum = round(sum(r["amount"] for r in live), 2)
        if ccy == base and xname in summary:
            xero_bal, how = summary[xname], "Bank Summary"
        else:
            xero_bal, how = records_sum, "sum of every record on the account"
            r = rates.rate(ccy, base)
            if xname in summary and r:
                check = records_sum * r
                if abs(check - summary[xname]) > max(0.02 * abs(summary[xname]), 5):
                    how += f" (Bank Summary {base} {summary[xname]:,.2f} does not agree: an opening balance?)"
        open_sum = round(sum(m["amount"] for m in mine if m["state"] == "OPEN" and m["in_window"]), 2)
        unrec_sum = round(sum(r["amount"] for r in live if not r["reconciled"]), 2)
        diff = round((bal["ledger"] - xero_bal) - (open_sum - unrec_sum), 2)
        conf = confirmed.get(lab)
        ties = abs(diff) < 0.005
        out[xname] = {"bank": lab, "currency": ccy, "bank_balance": bal["ledger"], "xero_balance": round(xero_bal, 2),
                      "xero_balance_from": how, "open_sum": open_sum, "unreconciled_sum": unrec_sum,
                      "difference": diff, "ties": ties, "confirmed_at": conf["at"] if conf else "",
                      "verified": ties or bool(conf), "lines_open": sum(1 for m in mine if m["state"] == "OPEN")}
    return out


# --------------------------------------------------------------- the run
def _feed_ident(key: str, heading: str, currency: str) -> str:
    """The configured bank account a native feed heading belongs to, or "" when
    none or more than one does."""
    hits = [b for b in config.entity(key).bank_accounts if _accepts(b, currency, heading, feed=True)]
    return bank_ident(hits[0]) if len(hits) == 1 else ""


def native_feed(key: str) -> list[dict]:
    """The native bank feed's lines, with the bank account each sits on."""
    path = FEED / f"{key}.md"
    out = []
    if not path.exists():
        return out
    head = re.compile(r"^## (.+?)(?: · last checked (\S+))?\s*$")
    line = re.compile(r"^(\d{4}-\d{2}-\d{2}) \| (.*?) \| (spent|received) \| ([A-Z]{3}) ([\d.,]+) \|")
    account = ""
    for raw in path.read_text().splitlines():
        h = head.match(raw)
        if h:
            account = h.group(1)
            continue
        m = line.match(raw)
        if m:
            out.append({"heading": account, "bank": _feed_ident(key, account, m.group(4)), "date": m.group(1),
                        "payee": m.group(2), "direction": m.group(3), "currency": m.group(4),
                        "amount": float(m.group(5).replace(",", "")), "raw": raw,
                        "marker": (re.findall(r"\b(?:rev|mer):[0-9a-f]{8}\b", raw) or [""])[0]})
    return out


def _feed_match(f: dict, moves: list[dict]) -> dict | None:
    best = None
    for m in moves:
        if m["bank"] != f["bank"] or (m["amount"] < 0) != (f["direction"] == "spent"):
            continue
        if _c(m["amount"]) != _c(f["amount"]):
            continue
        if f["marker"] and m["id"].startswith(f["marker"][:4]) and m["id"][4:12] == f["marker"][4:]:
            return m
        if _gap(m["date"], f["date"]) <= 2 and (best is None or _gap(m["date"], f["date"]) < _gap(best["date"], f["date"])):
            best = m
    return best


def window_since(key: str) -> tuple[str, str]:
    """Where the bank is read from for one entity: the oldest
    line the last run left open, less WATERMARK_PAD, or the last run's own day
    when it left nothing open, and never more than WINDOW_CAP_DAYS back. Nothing
    older than the oldest open line can still be open, short of somebody
    undoing a match, and the tie-out catches that (run_check then widens to the
    cap). -> (since, why)"""
    cap = _shift(_today(), -WINDOW_CAP_DAYS)
    path = OUT / "unreconciled.json"
    try:
        prev = json.loads(path.read_text()) if path.exists() else {}
    except ValueError:
        prev = {}
    e = (prev.get("entities") or {}).get(key)
    if not e or not prev.get("built"):
        return cap, f"no earlier run: the {WINDOW_CAP_DAYS}-day cap"
    dates = [m["date"] for m in e.get("movements", []) if m.get("date")]
    anchor = min(dates + [prev["built"][:10]])
    start = _shift(anchor, -WATERMARK_PAD)
    if start <= cap:
        return cap, f"the {WINDOW_CAP_DAYS}-day cap (oldest open line {anchor})"
    return start, (f"oldest open line {anchor}" if dates and anchor == min(dates) else f"last run {anchor}")


def previous_open(key: str) -> list[dict]:
    """The lines the last run left open for one entity."""
    path = OUT / "unreconciled.json"
    try:
        prev = json.loads(path.read_text()) if path.exists() else {}
    except ValueError:
        return []
    return [m for m in ((prev.get("entities") or {}).get(key) or {}).get("movements", [])
            if m.get("date") and m.get("bank")]


def run_check(targets: list[str] | None = None, since: str | None = SINCE, cache: Path | None = None,
              write: bool = True, sync_feed: bool = False, quiet: bool = False, agent: bool = True) -> dict:
    """The whole check, fresh. Returns the result; writes the outputs unless
    write is False. Raises on anything that stops it reading a source."""
    from accounting_agent import readonly
    targets = targets or list(ENTITIES)
    say = (lambda *a: None) if quiet else (lambda *a: print(*a, flush=True))
    if readonly.active() and not os.environ.get("BRC_OUT_DIR"):
        write, sync_feed = False, False
    if cache:
        cache.mkdir(parents=True, exist_ok=True)
    built = _now()
    cap = _shift(_today(), -WINDOW_CAP_DAYS)
    sinces, why = {}, {}
    for key in ENTITIES + DOC_ONLY:
        sinces[key], why[key] = (since, "given") if since else window_since(key)
    xero, docs_open, docs_by_id = {}, {}, {}
    for key in ENTITIES + DOC_ONLY:
        say(f"{SHORT[key]}: reading Xero ...")
        xero[key] = pull_xero(key, sinces[key], cache, docs_only=key in DOC_ONLY)
        od, by_id = documents(key, xero[key])
        for d in od:
            d["direction"] = DOC_DIRECTION[d["type"]]
        docs_open[key], docs_by_id[key] = od, by_id
    rates = Rates([d for k in docs_by_id for d in docs_by_id[k].values()], _shift(min(sinces.values()), -30))
    result = {"built": built, "since": min(sinces[k] for k in targets), "since_by_entity": {},
              "entities": {}, "refused": []}
    state: dict[str, dict] = {}
    for key in targets:
        map_problems = set_bank_map(key, xero[key]["accounts"])
        for attempt in (0, 1):
            refused_now: list[dict] = []
            since = sinces[key]
            confirmed = load_confirmed(key)
            feed = native_feed(key)
            lo = _shift(since, -PAD_DAYS)
            say(f"{SHORT[key]}: reading the bank from {lo} ({why[key]}) ...")
            bank = load_bank(key, lo, cache)
            all_recs, banks = records(key, xero[key], docs_by_id[key])
            problems = list(map_problems)
            moves = []
            for m in bank["movements"]:
                xname = BANK_MAP[key].get(m["bank"])
                if not xname or xname not in banks:
                    if m["date"] < since:
                        continue
                    refused_now.append({"entity": key, "bank": m["bank"], "line": _mline(m), "id": m["id"]})
                    continue
                if banks[xname].get("CurrencyCode") and banks[xname]["CurrencyCode"] != m["currency"]:
                    problems.append(f"{m['bank']}: currency {m['currency']} against Xero's {banks[xname]['CurrencyCode']}")
                    continue
                moves.append({**m, "xero": xname})
            for m in bank.get("unmapped", []):
                if m["date"] >= since:
                    refused_now.append({"entity": key, "bank": m.get("heading") or "", "line": _mline(m),
                                        "id": m["id"], "why": m.get("why", "")})
            # A native feed line the bank data does not hold (Xero's feed can have
            # a line before the bank's API does) is a statement line all the same
            for f in feed:
                xname = BANK_MAP[key].get(f["bank"])
                if not xname or xname not in banks:
                    continue
                hit = _feed_match(f, moves)
                if hit:
                    # read from the bank before the window: still a line the
                    # feed holds open, so it is reported and counted in the tie
                    if hit["date"] < since:
                        hit["source"] = "native feed"
                    continue
                moves.append({"id": f"feed:{f['heading']}:{f['date']}:{f['amount']}:{f['direction']}", "entity": key,
                              "bank": f["bank"], "heading": f["heading"], "xero": xname, "date": f["date"],
                              "currency": f["currency"],
                              "amount": -f["amount"] if f["direction"] == "spent" else f["amount"],
                              "payee": f["payee"], "alt": "", "who": "", "reference": "", "orig_amount": None,
                              "orig_currency": "", "source": "native feed", "settled_at": ""})
            # Lines the last run left open from before the window are carried,
            # not re-read from the bank: the window never reaches back for them
            for c_ in previous_open(key):
                xname = BANK_MAP[key].get(c_["bank"])
                if not xname or xname not in banks:
                    continue
                hit = next((m for m in moves if m["bank"] == c_["bank"] and _c(m["amount"]) == _c(c_["amount"])
                            and _gap(m["date"], c_["date"]) <= 2), None)
                if hit:
                    if hit["date"] < since and hit.get("source") != "native feed":
                        hit["source"] = "carried"
                    continue
                moves.append({"id": c_.get("id") or f"carried:{c_['bank']}:{c_['date']}:{c_['amount']}", "entity": key,
                              "bank": c_["bank"], "heading": c_.get("heading") or "", "xero": xname,
                              "date": c_["date"], "currency": c_["currency"],
                              "amount": c_["amount"], "payee": c_.get("payee") or "", "alt": "", "who": c_.get("who") or "",
                              "reference": "", "orig_amount": c_.get("orig_amount"),
                              "orig_currency": c_.get("orig_currency") or "", "source": "carried", "settled_at": ""})
            # A reconciled record dated before the window has its line before the
            # window too: it may only explain a carried or native feed line near
            # its own date, never steal a line inside the window
            old = [m["date"] for m in moves if m["date"] < lo]
            edge = _shift(lo, -RECONCILED_GAP)
            recs = [r for r in all_recs if not r["reconciled"] or r["date"] >= edge
                    or any(_gap(r["date"], d) <= RECONCILED_GAP_NAMED for d in old)]
            out = classify(key, moves, recs, docs_open, docs_by_id[key], rates, confirmed, since, lo)
            ties = tie_out(key, xero[key], bank["balances"], out["movements"], all_recs, banks, rates, confirmed)
            # A match somebody undid before the window reopens a line the window
            # cannot see, and the account stops tying: read back to the cap once
            off = [t["bank"] for t in ties.values() if "difference" in t and not t["ties"] and not t.get("confirmed_at")]
            if attempt == 0 and off and sinces[key] > cap and why[key] != "given":
                sinces[key], why[key] = cap, f"widened to the {WINDOW_CAP_DAYS}-day cap: {', '.join(off)} did not tie"
                say(f"{SHORT[key]}: {why[key]}")
                continue
            for m in out["movements"]:
                t = ties.get(m["xero"], {})
                m["verified"] = bool(t.get("verified"))
                m["in_window"] = bool(m.get("in_window")) or m["source"] == "confirmed"
            for f in feed:
                m = _feed_match(f, out["movements"]) if f["bank"] else None
                if m:
                    m["on_feed"] = True
            state[key] = {"bank": bank, "ties": ties, "problems": problems, "out": out, "feed": feed, "since": since}
            result["refused"] += refused_now
            result["since_by_entity"][key] = {"since": since, "why": why[key]}
            break
    # tier 3, once for the whole run, before any line becomes a chase
    result["line_match"] = settle_tier3(state, write, agent, say)
    for key, st in state.items():
        out, feed, since = st["out"], st["feed"], st["since"]
        # native feed drift
        drift = {"missing_from_feed": [], "reconciled_on_feed": [], "unmatched_feed_lines": [], "annotate": []}
        movable = out["movements"]
        for f in feed:
            if not f["bank"]:
                continue
            m = _feed_match(f, movable)
            if not m:
                drift["unmatched_feed_lines"].append(f["raw"])
                continue
            m["on_feed"] = True
            if m["state"] == "RECONCILED":
                exact_named = (m["source"] == "confirmed" or (
                    m.get("matched_to") and not m["siblings"] and not m.get("suspect") and not m.get("note")
                    and agree(set(m["words"]), set(words(m["matched_to"])))))
                drift["reconciled_on_feed"].append({"line": f["raw"], "heading": f["heading"],
                                                    "matched_to": m["matched_to"], "source": m["source"],
                                                    "removable": bool(exact_named)})
            elif m["status"] == "NO DOCUMENT" and m.get("awaiting", {}).get("note"):
                # the duplicate guard reads the feed before posting a receipt
                # that arrives: it sees the bill already there may be this one
                new = _annotated(f["raw"], m["awaiting"]["note"])
                if new != f["raw"]:
                    drift["annotate"].append({"line": f["raw"], "new": new})
        for m in out["movements"]:
            # only from an account that ties or has a paste: a line the
            # inference wrongly calls open must never become a chase
            if (m["status"] == "NO DOCUMENT" and m["in_window"] and not m.get("on_feed") and m["verified"]
                    and m.get("origin") not in ("paste", "native feed")):
                drift["missing_from_feed"].append({"line": _feed_text(key, m), "heading": m.get("heading") or m["bank"]})
        if sync_feed:
            sync_native_feed(key, drift, write)
        result["entities"][key] = {"bank_pulled": st["bank"]["pulled"], "xero_pulled": xero[key]["pulled"],
                                   "accounts": st["ties"], "problems": st["problems"], "drift": drift,
                                   "suspects": out["suspects"], "disagreements": out["disagreements"],
                                   "rate_notes": out["rate_notes"],
                                   "reconciled_without_line": [
                                       {k: r[k] for k in ("kind", "xero", "date", "amount", "currency", "contact", "number")}
                                       for r in out["reconciled_without_line"]],
                                   "unmatched_records": [{k: r[k] for k in ("kind", "xero", "date", "amount", "currency",
                                                                            "contact", "number", "reference")}
                                                         for r in out["unmatched_records"] if r["date"] >= since],
                                   "movements": [_public(m) for m in out["movements"]
                                                 if m["state"] == "OPEN" and m["in_window"]],
                                   "reconciled_count": sum(1 for m in out["movements"] if m["state"] == "RECONCILED"),
                                   "since": since, "since_why": why[key],
                                   "_all": out["movements"]}
    if write:
        write_outputs(result)
    return result


# ----------------------------------------------------------- tier 3: the agent

def _cand_text(c: dict) -> str:
    """One candidate in a note: no pipes or middots, the feed's separators."""
    s = " ".join(filter(None, (c["contact"], c["number"], _money(c["currency"], c["amount"]), _d(c["date"]))))
    return s.replace("|", "/").replace("·", ",")


def _await(m: dict, why: str, cands: list[dict] | None = None, reason: str = "") -> None:
    """AWAITING INFORMATION: the NO DOCUMENT path, the native
    feed and the chase, with the candidates and the agent's reason kept on the
    line so the duplicate guard sees them before a receipt that arrives is
    posted."""
    cands = cands if cands is not None else (m.get("_t3") or {}).get("candidates", [])
    texts = [_cand_text(c) for c in cands]
    note = ""
    if texts:
        note = (f"awaiting information, possible: {' or '.join(texts[:2])}"
                + (f" and {len(texts) - 2} more" if len(texts) > 2 else "") + ", not certain")
        if reason:
            note += f"; {reason}"
        note = note.replace("|", "/").replace("·", ",")
    m["status"] = "NO DOCUMENT"
    m["awaiting"] = {"why": why, "candidates": texts, "reason": reason, "note": note}
    if note:
        m["note"] = "; ".join(filter(None, (m.get("note"), note)))


def _signature(m: dict, c: dict) -> tuple:
    """What tells a candidate apart from another, seen from the line."""
    return (c["currency"], _c(c["amount"]), c["contact"].lower(), c["who"], c["ref"], c["shared"])


def _amount_ok(m: dict, c: dict, rates: "Rates | None") -> str:
    """'' when the pick's amount differs from the line in a way the allowed
    reasons can explain, else why not. A part payment is left to its own rule."""
    if c["kind"] == "part" or not c["amount"]:
        return ""
    if c["currency"] in (m["currency"], m.get("orig_currency")):
        line_amt = abs(m["amount"]) if c["currency"] == m["currency"] else abs(m.get("orig_amount") or 0)
        r = line_amt / c["amount"]
        if abs(r - 1) <= 0.005 or 1 < r <= TIP_MAX:
            return ""
        text = (c["text"] + " " + c["number"]).lower().replace("-", "")
        if r < 1 and r >= ESTIMATE_MIN and ESTIMATE_WORDS & set(re.findall(r"[a-z]+", text)):
            return ""
        return (f"the line is {r - 1:+.1%} on it in {c['currency']}, where no exchange rate applies"
                + ("" if r > 1 else " and nothing calls it an estimate"))
    opts = convert_options(c["amount"], c["currency"], m["currency"], None, rates) if rates else []
    if any(1 - FX_BAND <= abs(m["amount"]) / v <= TIP_MAX * (1 + FX_BAND) for v in opts if v):
        return ""
    return "the amounts are further apart than an exchange rate and a tip explain"


def _verify(m: dict, c: dict, cands: list[dict], used: tuple[set, set], rates: "Rates | None" = None) -> str:
    """'' when the agent's pick may stand, else why it is refused."""
    used_records, used_docs = used
    if any((x[2:] in used_records) if x[0] == "r" else (x[2:] in used_docs) for x in c["res"]):
        return "already allocated to another line"
    if c["entity"] != m["entity"]:
        return "a document of another entity"
    if c["direction"] != ("spent" if m["amount"] < 0 else "received"):
        return "the other direction"
    # a different supplier is never a match: code must see the payee's name,
    # who paid or a reference agreeing, whatever the agent read into it
    if not (c["named"] or c["who"] is True or c["ref"]):
        return "code finds neither the payee's name, who paid nor a reference on it"
    why = _amount_ok(m, c, rates)
    if why:
        return why
    for o in cands:
        if o is c or _signature(m, o) != _signature(m, c):
            continue
        if c["gap"] <= DATE_CLEAR and o["gap"] >= DATE_FAR:
            continue
        return f"nothing on the line tells it from {_cand_text(o)}"
    # another candidate of the same supplier nearer the line's amount, and no
    # clear date to prefer the pick: the evidence does not single it out
    for o in cands:
        if (o is c or o["contact"].lower() != c["contact"].lower() or o["who"] is False
                or o.get("dev") is None or c.get("dev") is None):
            continue
        if o["dev"] < c["dev"] - 1e-9 and not (c["gap"] <= DATE_CLEAR and o["gap"] >= DATE_FAR):
            return f"{_cand_text(o)} is nearer the line's amount"
    return ""


def _question(qid: str, m: dict, cands: list[dict]) -> dict:
    """The compact form the agent is sent: no raw Xero JSON."""
    amount = f"{'spent' if m['amount'] < 0 else 'received'} {_money(m['currency'], m['amount'])}"
    if m.get("orig_currency") and m.get("orig_amount") is not None:
        amount += f" (card {_money(m['orig_currency'], m['orig_amount'])})"
    payee = m["payee"] if not m.get("alt") or m["alt"].lower() in m["payee"].lower() else f"{m['payee']} / {m['alt']}"
    q = {"line_id": qid, "date": m["date"], "amount": amount, "payee": payee[:60],
         "who_paid": m.get("who") or "", "reference": (m.get("reference") or "")[:40], "candidates": []}
    for j, c in enumerate(cands, 1):
        q["candidates"].append({k: v for k, v in {
            "id": f"{qid}.C{j}", "contact": c["contact"][:40], "number": c["number"][:40], "date": c["date"],
            "amount": _money(c["currency"], c["amount"]), "desc": " ".join(c["text"].split())[:80]}.items() if v})
    return {k: v for k, v in q.items() if v not in ("", None)}


def _payload_text(qs: list[dict]) -> str:
    return ("Match each bank line to one of its own candidates, or to none. Return the JSON array only.\n"
            + json.dumps({"lines": qs}, ensure_ascii=False, separators=(",", ":")))


def _budget(qs: list[dict]) -> tuple[list[dict], list[dict]]:
    """Keep the payload under LINE_MATCH_BUDGET_TOKENS (chars / 4): drop the
    furthest candidates down to 3 a line, then whole lines from the end.
    -> (kept, dropped lines)"""
    qs = [dict(q, candidates=list(q["candidates"])) for q in qs]
    est = lambda xs: len(_payload_text(xs)) / 4                       # noqa: E731
    for k in range(LINE_MATCH_CANDIDATES - 1, 2, -1):
        if est(qs) <= LINE_MATCH_BUDGET_TOKENS:
            break
        for q in qs:
            q["candidates"] = q["candidates"][:k]
    dropped = []
    while qs and est(qs) > LINE_MATCH_BUDGET_TOKENS:
        dropped.append(qs.pop())
    return qs, dropped


def _agent_prompt() -> str:
    """The agent definition's body, without its frontmatter: the whole system
    prompt of the call, so nothing of the repo is loaded."""
    text = AGENT_FILE.read_text()
    return re.sub(r"^---\n.*?\n---\n", "", text, count=1, flags=re.S).strip()


def _load_env() -> None:
    """The run has .env exported; a hand-run on the server may not."""
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return
    env = ROOT / ".env"
    if not env.exists():
        return
    for raw in env.read_text().splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip().removeprefix("export ").strip(), v.strip().strip("'\""))


def parse_answers(text: str) -> list[dict] | None:
    """The agent's JSON array, fenced or bare."""
    if not text:
        return None
    cands = [text.strip()] + re.findall(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.S)[::-1]
    if "[" in text:
        cands.append(text[text.find("["): text.rfind("]") + 1])
    for c in cands:
        try:
            obj = json.loads(c)
        except ValueError:
            continue
        if isinstance(obj, list):
            return [a for a in obj if isinstance(a, dict)]
    return None


def ask_agent(payload: str) -> dict:
    """One headless call of the bank-line-match agent: its body as the whole
    system prompt, no tools, no settings, no MCP, from an empty folder, so the
    repo's CLAUDE.md, skills and hooks never load. -> {answers, cost_usd, usage, seconds, error, session_id}"""
    import subprocess
    import tempfile
    import time
    _load_env()
    env = dict(os.environ)
    env["PATH"] = f"{Path.home() / '.local' / 'bin'}:{env.get('PATH', '')}"
    cmd = [CLAUDE_BIN, "-p", "--model", LINE_MATCH_MODEL, "--effort", LINE_MATCH_EFFORT,
           "--system-prompt", _agent_prompt(), "--tools", "", "--strict-mcp-config", "--setting-sources", "",
           "--disable-slash-commands", "--no-session-persistence", "--output-format", "json",
           "--max-budget-usd", LINE_MATCH_MAX_USD]
    started, out, err = time.time(), {}, ""
    try:
        with tempfile.TemporaryDirectory(prefix="bank-line-match-") as cwd:
            proc = subprocess.run(cmd, input=payload, cwd=cwd, env=env, capture_output=True, text=True,
                                  timeout=LINE_MATCH_TIMEOUT)
        try:
            out = json.loads(proc.stdout or "{}")
        except ValueError:
            out = {"result": proc.stdout}
        if proc.returncode or out.get("is_error"):
            err = f"the agent exited {proc.returncode}: {(proc.stderr or out.get('result') or '')[:200]}"
    except subprocess.TimeoutExpired:
        err = f"the agent timed out after {LINE_MATCH_TIMEOUT} seconds"
    except Exception as e:                                             # noqa: BLE001
        err = f"{type(e).__name__}: {e}"
    answers = None if err else parse_answers(out.get("result") or "")
    if answers is None and not err:
        err = "the agent's answer held no JSON array"
    u = out.get("usage") or {}
    return {"answers": answers, "error": err, "cost_usd": out.get("total_cost_usd"),
            "usage": {k: u.get(k) for k in ("input_tokens", "output_tokens", "cache_read_input_tokens",
                                             "cache_creation_input_tokens")},
            "seconds": round(time.time() - started, 1), "session_id": out.get("session_id")}


def settle_tier3(state: dict, write: bool, agent: bool, say=print, ask=None) -> dict:
    """Tier 3 for the whole run: every line left open with candidates goes to
    the bank-line-match agent in one call, and code verifies each answer. A
    verified "certain" pick is TO MATCH, high, by "agent"; anything else is
    AWAITING INFORMATION (the NO DOCUMENT path) with its candidates noted.
    Fail safe: a failed call leaves every line awaiting information and the
    check completes. -> what was asked and answered, the cost and the tokens"""
    ask = ask or ask_agent
    summary = {"asked": [], "cached": 0, "not_assessed": defaultdict(int), "error": "", "cost_usd": None,
               "usage": {}, "seconds": 0.0}
    pend = [(key, m) for key, st in state.items() for m in st["out"]["movements"]
            if m.get("_t3") and m["state"] == "OPEN" and m["status"] == "NO DOCUMENT" and not m.get("goes_to")]
    eligible = []
    for key, m in pend:
        if m.get("verified") or m.get("on_feed"):
            eligible.append((key, m))
        else:
            _await(m, "not assessed, the account does not tie")
            summary["not_assessed"]["the account does not tie"] += 1
    # tied lines first, then the oldest
    eligible.sort(key=lambda km: (not km[1]["_t3"]["tied"], km[1]["date"], km[1]["id"]))
    for key, m in eligible[LINE_MATCH_CAP:]:
        _await(m, "not assessed, over the cap")
        summary["not_assessed"]["over the cap"] += 1
    eligible = eligible[:LINE_MATCH_CAP]
    cache_path = OUT / "line_match_cache.json"
    try:
        cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    except ValueError:
        cache = {}
    qmap: dict[str, tuple] = {}
    answers: dict[str, dict] = {}
    qs = []
    for i, (key, m) in enumerate(eligible, 1):
        cands = m["_t3"]["candidates"]
        ck = m["id"] + "|" + ",".join(sorted(c["cid"] for c in cands))
        qid = f"L{i}"
        qmap[qid] = (key, m, cands, ck)
        if ck in cache:
            answers[qid] = {**cache[ck], "cached": True}
            summary["cached"] += 1
        else:
            qs.append(_question(qid, m, cands))
    if qs and not agent:
        for q in qs:
            _await(qmap[q["line_id"]][1], "not assessed, the agent was not called (--no-agent)")
        summary["not_assessed"]["--no-agent"] += len(qs)
        qs = []
    if qs:
        qs, dropped = _budget(qs)
        for q in dropped:
            _await(qmap[q["line_id"]][1], "not assessed, over the budget")
        summary["not_assessed"]["over the budget"] += len(dropped)
    if qs:
        payload = _payload_text(qs)
        summary["payload_tokens_est"] = round(len(payload) / 4)
        say(f"line match: asking {LINE_MATCH_AGENT} about {len(qs)} lines (~{summary['payload_tokens_est']} tokens) ...")
        res = ask(payload)
        summary.update(error=res.get("error") or "", cost_usd=res.get("cost_usd"), usage=res.get("usage") or {},
                       seconds=res.get("seconds") or 0.0, session_id=res.get("session_id"))
        sent = {q["line_id"]: q for q in qs}
        got = {str(a.get("line_id")): a for a in (res.get("answers") or [])}
        for qid, q in sent.items():
            key, m, cands, _ = qmap[qid]
            ids = {c_["id"]: cands[j] for j, c_ in enumerate(q["candidates"])}
            a = got.get(qid)
            if a is None:
                answers[qid] = {"pick": None, "confidence": "failed",
                                "reason": res.get("error") or "no answer for this line"}
            else:
                pick = a.get("pick")
                answers[qid] = {"pick": ids[pick]["cid"] if pick in ids else (f"?{pick}" if pick else None),
                                "confidence": "certain" if a.get("confidence") == "certain" else "unsure",
                                "reason": " ".join(str(a.get("reason") or "").split())[:300]}
                cache[qmap[qid][3]] = {"pick": answers[qid]["pick"], "confidence": answers[qid]["confidence"],
                                       "reason": answers[qid]["reason"], "at": _now()}
            summary["asked"].append({"qid": qid, "question": q, "answer": a})
    # code verifies every answer; one candidate goes to one line at most
    certain = {}
    for qid, ans in answers.items():
        key, m, cands, _ = qmap[qid]
        by_cid = {c["cid"]: c for c in cands}
        if ans.get("confidence") == "failed":
            _await(m, "the agent failed", cands, ans.get("reason", ""))
            continue
        if ans.get("confidence") != "certain" or not ans.get("pick"):
            _await(m, "the agent was not certain", cands, ans.get("reason", ""))
            continue
        c = by_cid.get(ans["pick"])
        why = ("not one of its candidates" if c is None
               else _verify(m, c, cands, state[key]["out"]["_used"], state[key]["out"].get("_rates")))
        if why:
            _await(m, f"the agent's pick refused: {why}", cands, f"code refused the agent's pick: {why}")
            m["awaiting"]["refused"] = why
            continue
        certain[qid] = c
    wanted: dict[str, int] = defaultdict(int)
    for c in certain.values():
        for r in c["res"]:
            wanted[r] += 1
    for qid, c in certain.items():
        key, m, cands, _ = qmap[qid]
        if any(wanted[r] > 1 for r in c["res"]):
            _await(m, "the agent's pick refused: picked for another line too", cands,
                   "code refused the agent's pick: picked for another line too")
            m["awaiting"]["refused"] = "picked for another line too"
            continue
        used_records, used_docs = state[key]["out"]["_used"]
        m["status"] = ""                      # NO DOCUMENT was provisional
        _apply(m, c, "agent", answers[qid].get("reason", ""), used_records, used_docs)
        m["reason"] = answers[qid].get("reason", "")
    for key, m in eligible:
        if m["status"] == "NO DOCUMENT" and not m.get("awaiting"):
            _await(m, "not assessed")
    # the counts per tier, and the cost, logged like investigate_query's
    tiers = defaultdict(int)
    for st in state.values():
        for m in st["out"]["movements"]:
            if m["state"] != "OPEN" or not m.get("in_window"):
                continue
            if m.get("match_by") == "code":
                tiers["code, unique" if m.get("match_how") == "unique" else "code, tie-break"] += 1
            elif m.get("match_by") == "agent":
                tiers["agent, certain"] += 1
            elif m.get("awaiting"):
                tiers["awaiting information, with candidates" if m["awaiting"]["candidates"] else "awaiting information"] += 1
            elif m["status"] == "NO DOCUMENT":
                tiers["no document, no candidates"] += 1
    summary["tiers"] = dict(tiers)
    summary["not_assessed"] = dict(summary["not_assessed"])
    if write:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(cache, indent=1, ensure_ascii=False) + "\n")
        if summary["asked"] or summary["error"]:
            log = OUT / "line_match" / f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
            log.parent.mkdir(parents=True, exist_ok=True)
            log.write_text(json.dumps({"at": _now(), "run_id": os.environ.get("AGENT_RUN") or "",
                                       "model": LINE_MATCH_MODEL, "effort": LINE_MATCH_EFFORT,
                                       "lines": len(summary["asked"]), "cached": summary["cached"],
                                       "cost_usd": summary["cost_usd"], "usage": summary["usage"],
                                       "seconds": summary["seconds"], "session_id": summary.get("session_id"),
                                       "error": summary["error"], "asked": summary["asked"]},
                                      indent=1, ensure_ascii=False, default=str) + "\n")
    return summary
def _public(m: dict) -> dict:
    keep = ("id", "entity", "bank", "heading", "xero", "date", "amount", "currency", "payee", "who", "state", "status",
            "source", "goes_to", "confidence", "siblings", "note", "verified", "suspect", "orig_amount",
            "orig_currency", "on_feed", "origin", "matched_to", "match_by", "match_how", "reason", "awaiting")
    return {k: m.get(k) for k in keep}


def _label(m: dict) -> str:
    """The status as the lists show it: a NO DOCUMENT line with candidates the
    agent could not settle is AWAITING INFORMATION."""
    if m.get("status") == "NO DOCUMENT" and (m.get("awaiting") or {}).get("candidates"):
        return "AWAITING INFORMATION"
    return m.get("status") or ""


AWAIT_NOTE = re.compile(r"^awaiting information, possible: [^·]*?(?: · |$)")


def _annotated(raw: str, note: str) -> str:
    """A native feed line with the awaiting-information note at the head of its
    note column, replacing an earlier one; the rest of the note and the id
    marker are kept."""
    parts = [p.strip() for p in raw.split("|")]
    while len(parts) < 7:
        parts.append("")
    rest = AWAIT_NOTE.sub("", " / ".join(p for p in parts[6:] if p)).strip()
    parts = parts[:6] + [f"{note} · {rest}" if rest else note]
    return " | ".join(parts).replace(" |  | ", " | | ").rstrip()


def _feed_text(key: str, m: dict) -> str:
    marker = m["id"].split(":")[0] + ":" + m["id"].split(":")[1][:8] if m["id"].startswith(("rev:", "mer:")) else ""
    note = (m.get("awaiting") or {}).get("note") or ""
    note = f"{note} · {marker}" if note and marker else (note or marker)
    return (f"{m['date']} | {m['payee']} | {'spent' if m['amount'] < 0 else 'received'} | "
            f"{m['currency']} {abs(m['amount']):.2f} | {m.get('who') or ''} | | {note}").rstrip()


def sync_native_feed(key: str, drift: dict, write: bool) -> None:
    """Add the NO DOCUMENT lines the native feed is missing; remove the lines a
    paste, or a one-to-one exact match with the payee's name, shows reconciled.
    Ambiguous ones are left and reported."""
    import bankfeed
    bankfeed.FEED = FEED
    for r in drift["reconciled_on_feed"]:
        if not r["removable"]:
            continue
        needle = r["line"]
        why = f"reconciled in Xero ({r['source']}): {r['matched_to']}"
        if write:
            try:
                bankfeed.cmd_remove(key, needle, why)
            except SystemExit as e:
                print(f"  not removed: {e}")
        else:
            print(f"  DRY RUN would remove from {key}: {needle} · {why}")
    for r in drift["missing_from_feed"]:
        if write:
            try:
                bankfeed.cmd_add(key, r["heading"], r["line"], create=True)
            except SystemExit as e:
                print(f"  not added: {e}")
        else:
            print(f"  DRY RUN would add to {key} [{r['heading']}]: {r['line']}")
    # the candidates the agent could not settle, on the feed line itself, so
    # the duplicate guard sees them before a receipt that arrives is posted
    for r in drift.get("annotate", []):
        if not write:
            print(f"  DRY RUN would note on {key}: {r['new']}")
            continue
        path = bankfeed._path(key)
        blocks = bankfeed._parse(path)
        hit = [(i, j) for i, b in enumerate(blocks) for j, l in enumerate(b[2]) if l == r["line"]]
        if len(hit) != 1:
            print(f"  not noted, {len(hit)} lines match: {r['line']}")
            continue
        i, j = hit[0]
        blocks[i][2][j] = r["new"]
        path.write_text(bankfeed._render(bankfeed._title(path), blocks))
        print(f"  noted on {key}: {r['new']}")

# --------------------------------------------------------------- outputs

def _acct_name(xname: str) -> str:
    return xname


def entity_md(key: str, e: dict, built: str) -> str:
    out = [f"# Not yet reconciled, {SHORT[key]}", "",
           f"As at {_local(built)}. Every bank line still open on Xero's reconcile screen as far as "
           "the check can tell, what it goes to, and whether the account ties. Built by "
           "scripts/bank_reconcile_check.py; rules in the xero-bank-reconcile-check skill.", ""]
    allm = e["movements"]
    for xname, t in e["accounts"].items():
        ms = sorted((m for m in allm if m["xero"] == xname), key=lambda m: (m["date"], m["amount"]))
        if "difference" not in t:
            out += [f"## {xname}", f"not checked: {t.get('why')}", ""]
            continue
        tie = ("TIES" if t["ties"] else f"OFF BY {t['currency']} {t['difference']:,.2f}")
        conf = f" · confirmed by paste {t['confirmed_at']}" if t["confirmed_at"] else ""
        verified = "" if t["verified"] else " · UNVERIFIED: these lines are not put to anyone as reconcile this"
        out.append(f"## {xname} · {len(ms)} open · {tie}{conf}{verified}")
        out.append(f"bank {t['currency']} {t['bank_balance']:,.2f} · Xero {t['currency']} {t['xero_balance']:,.2f} "
                   f"({t['xero_balance_from']}) · open lines {t['open_sum']:,.2f} · unreconciled Xero records "
                   f"{t['unreconciled_sum']:,.2f}")
        for m in ms:
            goes = m["goes_to"]["text"] if m.get("goes_to") else ""
            sib = f" · one of {len(m['siblings']) + 1} lines of this amount" if m.get("siblings") else ""
            out.append(f"- {m['date']} | {m['payee']} | {'spent' if m['amount'] < 0 else 'received'} | "
                       f"{m['currency'] or t['currency']} {abs(m['amount']):,.2f} | {_label(m)}"
                       + (f" | {goes}" if goes else "") + (f" | {m['confidence']}" if m.get("confidence") else "")
                       + (f" by {m['match_by']} ({m['match_how']})" if m.get("match_by") else "")
                       + f" | {m['source']}" + (" (from the native feed, not in the bank data)" if m.get("origin") == "native feed" else "")
                       + sib + (f" | {m['note']}" if m.get("note") else ""))
        out.append("")
    if e["suspects"]:
        out.append("## suspect wrong matches")
        for s in e["suspects"]:
            out.append(f"- {s['xero']} · {s['line'] or 'line not found'} · {s['kind']}: {s['why']}")
        out.append("")
    if e["disagreements"]:
        out.append("## paste and inference disagree")
        for d in e["disagreements"]:
            out.append(f"- {d['account']} · {d['kind']} · {d['line']}" + (f" · was {d['was']}" if d.get("was") else ""))
        out.append("")
    if e.get("rate_notes"):
        out.append("## reconciled to the right payee at a rate far from the market")
        for s in e["rate_notes"]:
            out.append(f"- {s['xero']} · {s['line']} · {s['record']}: {s['why']}")
        out.append("")
    if e.get("reconciled_without_line"):
        out.append("## reconciled Xero records no bank line was found for")
        for r in e["reconciled_without_line"]:
            out.append(f"- {r['xero']} · {r['date']} · {r['kind']} · {r['contact']} · {r['currency']} "
                       f"{abs(r['amount']):,.2f}" + (f" · {r['number']}" if r["number"] else ""))
        out.append("")
    if e["unmatched_records"]:
        out.append("## unreconciled Xero records with no bank line")
        for r in e["unmatched_records"]:
            out.append(f"- {r['xero']} · {r['date']} · {r['kind']} · {r['contact']} · {r['currency']} "
                       f"{abs(r['amount']):,.2f}" + (f" · {r['number']}" if r["number"] else ""))
        out.append("")
    d = e["drift"]
    if any(d.values()):
        out.append("## native bank feed drift")
        out += [f"- missing from the feed, no document: {r['line']}" for r in d["missing_from_feed"]]
        out += [f"- on the feed, reconciled in Xero ({r['source']}{', removable' if r['removable'] else ', check'}): "
                f"{r['line']} · {r['matched_to']}" for r in d["reconciled_on_feed"]]
        out += [f"- on the feed, no bank movement found: {r}" for r in d["unmatched_feed_lines"]]
        out.append("")
    if e["problems"]:
        out.append("## problems")
        out += [f"- {p}" for p in e["problems"]]
    return "\n".join(out).rstrip() + "\n"


def write_outputs(result: dict) -> None:
    d = OUT / "unreconciled"
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    for key, e in result["entities"].items():
        p = d / f"{key}.md"
        p.write_text(entity_md(key, e, result["built"]))
        p.chmod(0o600)
    slim = {"built": result["built"], "since": result["since"], "since_by_entity": result.get("since_by_entity", {}),
            "refused": result["refused"],
            "line_match": {k: v for k, v in (result.get("line_match") or {}).items() if k != "asked"},
            "entities": {k: {kk: vv for kk, vv in e.items() if kk != "_all"} for k, e in result["entities"].items()}}
    p = OUT / "unreconciled.json"
    p.write_text(json.dumps(slim, indent=1, ensure_ascii=False, default=str) + "\n")
    p.chmod(0o600)


def open_lines(entity: str | None = None) -> list[dict]:
    """The open lines of the last run, from unreconciled.json."""
    p = OUT / "unreconciled.json"
    if not p.exists():
        return []
    data = json.loads(p.read_text())
    return [m for k, e in data["entities"].items() if entity in (None, k) for m in e["movements"]]


def print_summary(result: dict) -> None:
    print(f"\nas at {_local(result['built'])}")
    for k, w in (result.get("since_by_entity") or {}).items():
        print(f"  {SHORT[k]}: bank read from {w['since']} ({w['why']})")
    for key, e in result["entities"].items():
        print(f"\n{SHORT[key]}  ({e['reconciled_count']} reconciled lines allocated)")
        for xname, t in e["accounts"].items():
            if "difference" not in t:
                print(f"  {xname}: not checked, {t.get('why')}")
                continue
            tie = "TIES" if t["ties"] else f"OFF BY {t['difference']:,.2f}"
            print(f"  {xname} [{t['currency']}] bank {t['bank_balance']:,.2f} Xero {t['xero_balance']:,.2f} "
                  f"open {t['open_sum']:,.2f} unrec {t['unreconciled_sum']:,.2f} -> {tie}"
                  + ("" if t["verified"] else " UNVERIFIED") + (f" (paste {t['confirmed_at']})" if t["confirmed_at"] else ""))
            for m in sorted((m for m in e["movements"] if m["xero"] == xname), key=lambda m: m["date"]):
                print(f"    {m['date']} {m['payee'][:34]:34} {m['amount']:>11,.2f} {_label(m):20} "
                      f"{m['confidence'] or '':6} {(m.get('match_by') or m['source']):9} "
                      f"{(m['goes_to'] or {}).get('text', '')[:90]}"
                      + (f" [siblings {len(m['siblings'])}]" if m.get("siblings") else "")
                      + (f" [{(m.get('awaiting') or {}).get('why')}]" if m.get("awaiting") else ""))
        for s in e["suspects"]:
            print(f"  SUSPECT {s['xero']} · {s['line']} · {s['why']}")
        for s in e.get("rate_notes", []):
            print(f"  RATE {s['xero']} · {s['line']} · {s['record']} · {s['why']}")
        for r in e.get("reconciled_without_line", []):
            print(f"  REC-NO-LINE {r['xero']} · {r['date']} · {r['kind']} · {r['contact']} · {r['amount']:,.2f} {r['number']}")
        for r in e.get("unmatched_records", []):
            print(f"  UNREC-NO-LINE {r['xero']} · {r['date']} · {r['kind']} · {r['contact']} · {r['amount']:,.2f} {r['number']}")
        for d in e["disagreements"]:
            print(f"  PASTE {d['account']} · {d['kind']} · {d['line']}" + (f" · was {d['was']}" if d.get("was") else ""))
        dr = e["drift"]
        for r in dr["missing_from_feed"]:
            print(f"  FEED missing: {r['line']}")
        for r in dr["reconciled_on_feed"]:
            print(f"  FEED reconciled ({'removable' if r['removable'] else 'check'}): {r['line']} -> {r['matched_to']}")
        for r in dr["unmatched_feed_lines"]:
            print(f"  FEED no movement: {r}")
        for r in dr.get("annotate", []):
            print(f"  FEED note: {r['new']}")
        for p in e["problems"]:
            print(f"  PROBLEM {p}")
    lm = result.get("line_match") or {}
    if lm:
        u = lm.get("usage") or {}
        print("\nwhat the open lines go to: " + ", ".join(f"{k} {v}" for k, v in sorted((lm.get("tiers") or {}).items())))
        print(f"line match agent: {len(lm.get('asked', []))} lines asked, {lm.get('cached', 0)} from the cache"
              + "".join(f", {v} not assessed ({k})" for k, v in (lm.get("not_assessed") or {}).items())
              + (f" · USD {lm['cost_usd']:.4f}" if isinstance(lm.get("cost_usd"), (int, float)) else "")
              + (f" · tokens in {u.get('input_tokens')} (cache read {u.get('cache_read_input_tokens')}, "
                 f"written {u.get('cache_creation_input_tokens')}), out {u.get('output_tokens')}" if u else "")
              + (f" · {lm.get('seconds')}s" if lm.get("seconds") else "")
              + (f" · FAILED: {lm['error']}" if lm.get("error") else ""))
        for a in lm.get("asked", []):
            q = a["question"]
            print(f"  {q['line_id']} {q['date']} {q['amount']} {q.get('payee', '')}"
                  + (f" · who {q['who_paid']}" if q.get("who_paid") else "")
                  + (f" · ref {q['reference']}" if q.get("reference") else ""))
            for c in q["candidates"]:
                print(f"      {c['id']} {c.get('contact', '')} · {c.get('number', '')} · {c['date']} · {c['amount']}"
                      + (f" · {c['desc']}" if c.get("desc") else ""))
            ans = a.get("answer") or {}
            print(f"    -> {ans.get('pick')} {ans.get('confidence')}: {ans.get('reason')}")
    if result["refused"]:
        print(f"\nrefused, account not mapped: {len(result['refused'])} lines")
        for r in result["refused"][:20]:
            print(f"  {SHORT[r['entity']]} · {r['bank']} · {r['line']}" + (f" · {r['why']}" if r.get("why") else ""))


def cmd_show(entity: str | None) -> int:
    d = OUT / "unreconciled"
    keys = [config.entity(entity).slug] if entity else ENTITIES
    for k in keys:
        p = d / f"{k}.md"
        if p.exists():
            print(p.read_text())
        else:
            print(f"{k}: no list yet; run `bank_reconcile_check.py run` first")
    return 0


def main(argv=None) -> int:
    global OUT
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("entity", nargs="?", default="all")
    r.add_argument("--since", default=None, help="override the window; default: window_since() per entity")
    r.add_argument("--dry-run", action="store_true")
    r.add_argument("--cache")
    r.add_argument("--out")
    r.add_argument("--sync-feed", action="store_true")
    r.add_argument("--no-agent", action="store_true",
                   help="skip tier 3: lines it would ask about are awaiting information, not assessed")
    c_ = sub.add_parser("confirm")
    c_.add_argument("entity")
    c_.add_argument("account")
    c_.add_argument("--paste", required=True)
    c_.add_argument("--at")
    c_.add_argument("--out")
    s = sub.add_parser("show")
    s.add_argument("entity", nargs="?")
    s.add_argument("--out")
    a = p.parse_args(argv)
    if getattr(a, "out", None):
        OUT = Path(a.out)
        os.environ["BRC_OUT_DIR"] = a.out
    if a.cmd == "confirm":
        return cmd_confirm(a.entity, a.account, a.paste, a.at)
    if a.cmd == "show":
        return cmd_show(a.entity)
    try:
        targets = ENTITIES if a.entity == "all" else [config.entity(a.entity).slug]
    except config.ConfigError as exc:
        sys.exit(str(exc))
    if any(t not in ENTITIES for t in targets):
        sys.exit(f"{a.entity!r} is not read in full; one of {', '.join(ENTITIES)} or all")
    result = run_check(targets, a.since, Path(a.cache) if a.cache else None, write=not a.dry_run,
                       sync_feed=a.sync_feed, agent=not a.no_agent)
    print_summary(result)
    if a.dry_run:
        print("\nDRY RUN: nothing written")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
