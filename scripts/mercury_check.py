#!/usr/bin/env python3
"""Mercury API connection check for one business.

    .venv/bin/python scripts/mercury_check.py <slug>

<slug> is the `slug` of a provider = "mercury" bank account in
config/group.toml (the token is MERCURY_<SLUG>_KEY in .env). Run with no slug
to list the configured ones.

Runs in stages and stops at the first failure:

  1. config     MERCURY_<SLUG>_KEY is in .env
  2. live       Mercury accepts the token and returns the accounts
  3. feed       last 7 days of transactions across every account
  4. read-only  the client refuses a non-GET before any network call
  5. scope      Mercury itself refuses a write with the raw token. The probe
                is DELETE /webhooks/<all-zero uuid>: no body, nothing to
                delete, and Mercury checks the token type before it looks the
                id up, so a read-only token gets 403 readWriteTokenRequired
                and a write-enabled one gets 404. (Endpoints that take a body
                parse it first and answer 400 regardless of scope, and
                /recipients and /transactions look the id up first: neither
                tells you anything about the token.)
"""

from __future__ import annotations

import sys
import urllib.error
import urllib.request
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

OK, BAD, INFO = "  ok  ", " FAIL ", "      "


def fail(stage: str, message: str, *fixes: str) -> None:
    print(f"{BAD}{stage}: {message}")
    for fix in fixes:
        print(f"{INFO}  -> {fix}")
    raise SystemExit(1)


def configured_slugs() -> str:
    """The mercury slugs in config/group.toml, marked with whether .env has a key."""
    from accounting_agent import config
    try:
        from accounting_agent.mercury import configured_entities
        in_env = set(configured_entities())
    except Exception:  # noqa: BLE001 - a broken .env must not hide the list
        in_env = set()
    rows = []
    for b in config.bank_accounts():
        if b.provider == "mercury":
            mark = "" if b.slug in in_env else f"  (no MERCURY_{b.slug.upper()}_KEY in .env yet)"
            rows.append(f"  {b.slug:16} {config.entity(b.entity).short} · {b.label}{mark}")
    return "\n".join(rows) or "  (none: add a provider = \"mercury\" bank account to config/group.toml)"


def check(slug: str) -> None:
    from accounting_agent import revolut_auth
    from accounting_agent.mercury import MercuryClient, MercuryError, MercuryReadOnlyError
    from accounting_agent.mercury.client import API_BASE

    print(f"Mercury connection check: account {slug}\n")

    try:
        mer = MercuryClient(slug)
    except MercuryError as e:
        fail("config", str(e))
    print(f"{OK}config     token from {mer.key_source} ({len(mer._token)} chars)")

    try:
        accounts = mer.accounts(include_closed=True)
    except MercuryError as e:
        fail("live", str(e), "Mercury > Settings > API Tokens: is the token active, and for this business?")
    business = {a.get("legalBusinessName") for a in accounts}
    kinds = sorted({a.get("kind") for a in accounts if a.get("kind")})
    print(f"{OK}live       {len(accounts)} account(s), business {', '.join(sorted(b for b in business if b))}, "
          f"kinds {', '.join(kinds)}")
    if "credit" not in kinds:
        print(f"{INFO}  (no credit card account on this business)")
    for a in accounts:
        print(f"{INFO}  {(a.get('nickname') or a.get('name') or '(unnamed)'):28} {a.get('kind', ''):10} "
              f"USD {a.get('currentBalance', 0):>14,.2f}  {a.get('status')}")

    since = (date.today() - timedelta(days=7)).isoformat()
    lines = mer.statement_lines(from_date=since)
    by_kind = {}
    for line in lines:
        by_kind[line.get("account_kind")] = by_kind.get(line.get("account_kind"), 0) + 1
    print(f"{OK}feed       {len(lines)} statement line(s) since {since}: "
          + ", ".join(f"{k} {n}" for k, n in sorted(by_kind.items(), key=lambda kv: str(kv[0]))))
    for line in sorted(lines, key=lambda r: r["date"], reverse=True)[:5]:
        print(f"{INFO}  {line['date']}  {line['currency']} {line['amount']:>12,.2f}  {line['state']:9} "
              f"{(line['merchant'] or line['description'] or '')[:40]}")

    try:
        mer._request("POST", "recipients")
        fail("read-only", "a POST was NOT blocked: do not use this client")
    except MercuryReadOnlyError:
        print(f"{OK}read-only  writes refused at the transport layer")

    # Mercury's own view of the token, bypassing the client. DELETE on a
    # webhook id that cannot exist: no body, nothing to delete. Mercury checks
    # the token type before the lookup, so the answer is about the token.
    request = urllib.request.Request(
        f"{API_BASE}/webhooks/00000000-0000-0000-0000-000000000000", method="DELETE",
        headers={"Authorization": f"Bearer {mer._token}"})
    try:
        urllib.request.urlopen(request, context=revolut_auth._SSL_CONTEXT)
        fail("scope", "Mercury ACCEPTED a write with this token: replace it with a READ ONLY token now")
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        if e.code == 403 and "readWriteTokenRequired" in body:
            print(f"{OK}scope      Mercury refuses writes with this token (403 readWriteTokenRequired)")
        elif e.code == 404:
            fail("scope", "Mercury got past the token-type check (404 on a non-existent webhook)",
                 "this token is write-enabled: revoke it in Mercury and create a READ ONLY one")
        else:
            fail("scope", f"unexpected answer HTTP {e.code}: {body[:160]}",
                 "check the token type in Mercury > Settings > API Tokens by hand")


if __name__ == "__main__":
    args = sys.argv[1:]
    if any(a in ("-h", "--help") for a in args):
        print(__doc__)
        print("configured mercury slugs:\n" + configured_slugs())
        raise SystemExit(0)
    if len(args) != 1:
        raise SystemExit("usage: mercury_check.py <slug>\n"
                         "configured mercury slugs:\n" + configured_slugs())
    check(args[0])
