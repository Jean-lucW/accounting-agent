#!/usr/bin/env python
"""The bill feed: every AUTHORISED, unpaid bill per entity, pulled from Xero.

The bank feed (`scripts/bankfeed.py`) is the cash that has not reached Xero.
The bill feed is the other side: the accounts payable that has not been paid.
Read together they answer the question a bookkeeping run could not answer
before: which open bill has in fact been paid, and from which entity's bank.

    data/billfeed/<slug>.md            SERVER ONLY, gitignored

Xero is the source for this file. `refresh` pulls every ACCPAY bill with
Status AUTHORISED and AmountDue > 0 and rewrites the file as a snapshot, so
nothing is inferred and nothing has to be removed by hand: a bill leaves the
feed when Xero shows it paid. Refresh it early in every bookkeeping run and
again right before the bill-payments check, so bills posted this run are in.

Line format, pipe-separated:

    <date> | <supplier> | <CCY> <amount due> | <account> | <invoice number> | <reference> | <due> | xid:<8>

  date      the invoice date
  supplier  the Xero contact name
  account   the account name(s) the bill lines are coded to
  xid       the first 8 characters of the Xero InvoiceID, for matching only.
            Never put it in a message: COMMS.md, the reference is the number
            Xero shows, never a GUID.

Usage:

    python scripts/billfeed.py status                    counts, per entity
    python scripts/billfeed.py list <entity>|all [--grep TEXT]   the lines
    python scripts/billfeed.py refresh <entity>|all [--dry-run]
    python scripts/billfeed.py match [--days N] [--cross] [--summary] [--offline]   open bills vs bank feed lines

<entity> is an entity key, slug, alias or short name from config/group.toml.

`list all --grep TEXT` prints only the open bills, in any entity, whose line
holds TEXT (a supplier, a number, an amount), so a run looking for one bill
never prints the whole feed. `match --cross` prints only the CROSS ENTITY
pairs, the ones the bill-payments check acts on; `match --summary` prints the
two counts and nothing else.

`match` reads the two feeds already on disk and prints, per open bill, every
bank feed line of the same currency and amount dated on or after the invoice
date (less accounts_payable.card_lag_days for card timing), grouped as SAME
ENTITY (a person matches by hand; nothing to do) and CROSS ENTITY (the
`bill-payments` skill posts the payer's spend money to the pair's
intercompany loan and reports the bill). A name score counts shared words
between the bank payee and the supplier; 0 means amount-and-date only, look
before acting.

A bill in another currency than the bank line is an EXACT match when the
Revolut leg's own `bill_amount` and `bill_currency` equal the bill's AmountDue
and currency to the cent: the bank converted the bill itself. The feed file
does not carry those two fields, so `match` reads the leg again from the
read-only Revolut API for the spent lines that need it (`--offline` skips
that). The pair prints the payer's bank amount and the rate that produces it,
which the report line gives the person who reconciles the bill.
"""

from __future__ import annotations

import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FEED = ROOT / "data" / "billfeed"
BANKFEED = ROOT / "data" / "bankfeed"
sys.path.insert(0, str(ROOT / "src"))

from accounting_agent import config  # noqa: E402


def _entities() -> dict[str, config.Entity]:
    """slug -> Entity, in config order."""
    return {e.slug: e for e in config.entities()}


def _card_lag_days() -> int:
    return int(config.accounts_payable().get("card_lag_days", 3))


def _slug(entity: str) -> str:
    try:
        return config.entity(entity).slug
    except config.ConfigError as exc:
        sys.exit(str(exc))


def _short(slug: str) -> str:
    ent = _entities().get(slug)
    return ent.short if ent else slug


def _path(entity: str) -> Path:
    return FEED / f"{_slug(entity)}.md"


