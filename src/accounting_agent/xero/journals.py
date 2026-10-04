"""Manual journals: the only write path into the Xero general ledger.

Key Xero constraints (see docs/XERO_API_REFERENCE.md section 4.1/4.2):
- Journals must balance: line amounts (debits positive, credits negative)
  net to zero, else Xero returns a 400 ValidationException. We validate
  locally in prepare_manual_journal before any API call.
- At least two journal lines; max 2 tracking categories per line.
- System accounts (AR, AP, retained earnings) and bank accounts cannot be
  journalled to, use clearing accounts.
- Statuses: DRAFT (default) -> POSTED. A draft is removed with DELETED;
  a posted journal is reversed with VOIDED (posted journals cannot be
  deleted). Writes dated on or before the org's PeriodLockDate are rejected.

NOTE on the raw GL feed: the read-only /Journals endpoint (every posted
transaction, offset-paged) requires the accounting.journals.read scope,
a premium scope that apps created after March 2026 hold only with Xero's
approval. client.iter_journals exists but will 403 until the scope is
granted; use the Reports endpoints (reports.py) plus per-endpoint reads for
GL review instead.
"""

from __future__ import annotations

from typing import Any

from .client import XeroClient


def make_journal_line(
    account_code: str,
    amount: float,
    description: str | None = None,
    tax_type: str | None = None,
    tracking: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Build one JournalLine. Debits are positive, credits negative.

    tracking: list of {"Name": <category>, "Option": <option>}, max 2 per
    line; both category and option must already exist in the organisation.
    """
    if tracking and len(tracking) > 2:
        raise ValueError("Xero allows at most 2 tracking categories per journal line")
    line: dict[str, Any] = {"AccountCode": str(account_code), "LineAmount": amount}
    if description is not None:
        line["Description"] = description
    if tax_type is not None:
        line["TaxType"] = tax_type
    if tracking:
        line["Tracking"] = tracking
    return line


def prepare_manual_journal(
    narration: str,
    lines: list[dict[str, Any]],
    date: str | None = None,
    status: str = "DRAFT",
    line_amount_types: str = "NoTax",
) -> dict[str, Any]:
    """Build a ManualJournal payload, validating balance locally.

    Raises ValueError with the imbalance if line amounts don't net to zero
    (Xero would reject with a 400 otherwise). With Exclusive/Inclusive tax,
    Xero evaluates balance on tax-adjusted amounts, so a locally-balanced
    journal can still be rejected, NoTax (the manual-journal default) is
    exact. date is "YYYY-MM-DD"; Xero defaults to today if omitted.
    """
    if len(lines) < 2:
        raise ValueError("A manual journal needs at least two journal lines")
    total = round(sum(float(line.get("LineAmount", 0)) for line in lines), 2)
    if total != 0:
        raise ValueError(
            f"Journal lines must sum to 0; they sum to {total:+.2f} "
            f"(debits positive, credits negative)"
        )
    payload: dict[str, Any] = {
        "Narration": narration,
        "JournalLines": lines,
        "Status": status,
        "LineAmountTypes": line_amount_types,
    }
    if date is not None:
        payload["Date"] = date
    return payload


def create_manual_journal(client: XeroClient, payload: dict[str, Any],
                          idempotency_key: str | None = None) -> dict[str, Any]:
    """Create a manual journal as DRAFT (PUT = strictly create-new).

    Returns the created journal (with ManualJournalID) from Xero's response.
    """
    payload = {**payload, "Status": "DRAFT"}
    resp = client.put("ManualJournals", {"ManualJournals": [payload]},
                      idempotency_key=idempotency_key)
    return resp["ManualJournals"][0]


def post_manual_journal(client: XeroClient, payload_or_id: dict[str, Any] | str,
                        idempotency_key: str | None = None) -> dict[str, Any]:
    """Post a journal to the ledger.

    Pass a payload dict (from prepare_manual_journal) to create it directly
    as POSTED, or a ManualJournalID to flip an existing DRAFT to POSTED.
    Rejected if dated on/before the org's lock dates.
    """
    if isinstance(payload_or_id, str):
        journal = get_manual_journal(client, payload_or_id)
        update = {
            k: journal[k]
            for k in ("Narration", "Date", "LineAmountTypes", "JournalLines",
                      "Url", "ShowOnCashBasisReports")
            if k in journal
        }
        update["ManualJournalID"] = payload_or_id
        update["Status"] = "POSTED"
        resp = client.post(f"ManualJournals/{payload_or_id}", {"ManualJournals": [update]})
        return resp["ManualJournals"][0]
    payload = {**payload_or_id, "Status": "POSTED"}
    if "ManualJournalID" in payload:
        resp = client.post(f"ManualJournals/{payload['ManualJournalID']}",
                           {"ManualJournals": [payload]},
                           idempotency_key=idempotency_key)
    else:
        resp = client.put("ManualJournals", {"ManualJournals": [payload]},
                          idempotency_key=idempotency_key)
    return resp["ManualJournals"][0]


def get_manual_journal(client: XeroClient, manual_journal_id: str) -> dict[str, Any]:
    """Fetch a single manual journal (includes JournalLines)."""
    return client.get(f"ManualJournals/{manual_journal_id}")["ManualJournals"][0]


def get_manual_journals(
    client: XeroClient,
    status: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    narration_contains: str | None = None,
) -> list[dict[str, Any]]:
    """List manual journals, optionally filtered.

    status: DRAFT/POSTED/DELETED/VOIDED. Dates are "YYYY-MM-DD" (inclusive).
    narration_contains uses Xero's Contains(), fine for ManualJournals
    volumes, unlike Contacts where searchTerm is preferred. Paged requests
    are used so JournalLines are included (Xero omits lines on unpaged GETs).
    """
    clauses: list[str] = []
    if status is not None:
        clauses.append(f'Status=="{status}"')
    if date_from is not None:
        y, m, d = date_from.split("-")
        clauses.append(f"Date>=DateTime({int(y)},{int(m)},{int(d)})")
    if date_to is not None:
        y, m, d = date_to.split("-")
        clauses.append(f"Date<=DateTime({int(y)},{int(m)},{int(d)})")
    if narration_contains is not None:
        escaped = narration_contains.replace("\\", "\\\\").replace('"', '\\"')
        clauses.append(f'Narration.Contains("{escaped}")')
    params: dict[str, Any] = {}
    if clauses:
        params["where"] = " AND ".join(clauses)
    return client.get_all("ManualJournals", page_size=100, **params)


def update_manual_journal(
    client: XeroClient, manual_journal_id: str, changes: dict[str, Any]
) -> dict[str, Any]:
    """Update an existing manual journal (POST partial update).

    changes: fields to change, e.g. {"Narration": ...} or {"JournalLines": [...]}.
    If JournalLines are supplied they replace the existing lines and must
    balance (validated locally).
    """
    if "JournalLines" in changes:
        total = round(sum(float(l.get("LineAmount", 0)) for l in changes["JournalLines"]), 2)
        if total != 0:
            raise ValueError(f"Journal lines must sum to 0; they sum to {total:+.2f}")
    body = {"ManualJournalID": manual_journal_id, **changes}
    resp = client.post(f"ManualJournals/{manual_journal_id}", {"ManualJournals": [body]})
    return resp["ManualJournals"][0]


def void_manual_journal(client: XeroClient, manual_journal_id: str) -> dict[str, Any]:
    """Remove a journal: DRAFT -> DELETED, POSTED -> VOIDED.

    Posted journals cannot be deleted, only voided (voiding creates
    reversing entries; history is preserved).
    """
    journal = get_manual_journal(client, manual_journal_id)
    current = journal.get("Status")
    if current == "DRAFT":
        new_status = "DELETED"
    elif current == "POSTED":
        new_status = "VOIDED"
    else:
        raise ValueError(f"Cannot void/delete a journal with status {current!r}")
    return update_manual_journal(client, manual_journal_id, {"Status": new_status})
