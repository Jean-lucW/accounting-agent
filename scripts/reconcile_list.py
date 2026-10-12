#!/usr/bin/env python
"""The reconcile list: every bank line still open on Xero's bank feed whose
bill, spend money, transfer or payment is already posted, and which one it goes
to.

The agent never reconciles (xero skill rule 7): it posts the bill or the spend
money and a person matches the statement line to it by hand. This list tells
the bookkeeper which line goes to which. A line is on it only when both halves
exist: a settled bank movement nothing in Xero is reconciled to, and the posted
document it belongs to. A line with no document yet is the bank feed's business
(data/bankfeed), not this list's; a line once reconciled is off it.

Where the pairs come from, all read-only:
  - open bills: the bills report's own matching (scripts/bills_report.py), the
    bills it calls MATCH IN XERO because the entity's own bank paid them, and
    where the line and the bill are in one currency, only at the bill's amount
    to the cent. A bill paid from another entity's bank is the bill-payments
    report line (reconcile the bill to intercompany), not this list
  - spend money, receive money and transfer legs not reconciled
    (IsReconciled false)
  - payments a person entered on a bank account and never reconciled, one to
    a line or several of one day to one line
  each Xero entry paired with the settled statement line of the same entity,
  account, direction and amount within [accounts_payable] leg_window_days, from
  the bills report's bank sources (the statement CSVs, then the bank APIs). A
  Xero entry with no statement line is left off: there is nothing on the feed
  to match it to.

Nothing reaches the list unless the bank reconcile check
(scripts/bank_reconcile_check.py, skill xero-bank-reconcile-check) holds the
line open, fresh, on an account that ties or was confirmed by a paste. The
check runs first, every time: a line it calls reconciled is dropped, a line on
an account it cannot verify is held back for the admin, an open line it pairs
with a posted document is added, a repeated amount it cannot settle is put as
"one of these", and a line it finds matched to the wrong bill goes under its
own heading. A match that does not fit, with no better bill to point at, goes
to the admin, not the bookkeeper. If the check fails, nothing is written and
no DM is sent.

Every run replaces the whole list; nothing on it is closed by hand. Outputs:
  data/reports/reconcile_list.json   the list, the server's master
  data/reports/reconcile_list.md     the block a run puts, verbatim, at the end
                                     of its MANUAL ITEMS thread
  docs/bookkept/reconcile.json       the register's "reconcile on the bank
                                     feed" section (scripts/outstanding.py),
                                     which rebuilds the Word copy on Drive
  a Slack DM to [accounts_payable] reconcile_notify (else unpaid_notify): the
                                     same list, entity by entity; skipped when
                                     they already had this exact list today

Usage:
    .venv/bin/python scripts/reconcile_list.py run [--no-refresh] [--no-dm] [--dry-run] [--check-cache DIR]
    .venv/bin/python scripts/reconcile_list.py show

`run` pulls the open bills from Xero again unless --no-refresh, which uses the
bills report's store as it stands. --dry-run prints the list and writes and
sends nothing. --check-cache hands the bank reconcile check a cache folder
(development only). Server only: it is the only Xero client. AGENT_ROOT points
it at another repo, as for the check.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = (Path(os.environ["AGENT_ROOT"]).resolve() if os.environ.get("AGENT_ROOT")
        else Path(__file__).resolve().parents[1])
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.append(str(Path(__file__).resolve().parent))

from accounting_agent import config                           # noqa: E402
import bills_report as br                                     # noqa: E402
import bank_reconcile_check as brc                            # noqa: E402

OUT_JSON = br.OUT_DIR / "reconcile_list.json"
OUT_MD = br.OUT_DIR / "reconcile_list.md"
DM_STATE = br.OUT_DIR / "reconcile_dm.json"
# Xero entries older than the group's records start are not looked for: an
# older entry still unreconciled is a review finding, not a line to match today.
SINCE = br._records_from()
WINDOW = br.LEG_WINDOW_DAYS
SPENT_PAYMENTS = {"ACCPAYPAYMENT", "ARCREDITPAYMENT", "AROVERPAYMENTPAYMENT", "ARPREPAYMENTPAYMENT"}
DOCUMENT = {
    "SPEND": "spend money", "RECEIVE": "receive money",
    "SPEND-TRANSFER": "transfer", "RECEIVE-TRANSFER": "transfer",
    "SPEND-PREPAYMENT": "prepayment", "RECEIVE-PREPAYMENT": "prepayment",
    "SPEND-OVERPAYMENT": "overpayment", "RECEIVE-OVERPAYMENT": "overpayment",
}


def _d(iso: str) -> str:
    return f"{int(iso[8:10])} {date.fromisoformat(iso):%b}"


def _dy(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{d.day:02d} {d:%b %Y}"


def _days(a: str, b: str) -> int:
    return abs((date.fromisoformat(a) - date.fromisoformat(b)).days)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")


# ------------------------------------------------------------- the open bills

def assess_bills(refresh: bool) -> tuple[dict, dict[str, dict]]:
    """The bills report's assessment, and the bank line each open bill resolved
    to. bills_report keeps only that line's text on the bill; the wrap hands
    the line itself back without changing anything the report decides."""
    store = br.load_store()
    lines: dict[str, dict] = {}
    current: dict = {}
    classify, evidence = br.classify, br._evidence

    def keep_line(line: dict) -> str:
        current["line"] = dict(line)
        return evidence(line)

    def classify_keeping_line(key, bill, *a, **k):
        current.clear()
        out = classify(key, bill, *a, **k)
        if out[0] == "MATCH IN XERO" and current.get("line"):
            lines[bill["id"]] = current["line"]
        return out

    from accounting_agent import readonly
    lock_was_ours = not readonly.active()
    os.environ[readonly.ENV] = "1"
    br.classify, br._evidence = classify_keeping_line, keep_line
    try:
        if refresh:
            since = next((e.get("from") for e in store["entities"].values() if e.get("from")),
                         None) or br._records_from()
            for key in br.ORDER():
                print(f"{brc.SHORT[key]}: pulling bills from Xero ...", flush=True)
                store["entities"][key] = br.pull_entity(key, since)
        print("bank: reading the statements and the bank APIs ...", flush=True)
        br.assess(store)
    finally:
        br.classify, br._evidence = classify, evidence
        if lock_was_ours:
            del os.environ[readonly.ENV]
    return store, lines


# ----------------------------------------------- what Xero holds unreconciled

def pull_unreconciled(key: str) -> dict:
    """The entity's bank accounts and every bank-side entry not reconciled."""
    from accounting_agent.xero.banking import get_bank_transactions, get_bank_transfers, get_payments
    c = br._client(key)
    chart = c.get("Accounts").get("Accounts", [])
    names = {a.get("Code"): a.get("Name") for a in chart if a.get("Code")}
    banks = {a["AccountID"]: {"name": a.get("Name") or "", "currency": a.get("CurrencyCode") or ""}
             for a in chart if a.get("Type") == "BANK"}
    brc.set_bank_map(key, chart)            # which Xero account each configured bank account feeds
    txs = get_bank_transactions(c, from_date=SINCE, reconciled=False, status="AUTHORISED")
    transfers = get_bank_transfers(c, from_date=SINCE)
    leg_of = {}
    for t in transfers:
        frm = (t.get("FromBankAccount") or {}).get("Name") or ""
        to = (t.get("ToBankAccount") or {}).get("Name") or ""
        for side in ("FromBankTransactionID", "ToBankTransactionID"):
            if t.get(side):
                leg_of[t[side]] = (frm, to, t.get("Reference") or "")
    entries = []
    for t in txs:
        acct = banks.get((t.get("BankAccount") or {}).get("AccountID"))
        if not acct:
            continue
        codes = []
        for li in t.get("LineItems") or []:
            code = li.get("AccountCode")
            if code and names.get(code, code) not in codes:
                codes.append(names.get(code, code))
        entries.append({
            "kind": DOCUMENT.get(t.get("Type") or "", "bank transaction"),
            "spent": (t.get("Type") or "").startswith("SPEND"),
            "date": br._xdate(t.get("Date")),
            "amount": round(float(t.get("Total") or 0), 2),
            "account": acct["name"], "currency": acct["currency"],
            "contact": ((t.get("Contact") or {}).get("Name") or "").strip(),
            "reference": (t.get("Reference") or "").strip(),
            "accounts": codes,
            "transfer": leg_of.get(t.get("BankTransactionID")),
        })
    payments = []
    for p in get_payments(c, from_date=SINCE, where="IsReconciled==false", status="AUTHORISED"):
        acct = banks.get((p.get("Account") or {}).get("AccountID"))
        if not acct:
            continue
        inv = p.get("Invoice") or {}
        payments.append({
            "kind": "payment",
            "spent": (p.get("PaymentType") or "") in SPENT_PAYMENTS,
            "date": br._xdate(p.get("Date")),
            "amount": round(float(p.get("BankAmount") or p.get("Amount") or 0), 2),
            "account": acct["name"], "currency": acct["currency"],
            "contact": " ".join(((inv.get("Contact") or {}).get("Name") or "").split()),
            "number": inv.get("InvoiceNumber") or "",
            "doc_currency": inv.get("CurrencyCode") or acct["currency"],
            "doc_amount": round(float(p.get("Amount") or 0), 2),
        })
    return {"banks": list(banks.values()), "entries": entries, "payments": payments}