def _date(value) -> str:
    """Xero dates arrive as ISO strings or '/Date(ms+0000)/'; return YYYY-MM-DD."""
    if not value:
        return ""
    s = str(value)
    m = re.search(r"/Date\((\d+)", s)
    if m:
        return datetime.fromtimestamp(int(m.group(1)) / 1000, tz=timezone.utc).date().isoformat()
    return s[:10]


def _client(key: str):
    # Exact Xero organisation name: one org name can be a substring of another,
    # so a fuzzy match is never used.
    from accounting_agent.xero import XeroClient
    return XeroClient(_entities()[key].xero_name)


def _render(key: str, stamp: str, lines: list[str]) -> str:
    body = [f"# Bill feed: {_entities()[key].title} · pulled {stamp}", "",
            "AUTHORISED, unpaid bills as Xero holds them. A snapshot: refresh, never",
            "edit. Read alongside data/bankfeed by every bookkeeping run; the",
            "bill-payments skill acts on a bill whose payment left another entity's",
            "bank. Format and rules: docs/BANKING.md, the bill feed.", ""]
    body.extend(lines)
    return "\n".join(body).rstrip() + "\n"


def _parse(path: Path) -> tuple[str, list[str]]:
    if not path.exists():
        return "never", []
    stamp, lines = "never", []
    for raw in path.read_text().splitlines():
        m = re.match(r"^# Bill feed: .*· pulled (\S+)", raw)
        if m:
            stamp = m.group(1)
        elif raw.strip() and raw.count("|") >= 6 and not raw.startswith("#"):
            lines.append(raw.rstrip())
    return stamp, lines


def _bill_line(bill: dict, names: dict[str, str]) -> str:
    codes = []
    for li in bill.get("LineItems") or []:
        c = li.get("AccountCode")
        if c and c not in codes:
            codes.append(c)
    accounts = "; ".join(names.get(c, c) for c in codes) or "-"
    supplier = " ".join(((bill.get("Contact") or {}).get("Name") or "").split())
    ccy = bill.get("CurrencyCode") or ""
    due = float(bill.get("AmountDue") or 0)
    xid = f"xid:{(bill.get('InvoiceID') or '')[:8]}"
    return (f"{_date(bill.get('Date'))} | {supplier} | {ccy} {due:.2f} | {accounts} | "
            f"{bill.get('InvoiceNumber') or ''} | {bill.get('Reference') or ''} | "
            f"{_date(bill.get('DueDate'))} | {xid}")


def cmd_refresh(entity: str, dry_run: bool) -> int:
    key = _slug(entity)
    from accounting_agent.xero.purchases import get_bills
    c = _client(key)
    bills = get_bills(c, status="AUTHORISED", unpaid_only=True)
    chart = c.get("Accounts").get("Accounts", [])
    names = {a.get("Code"): a.get("Name") for a in chart if a.get("Code")}
    lines = sorted(_bill_line(b, names) for b in bills)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    tag = "DRY RUN " if dry_run else ""
    print(f"{tag}{key} · {len(lines)} open bills")
    if dry_run:
        for l in lines:
            print("  " + l)
        return 0
    FEED.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = _path(key)
    path.write_text(_render(key, stamp, lines))
    path.chmod(0o600)
    print(f"  written {path.relative_to(ROOT)} · pulled {stamp}")
    return 0


def cmd_status() -> int:
    if not FEED.exists():
        print("no bill feed yet")
        return 0
    total = 0
    for key in _entities():
        stamp, lines = _parse(FEED / f"{key}.md")
        total += len(lines)
        by_ccy: dict[str, float] = {}
        for l in lines:
            ccy, amt = l.split("|")[2].strip().split(" ", 1)
            by_ccy[ccy] = by_ccy.get(ccy, 0.0) + float(amt)
        sums = ", ".join(f"{k} {v:,.2f}" for k, v in sorted(by_ccy.items()))
        print(f"{key:10} {len(lines):4} open  pulled {stamp}  {sums}")
    print(f"{'total':10} {total:4} open")
    return 0


