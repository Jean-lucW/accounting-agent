"""What in Xero already accounts for a bank line, found without trusting the
amount alone.

An exact-amount search misses anything recorded in another currency. A staff
expense claim in a USD-based entity is held in USD and paid from a GBP account:
the GBP figure the statement shows exists only as the payment's `BankAmount`,
and the claim itself is dated at the receipt, weeks before the payment. A run
that searched for the GBP amount on the same day found nothing, called the line
undocumented, and a second record of the same reimbursement was posted.

So a line is matched four ways, and any one of them is a candidate:

  bank amount   a payment whose BankAmount (the bank account's own currency)
                equals the line: the line is already paid in Xero
  amount        a bill, invoice or bank transaction whose total equals the line
                in the line's currency
  converted     a total in ANOTHER currency that comes within TOLERANCE of the
                line once converted at the rates the window's own documents
                carry
  name          the payee named on the line appears in the contact, reference
                or line descriptions, whatever the amount, inside the date
                window. Not the cardholder: the cardholder is whoever spent,
                and their name is on every receipt they have ever filed

Read-only: every call here is a GET. One window per entity costs about five
calls (Organisation, bank accounts, Payments, BankTransactions, Invoices,
paged), so a whole feed is checked for the price of a handful of requests
rather than one per line.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from statistics import median

TOLERANCE = 0.03          # card and bank FX spreads stay inside 3%
NEAR_DAYS = 7             # a payment or spend is dated within a week of its line
DOCS_BEFORE_DAYS = 60     # a bill or claim can be dated weeks before it is paid

# Words in a payee or description that name nobody. The group's own words (its
# entity names, short codes, aliases and countries) are added at run time by
# group_words(), so nothing company-specific lives in this list.
STOP = {
    "to", "from", "the", "and", "for", "payment", "payments", "transfer", "card",
    "expenses", "expense", "claims", "claim", "main", "spent", "received", "staff",
    "reimbursement", "invoice", "bill", "www", "com", "revolut", "mercury",
    "exchanged", "usd", "gbp", "eur", "cloud", "services", "service", "group",
    "global", "international", "company", "store", "shop", "market", "online",
    "digital", "systems", "technologies", "solutions", "pay", "holdings",
}


@lru_cache(maxsize=4)
def _group_words(path_key: str) -> frozenset[str]:
    from accounting_agent import config
    words: set[str] = set()
    try:
        texts = [config.company_name()]
        for e in config.entities():
            texts += [e.xero_name, e.short, e.key, e.country, *e.aliases]
        for text in texts:
            words.update(re.findall(r"[a-z][a-z0-9'-]+", (text or "").lower()))
        words |= set(config.legal_form_words())
    except config.ConfigError:
        pass
    return frozenset(w.strip("'-") for w in words if w)


def group_words() -> frozenset[str]:
    """Words that name the group itself (entity names, short codes, aliases,
    countries, legal forms) and so identify no payee."""
    from accounting_agent import config
    return _group_words(str(config.config_path()))


def _xdate(d: date) -> str:
    return f"DateTime({d.year},{d.month:02d},{d.day:02d})"


def when(rec: dict) -> str:
    """ISO date of a Xero record: DateString where the endpoint gives one, the
    /Date(ms)/ epoch otherwise (Payments and ManualJournals lists)."""
    if rec.get("DateString"):
        return rec["DateString"][:10]
    m = re.search(r"\d+", rec.get("Date") or "")
    return datetime.fromtimestamp(int(m.group()) / 1000, timezone.utc).date().isoformat() if m else ""


def name_tokens(*texts: str | None) -> set[str]:
    skip = STOP | group_words()
    out: set[str] = set()
    for t in texts:
        for w in re.findall(r"[a-z][a-z0-9'-]+", (t or "").lower()):
            w = w.strip("'-")
            if len(w) >= 3 and w not in skip:
                out.add(w)
    return out


def load_window(client, lo: date, hi: date) -> dict:
    """Every payment, live bank transaction and document in one entity between
    `lo - DOCS_BEFORE_DAYS` and `hi + NEAR_DAYS`, plus the base currency and the
    rate each foreign currency carried in that window."""
    org = client.get("Organisation")["Organisations"][0]
    base = org.get("BaseCurrency") or ""
    accounts = {a["AccountID"]: (a.get("Name") or a.get("Code") or "", a.get("CurrencyCode") or base)
                for a in client.get("Accounts", where='Type=="BANK"').get("Accounts", [])}
    start, end = lo - timedelta(days=DOCS_BEFORE_DAYS), hi + timedelta(days=NEAR_DAYS)
    rng = f"Date>={_xdate(start)} AND Date<={_xdate(end)}"
    payments = [p for p in client.get_all("Payments", where=rng)
                if p.get("Status") != "DELETED"]
    txns = [t for t in client.get_all("BankTransactions", where=rng)
            if t.get("Status") == "AUTHORISED"]
    docs = [i for i in client.get_all("Invoices", where=rng)
            if i.get("Status") not in ("DELETED", "VOIDED")]
    rates: dict[str, list[float]] = {}
    for r in txns + docs:
        ccy, rate = r.get("CurrencyCode"), r.get("CurrencyRate")
        if ccy and ccy != base and rate:
            rates.setdefault(ccy, []).append(float(rate))
    return {"base": base, "accounts": accounts, "payments": payments, "txns": txns,
            "docs": docs, "rates": {k: median(v) for k, v in rates.items()}}


def _convert(window: dict, total: float, ccy: str, rate, to_ccy: str) -> float | None:
    """`total` in `ccy` expressed in `to_ccy`. Xero's CurrencyRate is foreign
    units per unit of base, so base = total / rate."""
    if ccy == to_ccy:
        return total
    base = window["base"]
    in_base = total if ccy == base else (total / float(rate) if rate else None)
    if in_base is None:
        return None
    if to_ccy == base:
        return in_base
    r = window["rates"].get(to_ccy)
    return in_base * r if r else None


def _days(a: str, b: date) -> int:
    try:
        return abs((date.fromisoformat(a) - b).days)
    except ValueError:
        return 9999


def candidates(window: dict, *, amount: float, currency: str, day: date,
               names: set[str], spent: bool = True, own: bool = True,
               limit: int = 8) -> list[dict]:
    """Ranked records in one entity's window that may account for a bank line
    of `currency amount` on `day`, strongest first. `own` is False for every
    entity but the one whose bank the line is on: another entity's cash cannot
    be this line, so only its documents are looked at (a bill recognised there
    and paid from here is the bill-payments case).

    Strength: 4 the exact amount, or a payment's (or one day's payments')
    exact bank amount from an account in the line's currency, with the payee's
    name; 3 that bank amount without the name, or a converted amount with it;
    2 the exact amount alone; 1 the name alone. A
    converted amount with no name is coincidence and is dropped. Each
    candidate carries `reasons` from {"bank amount", "amount", "converted",
    "name"}."""
    out: list[dict] = []

    def consider(kind, rec, d, total, ccy, rate, contact, texts, *, bank_amount=None,
                 bank_account="", reconciled=None, number=""):
        if not d:
            return
        offset = (date.fromisoformat(d) - day).days
        gap = abs(offset)
        # Cash records sit by the line; bills and claims sit before it.
        if kind.endswith(("payment", "payments", "bank transaction")):
            if gap > NEAR_DAYS:
                return
        elif not -DOCS_BEFORE_DAYS <= offset <= NEAR_DAYS:
            return
        reasons, conv = [], None
        if bank_amount is not None and abs(float(bank_amount) - amount) < 0.005:
            reasons.append("bank amount")
        elif ccy == currency and abs(float(total) - amount) < 0.005:
            reasons.append("amount")
        elif ccy != currency and bank_amount is None:
            # Only across currencies: in the same currency a near miss is
            # another purchase, and a payment's BankAmount is the bank's figure.
            conv = _convert(window, float(total), ccy, rate, currency)
            if conv and abs(conv - amount) <= TOLERANCE * amount:
                reasons.append("converted")
        named = bool(names & name_tokens(contact, *texts))
        if named:
            reasons.append("name")
        if ("bank amount" in reasons or "amount" in reasons) and named:
            strength = 4
        elif "bank amount" in reasons or ("converted" in reasons and named):
            strength = 3
        elif "amount" in reasons:
            strength = 2
        elif named and reasons == ["name"] and gap <= 31:
            strength = 1
        else:
            return
        out.append({
            "strength": strength, "reasons": reasons, "record": kind, "date": d,
            "contact": contact, "number": number, "total": float(total), "currency": ccy,
            "converted": round(conv, 2) if conv else None, "bank_amount": bank_amount,
            "bank_account": bank_account, "reconciled": reconciled,
            "status": rec.get("Status"), "gap_days": gap,
            "id": rec.get("PaymentID") or rec.get("BankTransactionID") or rec.get("InvoiceID"),
            "detail": "; ".join(t for t in texts if t)[:160],
        })

    if own:
        for p in window["payments"]:
            ptype = p.get("PaymentType") or ""
            if spent != ptype.startswith(("ACCPAY", "ARCREDIT", "AROVER", "ARPREPAY")):
                continue
            inv = p.get("Invoice") or p.get("CreditNote") or {}
            acct, acct_ccy = window["accounts"].get((p.get("Account") or {}).get("AccountID"), ("", ""))
            consider("payment", p, when(p), p.get("Amount") or 0, inv.get("CurrencyCode") or "",
                     p.get("CurrencyRate"), (inv.get("Contact") or {}).get("Name"),
                     [inv.get("InvoiceNumber"), p.get("Reference")],
                     bank_amount=p.get("BankAmount") if acct_ccy == currency else None,
                     bank_account=acct, reconciled=p.get("IsReconciled"),
                     number=inv.get("InvoiceNumber") or "")
        # One transfer can settle several claims or bills: Xero holds one
        # payment each, the bank one line. Same day, same bank account, the
        # bank amounts summing to the line exactly, per person first (several
        # people are often reimbursed the same day), then the whole day.
        groups: dict[tuple, list[dict]] = {}
        for p in window["payments"]:
            ptype = p.get("PaymentType") or ""
            aid = (p.get("Account") or {}).get("AccountID")
            if (spent == ptype.startswith(("ACCPAY", "ARCREDIT", "AROVER", "ARPREPAY"))
                    and window["accounts"].get(aid, ("", ""))[1] == currency
                    and p.get("BankAmount") is not None):
                who = ((p.get("Invoice") or p.get("CreditNote") or {}).get("Contact") or {}).get("ContactID")
                groups.setdefault((when(p), aid, who), []).append(p)
                groups.setdefault((when(p), aid, None), []).append(p)
        seen: set[frozenset] = set()
        for (d, aid, _who), ps in groups.items():
            ids = frozenset(p.get("PaymentID") for p in ps)
            if len(ps) < 2 or ids in seen or abs(sum(float(p["BankAmount"]) for p in ps) - amount) >= 0.005:
                continue
            seen.add(ids)
            contacts = sorted({((p.get("Invoice") or {}).get("Contact") or {}).get("Name") or "" for p in ps})
            numbers = [(p.get("Invoice") or {}).get("InvoiceNumber") or "" for p in ps]
            consider(f"{len(ps)} payments", ps[0], d, sum(float(p.get("Amount") or 0) for p in ps),
                     ((ps[0].get("Invoice") or {}).get("CurrencyCode") or ""), None,
                     ", ".join(contacts), numbers, bank_amount=round(sum(float(p["BankAmount"]) for p in ps), 2),
                     bank_account=window["accounts"][aid][0],
                     reconciled=all(p.get("IsReconciled") for p in ps), number=", ".join(sorted(set(numbers))))
        for t in window["txns"]:
            if spent != (t.get("Type") or "").startswith("SPEND"):
                continue
            consider("bank transaction", t, when(t), t.get("Total") or 0, t.get("CurrencyCode") or "",
                     t.get("CurrencyRate"), (t.get("Contact") or {}).get("Name"),
                     [t.get("Reference")] + [li.get("Description") for li in t.get("LineItems") or []],
                     bank_account=(t.get("BankAccount") or {}).get("Name", ""),
                     reconciled=t.get("IsReconciled"), number=t.get("Reference") or "")
    for i in window["docs"]:
        if spent != (i.get("Type") == "ACCPAY"):
            continue
        consider("bill" if i.get("Type") == "ACCPAY" else "invoice", i, when(i),
                 i.get("Total") or 0, i.get("CurrencyCode") or "", i.get("CurrencyRate"),
                 (i.get("Contact") or {}).get("Name"),
                 [i.get("InvoiceNumber"), i.get("Reference")]
                 + [li.get("Description") for li in i.get("LineItems") or []],
                 number=i.get("InvoiceNumber") or "")
    out.sort(key=lambda c: (-c["strength"], c["gap_days"]))
    return out[:limit]


def describe(c: dict) -> str:
    """One line per candidate, references at the end."""
    money = f"{c['currency']} {c['total']:,.2f}"
    if c.get("bank_amount") is not None and "payment" in c["record"]:
        money += f", bank {c['bank_amount']:,.2f}"
    if c.get("converted") and "converted" in c["reasons"]:
        money += f" (about {c['converted']:,.2f})"
    rec = " · reconciled" if c.get("reconciled") else ""
    acct = f" · {c['bank_account']}" if c.get("bank_account") else ""
    return (f"{c['record']} {c['date']} · {c.get('contact') or ''} · {money}{acct}{rec}"
            f" · {c.get('status') or ''} · {c.get('number') or ''}  [{', '.join(c['reasons'])}]")
