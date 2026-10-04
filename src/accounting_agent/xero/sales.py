"""Sales side: invoices (ACCREC), customer credit notes (ACCRECCREDIT), quotes.

Invoices and credit notes share the status lifecycle:
  DRAFT / SUBMITTED -> fully editable; removed with Status=DELETED (not VOIDED).
  AUTHORISED        -> posts journals; fully editable while unpaid; only
                       AUTHORISED->AUTHORISED and ->VOIDED transitions are
                       valid, and voiding requires no payments/allocations.
  PAID              -> set by Xero when payments/credits cover the total. With
                       any payment applied, only Reference, DueDate,
                       InvoiceNumber, BrandingThemeID, Url, Contact,
                       ExpectedPaymentDate and per-line Description /
                       AccountCode (non-CIS) / Tracking remain editable.
  VOIDED / DELETED  -> terminal.
No updates in locked periods. Emailing and online-invoice URLs are ACCREC-only.

Quote statuses: DRAFT -> SENT -> ACCEPTED/DECLINED -> INVOICED (+DELETED);
DRAFT and SENT are fully editable, later statuses allow contact/notes only.
"""

from __future__ import annotations

from typing import Any

from .client import XeroClient, XeroError
from .purchases import (  # shared with the purchases side
    BILL_CREATE_STATUSES as _CREATE_STATUSES,
    _query_credit_notes,
    _query_invoices,
    make_line,
)

__all__ = [
    "make_line",
    "create_invoice", "get_invoices", "update_invoice", "void_invoice",
    "approve_invoice", "email_invoice", "get_online_invoice_url",
    "create_credit_note", "get_credit_notes",
    "get_quotes", "create_quote",
]


# -- sales invoices (ACCREC) --------------------------------------------------

def create_invoice(
    client: XeroClient,
    contact_id: str,
    line_items: list[dict],
    date: str | None = None,
    due_date: str | None = None,
    invoice_number: str | None = None,
    reference: str | None = None,
    status: str = "DRAFT",
    line_amount_types: str = "Exclusive",
    currency_code: str | None = None,
    branding_theme_id: str | None = None,
    expected_payment_date: str | None = None,
    idempotency_key: str | None = None,
) -> dict:
    """Create an ACCREC sales invoice (strict create via PUT).

    status: DRAFT (default), SUBMITTED or AUTHORISED, never PAID (apply a
    Payment to an AUTHORISED invoice instead). InvoiceNumber must be unique and
    is auto-generated from org settings if omitted. Line discounts
    (DiscountRate/DiscountAmount) are supported on ACCREC. Dates "YYYY-MM-DD".
    """
    if status not in _CREATE_STATUSES:
        raise XeroError(f"Invoices can only be created as {_CREATE_STATUSES}, not {status!r}")
    inv: dict[str, Any] = {
        "Type": "ACCREC",
        "Contact": {"ContactID": contact_id},
        "LineItems": line_items,
        "Status": status,
        "LineAmountTypes": line_amount_types,
    }
    if date:
        inv["Date"] = date
    if due_date:
        inv["DueDate"] = due_date
    if invoice_number:
        inv["InvoiceNumber"] = invoice_number
    if reference:
        inv["Reference"] = reference
    if currency_code:
        inv["CurrencyCode"] = currency_code
    if branding_theme_id:
        inv["BrandingThemeID"] = branding_theme_id
    if expected_payment_date:
        inv["ExpectedPaymentDate"] = expected_payment_date
    resp = client.put("Invoices", {"Invoices": [inv]}, idempotency_key=idempotency_key)
    return resp["Invoices"][0]


def get_invoices(
    client: XeroClient,
    status: str | None = None,
    contact_id: str | None = None,
    contact_name: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    due_date_from: str | None = None,
    due_date_to: str | None = None,
    unpaid_only: bool = False,
) -> list[dict]:
    """Fetch ACCREC sales invoices (all pages, full line items), ordered by Date.

    status: single or comma-separated statuses (e.g. "AUTHORISED,PAID").
    Dates "YYYY-MM-DD", inclusive. unpaid_only keeps AmountDue > 0 (combine
    with status="AUTHORISED" for approved invoices awaiting payment).
    """
    return _query_invoices(client, "ACCREC", status, contact_id, contact_name,
                           date_from, date_to, due_date_from, due_date_to, unpaid_only)


def update_invoice(client: XeroClient, invoice_id: str, changes: dict) -> dict:
    """POST partial changes to a sales invoice.

    If "LineItems" is included it REPLACES the whole line set: keep each
    LineItemID to update a line, omit the ID to add one, and any existing line
    left out is deleted, always send the full set. Fully editable only while
    DRAFT / SUBMITTED / AUTHORISED-unpaid; with payments applied only the
    fields in the module docstring may change. No updates in locked periods;
    US auto-sales-tax invoices are read-only.
    """
    return client.post(f"Invoices/{invoice_id}", changes)["Invoices"][0]


def void_invoice(client: XeroClient, invoice_id: str) -> dict:
    """Void an AUTHORISED sales invoice (allowed only when no payments/credit
    allocations are applied, remove them first). DRAFT/SUBMITTED invoices must
    be deleted with Status=DELETED instead. VOIDED is terminal and reverses the
    posted journals."""
    return client.post(f"Invoices/{invoice_id}", {"Status": "VOIDED"})["Invoices"][0]


