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
  note     chases sent, why it is stuck, anything a later run needs. A line
           waiting on a document the run fetches itself (a filed payslip or
           statement) says `awaiting fetch`, which keeps the daily
           bookkeeping gate (scripts/daily_chain.py) from skipping the session

Usage:

    python scripts/bankfeed.py status                       counts, per account
    python scripts/bankfeed.py list <entity>|all [account] [--grep TEXT] [--since ISO]
    python scripts/bankfeed.py find <amount>|"<text>" [--ccy CCY] [--near ISO] [--days N]
    python scripts/bankfeed.py stamp <entity> <account> [iso]
    python scripts/bankfeed.py remove <entity> "<unique substring>" "<why>"
    python scripts/bankfeed.py add <entity> <account> "<line>"
    python scripts/bankfeed.py refresh <entity>|all [--dry-run] [--since ISO] [--brief]
    python scripts/bankfeed.py xero-check <entity>|all ["<substring>"] [--out FILE] [--summary] [--only V,V]
    python scripts/bankfeed.py xero-check --from FILE [--summary] [--only V,V]

<entity> is an entity key, slug, alias or short name from config/group.toml.

Output is sized to the step that reads it (a run's context keeps every line
it prints). `status` is the opening read; `find` answers "which entity paid
this" for one amount or payee across every entity's feed, without printing the
rest; `list --grep` narrows one feed. `refresh --brief` prints the count per
entity and not each new line, for the closing refresh.

`xero-check` asks Xero, read-only, what already accounts for each line, in
every entity, matching on the bank-side amount of payments, the amount, the
amount converted from another currency, and the payee by name inside the date
window (src/accounting_agent/xero/matching.py). The amount alone never
decides: a claim held in another currency, or dated at the receipt weeks
before it was paid, is invisible to an exact-amount search. Verdicts: PAID IN
XERO, LIKELY, CANDIDATES, NOTHING FOUND, TRANSFER. Only NOTHING FOUND is a
chase, a query or a spend money; a line reported as paid in Xero is removed,
not chased. Run it before chasing a line, before raising a query that says a
line has nothing behind it, and before posting spend money for a line.
`--out FILE --summary` writes the whole check to FILE and prints only the
counts per verdict; `--from FILE --only "PAID IN XERO"` then prints one
verdict's lines, with every candidate under them, from the saved file and
without asking Xero again. `--only` filters whole lines by verdict and never
trims a line's candidates.

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


def _keys(entity: str) -> list[str]:
    """Entity slugs whose feed file exists; every one for `all`."""
    if entity == "all":
        return [k for k in _entities() if (FEED / f"{k}.md").exists()]
    return [_slug(entity)]


def cmd_list(entity: str, account: str | None, grep: str | None = None,
             since: str | None = None) -> int:
    many = entity == "all"
    for key in _keys(entity):
        for acct, stamp, lines in _parse(FEED / f"{key}.md"):
            if account and account.lower() not in acct.lower():
                continue
            keep = [l for l in lines
                    if (not grep or grep.lower() in l.lower())
                    and (not since or l.split("|")[0].strip() >= since)]
            if (grep or since) and not keep:
                continue
            print(f"## {key + ' · ' if many else ''}{acct} · last checked {stamp}")
            for l in keep:
                print("  " + l)
    return 0


LINE = re.compile(r"^(\d{4}-\d{2}-\d{2}) \| (.*?) \| (spent|received) \| ([A-Z]{3}) ([\d.,]+) \| (.*?) \|")


def cmd_find(needle: str, ccy: str | None, near: str | None, days: int) -> int:
    """One amount or one payee, across every entity's feed. An amount matches
    to the cent; text matches anywhere in the line. `--near` keeps lines
    within `--days` of a date (card payments settle a few days after)."""
    from datetime import date
    try:
        amount = float(needle.replace(",", ""))
    except ValueError:
        amount = None
    hits = 0
    for key in _keys("all"):
        for acct, _, lines in _parse(FEED / f"{key}.md"):
            for l in lines:
                m = LINE.match(l)
                if amount is not None:
                    if not m or abs(float(m.group(5).replace(",", "")) - amount) >= 0.005:
                        continue
                elif needle.lower() not in l.lower():
                    continue
                if ccy and (not m or m.group(4) != ccy.upper()):
                    continue
                if near and m:
                    try:
                        gap = abs((date.fromisoformat(m.group(1)) - date.fromisoformat(near)).days)
                    except ValueError:
                        gap = 0
                    if gap > days:
                        continue
                hits += 1
                print(f"{key} · {acct} · {l}")
    if not hits:
        print(f"no feed line matches {needle!r}" + (f" in {ccy.upper()}" if ccy else "")
              + (f" within {days} days of {near}" if near else ""))
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