# ---------------------------------------------------------------- the pairing

def line_ident(key: str, line: dict) -> str:
    """The configured bank account (the bank reconcile check's name for it) a
    statement line sits on: by its heading and currency, else the entity's one
    account of that currency. "" where none, or more than one, fits."""
    ident = brc._feed_ident(key, line.get("account", ""), line["currency"])
    if ident:
        return ident
    same_ccy = [b for b in config.entity(key).bank_accounts
                if not b.currency or b.currency.upper() == str(line["currency"]).upper()]
    return brc.bank_ident(same_ccy[0]) if len(same_ccy) == 1 else ""


def same_account(key: str, xero_name: str, xero_ccy: str, line: dict) -> bool:
    """The Xero bank account is the one the statement line sits on: the one
    config/group.toml maps the line's bank account to (xero_account_code or
    xero_account), in the same currency."""
    if line["entity"] != key or line["currency"] != xero_ccy or not xero_name:
        return False
    return brc.BANK_MAP.get(key, {}).get(line_ident(key, line)) == xero_name


def xero_account_for(key: str, line: dict, banks: list[dict]) -> str:
    hits = [b["name"] for b in banks if same_account(key, b["name"], b["currency"], line)]
    return hits[0] if len(hits) == 1 else line["account"]


def find_line(key: str, entry: dict, history: list[dict], claimed: set) -> dict | None:
    """The settled statement line this Xero entry stands for, nearest date first."""
    best = None
    for l in history:
        if br._line_key(l) in claimed or (l["amount"] < 0) != entry["spent"]:
            continue
        if abs(abs(l["amount"]) - entry["amount"]) > 0.005 or _days(l["date"], entry["date"]) > WINDOW:
            continue
        if not same_account(key, entry["account"], entry["currency"], l):
            continue
        if best is None or _days(l["date"], entry["date"]) < _days(best["date"], entry["date"]):
            best = l
    return best