def approve_invoice(client: XeroClient, invoice_id: str) -> dict:
    """Move a DRAFT or SUBMITTED sales invoice to AUTHORISED (posts journals).

    No-op if already AUTHORISED; raises for PAID/VOIDED/DELETED. Drafts with
    incomplete lines fail Xero validation on approval.
    """
    inv = client.get(f"Invoices/{invoice_id}")["Invoices"][0]
    current = inv["Status"]
    if current == "AUTHORISED":
        return inv
    if current not in ("DRAFT", "SUBMITTED"):
        raise XeroError(
            f"Invoice {invoice_id} is {current}; only DRAFT/SUBMITTED can be approved"
        )
    return client.post(f"Invoices/{invoice_id}", {"Status": "AUTHORISED"})["Invoices"][0]


def email_invoice(client: XeroClient, invoice_id: str) -> None:
    """Email an ACCREC invoice to its contact (org default template, sender is
    the authorising user).

    Status must be SUBMITTED, AUTHORISED or PAID; ACCPAY is not supported.
    Success is a 204 with no body. Daily org-wide send limits apply
    (1000 paying / 20 trial / 0 demo).
    """
    client.post(f"Invoices/{invoice_id}/Email", {})


def get_online_invoice_url(client: XeroClient, invoice_id: str) -> str:
    """Return the shareable online-invoice URL for an ACCREC invoice
    (not available while DRAFT; never available for bills)."""
    data = client.get(f"Invoices/{invoice_id}/OnlineInvoice")
    return data["OnlineInvoices"][0]["OnlineInvoiceUrl"]


# -- customer credit notes (ACCRECCREDIT) --------------------------------------

def create_credit_note(
    client: XeroClient,
    contact_id: str,
    line_items: list[dict],
    date: str | None = None,
    reference: str | None = None,
    credit_note_number: str | None = None,
    status: str = "DRAFT",
    line_amount_types: str = "Exclusive",
    currency_code: str | None = None,
    idempotency_key: str | None = None,
) -> dict:
    """Create an ACCRECCREDIT customer credit note (strict create via PUT).

    Same status lifecycle and line schema as invoices; CreditNoteNumber is
    unique on the ACCREC side. Must be AUTHORISED before allocation, creating
    and allocating cannot happen in one call. Refunds of remaining credit go
    through the Payments endpoint.
    """
    if status not in _CREATE_STATUSES:
        raise XeroError(
            f"Credit notes can only be created as {_CREATE_STATUSES}, not {status!r}"
        )
    note: dict[str, Any] = {
        "Type": "ACCRECCREDIT",
        "Contact": {"ContactID": contact_id},
        "LineItems": line_items,
        "Status": status,
        "LineAmountTypes": line_amount_types,
    }
    if date:
        note["Date"] = date
    if reference:
        note["Reference"] = reference
    if credit_note_number:
        note["CreditNoteNumber"] = credit_note_number
    if currency_code:
        note["CurrencyCode"] = currency_code
    resp = client.put("CreditNotes", {"CreditNotes": [note]}, idempotency_key=idempotency_key)
    return resp["CreditNotes"][0]


def get_credit_notes(
    client: XeroClient,
    status: str | None = None,
    contact_id: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> list[dict]:
    """Fetch ACCRECCREDIT customer credit notes (all pages), ordered by Date.

    Useful read fields: RemainingCredit, Allocations[], FullyPaidOnDate.
    """
    return _query_credit_notes(client, "ACCRECCREDIT", status, contact_id, date_from, date_to)


# -- quotes --------------------------------------------------------------------

def get_quotes(
    client: XeroClient,
    status: str | None = None,
    contact_id: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    expiry_date_from: str | None = None,
    expiry_date_to: str | None = None,
    quote_number: str | None = None,
) -> list[dict]:
    """Fetch quotes (all pages) via the endpoint's native filters.

    quote_number is a PARTIAL match. Dates "YYYY-MM-DD". Statuses: DRAFT,
    SENT, ACCEPTED, DECLINED, INVOICED, DELETED.
    """
    return client.get_all(
        "Quotes",
        Status=status, ContactID=contact_id,
        DateFrom=date_from, DateTo=date_to,
        ExpiryDateFrom=expiry_date_from, ExpiryDateTo=expiry_date_to,
        QuoteNumber=quote_number,
    )


def create_quote(
    client: XeroClient,
    contact_id: str,
    line_items: list[dict],
    date: str,
    expiry_date: str | None = None,
    quote_number: str | None = None,
    reference: str | None = None,
    title: str | None = None,
    summary: str | None = None,
    terms: str | None = None,
    line_amount_types: str = "Exclusive",
    currency_code: str | None = None,
    idempotency_key: str | None = None,
) -> dict:
    """Create a DRAFT quote (strict create via PUT). ACCREC-side only.

    Date is required (unlike invoices). Quirks: a line's TaxType does NOT
    default from the account code, set it explicitly if tax matters, and line
    Tracking accepts TrackingOptionID only (not Name/Option). QuoteNumber is
    unique and auto-generated if omitted. Editable in full only while
    DRAFT/SENT.
    """
    quote: dict[str, Any] = {
        "Contact": {"ContactID": contact_id},
        "LineItems": line_items,
        "Date": date,
        "LineAmountTypes": line_amount_types,
    }
    if expiry_date:
        quote["ExpiryDate"] = expiry_date
    if quote_number:
        quote["QuoteNumber"] = quote_number
    if reference:
        quote["Reference"] = reference
    if title:
        quote["Title"] = title
    if summary:
        quote["Summary"] = summary
    if terms:
        quote["Terms"] = terms
    if currency_code:
        quote["CurrencyCode"] = currency_code
    resp = client.put("Quotes", {"Quotes": [quote]}, idempotency_key=idempotency_key)
    return resp["Quotes"][0]
