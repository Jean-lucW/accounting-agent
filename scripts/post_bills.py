#!/usr/bin/env python
"""Post a bookkeeping run's bills and supplier credit notes from one JSON spec.

A run used to write its own throwaway posting script each time. This is that
script, committed once: the run writes the spec, this file does the Xero side,
the same way every time.

    python scripts/post_bills.py <spec.json>              dry run: guard + payloads, sends nothing
    python scripts/post_bills.py <spec.json> --offline    no network at all: checks + payloads
    python scripts/post_bills.py <spec.json> --live       posts, then settles (refused when AGENT_READONLY is set)
        [--only NUM[,NUM]]  just these documents       [--no-cross]  skip the other entities' check
        [--results FILE]    default <spec>.results.jsonl, written by --live as each document settles
        [--verbose]         print every header and payload on a live run too

One --live call is the whole posting step of a run: every check, the guard,
the post, the files, the bank feed line, and then the spec's `settle` list. A
live run prints one line per document that posted cleanly and the full record
only for one that did not, so the run reads only what needs it; every payload
sent is kept in the results file.

What it does, per document, in this order:

  1. Checks the spec offline: known entity (through config/group.toml), every
     field present and typed, the due date given and not before the date
     (never defaulted, xero-bills section 2), every file on disk and under
     Xero's size and count limits, the tracking categories the entity requires
     on every line (`required_tracking`), tax types restricted to some
     entities (`[posting] restricted_tax_types`), the lines adding up to the
     total. One bad document stops the whole batch before anything is sent.
  2. Reads each target entity (GET only): the chart, the tax rates, the lock
     dates. Every account must exist, be active and not be a bank account,
     every tax type must exist, the date must be after the end-of-year lock
     date, and the total Xero will compute must equal the document's total. A
     document that fails is refused on its own and the rest go on. A tax type
     not enabled for expenses, or a date inside the period lock, is printed as
     a warning: Xero is the judge of those, per document.
  3. The duplicate guard (xero skill safety rule, xero-bills step 4), which
     stays the authority: in the target entity, any live bill with the same
     number; the same supplier and amount within four days, as a bill or as
     spend money; and `matching.candidates`, which adds the bank-side amount
     of payments and amounts converted from another currency, named for the
     supplier. In every other entity, the same number or the supplier and
     amount. Any hit skips the document and prints what it found. A hit the
     run has looked at and knows is not this document is acknowledged in the
     spec (`ack`) with the reason, never by editing this file.
  4. Prints the target entity in full and the exact payload, then (with
     --live) sends it with a deterministic idempotency key, checks the reply
     (no element errors, a real ID, AUTHORISED, amount due equal to the total,
     the total equal to the document's), attaches every file, removes the
     bill's bank feed line when the spec names one, and appends the outcome to
     the results file. A re-run of the same spec skips what the results file
     already shows posted.

What it never does: touch a payment, a credit note allocation, a reconcile, an
update or a void. The client it hands to the helpers refuses every write
except creating a bill, a supplier credit note, a contact and an attachment,
so no code path here can do anything else. The bill is left AUTHORISED with
the document attached and the full amount due; a person matches the bank line
to it by hand.

Spec, a JSON list (or {"docs": <dir>, "bills": [...]}; relative file paths
resolve against `docs`, else the spec's own folder):

    {"entity": "OPCO_US",                an entity key, alias or short name from config/group.toml
     "type": "bill",                     bill | credit (a supplier credit note, left unallocated)
     "contact": "Acme Hosting",          exact Xero contact name in that entity
     "contact_id": "...",                optional, pins one of several same-named contacts
     "new_contact": false,               true to create the contact when none exists
     "number": "INV-10135",              the invoice number (the reference on a credit note)
     "date": "2026-10-01",
     "due": "2026-10-16",                bills only, required, from rules/SUPPLIERS.md "Due dates"
     "currency": "USD",
     "amounts": "Exclusive",             Exclusive | Inclusive | NoTax
     "total": 248.60,                    the document's gross total
     "lines": [{"description": "...", "amount": 248.60, "account": "4900", "tax": "NONE",
                "quantity": 1, "tax_amount": 0.79, "tracking": {"Region": "North"}}],
     "files": ["S-2026-10-06-0912-Example-Supplier.pdf"],
     "feed": {"entity": "OPCO_US", "match": "rev:6abe6b09"},   optional: the line on this
                                         entity's own bank feed to remove once posted
     "ack": {"ACME-BK-22SEP2026": "a different day's lunch"},
     "paid": "OPCO_US",                  optional: the entity whose bank paid it, for the report
     "key": "..."}                       optional idempotency key override

`tracking` is {"<Category>": "<Option>"} or Xero's own list of {"Name", "Option"}.
`quantity` defaults to 1 and `amount` is the unit amount. `tax_amount` overrides
the tax Xero would compute on the line.

`settle`, in the dict form of the spec, is what the postings answer, done once
the documents each action names in `after` are posted (by this call or an
earlier --live of the same spec). An action with no `after` is done at once.
Each is done once: a re-run skips what the results file shows settled, and an
action waiting on a document that did not post is printed HELD and done by the
re-run that posts it.

    {"ledger": {"when": "2026-10-06 09:12", "source": "slack", "sender": "Sam Okafor",
                "seen": 1, "counterparty": "Example Cafe", "ref": "USD 13.70, 5 Oct",
                "entity": "OPCO_US", "outcome": "posted, 4900", "bookkept": 1},
     "after": ["CAFE-SO-05OCT2026"]}     one line in docs/bookkept/LEDGER.md; bookkept defaults to len(after)
    {"close": "<register key>", "reason": "...", "after": [...]}   scripts/outstanding.py close, batched
    {"email": "<gmail thread id>", "after": [...]}                 labelled processed, out of the inbox
    {"feed": {"entity": "OPCO_US", "match": "rev:1a2b3c4d", "why": "..."}, "after": [...]}
                                                                   a bank feed line the bills do not carry

Exit status: 0 everything posted (or ready, on a dry run); 1 the spec failed a
check and nothing was sent; 3 some documents skipped by the guard; 4 some
documents refused against the entity or rejected by Xero; 5 the batch stopped (the Xero rate limit, or a create
whose outcome is unknown: re-run the same spec, the idempotency key returns
the document if it went in).
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import math
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
LEDGER = ROOT / "docs" / "bookkept" / "LEDGER.md"

from accounting_agent import config, readonly  # noqa: E402

BILL_KEYS = {"entity", "type", "contact", "contact_id", "new_contact", "number", "date",
             "due", "currency", "amounts", "total", "lines", "files", "feed", "ack", "key", "paid"}
REQUIRED = ("entity", "type", "contact", "number", "date", "currency", "total", "lines", "files")
LINE_KEYS = {"description", "amount", "quantity", "account", "tax", "tax_amount", "tracking"}
AMOUNT_TYPES = ("Exclusive", "Inclusive", "NoTax")

GUARD_DAYS = 4                 # same supplier and amount this close is the same document
MAX_FILES = 10                 # Xero: attachments per document
MAX_BYTES = 10 * 1024 * 1024   # Xero: per attachment on Invoices / CreditNotes
BAD_NAME = re.compile(r'[<>:"/\\|?*+\x00]')


class SpecError(Exception):
    pass


class DryRun(Exception):
    """Raised by the dry-run client after it has printed a write."""


def full(key: str) -> str:
    return config.entity(key).title


def money(x: float) -> str:
    return f"{x:,.2f}"


def short_date(iso: str) -> str:
    try:
        return f"{date.fromisoformat(iso):%d %b}"
    except ValueError:
        return iso


def norm_number(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def norm_name(s: str | None) -> str:
    return " ".join((s or "").casefold().split())


def iso_of(value) -> str:
    """Xero dates arrive as ISO strings or '/Date(ms+0000)/'."""
    if not value:
        return ""
    s = str(value)
    m = re.search(r"/Date\((-?\d+)", s)
    if m:
        return datetime.fromtimestamp(int(m.group(1)) / 1000, tz=timezone.utc).date().isoformat()
    return s[:10]


# ---------------------------------------------------------------- the spec

def entity_key(raw) -> str:
    try:
        return config.entity(str(raw or "")).key
    except config.ConfigError:
        raise SpecError(f"unknown entity {raw!r}; one of {', '.join(config.entity_keys())}") from None


def tracking_of(raw):
    if raw is None:
        return None
    if isinstance(raw, dict) and raw and all(isinstance(k, str) and isinstance(v, str) for k, v in raw.items()):
        return [{"Name": k, "Option": v} for k, v in raw.items()]
    if isinstance(raw, list) and all(isinstance(t, dict) and set(t) == {"Name", "Option"} for t in raw):
        return raw
    raise SpecError(f"tracking {raw!r} is not {{\"<Category>\": \"<Option>\"}} or a list of Name/Option")


def _iso(value, field: str) -> str:
    try:
        return date.fromisoformat(str(value)).isoformat()
    except ValueError:
        raise SpecError(f"{field} {value!r} is not YYYY-MM-DD") from None


def _num(value, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise SpecError(f"{field} {value!r} is not a number")
    return float(value)


def load_spec(path: Path) -> list[dict]:
    raw = json.loads(path.read_text())
    if isinstance(raw, dict):
        docs = Path(raw.get("docs") or path.parent)
        bills = raw.get("bills")
    else:
        docs, bills = path.parent, raw
    if not isinstance(bills, list) or not bills:
        raise SpecError("the spec holds no documents")
    out, errors = [], []
    for n, b in enumerate(bills, 1):
        try:
            out.append(check_one(b, docs))
        except SpecError as e:
            label = b.get("number") if isinstance(b, dict) else None
            errors.append(f"document {n} ({label or 'no number'}): {e}")
    seen: dict[tuple, int] = {}
    for n, s in enumerate(out, 1):
        k = (s["entity"], s["type"], norm_number(s["number"]))
        if k in seen:
            errors.append(f"document {n} ({s['number']}): the same number twice in {full(s['entity'])}, "
                          f"documents {seen[k]} and {n}")
        seen[k] = n
    if errors:
        raise SpecError("\n".join(errors))
    return out


def check_one(b, docs: Path) -> dict:
    if not isinstance(b, dict):
        raise SpecError("not an object")
    unknown = set(b) - BILL_KEYS
    if unknown:
        raise SpecError(f"unknown field(s) {sorted(unknown)}; allowed {sorted(BILL_KEYS)}")
    missing = [k for k in REQUIRED if b.get(k) in (None, "", [])]
    if missing:
        raise SpecError(f"missing {missing}")
    s: dict = {"entity": entity_key(b["entity"])}
    s["type"] = str(b["type"]).lower()
    if s["type"] not in ("bill", "credit"):
        raise SpecError(f"type {b['type']!r}: bill or credit")
    s["contact"] = " ".join(str(b["contact"]).split())
    s["contact_id"] = b.get("contact_id") or None
    s["new_contact"] = bool(b.get("new_contact"))
    # The entity whose bank paid it, from the bank feed (COMMS.md, Quoting an
    # invoice); for the report only, never sent to Xero. Absent until the cash settles.
    s["paid"] = entity_key(b["paid"]) if b.get("paid") else None
    s["number"] = str(b["number"]).strip()
    if len(s["number"]) > 255:
        raise SpecError("number longer than 255 characters")
    s["date"] = _iso(b["date"], "date")
    if s["type"] == "bill":
        if not b.get("due"):
            raise SpecError("due is required on a bill: read it from the document or calculate it "
                            "(xero-bills section 2), never default it")
        s["due"] = _iso(b["due"], "due")
        if s["due"] < s["date"]:
            raise SpecError(f"due {s['due']} is before the date {s['date']}")
    elif b.get("due"):
        raise SpecError("a credit note has no due date")
    ccy = str(b["currency"]).upper()
    if not re.fullmatch(r"[A-Z]{3}", ccy):
        raise SpecError(f"currency {b['currency']!r} is not an ISO code")
    s["currency"] = ccy
    s["amounts"] = b.get("amounts") or "Exclusive"
    if s["amounts"] not in AMOUNT_TYPES:
        raise SpecError(f"amounts {s['amounts']!r}: one of {AMOUNT_TYPES}")
    s["total"] = round(_num(b["total"], "total"), 2)
    if s["total"] <= 0:
        raise SpecError("total must be positive (a refund is a credit note, not a negative bill)")

    s["lines"] = []
    restricted = config.posting()["restricted_tax_types"]
    for i, ln in enumerate(b["lines"], 1):
        if not isinstance(ln, dict):
            raise SpecError(f"line {i} is not an object")
        bad = set(ln) - LINE_KEYS
        if bad:
            raise SpecError(f"line {i}: unknown field(s) {sorted(bad)}; allowed {sorted(LINE_KEYS)}")
        for k in ("description", "amount", "account", "tax"):
            if ln.get(k) in (None, ""):
                raise SpecError(f"line {i}: {k} is required (tax explicit, never the account default)")
        line = {
            "description": str(ln["description"]).strip(),
            "amount": round(_num(ln["amount"], f"line {i} amount"), 2),
            "quantity": _num(ln.get("quantity", 1), f"line {i} quantity"),
            "account": str(ln["account"]).strip(),
            "tax": str(ln["tax"]).strip(),
            "tax_amount": (round(_num(ln["tax_amount"], f"line {i} tax_amount"), 2)
                           if ln.get("tax_amount") is not None else None),
            "tracking": tracking_of(ln.get("tracking")),
        }
        if line["tax_amount"] is not None and s["amounts"] == "NoTax":
            raise SpecError(f"line {i}: tax_amount on a NoTax document")
        have = {t["Name"] for t in line["tracking"] or []}
        for cat in config.entity(s["entity"]).required_tracking:
            if cat not in have:
                raise SpecError(f"line {i}: every {full(s['entity'])} line needs a {cat} tracking option")
        only = restricted.get(line["tax"])
        if only and s["entity"] not in only:
            raise SpecError(f"line {i}: tax type {line['tax']} is allowed in {', '.join(only)} only")
        s["lines"].append(line)

    s["files"] = []
    for f in b["files"]:
        p = Path(f)
        p = p if p.is_absolute() else docs / p
        if not p.is_file():
            raise SpecError(f"file not found: {p}")
        if p.stat().st_size > MAX_BYTES:
            raise SpecError(f"file over {MAX_BYTES // 1024 // 1024} MB: {p}")
        if p.stat().st_size == 0:
            raise SpecError(f"empty file: {p}")
        s["files"].append(p)
    if len(s["files"]) > MAX_FILES:
        raise SpecError(f"{len(s['files'])} files; Xero takes {MAX_FILES} per document")

    feed = b.get("feed")
    if feed is not None:
        if not isinstance(feed, dict) or set(feed) != {"entity", "match"} or not feed.get("match"):
            raise SpecError('feed is {"entity": ..., "match": "<unique substring>"}')
        fe = entity_key(feed["entity"])
        if fe != s["entity"]:
            raise SpecError("feed: a line on another entity's bank feed is the bill-payments check's "
                            "(xero-bills step 6b), not removed when the bill posts")
        s["feed"] = {"entity": fe, "match": str(feed["match"])}
    ack = b.get("ack") or {}
    if not isinstance(ack, dict) or any(not str(v).strip() for v in ack.values()):
        raise SpecError('ack is {"<number or id>": "why it is not this document"}, a reason for each')
    s["ack"] = {norm_number(k): str(v) for k, v in ack.items()}
    s["key"] = b.get("key") or idempotency_key(s)
    if len(s["key"]) > 128:
        raise SpecError("key longer than 128 characters")

    computed, _ = expected_total(s, None)
    if computed is not None and abs(computed - s["total"]) > tolerance(s):
        raise SpecError(f"the lines come to {s['currency']} {money(computed)}, the total is "
                        f"{money(s['total'])}")
    return s


SETTLE_KINDS = ("ledger", "close", "email", "feed")
LEDGER_REQUIRED = ("when", "source", "sender", "seen", "counterparty", "ref", "entity", "outcome")
LEDGER_WHEN = re.compile(r"^\d{4}-\d{2}-\d{2}( .{1,60})?$")


def load_settle(path: Path, specs: list[dict]) -> list[dict]:
    """The spec's `settle` list, checked: one kind per action, `after` naming
    only documents in this spec, a ledger line whose fields hold no pipe."""
    raw = json.loads(path.read_text())
    acts = raw.get("settle") if isinstance(raw, dict) else None
    if acts is None:
        return []
    if not isinstance(acts, list):
        raise SpecError("settle is a list of actions")
    numbers = {norm_number(s["number"]) for s in specs}
    out, errors = [], []
    for n, a in enumerate(acts, 1):
        try:
            if not isinstance(a, dict):
                raise SpecError("not an object")
            kinds = [k for k in SETTLE_KINDS if k in a]
            if len(kinds) != 1:
                raise SpecError(f"exactly one of {', '.join(SETTLE_KINDS)}")
            kind = kinds[0]
            extra = set(a) - {kind, "after", "reason"}
            if extra:
                raise SpecError(f"unknown field(s) {sorted(extra)}")
            after = a.get("after") or []
            if not isinstance(after, list) or not all(isinstance(x, str) and x.strip() for x in after):
                raise SpecError("after is a list of document numbers")
            stray = [x for x in after if norm_number(x) not in numbers]
            if stray:
                raise SpecError(f"after names {stray}, which are not documents in this spec")
            v = a[kind]
            if kind == "ledger":
                if not isinstance(v, dict):
                    raise SpecError("ledger is an object")
                missing = [k for k in LEDGER_REQUIRED if v.get(k) in (None, "")]
                if missing:
                    raise SpecError(f"ledger: missing {missing}")
                unknown = set(v) - set(LEDGER_REQUIRED) - {"bookkept"}
                if unknown:
                    raise SpecError(f"ledger: unknown field(s) {sorted(unknown)}")
                if not LEDGER_WHEN.match(str(v["when"])):
                    raise SpecError("ledger: when starts with the message's YYYY-MM-DD, then its UTC times")
                if str(v["source"]).lower() not in ("slack", "email", "n/a"):
                    raise SpecError("ledger: source is slack, email or n/a")
                for k in ("seen", "bookkept"):
                    if k in v and (isinstance(v[k], bool) or not isinstance(v[k], int) or v[k] < 0):
                        raise SpecError(f"ledger: {k} is a whole number")
                if any("|" in str(x) or "\n" in str(x) for x in v.values()):
                    raise SpecError("ledger: no field may hold a pipe or a line break")
            elif kind == "close":
                if not isinstance(v, str) or not v.strip():
                    raise SpecError("close names the register key")
            elif kind == "email":
                if not isinstance(v, str) or not re.fullmatch(r"[0-9a-fA-F]{8,32}", v):
                    raise SpecError("email is the Gmail thread id")
            elif kind == "feed":
                if not isinstance(v, dict) or set(v) != {"entity", "match", "why"} or not all(
                        str(x).strip() for x in v.values()):
                    raise SpecError('feed is {"entity", "match", "why"}')
                v = {**v, "entity": entity_key(v["entity"])}
            ident = hashlib.sha1(json.dumps([kind, v, sorted(norm_number(x) for x in after)],
                                            sort_keys=True).encode()).hexdigest()[:12]
            out.append({"n": n, "kind": kind, "value": v, "after": after,
                        "reason": str(a.get("reason") or ""), "id": ident})
        except SpecError as e:
            errors.append(f"settle {n}: {e}")
    if errors:
        raise SpecError("\n".join(errors))
    return out


def idempotency_key(s: dict) -> str:
    """Deterministic: the same document from the same spec always sends the
    same key, so a retried create returns the first one instead of a second."""
    basis = json.dumps([s["entity"], s["type"], norm_name(s["contact"]), s["number"], s["date"],
                        s["currency"], s["total"],
                        [(l["description"], l["amount"], l["quantity"], l["account"], l["tax"])
                         for l in s["lines"]]], sort_keys=True)
    digest = hashlib.sha1(basis.encode()).hexdigest()[:10]
    slug = re.sub(r"[^A-Za-z0-9]+", "-", s["number"]).strip("-")[:60]
    return f"postbills-{s['entity']}-{s['type']}-{slug}-{digest}"


def line_amount(line: dict) -> float:
    return round(line["quantity"] * line["amount"], 2)


def tolerance(s: dict) -> float:
    taxed = sum(1 for l in s["lines"] if l["tax_amount"] is None and l["tax"] != "NONE")
    return 0.011 + 0.01 * taxed


def expected_total(s: dict, rates: dict | None) -> tuple[float | None, str]:
    """The total Xero will compute. None when a rate is needed and not known
    (offline). Inclusive and NoTax lines are already gross."""
    total = 0.0
    for l in s["lines"]:
        amt = line_amount(l)
        if s["amounts"] != "Exclusive":
            total += amt
            continue
        if l["tax_amount"] is not None:
            tax = l["tax_amount"]
        elif l["tax"] == "NONE":
            tax = 0.0
        elif rates is None:
            return None, "a tax rate is needed"
        else:
            tax = round(amt * rates[l["tax"]] / 100, 2)
        total += amt + tax
    return round(total, 2), ""


def make_lines(s: dict) -> list[dict]:
    from accounting_agent.xero.purchases import make_line
    out = []
    for l in s["lines"]:
        line = make_line(l["description"], l["quantity"], l["amount"], l["account"],
                         tax_type=l["tax"], tracking=l["tracking"])
        if l["tax_amount"] is not None:
            line["TaxAmount"] = l["tax_amount"]
        out.append(line)
    return out


# ------------------------------------------------- the clients the helpers get

def _allowed(method: str, path: str) -> bool:
    if method != "PUT":
        return False
    if path in ("Invoices", "CreditNotes", "Contacts"):
        return True
    return bool(re.fullmatch(r"(Invoices|CreditNotes)/[0-9a-fA-F-]{36}/Attachments/[^/]+", path))


class WriteGate:
    """Wraps the real client. Reads pass through; the only writes it lets out
    are a new bill, a new supplier credit note, a new contact and an
    attachment, each printed in full, entity named, before it is sent. Any
    other write raises, whatever helper asks for it."""

    def __init__(self, client, key: str, live: bool):
        self._c, self._key, self._live = client, key, live
        self.sent: list[dict] = []            # every write, for the results file

    def __getattr__(self, name):
        return getattr(self._c, name)

    def _announce(self, method: str, path: str, body_text: str, idem: str | None):
        if not _allowed(method, path):
            raise RuntimeError(f"refused: {method} {path} is not a write this poster makes")
        if self._live:
            readonly.refuse(f"{method} {path} on {full(self._key)}")
        k = f" · key {idem}" if idem else ""
        print(f"    {method} {path} on {full(self._key)}{k}")
        print(f"    payload {body_text}")
        self.sent.append({"method": method, "path": path, "key": idem, "payload": body_text[:20000]})
        if not self._live:
            raise DryRun()

    def put(self, path: str, body, idempotency_key: str | None = None, **params):
        self._announce("PUT", path, json.dumps(body, separators=(",", ":"), ensure_ascii=False),
                       idempotency_key)
        return self._c.put(path, body, idempotency_key=idempotency_key, **params)

    def post(self, path: str, body, idempotency_key: str | None = None, **params):
        raise RuntimeError(f"refused: POST {path}; this poster never updates a record")

    def request(self, method: str, path: str, *a, **kw):
        if method.upper() != "GET":
            raise RuntimeError(f"refused: {method} {path}; writes go through put() only")
        return self._c.request(method, path, *a, **kw)

    def upload_attachment(self, endpoint: str, guid: str, filename: str, data: bytes,
                          content_type: str = "application/octet-stream", include_online: bool = False):
        self._announce("PUT", f"{endpoint}/{guid}/Attachments/{filename}",
                       f"<{len(data):,} bytes {content_type}>", None)
        return self._c.upload_attachment(endpoint, guid, filename, data,
                                         content_type=content_type, include_online=include_online)


# ---------------------------------------------------------------- the guard

def _shape(record: str, rec: dict, reasons: list[str]) -> dict:
    """A Xero row in the shape matching.describe prints."""
    return {
        "record": record, "date": iso_of(rec.get("DateString") or rec.get("Date")),
        "contact": (rec.get("Contact") or {}).get("Name"),
        "number": rec.get("InvoiceNumber") or rec.get("Reference") or "",
        "total": float(rec.get("Total") or 0), "currency": rec.get("CurrencyCode") or "",
        "converted": None, "bank_amount": None, "reasons": reasons,
        "bank_account": (rec.get("BankAccount") or {}).get("Name", ""),
        "reconciled": rec.get("IsReconciled"), "status": rec.get("Status"),
        "id": rec.get("InvoiceID") or rec.get("BankTransactionID") or rec.get("CreditNoteID") or "",
    }


def _same_supplier(rec: dict, cid: str | None, names: set[str]) -> bool:
    from accounting_agent.xero.matching import name_tokens
    contact = rec.get("Contact") or {}
    if cid and contact.get("ContactID") == cid:
        return True
    return bool(names & name_tokens(contact.get("Name")))


def _close(a: str, b: str, days: int) -> bool:
    try:
        return abs((date.fromisoformat(a) - date.fromisoformat(b)).days) <= days
    except ValueError:
        return False


def guard(s: dict, cid: str | None, windows: dict, number_rows: list[dict],
          credit_rows: list[dict], posted_now: list[dict]) -> tuple[list[dict], list[dict]]:
    """-> (blocking hits, weaker leads worth a look). Pure: everything it
    reads is passed in, so it is tested offline with fixture windows."""
    from accounting_agent.xero.matching import candidates, name_tokens
    names = name_tokens(s["contact"])
    num = norm_number(s["number"])
    hits: list[dict] = []
    leads: list[dict] = []
    seen: set[str] = set()

    def add(where: str, why: str, c: dict, block: bool = True):
        ident = f"{where}:{c.get('id') or c.get('number')}"
        if ident in seen:
            return
        seen.add(ident)
        (hits if block else leads).append({"entity": where, "why": why, "c": c})

    for rec in number_rows:
        if rec.get("Status") not in ("VOIDED", "DELETED"):
            add(s["entity"], "same number", _shape("bill", rec, ["number"]))
    for p in posted_now:
        if p["entity"] != s["entity"] or p["type"] != s["type"]:
            continue
        same_num = norm_number(p["number"]) == num
        same_doc = (norm_name(p["contact"]) == norm_name(s["contact"]) and p["currency"] == s["currency"]
                    and abs(p["total"] - s["total"]) < 0.011 and _close(p["date"], s["date"], GUARD_DAYS))
        if same_num or same_doc:
            add(s["entity"], "posted earlier in this batch" if same_doc else "same number, this batch",
                {"record": "this batch", "date": p["date"], "contact": p["contact"], "number": p["number"],
                 "total": p["total"], "currency": p["currency"], "converted": None, "bank_amount": None,
                 "reasons": ["batch"], "bank_account": "", "reconciled": None, "status": "posted",
                 "id": p.get("id") or ""})

    if s["type"] == "credit":
        for rec in credit_rows:
            if rec.get("Status") in ("VOIDED", "DELETED"):
                continue
            same_ref = num and norm_number(rec.get("Reference") or rec.get("CreditNoteNumber")) == num
            same_amt = (rec.get("CurrencyCode") == s["currency"]
                        and abs(float(rec.get("Total") or 0) - s["total"]) < 0.011)
            if same_ref or same_amt:
                add(s["entity"], "same reference" if same_ref else "same supplier and amount",
                    _shape("credit note", rec, ["number" if same_ref else "amount"]))
        return hits, leads

    for key, win in windows.items():
        own = key == s["entity"]
        for rec in win.get("docs", []):
            if rec.get("Type") != "ACCPAY" or rec.get("Status") in ("VOIDED", "DELETED"):
                continue
            if num and norm_number(rec.get("InvoiceNumber")) == num:
                add(key, "same number" if own else "same number, another entity",
                    _shape("bill", rec, ["number"]))
            elif (own and _same_supplier(rec, cid, names) and rec.get("CurrencyCode") == s["currency"]
                  and abs(float(rec.get("Total") or 0) - s["total"]) < 0.011
                  and _close(iso_of(rec.get("DateString") or rec.get("Date")), s["date"], GUARD_DAYS)):
                add(key, "same supplier and amount", _shape("bill", rec, ["amount", "name"]))
        if own:
            for t in win.get("txns", []):
                if (str(t.get("Type") or "").startswith("SPEND") and _same_supplier(t, cid, names)
                        and t.get("CurrencyCode") == s["currency"]
                        and abs(float(t.get("Total") or 0) - s["total"]) < 0.011
                        and _close(iso_of(t.get("DateString") or t.get("Date")), s["date"], GUARD_DAYS)):
                    add(key, "spend money for the same purchase",
                        _shape("bank transaction", t, ["amount", "name"]))
        for c in candidates(win, amount=s["total"], currency=s["currency"],
                            day=date.fromisoformat(s["date"]), names=names, spent=True, own=own):
            reasons = set(c["reasons"])
            named_amount = "name" in reasons and reasons & {"amount", "bank amount", "converted"}
            if named_amount and c["gap_days"] <= GUARD_DAYS:
                add(key, "named, same amount" + ("" if own else ", another entity"), c)
            elif c["strength"] >= 3 and own:
                add(key, "lead", c, block=False)
    return hits, leads


def acknowledged(s: dict, hit: dict) -> str | None:
    c = hit["c"]
    for k in (norm_number(c.get("number")), norm_number((c.get("id") or "")[:8]), norm_number(c.get("id"))):
        if k and k in s["ack"]:
            return s["ack"][k]
    return None


# ---------------------------------------------------------------- Xero reads

def connect(key: str):
    from accounting_agent.xero import client_for
    return client_for(key)


def read_entity(c) -> dict:
    org = (c.get("Organisation").get("Organisations") or [{}])[0]
    accounts = {a.get("Code"): a for a in c.get("Accounts").get("Accounts", []) if a.get("Code")}
    rates = {t["TaxType"]: t for t in c.get("TaxRates").get("TaxRates", [])}
    return {"accounts": accounts, "rates": rates, "base": org.get("BaseCurrency"),
            "period_lock": iso_of(org.get("PeriodLockDate")),
            "year_lock": iso_of(org.get("EndOfYearLockDate"))}


def check_against(s: dict, ctx: dict) -> tuple[list[str], list[str]]:
    """-> (errors that refuse the document, warnings Xero will rule on)."""
    errs, warns = [], []
    if ctx.get("year_lock") and s["date"] <= ctx["year_lock"]:
        errs.append(f"dated {s['date']}, on or before the end-of-year lock date {ctx['year_lock']}")
    elif ctx.get("period_lock") and s["date"] <= ctx["period_lock"]:
        warns.append(f"dated {s['date']}, inside the period lock {ctx['period_lock']}: Xero may reject it")
    for i, l in enumerate(s["lines"], 1):
        a = ctx["accounts"].get(l["account"])
        if not a:
            errs.append(f"line {i}: no account {l['account']} in {full(s['entity'])}")
        elif a.get("Status") != "ACTIVE":
            errs.append(f"line {i}: {a.get('Name')} ({l['account']}) is {a.get('Status')}")
        elif a.get("Type") == "BANK":
            errs.append(f"line {i}: {a.get('Name')} ({l['account']}) is a bank account")
        t = ctx["rates"].get(l["tax"])
        if not t:
            errs.append(f"line {i}: no tax type {l['tax']} in {full(s['entity'])}")
        elif t.get("Status") != "ACTIVE":
            errs.append(f"line {i}: tax type {l['tax']} ({t.get('Name')}) is {t.get('Status')}")
        elif t.get("CanApplyToExpenses") is False:
            warns.append(f"line {i}: tax type {l['tax']} ({t.get('Name')}) is not enabled for expenses")
    if not errs:
        rates = {k: float(v.get("EffectiveRate") or 0) for k, v in ctx["rates"].items()}
        computed, _ = expected_total(s, rates)
        if abs(computed - s["total"]) > tolerance(s):
            errs.append(f"Xero will compute {s['currency']} {money(computed)} from these lines and tax "
                        f"types, the document says {money(s['total'])}")
    return errs, warns


def resolve_contact(c, s: dict) -> tuple[str | None, str]:
    """-> (ContactID or None for a new one, how it was found). Raises SpecError."""
    from accounting_agent.xero.contacts import find_contacts, get_contact
    if s["contact_id"]:
        ct = get_contact(c, s["contact_id"])
        if norm_name(ct.get("Name")) != norm_name(s["contact"]):
            raise SpecError(f"contact_id {s['contact_id'][:8]} is {ct.get('Name')!r}, not {s['contact']!r}")
        return ct["ContactID"], "pinned"
    exact = [x for x in find_contacts(c, s["contact"]) if norm_name(x.get("Name")) == norm_name(s["contact"])]
    active = [x for x in exact if x.get("ContactStatus") == "ACTIVE"] or exact
    if len(active) == 1:
        return active[0]["ContactID"], "existing"
    if len(active) > 1:
        raise SpecError(f"{len(active)} contacts named {s['contact']!r} in {full(s['entity'])}: "
                        f"pin one with contact_id ({', '.join(x['ContactID'][:8] for x in active)})")
    if not s["new_contact"]:
        raise SpecError(f"no contact named {s['contact']!r} in {full(s['entity'])}; check the family "
                        "variants (find_contacts), then pass new_contact: true to create it")
    return None, "new"


def number_query(c, s: dict) -> list[dict]:
    q = s["number"].replace("\\", "\\\\").replace('"', '\\"')
    if s["type"] == "credit":
        return []
    return c.get("Invoices", where=f'Type=="ACCPAY" AND InvoiceNumber=="{q}"').get("Invoices", [])


def credit_query(c, s: dict, cid: str | None) -> list[dict]:
    if s["type"] != "credit" or not cid:
        return []
    d = date.fromisoformat(s["date"])
    lo, hi = d - timedelta(days=30), d + timedelta(days=30)
    w = (f'Type=="ACCPAYCREDIT" AND Contact.ContactID==Guid("{cid}") AND '
         f"Date>=DateTime({lo.year},{lo.month},{lo.day}) AND Date<=DateTime({hi.year},{hi.month},{hi.day})")
    return c.get_all("CreditNotes", where=w)


# ---------------------------------------------------------------- printing

def account_label(s: dict, code: str, ctx: dict | None) -> str:
    a = (ctx or {}).get("accounts", {}).get(code)
    return f"{a.get('Name')} ({code})" if a else f"({code})"


def print_header(n: int, total_n: int, s: dict, ctx: dict | None, contact_note: str):
    due = f" due {short_date(s['due'])}" if s["type"] == "bill" else ""
    kind = "bill" if s["type"] == "bill" else "supplier credit note"
    print(f"[{n}/{total_n}] {full(s['entity'])} · {kind} · {s['contact']}{contact_note} · {s['number']} · "
          f"{short_date(s['date'])}{due} · {s['currency']} {money(s['total'])} {s['amounts'].lower()}")
    for l in s["lines"]:
        tr = ", ".join(f"{t['Name']} {t['Option']}" for t in l["tracking"] or [])
        tax = l["tax"] + (f" {money(l['tax_amount'])}" if l["tax_amount"] is not None else "")
        qty = f"{l['quantity']:g} x " if l["quantity"] != 1 else ""
        print(f"    {account_label(s, l['account'], ctx)} · {tax}{' · ' + tr if tr else ''} · "
              f"{qty}{money(l['amount'])}")
    files = ", ".join(f"{p.name} ({max(1, p.stat().st_size // 1024)} KB)" for p in s["files"])
    print(f"    files {files}")


def print_hits(s: dict, hits: list[dict], leads: list[dict]) -> list[dict]:
    from accounting_agent.xero.matching import describe
    blocking = []
    for h in hits:
        why_ok = acknowledged(s, h)
        tag = config.entity(h["entity"]).short
        if why_ok:
            print(f"    ack   {tag} {h['why']}: {describe(h['c'])} · acknowledged: {why_ok}")
        else:
            print(f"    DUPLICATE? {tag} {h['why']}: {describe(h['c'])}")
            blocking.append(h)
    for h in leads[:3]:
        print(f"    look  {config.entity(h['entity']).short}: {describe(h['c'])}")
    if not hits:
        print("    guard clear" + (f", {len(leads)} weaker lead(s) above" if leads else ""))
    return blocking


# ---------------------------------------------------------------- posting

def safe_copy(p: Path, tmp: Path) -> Path:
    """Xero rejects < > : " / \\ | ? * + in an attachment name."""
    if not BAD_NAME.search(p.name):
        return p
    q = tmp / BAD_NAME.sub("_", p.name)
    shutil.copyfile(p, q)
    return q


