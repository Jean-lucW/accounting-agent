#!/usr/bin/env python
"""The reconstructed bank feed: what is still unreconciled, per entity.

Xero has a bank feed and the API cannot read it. So we keep our own: one file
per entity, every bank account, one line per statement line that has NOT been
reconciled and has NOT been reported as reconciled by hand.

    data/bankfeed/<slug>.md            SERVER ONLY, gitignored

It is the agent's standing to-do list for cash. Every bookkeeping or
reconciliation run reads it BEFORE doing anything else, so it starts knowing
what cash is unaccounted for rather than discovering it. A line leaves the file
only when the run has posted the bill / spend money / transfer that accounts
for it, or when an admin says it was reconciled by hand.

Each account heading carries `last checked`, the point the bank was last
queried. A refresh only needs transactions after that stamp; everything before
it is already in the file or already gone from it.

Line format, pipe-separated:

    <date> | <payee> | spent|received | <CCY> <amount> | <who> | <staged> | <note>

  date     ISO, the statement date
  payee    as the bank wrote it, not cleaned up: that is what you match on
  who      cardholder or payer where the feed names one
  staged   coding already prepared in the Xero UI by a human, or blank. It is
           a strong hint and not an instruction; the invoice still decides.
  note     chases sent, why it is stuck, anything a later run needs

Usage:

    python scripts/bankfeed.py status                       counts, per account
    python scripts/bankfeed.py list <entity> [account]      the lines
    python scripts/bankfeed.py stamp <entity> <account> [iso]
    python scripts/bankfeed.py remove <entity> "<unique substring>" "<why>"
    python scripts/bankfeed.py add <entity> <account> "<line>"
    python scripts/bankfeed.py refresh <entity>|all [--dry-run] [--since ISO]

<entity> is an entity key, slug, alias or short name from config/group.toml.

`remove` prints the line it took out and refuses on anything but exactly one
match, so a run cannot quietly drop the wrong payment.

`refresh` is the bank half of the loop. For every bank account configured in
config/group.toml with an API provider (revolut or mercury, read-only clients,
server only) it asks the bank for every SETTLED movement after each account's
`last checked` stamp, appends the ones not already in the file, and moves the
stamp to now. It never removes a line: only a posted transaction or an admin
does that, via `remove`. Pending / declined / reverted / failed rows are not
cash and are skipped; a pending card payment is picked up by the refresh after
it settles. Each added line carries `rev:<id>` or `mer:<id>` at the end of its
note so a later refresh cannot add it twice. Run it at the START of every
bookkeeping or reconciliation run and again at the END, once the bills are
posted, so the file the next run opens is current. A `csv` bank account has no
API: it is fed by its statement CSV only (docs/BANKING.md), and `refresh`
leaves it alone.
"""

from __future__ import annotations

import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FEED = ROOT / "data" / "bankfeed"
sys.path.insert(0, str(ROOT / "src"))

from accounting_agent import config  # noqa: E402

CASH_STATES = {"revolut": {"completed"}, "mercury": {"sent"}}
LOOKBACK_DAYS = 10   # a card payment settles 1-3 days after purchase; be generous

HEAD = re.compile(r"^## (.+?)(?: · last checked (\S+))?\s*$")


def _entities() -> dict[str, config.Entity]:
    """slug -> Entity, in config order."""
    return {e.slug: e for e in config.entities()}


def _sources(slug: str) -> list[config.BankAccount]:
    """The API-fed bank accounts of one entity."""
    return [b for b in _entities()[slug].bank_accounts if b.has_api]


def _slug(entity: str) -> str:
    try:
        return config.entity(entity).slug
    except config.ConfigError as exc:
        sys.exit(str(exc))


def _path(entity: str) -> Path:
    return FEED / f"{_slug(entity)}.md"


def _parse(path: Path) -> list[tuple[str, str, list[str]]]:
    """-> [(account, last_checked, [lines])]"""
    if not path.exists():
        return []
    out: list[tuple[str, str, list[str]]] = []
    for raw in path.read_text().splitlines():
        m = HEAD.match(raw)
        if m:
            out.append((m.group(1), m.group(2) or "never", []))
        elif out and raw.strip() and "|" in raw:
            out[-1][2].append(raw.rstrip())
    return out


def _render(title: str, blocks) -> str:
    body = [f"# Bank feed: {title}", "",
            "Unreconciled bank statement lines. Read by every bookkeeping and",
            "reconciliation run before it starts. A line goes when the cash is",
            "accounted for in Xero, or when an admin says it was matched by hand.",
            "Format and rules: docs/BANKING.md.", ""]
    for account, stamp, lines in blocks:
        body.append(f"## {account} · last checked {stamp}")
        body.extend(lines)
        body.append("")
    return "\n".join(body).rstrip() + "\n"