def _line_words(line: dict, account: str) -> str:
    what = "payment" if line["amount"] < 0 else "receipt"
    return (f"the {_d(line['date'])} {line['currency']} {abs(line['amount']):,.2f} {what} "
            f"on {account} ({line['payee']})")


def _refs(*parts: str) -> str:
    return " · ".join(p for p in parts if p)


def _ref(line: dict) -> dict:
    """What the bank reconcile check needs to find the same statement line."""
    return {"entity": line["entity"], "currency": line["currency"], "date": line["date"],
            "amount": round(line["amount"], 2), "payee": line.get("payee", "")}


# ------------------------------------------- the bank reconcile check's verdict

def _move_words(m: dict) -> str:
    what = "payment" if m["amount"] < 0 else "receipt"
    return f"the {_d(m['date'])} {m['currency']} {abs(m['amount']):,.2f} {what} on {m['xero']} ({m['payee']})"


def _doc_refs(key: str, d: dict, paid_by: str) -> str:
    recognised = brc.SHORT.get(d.get("entity") or key, "")
    return _refs(d.get("contact", ""), _dy(d["date"]), f"{d['currency']} {d['due']:,.2f}", f"paid {paid_by}",
                 f"recognised {recognised}", d.get("number") or d.get("reference") or "")


def check_items(check: dict, kept_ids: set) -> tuple[list[dict], list[dict], list[dict]]:
    """From the check alone: open lines it pairs with a posted document that
    the list above did not find, "one of these" lines, and lines matched to
    the wrong bill. -> (reconcile items, wrong-match items, held back)"""
    add, wrong, held = [], [], []
    for key, e in check["entities"].items():
        ent = brc.SHORT[key]
        by_id = {m["id"]: m for m in e.get("_all", [])}
        seen_groups = set()
        for m in e["movements"]:
            g = m.get("goes_to") or {}
            if not m.get("verified"):
                if m["status"] not in ("TO MATCH", "AMBIGUOUS", "SUSPECT WRONG MATCH"):
                    continue
                held.append({"entity": key, "line": _move_words(m), "status": m["status"],
                             "why": f"{m['xero']} does not tie"})
                continue
            if m["id"] in kept_ids or not g:
                continue
            if m["status"] == "TO MATCH" and (m["confidence"] == "high" or g.get("kind") == "record"):
                if g["kind"] == "record":
                    rs = g["records"]
                    kind = rs[0]["kind"] if len(rs) == 1 else f"{len(rs)} records"
                    text = f"reconcile {_move_words(m)} to the {kind} already posted"
                    refs = _refs(", ".join(sorted({r['contact'] for r in rs if r['contact']})),
                                 _dy(rs[0]["date"]), " + ".join(f"{r['currency']} {abs(r['amount']):,.2f}" for r in rs),
                                 f"{'paid' if m['amount'] < 0 else 'received'} {ent}",
                                 ", ".join(a for r in rs for a in r.get("accounts", [])),
                                 ", ".join(r["number"] or r["reference"] for r in rs if r["number"] or r["reference"]))
                else:
                    ds = g["documents"]
                    text = f"reconcile {_move_words(m)} to its {'bill' if len(ds) == 1 else f'{len(ds)} bills together'}"
                    refs = " + ".join(_doc_refs(key, d, ent) for d in ds)
                add.append({"entity": key, "date": m["date"], "kind": "check", "text": text, "refs": refs,
                            "line": {"entity": key, "currency": m["currency"], "date": m["date"],
                                     "amount": round(m["amount"], 2), "payee": m["payee"]}})
            elif m["status"] == "AMBIGUOUS" and g.get("kind") == "document":
                group = tuple(sorted([m["id"]] + list(m.get("siblings") or [])))
                if group in seen_groups:
                    continue
                seen_groups.add(group)
                sibs = sorted((by_id[i] for i in group if i in by_id), key=lambda x: x["date"])
                dates = ", ".join(_d(s["date"]) for s in sibs[:-1]) + f" and {_d(sibs[-1]['date'])}"
                text = (f"one of these is still open: the {dates} {m['currency']} {abs(m['amount']):,.2f} "
                        f"{'payments' if m['amount'] < 0 else 'receipts'} on {m['xero']} ({m['payee']}). "
                        f"Reconcile the open one to its bill")
                add.append({"entity": key, "date": m["date"], "kind": "one of these", "text": text,
                            "refs": " + ".join(_doc_refs(key, d, ent) for d in g["documents"])})
            elif m["status"] == "SUSPECT WRONG MATCH":
                d = g["documents"][0]
                taken = g.get("taken_by", {})
                wrong.append({"entity": key, "date": m["date"], "kind": "wrong match",
                              "text": (f"{_move_words(m)} is open and its bill is marked paid by "
                                       f"{taken.get('line') or 'a payment'} on {taken.get('xero', '')}. "
                                       f"Undo that match, then reconcile this line to the bill"),
                              "refs": _doc_refs(key, d, ent)})
        for s in e.get("suspects", []):
            if not s.get("line"):
                continue
            mv = {"date": s["date"], "currency": s.get("currency") or "", "amount": s["amount"],
                  "xero": s["xero"], "payee": s["payee"]}
            if s["kind"] == "line matched to the wrong bill":
                ld = s["likely_doc"]
                text = (f"{_move_words(mv)} is matched to the wrong bill. Undo the match and reconcile it "
                        f"to the bill that fits")
                refs = _refs(_doc_refs(key, ld, ent), f"now on {s['bill']}" if s.get("bill") else "")
            else:
                # A rate that does not fit, with no other bill the line fits, is
                # not something the bookkeeper can act on: it goes to the admin.
                held.append({"entity": key, "line": _move_words(mv), "status": "check the match",
                             "why": _refs(f"matched to {s['contact']}", f"{s['doc_currency']} {abs(s['doc_amount']):,.2f}",
                                          s["bill"], s["why"].split(";")[0])})
                continue
            wrong.append({"entity": key, "date": s["date"], "kind": "wrong match", "text": text, "refs": refs})
    return add, wrong, held