def create(gate, s: dict, cid: str) -> dict:
    from accounting_agent.xero.purchases import create_bill, create_supplier_credit_note
    if s["type"] == "bill":
        return create_bill(gate, cid, make_lines(s), date=s["date"], due_date=s["due"],
                           invoice_number=s["number"], status="AUTHORISED",
                           line_amount_types=s["amounts"], currency_code=s["currency"],
                           idempotency_key=s["key"])
    return create_supplier_credit_note(gate, cid, make_lines(s), date=s["date"], reference=s["number"],
                                       status="AUTHORISED", line_amount_types=s["amounts"],
                                       currency_code=s["currency"], idempotency_key=s["key"])


def verify(s: dict, el: dict) -> tuple[str | None, list[str]]:
    """-> (record id or None, problems). Element errors come back as 200."""
    rid = el.get("InvoiceID") or el.get("CreditNoteID")
    if el.get("HasErrors") or not rid or set(rid.replace("-", "")) == {"0"}:
        errs = [e.get("Message", "") for e in el.get("ValidationErrors") or []]
        return None, errs or ["Xero returned no record"]
    flags = []
    if el.get("Status") != "AUTHORISED":
        flags.append(f"status {el.get('Status')}, not AUTHORISED")
    total = float(el.get("Total") or 0)
    if abs(total - s["total"]) > 0.011:
        flags.append(f"TOTAL MISMATCH: Xero holds {money(total)}, the document {money(s['total'])}")
    left = el.get("AmountDue") if s["type"] == "bill" else el.get("RemainingCredit")
    if left is not None and abs(float(left) - total) > 0.011:
        flags.append(f"{'amount due' if s['type'] == 'bill' else 'remaining credit'} {money(float(left))} "
                     f"is not the total {money(total)}")
    return rid, flags


