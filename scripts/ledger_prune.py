#!/usr/bin/env python
"""Keep docs/bookkept/LEDGER.md a strict rolling window.

The ledger is the completeness index for the invoice intake: one line per
message received in the last week, so a run knows what earlier runs already
handled without re-reading the invoices. Anything older than the window (one
week by default) is stale by definition and is deleted here, entries and run
sections alike, so the file the next run reads whole stays small.

    python scripts/ledger_prune.py [--days 7] [--dry-run]

Rules:
  - the header (everything above "## Entries") is kept as is, its window
    line rewritten to the current setting
  - an entry line starts with its message date, YYYY-MM-DD; it is kept when
    that date is inside the window
  - a "## Run YYYY-MM-DD ..." section is kept or dropped whole by the date in
    its heading
  - an undated line inside the entries block is kept (never guess)
Run at the start of every bookkeeping run, before the ledger is read. The
ledger is gitignored runtime state: when it is missing (a fresh checkout) this
creates it with its header first.
"""

from __future__ import annotations

import re
import sys
from datetime import date, timedelta
from pathlib import Path

LEDGER = Path(__file__).resolve().parents[1] / "docs" / "bookkept" / "LEDGER.md"

# The ledger is runtime state on the server and gitignored; a fresh checkout
# has none, so the first run creates it with this header.
HEADER = """# Bookkept-invoices ledger (rolling 7 days)

Shared completeness memory for the daily invoice bookkeeping run. Any agent
running `xero-bills` MUST read this file at run start and append to it as it
posts. Protocol: `.claude/skills/xero-bills/SKILL.md` § "Rolling ledger".

Format: one line per intake message (email or Slack message), pipe-separated:

`msg datetime (UTC) | source | sender | docs seen/bookkept | counterparty | reference | entity | outcome`

- `docs seen/bookkept`: attachments (or distinct invoices) in the message
  against how many are now in Xero. A mismatch is outstanding work.
- `outcome`: `posted <ref>`, `already-in-xero`, `queried: <question>`,
  `not-a-bill: <route>`. Run-level lines (run footers, bill-payments checks,
  AP check, bank-feed notes, blocked lines, defect and watch lines) are
  free-form in the outcome field, e.g. "BLOCKED", "REGISTERED OI-0001",
  "check_no_phantom_payments.py = PASS", and serve as the run's own record.
- Each run opens its own `## Run YYYY-MM-DD` section under "Entries".
- Lines older than 7 days are pruned at each run start by
  `scripts/ledger_prune.py` (this header is kept).

## Entries
"""


def ensure_ledger(path: Path = LEDGER) -> bool:
    """Create the ledger with its header when it is missing. True if created."""
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(HEADER, encoding="utf-8")
    return True
DATE = re.compile(r"^(\d{4}-\d{2}-\d{2})")
RUN = re.compile(r"^## Run (\d{4}-\d{2}-\d{2})")


def prune(text: str, days: int, today: date) -> tuple[str, dict]:
    floor = (today - timedelta(days=days)).isoformat()
    lines = text.split("\n")
    try:
        start = next(i for i, l in enumerate(lines) if l.strip() == "## Entries")
    except StopIteration:
        sys.exit("LEDGER.md has no '## Entries' heading; not touching it")
    header = lines[: start + 1]
    header = [re.sub(r"\(rolling \d+ days\)", f"(rolling {days} days)", l) for l in header]
    header = [re.sub(r"older than \d+ days", f"older than {days} days", l) for l in header]
    out, stats = header[:], {"entries_kept": 0, "entries_dropped": 0, "runs_kept": 0, "runs_dropped": 0}
    keep_section = True
    for l in lines[start + 1:]:
        m = RUN.match(l)
        if m:
            keep_section = m.group(1) >= floor
            stats["runs_kept" if keep_section else "runs_dropped"] += 1
            if keep_section:
                out.append(l)
            continue
        if l.startswith("## "):          # any other heading: keep, reset
            keep_section = True
            out.append(l)
            continue
        if not keep_section:
            continue
        d = DATE.match(l)
        if d:
            if d.group(1) >= floor:
                stats["entries_kept"] += 1
                out.append(l)
            else:
                stats["entries_dropped"] += 1
        else:
            out.append(l)
    # collapse runs of blank lines left behind
    cleaned, blank = [], False
    for l in out:
        if l.strip() == "":
            if blank:
                continue
            blank = True
        else:
            blank = False
        cleaned.append(l)
    return "\n".join(cleaned).rstrip() + "\n", stats


def main(argv: list[str]) -> int:
    days = int(argv[argv.index("--days") + 1]) if "--days" in argv else 7
    dry = "--dry-run" in argv
    if not dry and ensure_ledger():
        print(f"{LEDGER.relative_to(LEDGER.parents[2])} created with its header")
    text = LEDGER.read_text(encoding="utf-8") if LEDGER.exists() else HEADER
    new, stats = prune(text, days, date.today())
    print(f"window {days} days: entries kept {stats['entries_kept']}, dropped {stats['entries_dropped']}; "
          f"run sections kept {stats['runs_kept']}, dropped {stats['runs_dropped']}; "
          f"{len(text):,} -> {len(new):,} bytes")
    if not dry and new != text:
        LEDGER.write_text(new, encoding="utf-8")
        print("written")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