def _same_target(item: dict, m: dict) -> bool:
    """The list's pairing and the check's name the same bill or record."""
    g = m.get("goes_to") or {}
    refs = item.get("refs", "")
    names = [d.get("number") or d.get("reference") for d in g.get("documents") or []]
    names += [r.get("number") or r.get("reference") for r in g.get("records") or []]
    return any(n and n in refs for n in names)


def apply_check(items: list[dict], check: dict) -> tuple[list[dict], list[dict], list[dict], list[str]]:
    """Keep only the items whose statement line the check holds open (TO MATCH)
    on a verified account; then add what the check finds on its own.
    -> (items, wrong matches, held back, dropped)"""
    idx: dict[tuple, list[dict]] = defaultdict(list)
    state: dict[tuple, list[dict]] = defaultdict(list)
    for key, e in check["entities"].items():
        for m in e.get("_all", []):
            k = (key, m.get("currency"), m["date"], round(m["amount"], 2))
            state[k].append(m)
            if m["state"] == "OPEN" and m.get("in_window"):
                idx[k].append(m)
    kept, dropped, kept_ids = [], [], set()
    for i in items:
        ln = i.get("line") or {}
        k = (ln.get("entity"), ln.get("currency"), ln.get("date"), ln.get("amount"))
        cands = [m for m in idx.get(k, []) if m["id"] not in kept_ids]
        cands.sort(key=lambda m: (m["status"] != "TO MATCH", not m.get("verified"),
                                  not brc.agree(set(m["words"]), brc.words(ln.get("payee", "")))))
        m = cands[0] if cands else None
        # Both sides must name the same bill or record: a pairing the check
        # does not make itself is dropped here and the check's own goes in
        # (check_items), so no looser rule elsewhere decides what the bookkeeper matches
        if m and m["status"] == "TO MATCH" and m.get("verified") and not _same_target(i, m):
            dropped.append(f"{brc.SHORT.get(ln.get('entity'), '')} · {i['text']} · the check pairs the line with something else")
            continue
        if m and m["status"] in ("TO MATCH", "AMBIGUOUS", "SUSPECT WRONG MATCH") and m.get("verified"):
            kept_ids.add(m["id"])
            if m["status"] == "AMBIGUOUS":
                i = {**i, "text": i["text"] + ". One of these: another line of this amount is already "
                                              "reconciled, so reconcile whichever is still open"}
            kept.append(i)
            continue
        if m:
            kept_ids.add(m["id"])
            # AWAITING INFORMATION: the check's agent saw a possible bill and
            # was not certain; the line is chased, never put to the bookkeeper
            why = "the account does not tie" if not m.get("verified") else brc._label(m).lower()
        else:
            seen = state.get(k, [])
            why = ("reconciled in Xero" if any(s["state"] == "RECONCILED" for s in seen)
                   else "the check has no such line")
        dropped.append(f"{brc.SHORT.get(ln.get('entity'), '')} · {i['text']} · {why}")
    add, wrong, held = check_items(check, kept_ids)
    out = kept + add
    out.sort(key=lambda i: (br.ORDER().index(i["entity"]), i["date"], i["text"]))
    wrong.sort(key=lambda i: (br.ORDER().index(i["entity"]), i["date"], i["text"]))
    return out, wrong, held, dropped