def remove_feed_line(s: dict) -> str:
    why = f"{'bill' if s['type'] == 'bill' else 'credit note'} {s['number']} posted"
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "bankfeed.py"), "remove",
                        config.entity(s["feed"]["entity"]).slug, s["feed"]["match"], why], capture_output=True, text=True)
    out = (r.stdout + r.stderr).strip().splitlines()
    return ("feed " + (out[1].strip() if r.returncode == 0 and len(out) > 1 else "NOT removed: " +
                       (out[0] if out else f"exit {r.returncode}")))


def already_posted(results: Path) -> dict[tuple, dict]:
    done = {}
    if results.exists():
        for raw in results.read_text().splitlines():
            try:
                r = json.loads(raw)
            except ValueError:
                continue
            if r.get("status") == "posted":
                done[(r["entity"], r["type"], norm_number(r["number"]))] = r
    return done


def run(specs: list[dict], *, live: bool, offline: bool, cross: bool, results: Path,
        settle: list[dict] | None = None, verbose: bool = False) -> int:
    from accounting_agent.xero.client import XeroError, XeroValidationError
    mode = "LIVE" if live else ("OFFLINE CHECK" if offline else "DRY RUN")
    compact = live and not verbose
    if live:
        readonly.refuse("posting bills")
    print(f"{mode} · {len(specs)} document(s) · "
          + ", ".join(f"{full(k)} {sum(1 for s in specs if s['entity'] == k)}"
                      for k in config.entity_keys() if any(s["entity"] == k for s in specs)))
    if offline:
        for n, s in enumerate(specs, 1):
            print_header(n, len(specs), s, None, "")
            computed, why = expected_total(s, None)
            print("    total " + (f"checked {money(computed)}" if computed is not None
                                  else f"not checked offline ({why})"))
            try:
                create(WriteGate(None, s["entity"], live=False), s, "<resolved when posted>")
            except DryRun:
                pass
        print(f"OFFLINE CHECK passed: {len(specs)} document(s) well formed. Nothing was read or sent.")
        return 0

    # Every read the batch needs, before a single write. A failure here
    # leaves nothing half done.
    clients, ctxs = {}, {}
    try:
        for k in sorted({s["entity"] for s in specs}):
            clients[k] = connect(k)
            ctxs[k] = read_entity(clients[k])
        checks = {n: check_against(s, ctxs[s["entity"]]) for n, s in enumerate(specs, 1)}
        from accounting_agent.xero.matching import load_window
        lo = min(date.fromisoformat(s["date"]) for s in specs)
        hi = max(date.fromisoformat(s["date"]) for s in specs)
        windows = {}
        every = [e.key for e in config.entities() if e.bank_check_mode != "off"]
        for k in (every if cross else list(clients)):
            if k not in clients:
                clients[k] = connect(k)
            windows[k] = load_window(clients[k], lo, hi)
    except XeroError as e:
        print(f"STOPPED before anything was sent: {e}")
        return 5

    done = already_posted(results) if live else {}
    posted_now: list[dict] = []
    tally = {"ready": 0, "posted": 0, "skipped": 0, "error": 0, "posted before": 0}
    stopped, stop_at = None, None
    tmp = Path(tempfile.mkdtemp(prefix="post_bills_"))

    def record(s, status, **extra):
        if not live:
            return
        row = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "status": status,
               "entity": s["entity"], "type": s["type"], "contact": s["contact"], "number": s["number"],
               "date": s["date"], "currency": s["currency"], "total": s["total"], "key": s["key"],
               "paid": s.get("paid"), **extra}
        with results.open("a") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    def one(n: int, s: dict) -> tuple[str, str]:
        """Work one document. -> (outcome, the one line a clean post prints).
        outcome: posted, posted-flagged, ready, skipped, error, before, stop."""
        nonlocal stopped, stop_at
        k, c = s["entity"], clients[s["entity"]]
        prior = done.get((k, s["type"], norm_number(s["number"])))
        if prior:
            tally["posted before"] += 1
            posted_now.append({**s, "id": prior.get("id", "")})
            return "before", (f"[{n}/{len(specs)}] {full(k)} · {s['number']} · posted by an earlier --live of "
                              f"this spec (xid:{prior.get('id', '')[:8]}), skipped")

        errs, warns = checks[n]
        if errs:
            print_header(n, len(specs), s, ctxs[k], "")
            for e in errs:
                print(f"    REFUSED: {e}")
            tally["error"] += 1
            record(s, "error", problems=errs)
            return "error", ""

        # Reads for this document: the contact and the fresh number search.
        try:
            cid, _ = resolve_contact(c, s)
            print_header(n, len(specs), s, ctxs[k], f" ({cid[:8]})" if cid else " (NEW contact)")
            for w in warns:
                print(f"    WARN {w}")
            hits, leads = guard(s, cid, windows, number_query(c, s), credit_query(c, s, cid), posted_now)
        except SpecError as e:
            print_header(n, len(specs), s, ctxs[k], "")
            print(f"    REFUSED: {e}")
            tally["error"] += 1
            record(s, "error", problems=[str(e)])
            return "error", ""
        except XeroError as e:
            if e.status == 429:
                stopped, stop_at = f"Xero rate limit on {full(k)}", n
                return "stop", ""
            print(f"    ERROR reading Xero: {e}")
            tally["error"] += 1
            record(s, "error", problems=[str(e)])
            return "error", ""
        if print_hits(s, hits, leads):
            tally["skipped"] += 1
            print("    SKIPPED: already in Xero, or a record to read first. Acknowledge it in the spec "
                  "(ack) only once it is shown not to be this document")
            record(s, "skipped", hits=[{"entity": h["entity"], "why": h["why"], "number": h["c"].get("number"),
                                        "id": (h["c"].get("id") or "")[:8]} for h in hits])
            return "skipped", ""

        # The writes: contact if new, the document, then its files.
        gate = WriteGate(c, k, live=live)
        try:
            if cid is None:
                from accounting_agent.xero.contacts import create_contact
                try:
                    cid = create_contact(gate, s["contact"])["ContactID"]
                    print(f"    contact created xid:{cid[:8]}")
                except DryRun:
                    cid = f"<new contact {s['contact']}>"
            el = create(gate, s, cid)
        except DryRun:
            tally["ready"] += 1
            posted_now.append({**s, "id": ""})
            print("    dry run: not sent")
            return "ready", ""
        except XeroValidationError as e:
            tally["error"] += 1
            print(f"    ERROR from Xero: {'; '.join(e.messages)}")
            record(s, "error", problems=e.messages, sent=gate.sent)
            return "error", ""
        except XeroError as e:
            stopped = (f"Xero rate limit on {full(k)}" if e.status == 429
                       else f"{s['number']}: {e}; outcome unknown")
            stop_at = n
            record(s, "unknown", problems=[str(e)], sent=gate.sent)
            return "stop", ""
        except Exception as e:                                   # noqa: BLE001 - a timeout, a dropped line
            stopped, stop_at = f"{s['number']}: {type(e).__name__}: {e}; outcome unknown", n
            record(s, "unknown", problems=[stopped], sent=gate.sent)
            return "stop", ""

        rid, flags = verify(s, el)
        if not rid:
            tally["error"] += 1
            print(f"    ERROR from Xero: {'; '.join(flags)}")
            record(s, "error", problems=flags, sent=gate.sent)
            return "error", ""
        posted_now.append({**s, "id": rid})
        from accounting_agent.xero.documents import attach_file
        attached = []
        for p in s["files"]:
            try:
                attach_file(gate, "Invoices" if s["type"] == "bill" else "CreditNotes", rid, safe_copy(p, tmp))
                attached.append(p.name)
            except Exception as e:                               # noqa: BLE001
                flags.append(f"ATTACH FAILED {p.name}: {e}")
        feed_note = remove_feed_line(s) if s.get("feed") and not flags else ""
        tally["posted"] += 1
        left = el.get("AmountDue") if s["type"] == "bill" else el.get("RemainingCredit")
        line = (f"    POSTED {config.entity(k).short} · {s['contact']} · {s['number']} · {s['currency']} "
                f"{money(float(el.get('Total') or 0))} · {el.get('Status')} · "
                f"{'due' if s['type'] == 'bill' else 'credit'} {left} · "
                f"{len(attached)}/{len(s['files'])} files · xid:{rid[:8]}" + (f" · {feed_note}" if feed_note else ""))
        print(line)
        for f in flags:
            print(f"    FLAG {f}")
        record(s, "posted", id=rid, amount_due=left, files=attached, flags=flags, feed=feed_note, sent=gate.sent)
        return ("posted-flagged" if flags or "NOT removed" in feed_note else "posted"), line

    for n, s in enumerate(specs, 1):
        buf = io.StringIO()
        with (contextlib.redirect_stdout(buf) if compact else contextlib.nullcontext()):
            outcome, line = one(n, s)
        if compact:
            # A clean post is one line; anything else is printed whole.
            if outcome in ("posted", "before"):
                print(line)
            else:
                sys.stdout.write(buf.getvalue())
        elif outcome == "before":
            print(line)
        if outcome == "stop":
            break

    shutil.rmtree(tmp, ignore_errors=True)
    print(f"{mode} done · " + (" · ".join(f"{v} {k}" for k, v in tally.items() if v) or "nothing to do"))
    held = 0
    if settle:
        held = run_settle(settle, posted_now, live=live, results=results)
    if live:
        print(f"results {results}")
    if stopped:
        rest = [s["number"] for s in specs[stop_at - 1:]]
        print(f"STOPPED: {stopped}. Not confirmed: {', '.join(rest)}")
        return 5
    if tally["error"]:
        return 4
    if tally["skipped"] or held:
        return 3
    return 0