def _title(path: Path) -> str:
    ent = _entities().get(path.stem)
    return ent.title if ent else path.stem


def cmd_status() -> int:
    if not FEED.exists():
        print("no bank feed yet")
        return 0
    total = 0
    for path in sorted(FEED.glob("*.md")):
        blocks = _parse(path)
        n = sum(len(b[2]) for b in blocks)
        total += n
        print(f"{path.stem:10} {n:4} outstanding")
        for account, stamp, lines in blocks:
            if lines:
                oldest = min(l.split("|")[0].strip() for l in lines)
                print(f"    {account:24} {len(lines):3}  oldest {oldest}  checked {stamp}")
            else:
                print(f"    {account:24}   -  clear            checked {stamp}")
    print(f"{'total':10} {total:4} outstanding")
    return 0


def cmd_list(entity: str, account: str | None) -> int:
    for acct, stamp, lines in _parse(_path(entity)):
        if account and account.lower() not in acct.lower():
            continue
        print(f"## {acct} · last checked {stamp}")
        for l in lines:
            print("  " + l)
    return 0


def cmd_stamp(entity: str, account: str, iso: str | None) -> int:
    stamp = iso or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    path = _path(entity)
    blocks = _parse(path)
    hit = [i for i, b in enumerate(blocks) if account.lower() in b[0].lower()]
    if len(hit) != 1:
        sys.exit(f"{len(hit)} accounts match {account!r}; be specific")
    acct, _, lines = blocks[hit[0]]
    blocks[hit[0]] = (acct, stamp, lines)
    path.write_text(_render(_title(path), blocks))
    print(f"{path.stem} · {acct} · checked {stamp}")
    return 0


def cmd_remove(entity: str, needle: str, why: str) -> int:
    path = _path(entity)
    blocks = _parse(path)
    hits = [(i, j, l) for i, b in enumerate(blocks)
            for j, l in enumerate(b[2]) if needle in l]
    if not hits:
        sys.exit(f"no line matches {needle!r}")
    if len(hits) > 1:
        print(f"{len(hits)} lines match {needle!r}; narrow it:", file=sys.stderr)
        for _, _, l in hits:
            print("  " + l, file=sys.stderr)
        sys.exit(1)
    i, j, line = hits[0]
    acct, stamp, lines = blocks[i]
    blocks[i] = (acct, stamp, lines[:j] + lines[j + 1:])
    path.write_text(_render(_title(path), blocks))
    print(f"removed from {path.stem} · {acct}\n  {line}\n  reason: {why}")
    return 0


def cmd_add(entity: str, account: str, line: str) -> int:
    path = _path(entity)
    blocks = _parse(path)
    hit = [i for i, b in enumerate(blocks) if account.lower() in b[0].lower()]
    labels = {b.label.lower(): b.label for b in _entities()[_slug(entity)].bank_accounts}
    if not hit and account.lower() in labels:
        # the first line of an account with no API (provider csv): open its
        # heading under the label config/group.toml gives it
        blocks.append((labels[account.lower()], "never", []))
        hit = [len(blocks) - 1]
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if len(hit) != 1:
        sys.exit(f"{len(hit)} accounts match {account!r}; be specific")
    i = hit[0]
    acct, stamp, lines = blocks[i]
    if line in lines:
        print("already present, nothing added")
        return 0
    blocks[i] = (acct, stamp, sorted(lines + [line]))
    path.write_text(_render(_title(path), blocks))
    print(f"added to {path.stem} · {acct}\n  {line}")
    return 0


# ---- refresh from the bank API -------------------------------------------

def _parse_stamp(stamp: str) -> datetime | None:
    if not stamp or stamp == "never":
        return None
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None


def _account_heading(bank: config.BankAccount, line: dict) -> str:
    """Map an API account to the heading the feed file uses: the bank's own
    account name (a Revolut one prefixed with its currency, "GBP Main"),
    else the configured label (config.statement_account_name)."""
    return bank.account_heading(line)


def _feed_line(source: str, line: dict) -> tuple[str, str]:
    """-> (line text, id marker)"""
    amount = float(line.get("amount") or 0)
    direction = "spent" if amount < 0 else "received"
    payee = (line.get("merchant") or line.get("description") or line.get("counterparty") or "").strip()
    payee = " ".join(payee.split())
    who = (line.get("cardholder") or "").strip()
    marker = f"{'rev' if source == 'revolut' else 'mer'}:{(line.get('id') or '')[:8]}"
    text = f"{line['date']} | {payee} | {direction} | {line.get('currency')} {abs(amount):.2f} | {who} | | {marker}"
    return text, marker


def _fingerprint(text: str) -> tuple:
    parts = [p.strip() for p in text.split("|")]
    if len(parts) < 4:
        return (text,)
    return (parts[0], parts[2], parts[3], parts[1].lower()[:10])