def build(store: dict, bill_lines: dict[str, dict], history: list[dict],
          xero: dict[str, dict]) -> list[dict]:
    items: list[dict] = []
    claimed: set = set()

    # 1. open bills the entity's own bank paid; one line paying several bills
    # of one supplier is one item
    for key in brc.ENTITIES:
        banks = xero.get(key, {}).get("banks", [])
        paid: dict[tuple, tuple[dict, list[dict]]] = {}
        for b in store["entities"].get(key, {}).get("bills", []):
            line = bill_lines.get(b["id"])
            if b.get("status") != "OPEN" or b.get("state") != "MATCH IN XERO" or not line:
                continue
            paid.setdefault(br._line_key(line), (line, []))[1].append(b)
        for lk, (line, bills) in paid.items():
            # In one currency the line has to be the bill to the cent: a pairing
            # the bills report made on the payee's name at another amount is a
            # question for a person, not a line to match
            if (line["currency"] == bills[0]["currency"]
                    and abs(abs(line["amount"]) - sum(b["due_amount"] for b in bills)) > 0.005):
                continue
            claimed.add(lk)
            bills.sort(key=lambda b: (b["date"], b["number"]))
            acct = xero_account_for(key, line, banks)
            first = bills[0]
            if len(bills) == 1:
                text = f"reconcile {_line_words(line, acct)} to its bill"
                amount = f"{first['currency']} {first['due_amount']:,.2f}"
            else:
                text = f"reconcile {_line_words(line, acct)} to its {len(bills)} bills together"
                amount = f"{first['currency']} " + " + ".join(f"{b['due_amount']:,.2f}" for b in bills)
            if line["currency"] != first["currency"]:
                text += f"; the bill is in {first['currency']} and the line in {line['currency']}"
            items.append({
                "entity": key, "date": line["date"], "kind": "bill", "line": _ref(line),
                "text": text,
                "refs": _refs(first["supplier"], _dy(first["date"]), amount,
                              f"paid {brc.SHORT[key]}", f"recognised {brc.SHORT[key]}",
                              ", ".join(b["number"] or b["reference"] for b in bills)),
            })

    # 2. spend money, receive money and transfer legs nobody reconciled
    for key in brc.ENTITIES:
        for e in xero.get(key, {}).get("entries", []):
            line = find_line(key, e, history, claimed)
            if not line:
                continue
            claimed.add(br._line_key(line))
            ent = brc.SHORT[key]
            if e["transfer"]:
                frm, to, ref = e["transfer"]
                text = f"reconcile {_line_words(line, e['account'])} to the transfer already posted"
                refs = _refs(f"from {frm} to {to}", _dy(e["date"]), f"{e['currency']} {e['amount']:,.2f}",
                             ent, ref)
            else:
                text = f"reconcile {_line_words(line, e['account'])} to the {e['kind']} already posted"
                refs = _refs(e["contact"], _dy(e["date"]), f"{e['currency']} {e['amount']:,.2f}",
                             f"{'paid' if e['spent'] else 'received'} {ent}", ", ".join(e["accounts"]),
                             e["reference"])
            items.append({"entity": key, "date": line["date"], "kind": e["kind"], "text": text, "refs": refs,
                          "line": _ref(line)})

    # 3. payments entered on a bank account and never reconciled: one to a
    # line first, then the rest of one day on one account against one line
    for key in brc.ENTITIES:
        pays = xero.get(key, {}).get("payments", [])
        left = []
        for p in pays:
            line = find_line(key, p, history, claimed)
            if not line:
                left.append(p)
                continue
            claimed.add(br._line_key(line))
            items.append({
                "entity": key, "date": line["date"], "kind": "payment", "line": _ref(line),
                "text": f"reconcile {_line_words(line, p['account'])} to the payment already entered on its bill",
                "refs": _refs(p["contact"], f"{p['doc_currency']} {p['doc_amount']:,.2f}",
                              f"{'paid' if p['spent'] else 'received'} {brc.SHORT[key]}", p["number"]),
            })
        groups: dict[tuple, list[dict]] = defaultdict(list)
        for p in left:
            groups[(p["account"], p["currency"], p["date"], p["spent"])].append(p)
        for (account, ccy, day, spent), group in groups.items():
            if len(group) < 2:
                continue
            total = {"spent": spent, "date": day, "amount": round(sum(p["amount"] for p in group), 2),
                     "account": account, "currency": ccy}
            line = find_line(key, total, history, claimed)
            if not line:
                continue
            claimed.add(br._line_key(line))
            contacts = sorted({p["contact"] for p in group if p["contact"]})
            items.append({
                "entity": key, "date": line["date"], "kind": "payment", "line": _ref(line),
                "text": f"reconcile {_line_words(line, account)} to the {len(group)} payments already "
                        f"entered that day, together",
                "refs": _refs(", ".join(contacts), " + ".join(f"{p['amount']:,.2f}" for p in group),
                              f"{'paid' if spent else 'received'} {brc.SHORT[key]}",
                              ", ".join(p["number"] for p in group if p["number"])),
            })

    items.sort(key=lambda i: (br.ORDER().index(i["entity"]), i["date"], i["text"]))
    return items


