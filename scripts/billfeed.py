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
    python scripts/billfeed.py list <entity>             the lines
    python scripts/billfeed.py refresh <entity>|all [--dry-run]
    python scripts/billfeed.py match [--days N]          open bills vs bank feed lines

<entity> is an entity key, slug, alias or short name from config/group.toml.

`match` reads the two feeds already on disk (it never calls a bank) and
prints, per open bill, every bank feed line of the same currency and amount
dated on or after the invoice date (less accounts_payable.card_lag_days for
card timing), grouped as SAME ENTITY (the user matches by hand; nothing to do)
and CROSS ENTITY (the `bill-payments` skill posts the payer's spend money to
the pair's intercompany loan and reports the bill). A name score counts shared
words between the bank payee and the supplier; 0 means amount-and-date only,
look before acting.
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


def cmd_list(entity: str) -> int:
    stamp, lines = _parse(_path(entity))
    print(f"# pulled {stamp}")
    for l in lines:
        print("  " + l)
    return 0


# ---- match open bills against the bank feed ------------------------------

# Legal forms (config.LEGAL_FORMS plus [accounts_payable]
# generic_supplier_words) and the words every bank line carries.
_STOP = config.legal_form_words() | {"the", "and", "card", "payment"}


def _tokens(s: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", s.lower()) if len(t) >= 3 and t not in _STOP}


def _bank_lines() -> list[tuple[str, str, str, str, str, float, str]]:
    """-> [(entity, account, date, payee, direction, amount, ccy)] from data/bankfeed."""
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
                out.append((path.stem, account, parts[0], parts[1], parts[2], float(amt.replace(",", "")), ccy))
            except ValueError:
                continue
    return out


def cmd_match(days: int) -> int:
    bank = [b for b in _bank_lines() if b[4] == "spent"]
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
            for ent, acct, bdate, payee, _, bamt, bccy in bank:
                if bccy != ccy or abs(bamt - amt) > 0.005 or bdate < floor or (since and bdate < since):
                    continue
                score = len(_tokens(payee) & _tokens(supplier))
                row = (key, l, ent, acct, bdate, payee, score)
                (same if ent == key else cross).append(row)

    def show(title, rows):
        print(f"== {title}: {len(rows)}")
        for key, l, ent, acct, bdate, payee, score in sorted(rows, key=lambda r: (-r[6], r[4])):
            parts = [p.strip() for p in l.split("|")]
            print(f"  bill {_short(key):9} {parts[0]} | {parts[1]} | {parts[2]} | {parts[3]} | {parts[4]} | {parts[7]}")
            print(f"    paid {_short(ent):9} {bdate} | {acct} | {payee} | name score {score}")
    show("SAME ENTITY (user matches by hand, nothing to post)", same)
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
        return cmd_list(rest[0])
    if cmd == "refresh" and rest:
        dry = "--dry-run" in rest
        targets = list(_entities()) if rest[0] == "all" else [rest[0]]
        rc = 0
        for t in targets:
            rc |= cmd_refresh(t, dry)
        return rc
    if cmd == "match":
        days = int(rest[rest.index("--days") + 1]) if "--days" in rest else 0
        return cmd_match(days)
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
