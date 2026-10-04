#!/usr/bin/env python
"""Post the prepayments, accruals and deposits findings to the agent's channel
(config.slack().channel_id).

The scheduled `balance-reports` job (deploy/run-scheduled.sh, after the daily
chain) refreshes the three balance reports and then runs this. A scheduled run
has no admin thread, so the channel is the only place it reports
(docs/COMMS.md).

Two messages, like every other run: a top-level message that is only
`<COMPANY> REPORTS RUN` and the local date and time, and the report itself as
the first reply in that thread. The top-level line is what the channel is
scrolled for; the detail belongs under it.

Each report carries a section per report and, inside it, what it leaves with a
person split the way docs/COMMS.md splits it everywhere: `queries` for a
question waiting on an answer, `manual` for an action waiting on a pair of
hands. Built from the stores, no Xero call.

    .venv/bin/python scripts/balance_reports_summary.py            # post
    .venv/bin/python scripts/balance_reports_summary.py --dry-run  # print
"""

from __future__ import annotations

import datetime as dt
import os
import sys
import zoneinfo
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from balance_report import (ACCRUALS, DEPOSITS, PREPAYMENTS, config,  # noqa: E402
                            load_store, summary_sections)

SLACK_CHUNK = 3500


def local_zone() -> zoneinfo.ZoneInfo:
    try:
        return zoneinfo.ZoneInfo(config.company("timezone", "UTC") or "UTC")
    except zoneinfo.ZoneInfoNotFoundError:
        return zoneinfo.ZoneInfo("UTC")


def drive_folder() -> str:
    """Where the workbooks are published, as a person would name it."""
    d = config.drive_settings()
    if not d.get("publish_folder_id"):
        return ""
    sub = (d.get("subfolders") or {}).get("reports", "")
    return f"the Drive publish folder / {sub}" if sub else "the Drive publish folder"


def headline(started: dt.datetime) -> str:
    """The top-level message, and nothing else in it (docs/COMMS.md)."""
    # %Z gives the zone abbreviation for the date, summer or winter
    return (f"{config.company_name().upper()} REPORTS RUN "
            f"({started.astimezone(local_zone()):%d %b %Y, %H:%M %Z})")


def report() -> str:
    """The detail, which goes in the headline's thread."""
    parts: list[str] = []
    counts = {"queries": 0, "manual": 0}
    body: list[str] = []
    for spec in (PREPAYMENTS, ACCRUALS, DEPOSITS):
        store = load_store(spec)
        head, queries, manual = summary_sections(spec, store)
        counts["queries"] += len(queries)
        counts["manual"] += len(manual)
        body.append("")
        body.append(f"*{spec.key}*")
        body.append(f"• {head}")
        published = (store.get("published") or {}).get("line", "")
        if published and "NOT published" in published:
            # Republishing the Drive copy is a person's job, so it is a manual
            # line, not a footnote nobody acts on.
            manual = manual + [f"• Drive copy not updated: {published}"]
            counts["manual"] += 1
        for title, lines in (("queries", queries), ("manual", manual)):
            if not lines:
                continue
            body.append(f"  *{title}*")
            body += [f"  {line}" for line in lines]

    tally = " · ".join(f"{n} {name}" for name, n in counts.items() if n)
    parts.append("reports · prepayments, accruals, deposits"
                 + (f" · {tally}" if tally else " · nothing to look at"))
    parts += body
    parts.append("")
    folder = drive_folder()
    parts.append((f"workbooks in {folder}, or " if folder else "workbooks: ")
                 + "deploy/fetch-report.sh prepayments|accruals|deposits")
    return "\n".join(parts)


def _chunks(text: str, limit: int = SLACK_CHUNK) -> list[str]:
    out, cur = [], ""
    for line in text.split("\n"):
        if cur and len(cur) + len(line) + 1 > limit:
            out.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        out.append(cur)
    return out


def main(argv: list[str]) -> int:
    if argv and argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    started = dt.datetime.now(dt.timezone.utc)
    top, detail = headline(started), report()
    if "--dry-run" in argv:
        print(top)
        print()
        print(detail)
        return 0
    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not token:
        sys.exit("SLACK_BOT_TOKEN not set (load .env)")
    channel = config.slack().channel_id
    if not channel:
        sys.exit("no Slack channel: set [slack] channel_id in config/group.toml")
    from slack_sdk import WebClient
    web = WebClient(token=token)
    posted = web.chat_postMessage(channel=channel, text=top)
    for chunk in _chunks(detail):
        web.chat_postMessage(channel=channel, text=chunk, thread_ts=posted["ts"])
    print(f"posted the reports summary to {channel}, thread {posted['ts']}, "
          f"{detail.count(chr(10)) + 1} lines")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