# ------------------------------------------------------------------- output

def by_entity(items: list[dict]) -> list[str]:
    out = []
    for key in brc.ENTITIES:
        rows = [i for i in items if i["entity"] == key]
        if not rows:
            continue
        out.append(f"\n*{brc.SHORT[key].lower()}*")
        out += [f"• {i['text']} · {i['refs']}" for i in rows]
    return out


def _split(items: list[dict]) -> tuple[list[dict], list[dict]]:
    wrong = [i for i in items if i.get("kind") == "wrong match"]
    return [i for i in items if i.get("kind") != "wrong match"], wrong


def manual_block(items: list[dict], as_of: str = "", held: list[dict] | None = None) -> str:
    """What a run appends to its MANUAL ITEMS thread. `held` is for the
    admin only: lines on accounts that do not tie, and matches whose amount
    does not fit with no other bill to point at. They never reach the DM."""
    rec, wrong = _split(items)
    head = "*reconcile on the bank feed*" + (f"\nas at {as_of}" if as_of else "")
    out = [head] + (by_entity(rec) or ["nothing to reconcile: no open bank line has its document posted"])
    if wrong:
        out += ["", "*matched to the wrong bill*"] + by_entity(wrong)
    if held:
        out += ["", "*for the admin, not sent to the bookkeeper*"]
        for key in brc.ENTITIES:
            rows = [h for h in held if h["entity"] == key]
            if rows:
                out += [f"_{brc.SHORT[key].lower()}_"] + [f"• {h['line']}, {h['status']} · {h['why']}" for h in rows]
    return "\n".join(out) + "\n"