# ---------------------------------------------------------------- settling

def settled_before(results: Path) -> set[str]:
    out = set()
    if results.exists():
        for raw in results.read_text().splitlines():
            try:
                r = json.loads(raw)
            except ValueError:
                continue
            if r.get("status") == "settled" and r.get("ok"):
                out.add(r["action"])
    return out


def ledger_line(v: dict, after: list[str], posted: dict[str, dict]) -> str:
    bookkept = v.get("bookkept", len(after))
    ids = [posted[norm_number(x)]["id"][:8] for x in after if posted.get(norm_number(x), {}).get("id")]
    outcome = str(v["outcome"]).strip() + (f" (xid {', '.join(ids)})" if ids else "")
    return " | ".join([str(v["when"]).strip(), str(v["source"]).lower(), str(v["sender"]).strip(),
                       f"{v['seen']}/{bookkept}", str(v["counterparty"]).strip(), str(v["ref"]).strip(),
                       str(v["entity"]).strip(), outcome])


def append_ledger(lines: list[str]) -> None:
    """Under today's `## Run` heading, opened when the file's last one is an
    earlier day's, in one write."""
    text = LEDGER.read_text() if LEDGER.exists() else ""
    heads = re.findall(r"^## Run (\d{4}-\d{2}-\d{2})", text, re.M)
    today = date.today().isoformat()
    add = "" if not text or text.endswith("\n") else "\n"
    if not heads or heads[-1] != today:
        add += f"\n## Run {today}\n"
    with LEDGER.open("a") as fh:
        fh.write(add + "\n".join(lines) + "\n")


