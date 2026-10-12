"""Contacts (docs/XERO_API_REFERENCE.md section 5.1).

Constraints:
- Always key on ContactID; Xero warns Name may stop being unique.
- searchTerm is the optimised text search (Name, First/LastName,
  ContactNumber, CompanyNumber, EmailAddress); avoid Contains() where
  clauses on large orgs.
- Only Name is required to create. IsSupplier/IsCustomer are READ-ONLY,
  set automatically by Xero once AP/AR invoices exist.
- Contacts cannot be merged or deleted via the API, archive instead.
"""

from __future__ import annotations

from typing import Any

from .client import XeroClient


def find_contacts(client: XeroClient, search_term: str) -> list[dict[str, Any]]:
    """Search contacts by text across name/email/number fields.

    Uses Xero's optimised searchTerm (case-insensitive). Paged, so the full
    field set (incl. Balances, IsSupplier/IsCustomer) is returned.
    """
    return client.get_all("Contacts", searchTerm=search_term)


def get_contact(client: XeroClient, contact_id: str) -> dict[str, Any]:
    """Fetch a single contact by ContactID (full field set, incl. Balances)."""
    return client.get(f"Contacts/{contact_id}")["Contacts"][0]


def create_contact(
    client: XeroClient,
    name: str,
    email: str | None = None,
    is_supplier: bool | None = None,
    is_customer: bool | None = None,
) -> dict[str, Any]:
    """Create a contact (PUT = create-only; fails if Name already exists).

    Name max 255 chars, no angle brackets or leading/trailing/repeated
    spaces. NOTE: IsSupplier/IsCustomer are read-only in Xero, set
    automatically when AP/AR invoices exist. Values passed here are sent
    but Xero ignores them; accepted for caller intent/documentation only.
    """
    contact: dict[str, Any] = {"Name": name}
    if email is not None:
        contact["EmailAddress"] = email
    if is_supplier is not None:
        contact["IsSupplier"] = is_supplier
    if is_customer is not None:
        contact["IsCustomer"] = is_customer
    resp = client.put("Contacts", {"Contacts": [contact]})
    return resp["Contacts"][0]


def archive_contact(client: XeroClient, contact_id: str) -> dict[str, Any]:
    """Archive a contact (the API cannot delete or merge contacts)."""
    resp = client.post(
        f"Contacts/{contact_id}",
        {"Contacts": [{"ContactID": contact_id, "ContactStatus": "ARCHIVED"}]},
    )
    return resp["Contacts"][0]