def dm_text(items: list[dict], as_of: str = "") -> str:
    rec, wrong = _split(items)
    today = date.today()
    head = f"bank lines to reconcile, as at {as_of}" if as_of else f"bank lines to reconcile, {today.day} {today:%b %Y}"
    out = [head] + (by_entity(rec) or ["none"])
    if wrong:
        out += ["", "*matched to the wrong bill*"] + by_entity(wrong)
    return "\n".join(out)


def send_dm(items: list[dict], who: str = "", as_of: str = "") -> str:
    """One DM to `who` (a Slack member ID; default [accounts_payable]
    reconcile_notify, else unpaid_notify), unless they already have this exact
    list today, or the list is empty and so was the last one they were sent."""
    who = who or config.reconcile_notify()
    text = dm_text(items, as_of)
    digest = hashlib.sha256("\n".join(f"{i['text']} {i['refs']}" for i in items).encode()).hexdigest()
    today = date.today().isoformat()
    last = json.loads(DM_STATE.read_text()) if DM_STATE.exists() else {}
    if last.get("hash") == digest and (last.get("date") == today or not items):
        return f"not sent to {who}: they already have this list ({last.get('date')})"
    uid = who
    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not uid or not token:
        return ("not sent: [accounts_payable] reconcile_notify and unpaid_notify are both blank" if not uid
                else f"NOT sent to {who}: SLACK_BOT_TOKEN not set (load .env)")
    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError
    client = WebClient(token=token)
    try:
        chunks = _chunks(text)
        for part in chunks:
            client.chat_postMessage(channel=uid, text=part)
    except SlackApiError as e:
        return f"NOT sent to {who}: {e.response.get('error')}"
    DM_STATE.write_text(json.dumps({"hash": digest, "date": today, "lines": len(items)}) + "\n")
    return f"sent {who} the reconcile list, {len(items)} lines"


def _chunks(text: str, limit: int = 3500) -> list[str]:
    out, cur = [], ""
    for line in text.splitlines(keepends=True):
        if cur and len(cur) + len(line) > limit:
            out.append(cur.rstrip())
            cur = ""
        cur += line
    if cur.strip():
        out.append(cur.rstrip())
    return out


