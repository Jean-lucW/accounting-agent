"""Core multi-entity Xero API client.

Usage:
    from accounting_agent.xero import XeroClient, client_for, entities

    entities()                          # {"<organisation name>": "<tenantId>", ...}
    us = client_for("OPCO_US")          # config key, slug or alias -> exact Xero name
    us = XeroClient("Example Operations Inc.", exact=True)   # the same thing
    us.get("Invoices", page=1)
    us.get_all("BankTransactions")      # auto-pagination, returns full list
    us.post("ManualJournals", {...})
    us.upload_attachment("Invoices", guid, "receipt.pdf", data)

Prefer client_for(): it resolves the entity through config/group.toml and
then matches the organisation name EXACTLY. A loose substring match is only
a fallback for interactive use, because one organisation's name can be a
substring of another's ("Example Operations" vs "Example Operations Inc.").

All requests carry the entity's xero-tenant-id header. 429s honour
Retry-After; 401s refresh the token once. Validation failures raise
XeroValidationError with Xero's per-element messages.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Iterator

from .. import readonly, xero_auth

BASE_URL = "https://api.xero.com/api.xro/2.0/"
CONNECTIONS_CACHE = xero_auth.TOKEN_PATH.parent / "connections.json"

# Endpoints whose collections page with `page=` (others return everything at once
# or use offset, see journals.py for the Journals offset scheme).
PAGED_MAX_PAGE_SIZE = 1000


def _row_id(row: dict) -> str:
    """Identity of a returned row, for spotting a repeated page.

    Xero names the id after the collection (InvoiceID, AccountID, ...), so take
    the first *ID key rather than guessing; fall back to the whole row.
    """
    if isinstance(row, dict):
        for key, value in row.items():
            if key.endswith("ID") and isinstance(value, str):
                return f"{key}={value}"
    return repr(row)



class XeroError(Exception):
    """API call failed. `.status` and `.body` carry the HTTP response."""

    def __init__(self, message: str, status: int | None = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body


class XeroValidationError(XeroError):
    """400 with Xero ValidationErrors. `.messages` lists them flat."""

    def __init__(self, status: int, body: str):
        self.messages: list[str] = []
        try:
            data = json.loads(body)
            for element in data.get("Elements", []):
                for err in element.get("ValidationErrors", []):
                    self.messages.append(err.get("Message", ""))
            if not self.messages and "Message" in data:
                self.messages.append(data["Message"])
        except (json.JSONDecodeError, AttributeError):
            self.messages = [body[:500]]
        super().__init__("; ".join(self.messages) or "validation failed", status, body)


def entities(refresh: bool = False) -> dict[str, str]:
    """Map of organisation name -> tenantId for every connected entity."""
    if not refresh and CONNECTIONS_CACHE.exists():
        return json.loads(CONNECTIONS_CACHE.read_text())
    token = xero_auth.get_access_token()
    mapping = {c["tenantName"]: c["tenantId"] for c in xero_auth.list_connections(token)}
    CONNECTIONS_CACHE.parent.mkdir(exist_ok=True)
    CONNECTIONS_CACHE.write_text(json.dumps(mapping, indent=2))
    return mapping


def _exact(known: dict[str, str], name_or_id: str) -> tuple[str, str] | None:
    for name, tid in known.items():
        if name_or_id == tid or name_or_id.strip().lower() == name.strip().lower():
            return name, tid
    return None


def resolve_entity(name_or_id: str, exact: bool = False) -> tuple[str, str]:
    """Resolve an organisation name or tenantId to (name, tenantId).

    An exact match (tenantId, or the full name ignoring case) always wins,
    and the connections cache is refreshed once before giving up on one, in
    case an organisation was renamed or newly connected. Only when no exact
    match exists, and exact=False, is a substring tried, and then it must be
    unique.
    """
    known = entities()
    hit = _exact(known, name_or_id)
    if hit is None:
        # Names may be stale (renamed or newly connected org): refresh once.
        known = entities(refresh=True)
        hit = _exact(known, name_or_id)
    if hit is not None:
        return hit
    if exact:
        raise XeroError(
            f"No connected Xero organisation is named exactly {name_or_id!r}. "
            f"Connected: {list(known)}. Make xero_name in config/group.toml match "
            f"the name Xero shows, or connect the organisation "
            f"(python -m accounting_agent.xero_auth)."
        )
    needle = name_or_id.lower()
    matches = [(n, t) for n, t in known.items() if needle in n.lower()]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise XeroError(f"No entity matches {name_or_id!r}. Known: {list(known)}")
    raise XeroError(f"Ambiguous entity {name_or_id!r}, matches {[m[0] for m in matches]}")


def client_for(entity_key_or_alias: str) -> "XeroClient":
    """XeroClient for a group entity named by its config key, slug, alias or
    short name (config/group.toml), matched to Xero by its EXACT xero_name."""
    from .. import config
    e = config.entity(entity_key_or_alias)
    return XeroClient(e.xero_name, exact=True)


class XeroClient:
    def __init__(self, entity: str, exact: bool = False):
        self.entity_name, self.tenant_id = resolve_entity(entity, exact=exact)

    # -- low-level ---------------------------------------------------------

    def request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        raw_body: bytes | None = None,
        content_type: str | None = None,
        accept: str = "application/json",
        idempotency_key: str | None = None,
        _retries: int = 3,
    ) -> tuple[int, bytes, dict[str, str]]:
        # Read-only sessions (AGENT_READONLY=1, see readonly.py) stop here,
        # before a token is fetched: nothing but GET leaves this process.
        if method.upper() != "GET":
            readonly.refuse(f"{method.upper()} {path} on {self.entity_name}")
        url = BASE_URL + path.lstrip("/")
        if params:
            url += "?" + urllib.parse.urlencode(
                {k: v for k, v in params.items() if v is not None}
            )
        data = raw_body
        headers = {
            "Authorization": f"Bearer {xero_auth.get_access_token()}",
            "xero-tenant-id": self.tenant_id,
            "Accept": accept,
        }
        if json_body is not None:
            data = json.dumps(json_body).encode()
            headers["Content-Type"] = "application/json"
        if content_type:
            headers["Content-Type"] = content_type
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key

        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with xero_auth._urlopen(req) as resp:
                return resp.status, resp.read(), dict(resp.headers)
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            if e.code == 429 and _retries > 0:
                wait = int(e.headers.get("Retry-After", "5")) + 1
                time.sleep(min(wait, 65))
                return self.request(method, path, params, json_body, raw_body,
                                    content_type, accept, idempotency_key, _retries - 1)
            if e.code == 401 and _retries > 0:
                # Token may have just expired mid-flight; force refresh and retry once.
                xero_auth.TOKEN_PATH.touch()
                tokens = json.loads(xero_auth.TOKEN_PATH.read_text())
                tokens["obtained_at"] = 0
                xero_auth.TOKEN_PATH.write_text(json.dumps(tokens))
                return self.request(method, path, params, json_body, raw_body,
                                    content_type, accept, idempotency_key, 0)
            if e.code == 400:
                raise XeroValidationError(e.code, body) from None
            raise XeroError(
                f"{method} {path} -> {e.code} for {self.entity_name}: {body[:300]}",
                e.code, body,
            ) from None

    def _json(self, method: str, path: str, params=None, json_body=None, **kw) -> dict:
        status, body, _ = self.request(method, path, params=params, json_body=json_body, **kw)
        return json.loads(body) if body else {}

    # -- convenience -------------------------------------------------------

    def get(self, path: str, **params) -> dict:
        return self._json("GET", path, params=params or None)

    def post(self, path: str, body: Any, idempotency_key: str | None = None, **params) -> dict:
        """POST: create new, or update existing when an *ID field is present."""
        return self._json("POST", path, params={"summarizeErrors": "false", **params},
                          json_body=body, idempotency_key=idempotency_key)

    def put(self, path: str, body: Any, idempotency_key: str | None = None, **params) -> dict:
        """PUT: strictly create-new (fails if the record exists)."""
        return self._json("PUT", path, params={"summarizeErrors": "false", **params},
                          json_body=body, idempotency_key=idempotency_key)

    def get_all(self, path: str, collection: str | None = None,
                page_size: int = PAGED_MAX_PAGE_SIZE, **params) -> list[dict]:
        """Fetch every page of a paged collection (Invoices, Contacts, ...)."""
        collection = collection or path.split("/")[0]
        out: list[dict] = []
        seen: set[str] = set()
        page = 1
        while True:
            data = self.get(path, page=page, pageSize=page_size, **params)
            items = data.get(collection, [])
            # Endpoints that do not page at all (Accounts, TaxRates, Currencies,
            # Users) ignore `page` and return the whole collection every time.
            # With more than 100 rows the size test below never fires, so the
            # loop asks for page 2, 3, 4 ... forever and silently eats the
            # tenant's 5,000-a-day cap. Identity is the only reliable stop:
            # a page that adds nothing new is the same page again.
            fresh = [it for it in items if _row_id(it) not in seen]
            seen.update(_row_id(it) for it in items)
            out.extend(fresh)
            pagination = data.get("pagination") or {}
            if pagination:
                if page >= pagination.get("pageCount", 1):
                    break
            elif not items or not fresh:
                break
            elif len(items) < min(page_size, 100):
                # Endpoints without pagination metadata ignore pageSize and cap
                # pages at 100 items, so compare against that floor, comparing
                # against a larger requested page_size would stop after page 1.
                break
            page += 1
        return out

    def iter_journals(self, payments_only: bool = False) -> Iterator[dict]:
        """Iterate the general-journal feed via offset paging (100 per call).

        NOTE: requires the accounting.journals.read scope, a premium scope that
        apps created after March 2026 hold only with Xero's approval; calls
        403 until it is granted. Use reports + per-endpoint reads for GL
        review instead.
        """
        offset = 0
        while True:
            params: dict[str, Any] = {"offset": offset}
            if payments_only:
                params["paymentsOnly"] = "true"
            batch = self.get("Journals", **params).get("Journals", [])
            if not batch:
                return
            yield from batch
            offset = batch[-1]["JournalNumber"]

    # -- attachments ---------------------------------------------------------

    def list_attachments(self, endpoint: str, guid: str) -> list[dict]:
        return self.get(f"{endpoint}/{guid}/Attachments").get("Attachments", [])

    def upload_attachment(self, endpoint: str, guid: str, filename: str,
                          data: bytes, content_type: str = "application/octet-stream",
                          include_online: bool = False) -> dict:
        params = {"IncludeOnline": "true"} if include_online else None
        status, body, _ = self.request(
            "PUT", f"{endpoint}/{guid}/Attachments/{urllib.parse.quote(filename)}",
            params=params, raw_body=data, content_type=content_type,
        )
        return json.loads(body)

    def download_attachment(self, endpoint: str, guid: str, attachment: dict) -> bytes:
        status, body, _ = self.request(
            "GET", f"{endpoint}/{guid}/Attachments/{urllib.parse.quote(attachment['FileName'])}",
            accept=attachment.get("MimeType", "application/octet-stream"),
        )
        return body

    def __repr__(self) -> str:
        return f"XeroClient({self.entity_name!r})"
