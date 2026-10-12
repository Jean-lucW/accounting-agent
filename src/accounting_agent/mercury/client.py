"""Read-only Mercury API client.

Usage:
    from accounting_agent.mercury import MercuryClient

    mer = MercuryClient("opcous")                   # slug from config/group.toml
    mer.accounts()                                  # every account + balances
    mer.transactions(account_id, from_date="2026-09-01")   # one account, paginated
    mer.statement_lines(from_date="2026-09-01")     # every account, statement shape

This client is READ-ONLY and enforces it at the transport layer: `_request`
refuses any method other than GET, so no code path here, present or future,
can send money or create a recipient. The same API that returns the feed can
move money out of the group's Mercury accounts, and per CLAUDE.md the agent
never creates payments: it reads the feed, and a person matches statement
lines by hand. The token itself is created READ ONLY in Mercury, so Mercury
would reject a write regardless; this is the second lock.

Mercury amounts are signed floats in USD; negative is money out. A Mercury
transaction already belongs to one account, so `statement_lines()` only has to
normalise field names to the shape `revolut.statement_lines()` returns; the two
feeds then line up column for column.

Authentication is a bearer token (MERCURY_<SLUG>_KEY in .env) in the
Authorization header, nothing to refresh. Read-only tokens do not need an IP
allowlist, and Mercury downgrades a token to the permissions it actually uses
after 45 days, so a token that only ever GETs stays read-only on Mercury's
side too. Setup guide: docs/setup/MERCURY.md. API reference:
docs/MERCURY_API_REFERENCE.md.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .. import revolut_auth  # for the shared .env loader and the SSL context

API_BASE = "https://api.mercury.com/api/v1"
MAX_PAGE = 1000
PROJECT_ROOT = Path(__file__).resolve().parents[3]


class MercuryError(Exception):
    """An API call failed; `status` and `body` carry Mercury's answer."""

    def __init__(self, message: str, status: int | None = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body


class MercuryReadOnlyError(MercuryError):
    """Raised before any network call when something tries a non-GET."""


def _load_env() -> dict[str, str]:
    env = revolut_auth._load_env()
    env.update({k: v for k, v in os.environ.items() if k.startswith("MERCURY_")})
    return env


def config_slugs() -> list[str]:
    """Slugs of the provider = "mercury" bank accounts in config/group.toml."""
    try:
        from .. import config
        return sorted({b.slug.lower() for b in config.bank_accounts()
                       if b.provider == "mercury" and b.slug})
    except Exception:  # noqa: BLE001 - no config: fall back to .env alone
        return []


def configured_entities() -> list[str]:
    """Slugs that have a MERCURY_<SLUG>_KEY in .env."""
    return sorted(
        name[len("MERCURY_") : -len("_KEY")].lower()
        for name in _load_env()
        if name.startswith("MERCURY_") and name.endswith("_KEY")
    )


def _as_date(value: str | date | datetime | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)[:10]


class MercuryClient:
    """One Mercury business, addressed by slug (e.g. "opcous")."""

    def __init__(self, entity: str):
        self.entity = entity.lower().replace("-", "_")
        var = f"MERCURY_{self.entity.upper()}_KEY"
        token = _load_env().get(var, "")
        if not token:
            raise MercuryError(
                f"Missing {var} in .env: a READ ONLY token from Mercury > Settings > API Tokens "
                f"(docs/setup/MERCURY.md). "
                f"Configured: {', '.join(configured_entities()) or '(none)'}"
            )
        self._token = token
        self.key_source = var

    # ---- transport -----------------------------------------------------

    def _request(self, method: str, path: str, params: dict | None = None,
                 _retried: bool = False) -> Any:
        if method.upper() != "GET":
            raise MercuryReadOnlyError(
                f"{method.upper()} {path} blocked, the Mercury client is read-only. "
                f"The agent never moves money or marks anything paid; it reads the "
                f"feed and the user matches statement lines by hand."
            )
        query = {k: v for k, v in (params or {}).items() if v is not None}
        url = f"{API_BASE}/{path.lstrip('/')}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"
        request = urllib.request.Request(
            url, method="GET",
            headers={"Authorization": f"Bearer {self._token}", "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, context=revolut_auth._SSL_CONTEXT) as response:
                raw = response.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            if e.code == 429 and not _retried:
                time.sleep(float(e.headers.get("Retry-After", 5)))
                return self._request(method, path, params, _retried=True)
            hint = ""
            if e.code == 401:
                hint = " (token rejected, wrong key, revoked, or the wrong Mercury business)"
            raise MercuryError(f"GET {path} failed: HTTP {e.code}{hint}", e.code, body) from None

    def get(self, path: str, **params) -> Any:
        return self._request("GET", path, params)

    # ---- accounts ------------------------------------------------------

    def accounts(self, include_closed: bool = False, include_credit: bool = True) -> list[dict]:
        """Every Mercury account: checking, savings, treasury AND the credit card.

        GET /accounts does not list the credit card; it lives behind GET /credit
        with only id, status and balances. It is merged in here with
        kind="credit" and name "Mercury Credit" so `statement_lines()` covers
        the card too, its transactions come from the same
        /account/{id}/transactions endpoint as any other account. Balances on
        the card are negative (money owed).

        Rows carry id, name, nickname, kind, type, status, currentBalance,
        availableBalance, accountNumber, routingNumber, legalBusinessName.
        """
        rows = list((self.get("accounts") or {}).get("accounts", []))
        if include_credit:
            business = next((a.get("legalBusinessName") for a in rows if a.get("legalBusinessName")), None)
            for c in (self.get("credit") or {}).get("accounts", []):
                rows.append({**c, "name": "Mercury Credit", "nickname": None, "kind": "credit",
                             "type": "mercury", "legalBusinessName": business})
        if not include_closed:
            rows = [a for a in rows if a.get("status") == "active"]
        return rows

    def credit_accounts(self) -> list[dict]:
        """The credit card account(s) only, as GET /credit returns them."""
        return (self.get("credit") or {}).get("accounts", [])

    def account(self, account_id: str) -> dict:
        return self.get(f"account/{account_id}")

    # ---- transactions --------------------------------------------------

    def transactions(
        self,
        account_id: str,
        from_date: str | date | datetime | None = None,
        to_date: str | date | datetime | None = None,
        status: str | None = None,
        limit: int = MAX_PAGE,
    ) -> list[dict]:
        """All transactions on one account in a window, following the offset
        pagination to the end. Mercury defaults `start` to 30 days back, so
        pass from_date explicitly for anything older."""
        params = {
            "start": _as_date(from_date),
            "end": _as_date(to_date),
            "status": status,
            "limit": min(limit, MAX_PAGE),
            "offset": 0,
            "order": "desc",
        }
        collected: list[dict] = []
        seen: set[str] = set()
        while True:
            page = self.get(f"account/{account_id}/transactions", **params) or {}
            rows = page.get("transactions", [])
            fresh = [t for t in rows if t.get("id") not in seen]
            seen.update(t.get("id") for t in fresh)
            collected.extend(fresh)
            total = page.get("total")
            params["offset"] += len(rows)
            if not fresh or len(rows) < params["limit"] or (total is not None and params["offset"] >= total):
                return collected

    def transaction(self, transaction_id: str) -> dict:
        return self.get(f"transaction/{transaction_id}")

    def statement_lines(
        self,
        from_date: str | date | datetime | None = None,
        to_date: str | date | datetime | None = None,
        account_id: str | None = None,
    ) -> list[dict]:
        """Transactions across every account, in the same shape as
        `RevolutClient.statement_lines()` so the two feeds compare like for like.

        `state` is Mercury's status: sent is cash; pending has not settled;
        failed, cancelled, reversed and blocked are not cash. `date` is the
        posted date when there is one, else the created date.
        """
        accounts = [{"id": account_id}] if account_id else self.accounts()
        names = {a["id"]: (a.get("nickname") or a.get("name")) for a in accounts if a.get("name")}
        kinds = {a["id"]: a.get("kind") for a in accounts}
        lines: list[dict] = []
        for acct in accounts:
            for txn in self.transactions(acct["id"], from_date, to_date):
                lines.append(
                    {
                        "id": txn.get("id"),
                        "leg_id": None,
                        "date": (txn.get("postedAt") or txn.get("createdAt") or "")[:10],
                        "completed_at": txn.get("postedAt"),
                        "state": txn.get("status"),
                        "type": txn.get("kind"),
                        "account_id": acct["id"],
                        "account_name": names.get(acct["id"]),
                        "amount": txn.get("amount"),
                        "currency": "USD",
                        "account_kind": kinds.get(acct["id"]),
                        "bill_amount": None,
                        "bill_currency": None,
                        "description": txn.get("bankDescription") or txn.get("counterpartyName"),
                        "reference": txn.get("note") or txn.get("externalMemo"),
                        "counterparty": txn.get("counterpartyName"),
                        "merchant": (txn.get("merchant") or {}).get("name") or txn.get("counterpartyName"),
                        "card_number": (txn.get("details") or {}).get("debitCardInfo", {}).get("lastFour")
                        if isinstance((txn.get("details") or {}).get("debitCardInfo"), dict) else None,
                        "cardholder": (txn.get("details") or {}).get("debitCardInfo", {}).get("cardholderName")
                        if isinstance((txn.get("details") or {}).get("debitCardInfo"), dict) else None,
                        "mercury_category": txn.get("mercuryCategory"),
                        "dashboard_link": txn.get("dashboardLink"),
                    }
                )
        return lines