def cmd_run(refresh: bool, dm: bool, dry_run: bool, check_cache: str | None = None) -> int:
    # The check first, fresh, every time: nobody is asked to reconcile a line
    # it does not hold open on a verified account. If it cannot run, nothing
    # is written and nobody is messaged.
    try:
        check = brc.run_check(None, cache=Path(check_cache) if check_cache else None, write=not dry_run)
    except Exception as e:                                         # noqa: BLE001
        print(f"FAILED: the bank reconcile check did not run ({type(e).__name__}: {e}). "
              "Nothing written and no DM sent: the list is only built on a fresh check.")
        return 1
    as_of = brc._local(check["built"])
    store, bill_lines = assess_bills(refresh)
    if refresh and not dry_run:
        br.save_store(store)
    history, upto = br.bank_history()
    xero = {}
    for key in brc.ENTITIES:
        print(f"{brc.SHORT[key]}: reading what is unreconciled in Xero ...", flush=True)
        xero[key] = pull_unreconciled(key)
    candidates = build(store, bill_lines, history, xero)
    items, wrong, held, dropped = apply_check(candidates, check)
    built = _now()
    counts = defaultdict(int)
    for i in items:
        counts[brc.SHORT[i["entity"]]] += 1
    print(f"\n{len(items)} bank lines to reconcile, as at {as_of}"
          + (f" ({', '.join(f'{k} {v}' for k, v in counts.items())})" if items else "")
          + f" · bank to {upto}")
    for i in items:
        print(f"  {brc.SHORT[i['entity']]:9} • {i['text']} · {i['refs']}")
    if wrong:
        print(f"{len(wrong)} matched to the wrong bill")
        for i in wrong:
            print(f"  {brc.SHORT[i['entity']]:9} • {i['text']} · {i['refs']}")
    if dropped:
        print(f"{len(dropped)} dropped by the check (not open in Xero, or on an account that does not tie)")
        for d in dropped:
            print(f"  - {d}")
    if held:
        print(f"{len(held)} held back for the admin: account does not tie, or a match to check")
        for h in held:
            print(f"  - {brc.SHORT[h['entity']]} · {h['line']} · {h['status']} · {h['why']}")
    out_items = items + wrong
    if dry_run:
        print("DRY RUN: nothing written, nothing sent")
        print("\n" + dm_text(out_items, as_of))
        return 0
    from accounting_agent import readonly
    readonly.refuse("writing the reconcile list and the register")
    OUT_JSON.write_text(json.dumps({"built": built, "as_of": as_of, "check_built": check["built"],
                                    "bank_upto": upto, "items": out_items, "held": held, "dropped": dropped},
                                   indent=1, ensure_ascii=False) + "\n")
    OUT_MD.write_text(manual_block(out_items, as_of, held))
    print(f"written {OUT_JSON.relative_to(ROOT)} and {OUT_MD.relative_to(ROOT)}")
    import outstanding
    outstanding.set_reconcile(
        [{"entity": brc.SHORT[i["entity"]], "text": i["text"], "refs": i["refs"]} for i in out_items], built)
    print(f"register: reconcile section replaced, {len(out_items)} lines")
    print(outstanding.publish(outstanding.load(), quiet=True))
    if dm:
        print(send_dm(out_items, as_of=as_of))
    return 0


def cmd_show() -> int:
    if not OUT_JSON.exists():
        sys.exit("no list yet; run `reconcile_list.py run` first")
    data = json.loads(OUT_JSON.read_text())
    print(f"built {data['built']} · {len(data['items'])} lines")
    print(manual_block(data["items"], data.get("as_of", ""), data.get("held")))
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="build the list, replace the register section, DM the bookkeeper")
    r.add_argument("--no-refresh", action="store_true", help="use the bills report's store as it stands")
    r.add_argument("--no-dm", action="store_true")
    r.add_argument("--dry-run", action="store_true")
    r.add_argument("--check-cache", help="cache folder for the bank reconcile check (development only)")
    sub.add_parser("show", help="the last list built")
    a = p.parse_args(argv)
    if a.cmd == "run":
        return cmd_run(not a.no_refresh, not a.no_dm, a.dry_run, a.check_cache)
    return cmd_show()


if __name__ == "__main__":
    raise SystemExit(main())