def close_register(pairs: list[tuple[str, str]]) -> dict[str, str]:
    """Close every line in one read-modify-write and publish the Word copy
    once, not once per line. -> {key: what happened}."""
    import outstanding
    out: dict[str, str] = {}
    with outstanding.locked():
        items = outstanding.load()
        keep = list(items)
        for key, reason in pairs:
            hit = outstanding.find(keep, key=key)
            if not hit:
                out[key] = "no such line on the register"
                continue
            keep = [i for i in keep if i is not hit]
            out[key] = f"{hit['id']} closed: {reason or 'answered by this posting'}"
        if len(keep) != len(items):
            outstanding.save(keep)
    if len(keep) != len(items):
        outstanding.publish(outstanding.load(), quiet=True)
    return out


def _gmail(token: str, method: str, path: str, body: dict | None = None) -> dict:
    import ssl
    import time
    import urllib.error
    import urllib.request
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ctx = ssl.create_default_context()
    url = "https://gmail.googleapis.com/gmail/v1/users/me/" + path
    data = json.dumps(body).encode() if body is not None else None
    for attempt in range(5):
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60, context=ctx) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            if e.code in (403, 429) or e.code >= 500:
                time.sleep(1.5 * 2 ** attempt)
                continue
            raise
    raise RuntimeError(f"Gmail kept refusing {method} {path}")


