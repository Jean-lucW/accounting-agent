#!/usr/bin/env python
"""What is left of the Xero rate limits, and when the daily cap resets.

Xero enforces three limits per app/tenant and reports all three in the headers
of every response: 60 calls a minute per tenant, 5000 a day per tenant, 10000 a
minute across the app. Only the daily one matters to a scheduled run: a minute
limit clears while the session waits, the daily cap does not clear for hours,
and the client's own 429 retry sleeps at most 65 seconds, so a run that meets
the daily cap fails rather than waits.

This probes one cheap GET per entity and reads the headers raw, because a 429
raises inside the client and the headers are what carry the answer.

    .venv/bin/python scripts/xero_rate_limit.py             # table, all entities
    .venv/bin/python scripts/xero_rate_limit.py --quiet     # exit code only
    .venv/bin/python scripts/xero_rate_limit.py --retry-at  # epoch to retry at

Exit codes
    0   every entity has daily calls left
    3   at least one entity is at its daily cap; --retry-at prints the epoch
        second the earliest of them resets
    1   could not probe (no token, network, anything else)
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from accounting_agent import xero_auth  # noqa: E402
from accounting_agent.xero import client as xero_client  # noqa: E402

# A GET that returns one row whatever the entity holds, so the probe costs one
# call and no memory.
PROBE = "Organisation"

# Xero's own header names. The daily one is the only one worth deferring for.
H_DAY = "X-DayLimit-Remaining"
H_MIN = "X-MinLimit-Remaining"
H_APP = "X-AppMinLimit-Remaining"
H_PROBLEM = "X-Rate-Limit-Problem"


def probe(name: str, tenant_id: str) -> dict:
    """One GET. Returns what the headers say, never raises for a 429."""
    req = urllib.request.Request(
        xero_client.BASE_URL + PROBE,
        headers={
            "Authorization": f"Bearer {xero_auth.get_access_token()}",
            "xero-tenant-id": tenant_id,
            "Accept": "application/json",
        },
        method="GET",
    )
    try:
        with xero_auth._urlopen(req) as resp:
            h = dict(resp.headers)
            return {
                "entity": name,
                "status": resp.status,
                "day_remaining": _int(h.get(H_DAY)),
                "min_remaining": _int(h.get(H_MIN)),
                "app_remaining": _int(h.get(H_APP)),
                "problem": None,
                "retry_after": None,
            }
    except urllib.error.HTTPError as e:
        h = dict(e.headers)
        e.read()
        return {
            "entity": name,
            "status": e.code,
            "day_remaining": _int(h.get(H_DAY)),
            "min_remaining": _int(h.get(H_MIN)),
            "app_remaining": _int(h.get(H_APP)),
            # Xero names the limit that was hit: minute, daily or appminute.
            "problem": h.get(H_PROBLEM) if e.code == 429 else None,
            "retry_after": _int(h.get("Retry-After")),
        }


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def exhausted(row: dict) -> bool:
    """Is this entity out of daily calls? Only the daily cap defers a run."""
    if row["problem"] == "daily":
        return True
    return row["day_remaining"] == 0


def retry_epoch(row: dict, now: float) -> float:
    """When to come back. Retry-After when Xero gave one, else the next UTC
    midnight, which is when the daily counter rolls."""
    if row["retry_after"]:
        return now + row["retry_after"] + 60
    tomorrow = (int(now) // 86400 + 1) * 86400
    return tomorrow + 60


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--quiet", action="store_true", help="no output, exit code only")
    ap.add_argument("--retry-at", action="store_true",
                    help="print the epoch second to retry at, when capped")
    ap.add_argument("--json", action="store_true", help="machine-readable")
    args = ap.parse_args()

    try:
        known = xero_client.entities()
    except Exception as e:  # noqa: BLE001 - no token, no network, anything
        print(f"could not read the Xero connections: {e}", file=sys.stderr)
        return 1

    now = time.time()
    rows = []
    for name, tid in known.items():
        try:
            rows.append(probe(name, tid))
        except Exception as e:  # noqa: BLE001
            print(f"probe failed for {name}: {e}", file=sys.stderr)
            return 1

    capped = [r for r in rows if exhausted(r)]

    if args.retry_at:
        if capped:
            print(int(min(retry_epoch(r, now) for r in capped)))
        return 3 if capped else 0

    if args.json:
        print(json.dumps({"rows": rows, "capped": [r["entity"] for r in capped]}, indent=2))
    elif not args.quiet:
        width = max(len(r["entity"]) for r in rows)
        for r in rows:
            note = ""
            if exhausted(r):
                when = time.strftime("%H:%M UTC", time.gmtime(retry_epoch(r, now)))
                note = f"  DAILY CAP REACHED, resets {when}"
            print(f"{r['entity']:<{width}}  day {str(r['day_remaining'] or 0):>4}/5000"
                  f"  minute {str(r['min_remaining'] or 0):>2}/60{note}")

    return 3 if capped else 0


if __name__ == "__main__":
    sys.exit(main())