def _client(source: str, slug: str):
    if source == "revolut":
        from accounting_agent.revolut import RevolutClient
        return RevolutClient(slug)
    from accounting_agent.mercury import MercuryClient
    return MercuryClient(slug)


def _settled_at(source: str, line: dict) -> datetime | None:
    raw = line.get("completed_at")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def cmd_refresh(entity: str, dry_run: bool, since: str | None) -> int:
    key = _slug(entity)
    sources = _sources(key)
    if not sources:
        print(f"{key}: no bank account with an API; fed by statement CSV only, nothing to refresh")
        return 0
    path = _path(key)
    blocks = _parse(path)
    now = datetime.now(timezone.utc)
    stamps = {acct: _parse_stamp(stamp) for acct, stamp, _ in blocks}
    forced = _parse_stamp(since) if since else None
    known = {s for s in stamps.values() if s}
    floor = forced or (min(known) if known else now - timedelta(days=LOOKBACK_DAYS))
    from_date = (floor - timedelta(days=LOOKBACK_DAYS)).date().isoformat()

    # One entity may hold several API accounts (a Revolut and a Mercury, say).
    lines: list[tuple[config.BankAccount, dict]] = []
    for bank in sources:
        rows = _client(bank.provider, bank.slug).statement_lines(from_date=from_date)
        if bank.currency:
            rows = [r for r in rows if (r.get("currency") or "").upper() == bank.currency.upper()]
        lines.extend((bank, r) for r in rows)

    existing_markers = {m for _, _, ls in blocks for l in ls for m in re.findall(r"\b(?:rev|mer):[0-9a-f]{8}\b", l)}
    existing_prints = {_fingerprint(l) for _, _, ls in blocks for l in ls}
    added: list[tuple[str, str]] = []
    skipped_state = 0
    for bank, line in lines:
        source = bank.provider
        if (line.get("state") or "").lower() not in CASH_STATES[source]:
            skipped_state += 1
            continue
        if abs(float(line.get("amount") or 0)) < 0.005:
            continue   # zero-value card authorisation, not a movement
        heading = _account_heading(bank, line)
        cutoff = forced or stamps.get(heading)
        settled = _settled_at(source, line)
        if cutoff and settled and settled <= cutoff:
            continue
        if cutoff and not settled and line.get("date", "") <= cutoff.date().isoformat():
            continue
        text, marker = _feed_line(source, line)
        if marker in existing_markers or _fingerprint(text) in existing_prints:
            continue
        existing_markers.add(marker); existing_prints.add(_fingerprint(text))
        added.append((heading, text))
        idx = next((i for i, b in enumerate(blocks) if b[0].lower() == heading.lower()), None)
        if idx is None:
            blocks.append((heading, "never", []))
            idx = len(blocks) - 1
        acct, stamp, ls = blocks[idx]
        blocks[idx] = (acct, stamp, sorted(ls + [text]))

    stamp_now = now.strftime("%Y-%m-%dT%H:%MZ")
    api_headings = {_account_heading(b, l) for b, l in lines}
    blocks = [(acct, stamp_now if (acct in api_headings or acct in stamps) else stamp, ls)
              for acct, stamp, ls in blocks]

    tag = "DRY RUN " if dry_run else ""
    via = ", ".join(f"{b.provider}:{b.slug}" for b in sources)
    print(f"{tag}{key} · {via} · {len(lines)} api rows since {from_date}, "
          f"{skipped_state} not settled, {len(added)} new")
    for heading, text in added:
        print(f"  + [{heading}] {text}")
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_text(_render(_title(path), blocks))
        path.chmod(0o600)
        print(f"  stamps -> {stamp_now}")
    return 0


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0 if argv else 2
    cmd, rest = argv[0], argv[1:]
    if cmd == "status":
        return cmd_status()
    if cmd == "list" and rest:
        return cmd_list(rest[0], rest[1] if len(rest) > 1 else None)
    if cmd == "stamp" and len(rest) >= 2:
        return cmd_stamp(rest[0], rest[1], rest[2] if len(rest) > 2 else None)
    if cmd == "remove" and len(rest) >= 2:
        return cmd_remove(rest[0], rest[1], rest[2] if len(rest) > 2 else "")
    if cmd == "add" and len(rest) >= 3:
        return cmd_add(rest[0], rest[1], rest[2])
    if cmd == "refresh" and rest:
        dry = "--dry-run" in rest
        since = rest[rest.index("--since") + 1] if "--since" in rest and rest.index("--since") + 1 < len(rest) else None
        targets = [s for s in _entities() if _sources(s)] if rest[0] == "all" else [rest[0]]
        rc = 0
        for t in targets:
            rc |= cmd_refresh(t, dry, since)
        return rc
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