_PROCESSED: dict[str, str] = {}


def move_email(thread_id: str) -> str:
    """xero-bills, Email intake: the thread gets the processed label
    (config.company("processed_label")) and leaves the inbox."""
    from accounting_agent.gmail_auth import get_access_token
    name = config.company("processed_label", "") or ""
    if not name:
        raise RuntimeError("no processed_label in [company]")
    token = get_access_token()
    if "id" not in _PROCESSED:
        labels = _gmail(token, "GET", "labels").get("labels") or []
        hit = next((l["id"] for l in labels if l.get("name") == name), None)
        if not hit:
            raise RuntimeError(f"no Gmail label named {name!r}")
        _PROCESSED["id"] = hit
    _gmail(token, "POST", f"threads/{thread_id}/modify",
           {"addLabelIds": [_PROCESSED["id"]], "removeLabelIds": ["INBOX"]})
    return f"labelled {name}, out of the inbox"


def remove_feed(entity: str, match: str, why: str) -> tuple[bool, str]:
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "bankfeed.py"), "remove", entity, match, why],
                       capture_output=True, text=True)
    out = (r.stdout + r.stderr).strip().splitlines()
    return r.returncode == 0, (out[1].strip() if r.returncode == 0 and len(out) > 1
                               else (out[0] if out else f"exit {r.returncode}"))