def cmd_list(entity: str, grep: str | None = None) -> int:
    keys = list(_entities()) if entity == "all" else [_slug(entity)]
    hits = 0
    for key in keys:
        stamp, lines = _parse(FEED / f"{key}.md")
        keep = [l for l in lines if not grep or grep.lower() in l.lower()]
        hits += len(keep)
        if grep and not keep:
            continue
        print(f"# {_short(key) + ' · ' if entity == 'all' else ''}pulled {stamp}")
        for l in keep:
            print("  " + l)
    if grep and not hits:
        print(f"no open bill matches {grep!r}")
    return 0


# ---- match open bills against the bank feed ------------------------------

# Legal forms (config.LEGAL_FORMS plus [accounts_payable]
# generic_supplier_words) and the words every bank line carries.
_STOP = config.legal_form_words() | {"the", "and", "card", "payment"}


def _tokens(s: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", s.lower()) if len(t) >= 3 and t not in _STOP}


MARKER = re.compile(r"\b(?:rev|mer):[0-9a-f]{8}\b")


def _bank_lines() -> list[tuple[str, str, str, str, str, float, str, str]]:
    """-> [(entity, account, date, payee, direction, amount, ccy, marker)] from data/bankfeed."""
    out = []
    if not BANKFEED.exists():
        return out
    head = re.compile(r"^## (.+?)(?: · last checked (\S+))?\s*$")
    for path in sorted(BANKFEED.glob("*.md")):
        account = ""
        for raw in path.read_text().splitlines():
            m = head.match(raw)
            if m:
                account = m.group(1)
                continue
            parts = [p.strip() for p in raw.split("|")]
            if len(parts) < 4 or not re.match(r"\d{4}-\d{2}-\d{2}", parts[0]):
                continue
            try:
                ccy, amt = parts[3].split(" ", 1)
                out.append((path.stem, account, parts[0], parts[1], parts[2], float(amt.replace(",", "")), ccy,
                            (MARKER.findall(raw) or [""])[0]))
            except ValueError:
                continue
    return out


def _revolut_legs(rows: list[tuple]) -> dict[str, dict[str, list[dict]]]:
    """The Revolut statement lines behind the feed's spent lines, read-only,
    per entity slug and keyed by the 8-character marker the feed carries."""
    from datetime import date as _date
    out: dict[str, dict[str, list[dict]]] = {}
    for slug, ent in _entities().items():
        mine = [r for r in rows if r[0] == slug and r[7].startswith("rev:")]
        banks = [b for b in ent.bank_accounts if b.provider == "revolut" and b.has_api]
        if not mine or not banks:
            continue
        frm = (_date.fromisoformat(min(r[2] for r in mine)) - timedelta(days=3)).isoformat()
        idx: dict[str, list[dict]] = {}
        from accounting_agent.revolut import RevolutClient
        for slug_ in sorted({b.slug for b in banks}):
            for leg in RevolutClient(slug_).statement_lines(from_date=frm):
                idx.setdefault(f"rev:{(leg.get('id') or '')[:8]}", []).append(leg)
        out[slug] = idx
    return out


def exact_cross_currency(bill_ccy: str, due: float, line_ccy: str, line_amount: float,
                         legs: list[dict]) -> dict | None:
    """The bill is in another currency than the bank line, and one of the
    Revolut legs behind the line carries the bill's own amount and currency to
    the cent: the bank converted the bill itself. -> the payer's bank amount
    and the rate to enter, or None."""
    if not bill_ccy or bill_ccy == line_ccy:
        return None
    for leg in legs:
        if (leg.get("currency") == line_ccy
                and abs(abs(float(leg.get("amount") or 0)) - line_amount) < 0.005
                and leg.get("bill_currency") == bill_ccy
                and leg.get("bill_amount") is not None
                and abs(abs(float(leg["bill_amount"])) - due) < 0.005):
            return {"bank_amount": line_amount, "bank_currency": line_ccy,
                    "rate": round(line_amount / due, 6) if due else None}
    return None


def pairs(days: int = 0, legs: dict | None = None, bank=None) -> tuple[list[tuple], list[tuple]]:
    """Every open bill paired with a spent bank line, split into (same entity,
    cross entity). Each row is (entity, bill line, paying entity, account,
    date, payee, name score, how) where `how` says why it is a match."""
    bank = [b for b in (bank if bank is not None else _bank_lines()) if b[4] == "spent"]
    since = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat() if days else ""
    lag = _card_lag_days()
    same, cross = [], []
    for key in _entities():
        _, lines = _parse(FEED / f"{key}.md")
        for l in lines:
            parts = [p.strip() for p in l.split("|")]
            date, supplier, amount_s = parts[0], parts[1], parts[2]
            ccy, amt = amount_s.split(" ", 1)
            amt = float(amt)
            floor = (datetime.fromisoformat(date) - timedelta(days=lag)).date().isoformat() if date else ""
            for ent, acct, bdate, payee, _, bamt, bccy, marker in bank:
                if bdate < floor or (since and bdate < since):
                    continue
                how = ""
                if bccy == ccy and abs(bamt - amt) <= 0.005:
                    how = "same currency and amount"
                elif bccy != ccy and legs and marker:
                    hit = exact_cross_currency(ccy, amt, bccy, bamt, (legs.get(ent) or {}).get(marker, []))
                    if hit:
                        how = (f"the card leg's own bill amount, {ccy} {amt:,.2f}: payer's bank {bccy} {bamt:,.2f}, "
                               f"rate {hit['rate']} {bccy} per {ccy}")
                if not how:
                    continue
                score = len(_tokens(payee) & _tokens(supplier))
                (same if ent == key else cross).append((key, l, ent, acct, bdate, payee, score, how))
    return same, cross


def cmd_match(days: int, cross_only: bool = False, summary: bool = False, offline: bool = False) -> int:
    legs = None
    if not offline:
        try:
            legs = _revolut_legs(_bank_lines())
        except Exception as e:                       # noqa: BLE001 - a bank outage costs the cross-currency rule only
            print(f"!! Revolut legs not read ({type(e).__name__}: {e}); cross-currency exact matches are not shown",
                  file=sys.stderr)
    same, cross = pairs(days, legs)

    def show(title, rows):
        print(f"== {title}: {len(rows)}")
        if summary:
            return
        for key, l, ent, acct, bdate, payee, score, how in sorted(rows, key=lambda r: (-r[6], r[4])):
            parts = [p.strip() for p in l.split("|")]
            print(f"  bill {_short(key):9} {parts[0]} | {parts[1]} | {parts[2]} | {parts[3]} | {parts[4]} | {parts[7]}")
            print(f"    paid {_short(ent):9} {bdate} | {acct} | {payee} | name score {score}"
                  + (f" | {how}" if how != "same currency and amount" else ""))
    if not cross_only:
        show("SAME ENTITY (a person matches by hand, nothing to post)", same)
    show("CROSS ENTITY (bill-payments skill: payer spend money to the intercompany loan, report the bill)", cross)
    return 0


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0 if argv else 2
    cmd, rest = argv[0], argv[1:]
    if cmd == "status":
        return cmd_status()
    if cmd == "list" and rest:
        grep = rest[rest.index("--grep") + 1] if "--grep" in rest and rest.index("--grep") + 1 < len(rest) else None
        return cmd_list(rest[0], grep)
    if cmd == "refresh" and rest:
        dry = "--dry-run" in rest
        targets = list(_entities()) if rest[0] == "all" else [rest[0]]
        rc = 0
        for t in targets:
            rc |= cmd_refresh(t, dry)
        return rc
    if cmd == "match":
        days = int(rest[rest.index("--days") + 1]) if "--days" in rest else 0
        return cmd_match(days, "--cross" in rest, "--summary" in rest, "--offline" in rest)
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
