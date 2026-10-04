"""Read-only Revolut Business API client.

Usage:
    from accounting_agent.revolut import RevolutClient

    rev = RevolutClient("holdco")                   # slug from config/group.toml
    rev.accounts()                                  # every account + balance
    rev.transactions(from_date="2026-09-01")        # auto-paginated
    rev.bank_details(account_id)

This client is READ-ONLY and enforces it at the transport layer: `_request`
refuses any method other than GET, so no code path here, present or future,
can create a payment, a transfer or a payout. That is deliberate. The same API
that returns the bank feed can move real money out of the group's accounts,
and per CLAUDE.md the agent never creates payments: it reads the feed, and a
person matches statement lines by hand. The consent itself is granted at
scope READ only, so Revolut would reject a write regardless; this is the
second lock.

Amounts come back as signed floats in the account currency; negative is money
out. `transactions()` returns Revolut's nested legs untouched; use
`statement_lines()` for the flattened, per-account view that lines up with a
bank statement and with Xero's bank statement lines.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterator

from .. import revolut_auth

# Revolut caps a transactions page at 1000; it has no cursor, so pagination
# walks the `to` boundary backwards.
MAX_PAGE = 1000


class RevolutError(Exception):
    """API call failed. `.status` and `.body` carry the HTTP response."""

    def __init__(self, message: str, status: int | None = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body


class RevolutReadOnlyError(RevolutError):
    """A write was attempted through a client that is read-only by design."""


def _as_date(value: str | date | datetime | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


class RevolutClient:
    """One Revolut Business account, addressed by its slug (e.g. "holdco")."""

    def __init__(self, entity: str):
        self.cfg = revolut_auth.config(entity)
        self.entity = self.cfg.slug

    # ---- transport -----------------------------------------------------

    def _request(self, method: str, path: str, params: dict | None = None,
                 _retried: bool = False) -> Any:
        if method.upper() != "GET":
            raise RevolutReadOnlyError(
                f"{method.upper()} {path} blocked, the Revolut client is read-only. "
                f"The agent never moves money or marks anything paid; it reads the "
                f"feed and the user matches statement lines by hand."
            )
        query = {k: v for k, v in (params or {}).items() if v is not None}
        url = f"{self.cfg.api_base}/{path.lstrip('/')}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"
        request = urllib.request.Request(
            url,
            method="GET",
            headers={
                "Authorization": f"Bearer {revolut_auth.access_token(self.entity)}",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, context=revolut_auth._SSL_CONTEXT) as response:
                raw = response.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            if e.code == 401 and not _retried:
                revolut_auth.refresh(self.cfg)
                return self._request(method, path, params, _retried=True)
            if e.code == 429 and not _retried:
                time.sleep(float(e.headers.get("Retry-After", 5)))
                return self._request(method, path, params, _retried=True)
            raise RevolutError(f"GET {path} failed: HTTP {e.code}", e.code, body) from None

    def get(self, path: str, **params) -> Any:
        return self._request("GET", path, params)

    # ---- accounts ------------------------------------------------------

    def accounts(self) -> list[dict]:
        """Every account on the business, with its balance and currency."""
        return self.get("accounts") or []

    def account(self, account_id: str) -> dict:
        return self.get(f"accounts/{account_id}")

    def bank_details(self, account_id: str) -> list[dict]:
        """IBAN / account number / sort code for an account."""
        return self.get(f"accounts/{account_id}/bank-details") or []

    def counterparties(self) -> list[dict]:
        return self.get("counterparties") or []

    # ---- transactions --------------------------------------------------

    def transactions(
        self,
        from_date: str | date | datetime | None = None,
        to_date: str | date | datetime | None = None,
        account_id: str | None = None,
        type: str | None = None,
        count: int = MAX_PAGE,
    ) -> list[dict]:
        """All transactions in a window, following pagination to the end.

        Revolut returns newest first and has no cursor, so each further page
        moves the `to` boundary back to the oldest row already seen. Rows are
        de-duplicated by id because that boundary is inclusive.
        """
        params = {
            "from": _as_date(from_date),
            "to": _as_date(to_date),
            "account": account_id,
            "type": type,
            "count": min(count, MAX_PAGE),
        }
        collected: list[dict] = []
        seen: set[str] = set()
        while True:
            page = self.get("transactions", **params) or []
            fresh = [t for t in page if t.get("id") not in seen]
            for t in fresh:
                seen.add(t.get("id"))
            collected.extend(fresh)
            if len(page) < params["count"] or not fresh:
                return collected
            oldest = min(
                (t.get("created_at", "") for t in page if t.get("created_at")), default=""
            )
            if not oldest:
                return collected
            params["to"] = oldest[:10]

    def transaction(self, transaction_id: str) -> dict:
        return self.get(f"transaction/{transaction_id}")

    def statement_lines(
        self,
        from_date: str | date | datetime | None = None,
        to_date: str | date | datetime | None = None,
        account_id: str | None = None,
    ) -> list[dict]:
        """Transactions flattened to one row per account movement.

        A Revolut transaction carries a `legs` list, an internal transfer
        between two of your own accounts has two, one negative and one
        positive. A bank statement, and a Xero bank statement line, has one row
        per movement per account, so this flattens to that shape.
        """
        names = {a.get("id"): a.get("name") for a in self.accounts()}
        lines: list[dict] = []
        for txn in self.transactions(from_date, to_date, account_id):
            for leg in txn.get("legs") or []:
                if account_id and leg.get("account_id") != account_id:
                    continue
                counterparty = leg.get("counterparty") or {}
                lines.append(
                    {
                        "id": txn.get("id"),
                        "leg_id": leg.get("leg_id"),
                        "date": (txn.get("completed_at") or txn.get("created_at") or "")[:10],
                        "completed_at": txn.get("completed_at"),
                        "state": txn.get("state"),
                        "type": txn.get("type"),
                        "account_id": leg.get("account_id"),
                        "account_name": names.get(leg.get("account_id")),
                        "amount": leg.get("amount"),
                        "currency": leg.get("currency"),
                        "bill_amount": leg.get("bill_amount"),
                        "bill_currency": leg.get("bill_currency"),
                        "description": leg.get("description") or txn.get("reference"),
                        "reference": txn.get("reference"),
                        "counterparty": counterparty.get("account_id")
                        or counterparty.get("id"),
                        "merchant": (txn.get("merchant") or {}).get("name"),
                        "card_number": (txn.get("card") or {}).get("card_number"),
                        "cardholder": (txn.get("card") or {}).get("first_name"),
                    }
                )
        return lines