def run_settle(actions: list[dict], posted_now: list[dict], *, live: bool, results: Path) -> int:
    """Do every action whose documents are posted; print the rest HELD.
    -> how many are held or failed, so the exit status says the run is not
    finished."""
    # Posted by this call, or by an earlier --live of the spec (a re-run with
    # --only carries just the documents it fixes).
    posted = {num: r for (_, _, num), r in already_posted(results).items()} if live else {}
    posted.update({norm_number(p["number"]): p for p in posted_now})
    done = settled_before(results) if live else set()
    ready, held = [], []
    for a in actions:
        if a["id"] in done:
            continue
        waiting = [x for x in a["after"] if norm_number(x) not in posted]
        (held if waiting else ready).append((a, waiting))

    def label(a: dict) -> str:
        v = a["value"]
        if a["kind"] == "ledger":
            return f"ledger line {v['when']} {v['source']} {v['sender']}"
        if a["kind"] == "feed":
            return f"feed line {v['entity']} {v['match']}"
        return f"{a['kind']} {v}"

    print(f"settle · {len(ready)} to do · {len(held)} held · {len(actions) - len(ready) - len(held)} done before")
    for a, waiting in held:
        print(f"    HELD settle {a['n']}, {label(a)}: waits on {', '.join(waiting)}")
    if not live:
        for a, _ in ready:
            print(f"    would {label(a)}")
        return 0

    def record(a: dict, ok: bool, note: str) -> None:
        row = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "status": "settled",
               "action": a["id"], "kind": a["kind"], "ok": ok, "note": note}
        with results.open("a") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(f"    {'done' if ok else 'FAILED'} settle {a['n']}, {label(a)}: {note}")

    failed = 0
    lines = [(a, ledger_line(a["value"], a["after"], posted)) for a, _ in ready if a["kind"] == "ledger"]
    if lines:
        try:
            append_ledger([l for _, l in lines])
            for a, l in lines:
                record(a, True, "appended to docs/bookkept/LEDGER.md")
        except OSError as e:
            failed += len(lines)
            for a, _ in lines:
                record(a, False, f"ledger not written: {e}")
    closes = [a for a, _ in ready if a["kind"] == "close"]
    if closes:
        try:
            got = close_register([(a["value"], a["reason"]) for a in closes])
            for a in closes:
                note = got.get(a["value"], "not reached")
                record(a, "closed" in note, note)
                failed += "closed" not in note
        except Exception as e:                                   # noqa: BLE001 - the register is not ours to lose
            failed += len(closes)
            for a in closes:
                record(a, False, f"register not changed: {type(e).__name__}: {e}")
    for a, _ in ready:
        if a["kind"] == "email":
            try:
                record(a, True, move_email(a["value"]))
            except Exception as e:                               # noqa: BLE001
                failed += 1
                record(a, False, f"Gmail: {type(e).__name__}: {e}")
        elif a["kind"] == "feed":
            v = a["value"]
            ok, note = remove_feed(v["entity"], v["match"], v["why"])
            failed += not ok
            record(a, ok, note)
    return len(held) + failed


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("spec", type=Path)
    p.add_argument("--live", action="store_true", help="post; without it nothing is sent")
    p.add_argument("--dry-run", action="store_true", help="the default: guard and payloads, nothing sent")
    p.add_argument("--offline", action="store_true", help="no network: spec checks and payloads only")
    p.add_argument("--only", default="", help="comma-separated document numbers to run")
    p.add_argument("--no-cross", action="store_true", help="skip the other entities' duplicate check")
    p.add_argument("--results", type=Path, default=None)
    p.add_argument("--verbose", action="store_true", help="a live run prints every header and payload too")
    a = p.parse_args(argv)
    if a.live and (a.dry_run or a.offline):
        p.error("--live cannot be combined with --dry-run or --offline")
    if a.live and readonly.active():
        print(f"REFUSED, nothing sent: this is a read-only session ({readonly.ENV} is set)")
        return 1
    try:
        specs = load_spec(a.spec)
        settle = load_settle(a.spec, specs)
    except (SpecError, ValueError, config.ConfigError) as e:
        print(f"REFUSED, nothing sent:\n{e}")
        return 1
    if a.only:
        want = {norm_number(x) for x in a.only.split(",") if x.strip()}
        specs = [s for s in specs if norm_number(s["number"]) in want]
        if not specs:
            print("no document in the spec matches --only")
            return 1
    results = a.results or a.spec.with_suffix(".results.jsonl")
    return run(specs, live=a.live, offline=a.offline, cross=not a.no_cross, results=results,
               settle=None if a.offline else settle, verbose=a.verbose)


if __name__ == "__main__":
    raise SystemExit(main())