def cmd_add(entity: str, account: str, line: str, create: bool = False) -> int:
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
    if not hit and create:
        # a heading the bank reconcile check names from a configured account's
        # own API name, before `refresh` has opened it
        blocks.append((account, "never", []))
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
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)   # a bare date is UTC


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


def cmd_refresh(entity: str, dry_run: bool, since: str | None, brief: bool = False) -> int:
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

    # Keyed by account as well as id: Revolut gives both legs of an exchange
    # between two of the entity's own accounts one transaction id, so an id
    # alone would drop the second leg as already present.
    existing_markers = {(acct.lower(), m) for acct, _, ls in blocks for l in ls
                        for m in re.findall(r"\b(?:rev|mer):[0-9a-f]{8}\b", l)}
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
        if (heading.lower(), marker) in existing_markers or _fingerprint(text) in existing_prints:
            continue
        existing_markers.add((heading.lower(), marker)); existing_prints.add(_fingerprint(text))
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
    if not brief:
        for heading, text in added:
            print(f"  + [{heading}] {text}")
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_text(_render(_title(path), blocks))
        path.chmod(0o600)
        print(f"  stamps -> {stamp_now}")
    return 0


# ---- what Xero already holds for each line --------------------------------

VERDICTS = ("PAID IN XERO", "LIKELY", "CANDIDATES", "NOTHING FOUND", "TRANSFER")


def _check_blocks(entity: str, needle: str | None) -> list[tuple[str, str, str, list[str]]]:
    """-> [(verdict, entity slug, verdict line, candidate lines)] for every
    feed line, in feed order. One Xero window per entity for the whole feed."""
    from datetime import date
    from accounting_agent.xero import XeroClient
    from accounting_agent.xero.matching import candidates, describe, load_window, name_tokens

    rows = []
    for key in _keys(entity):
        for acct, _, lines in _parse(FEED / f"{key}.md"):
            for l in lines:
                m = LINE.match(l)
                if m and (not needle or needle in l):
                    rows.append((key, acct, l, m))
    if not rows:
        return []
    days = [date.fromisoformat(m.group(1)) for *_, m in rows]
    lo, hi = min(days), max(days)
    windows = {}
    for slug, ent in _entities().items():    # every entity: staff spend is often booked elsewhere
        if ent.bank_check_mode == "off":
            continue
        try:
            windows[slug] = load_window(XeroClient(ent.xero_name), lo, hi)
        except Exception as e:                # noqa: BLE001
            print(f"!! {slug}: Xero window would not load: {type(e).__name__}: {e}", file=sys.stderr)
    out = []
    for key, acct, l, m in rows:
        day, payee, direction, ccy, amt, _who = m.groups()
        amt = amt.replace(",", "")
        if payee.startswith("Exchanged "):
            out.append(("TRANSFER", key, f"[TRANSFER] {key} · {acct} · {l}", []))
            continue
        names = name_tokens(payee)        # the payee, never the cardholder
        found = []
        for ekey, win in windows.items():
            for c in candidates(win, amount=float(amt), currency=ccy, day=date.fromisoformat(day),
                                names=names, spent=direction == "spent", own=ekey == key):
                found.append((ekey, c))
        found.sort(key=lambda x: (-x[1]["strength"], x[0] != key, x[1]["gap_days"]))
        top = found[0][1] if found else None
        verdict = ("NOTHING FOUND" if not top
                   else "PAID IN XERO" if {"bank amount", "name"} <= set(top["reasons"])
                   else "LIKELY" if top["strength"] >= 3 else "CANDIDATES")
        cands = [f"    {ekey:9} {describe(c)}"
                 for ekey, c in found[:5 if top and top["strength"] >= 3 else 3]]
        out.append((verdict, key, f"[{verdict}] {key} · {acct} · {l}", cands))
    return out


def _read_blocks(path: Path) -> list[tuple[str, str, str, list[str]]]:
    """A saved xero-check output, back into its blocks."""
    out: list[tuple[str, str, str, list[str]]] = []
    head = re.compile(r"^\[(" + "|".join(VERDICTS) + r")\] (\S+) · ")
    for raw in path.read_text().splitlines():
        m = head.match(raw)
        if m:
            out.append((m.group(1), m.group(2), raw, []))
        elif out and raw.startswith("    "):
            out[-1][3].append(raw)
    return out


def _summary(blocks) -> list[str]:
    def counts(bs):
        return " · ".join(f"{v} {n}" for v in VERDICTS if (n := sum(1 for b in bs if b[0] == v)))
    lines = [f"xero-check · {len(blocks)} lines · {counts(blocks) or 'none'}"]
    for key in dict.fromkeys(b[1] for b in blocks):
        lines.append(f"  {key:9} {counts([b for b in blocks if b[1] == key])}")
    return lines


def cmd_xero_check(entity: str | None, needle: str | None, *, out: str | None = None,
                   source: str | None = None, only: str | None = None, summary: bool = False) -> int:
    """For each feed line, what in any entity's Xero may already account for
    it. Verdict per line: PAID IN XERO (a payment, or one day's payments to one
    person, in this entity for the line's exact bank amount and to the payee
    the line names), LIKELY (the exact or converted amount with the payee's
    name), CANDIDATES (the amount alone or the name alone), NOTHING FOUND
    (only then is it a chase, a query or a spend money). Exchanges between the
    entity's own accounts are transfers and are not looked up.

    `out` saves the whole check; `source` reads a saved one instead of Xero;
    `only` keeps whole lines of the named verdicts; `summary` prints counts."""
    wanted = None
    if only:
        wanted = {v.strip().upper() for v in only.split(",") if v.strip()}
        bad = wanted - set(VERDICTS)
        if bad:
            sys.exit(f"unknown verdict(s) {sorted(bad)}; one of: {', '.join(VERDICTS)}")
    if source:
        blocks = _read_blocks(Path(source))
        if entity and entity != "all":
            blocks = [b for b in blocks if b[1] == _slug(entity)]
        if needle:
            blocks = [b for b in blocks if needle in b[2]]
    else:
        blocks = _check_blocks(entity or "all", needle)
    if not blocks:
        print("no feed lines to check")
        return 0
    if out:
        Path(out).write_text("".join(b[2] + "\n" + "".join(c + "\n" for c in b[3]) for b in blocks))
    if summary:
        print("\n".join(_summary(blocks)))
        if out:
            print(f'saved {out} · read one verdict: bankfeed.py xero-check --from {out} --only "<VERDICT>"')
    if wanted is not None or not summary:
        for verdict, _, line, cands in blocks:
            if wanted is None or verdict in wanted:
                print(line)
                for c in cands:
                    print(c)
    return 0


def _flag(rest: list[str], name: str, has_value: bool = True):
    """Take `--name [value]` out of rest; -> value, True, or None."""
    if name not in rest:
        return None
    i = rest.index(name)
    if not has_value:
        del rest[i]
        return True
    if i + 1 >= len(rest):
        sys.exit(f"{name} needs a value")
    value = rest[i + 1]
    del rest[i:i + 2]
    return value


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0 if argv else 2
    cmd, rest = argv[0], list(argv[1:])
    if cmd == "status":
        return cmd_status()
    if cmd == "list" and rest:
        grep, since = _flag(rest, "--grep"), _flag(rest, "--since")
        return cmd_list(rest[0], rest[1] if len(rest) > 1 else None, grep, since)
    if cmd == "find" and rest:
        ccy, near = _flag(rest, "--ccy"), _flag(rest, "--near")
        days = int(_flag(rest, "--days") or 5)
        return cmd_find(rest[0], ccy, near, days)
    if cmd == "stamp" and len(rest) >= 2:
        return cmd_stamp(rest[0], rest[1], rest[2] if len(rest) > 2 else None)
    if cmd == "remove" and len(rest) >= 2:
        return cmd_remove(rest[0], rest[1], rest[2] if len(rest) > 2 else "")
    if cmd == "add" and len(rest) >= 3:
        return cmd_add(rest[0], rest[1], rest[2])
    if cmd == "refresh" and rest:
        dry = bool(_flag(rest, "--dry-run", has_value=False))
        brief = bool(_flag(rest, "--brief", has_value=False))
        since = _flag(rest, "--since")
        targets = [s for s in _entities() if _sources(s)] if rest[0] == "all" else [rest[0]]
        rc = 0
        for t in targets:
            rc |= cmd_refresh(t, dry, since, brief)
        return rc
    if cmd == "xero-check":
        out, source, only = _flag(rest, "--out"), _flag(rest, "--from"), _flag(rest, "--only")
        summary = bool(_flag(rest, "--summary", has_value=False))
        if not rest and not source:
            print(__doc__)
            return 2
        return cmd_xero_check(rest[0] if rest else None, rest[1] if len(rest) > 1 else None,
                              out=out, source=source, only=only, summary=summary)
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
