"""Purchases side: bills (ACCPAY), supplier credit notes, purchase orders, expense claims.

Bills and credit notes share the invoice status lifecycle:
  DRAFT / SUBMITTED -> fully editable; removed with Status=DELETED (not VOIDED).
  AUTHORISED        -> posts journals; fully editable while unpaid; the only
                       transitions out are AUTHORISED->AUTHORISED and ->VOIDED,
                       and voiding requires no payments/credit allocations.
  PAID              -> set by Xero when payments/credits cover the total (never
                       set directly). With any payment applied, only Reference,
                       DueDate, InvoiceNumber, BrandingThemeID, Url, Contact,
                       PlannedPaymentDate and per-line Description /
                       AccountCode (non-CIS) / Tracking remain editable.
  VOIDED / DELETED  -> terminal.
Documents in a locked period cannot be updated at all.

Expense claims: the legacy /ExpenseClaims and /Receipts endpoints are restricted
to organisations that used classic expense claims in the 6 months before
10 July 2018, new integrations must record expenses as ACCPAY bills
(``create_bill``) instead. Only a read helper is provided here.
"""

from __future__ import annotations

from typing import Any

from .client import XeroClient, XeroError

BILL_CREATE_STATUSES = ("DRAFT", "SUBMITTED", "AUTHORISED")
PURCHASE_ORDER_STATUSES = ("DRAFT", "SUBMITTED", "AUTHORISED", "BILLED", "DELETED")

# Line fields that are NOT editable once a bill has a payment applied.
_PAYMENT_LOCKED_LINE_FIELDS = (
    "Quantity", "UnitAmount", "LineAmount", "TaxType", "TaxAmount",
    "ItemCode", "DiscountRate", "DiscountAmount",
)


# -- shared helpers ----------------------------------------------------------

def make_line(
    description: str,
    quantity: float,
    unit_amount: float,
    account_code: str,
    tax_type: str | None = None,
    tracking: list[dict] | None = None,
) -> dict:
    """Build one invoice/credit-note/PO line item dict.

    tracking: up to 2 entries of {"Name": ..., "Option": ...}. tax_type
    overrides the account's default tax code when given.
    """
    line: dict[str, Any] = {
        "Description": description,
        "Quantity": quantity,
        "UnitAmount": unit_amount,
        "AccountCode": account_code,
    }
    if tax_type is not None:
        line["TaxType"] = tax_type
    if tracking is not None:
        line["Tracking"] = tracking
    return line


def _dt(date: str) -> str:
    """'YYYY-MM-DD' -> Xero where-clause DateTime literal."""
    y, m, d = (int(part) for part in date.split("-"))
    return f"DateTime({y}, {m}, {d})"


def _str(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _query_invoices(
    client: XeroClient,
    doc_type: str,
    status: str | None,
    contact_id: str | None,
    contact_name: str | None,
    date_from: str | None,
    date_to: str | None,
    due_date_from: str | None,
    due_date_to: str | None,
    unpaid_only: bool,
) -> list[dict]:
    """Shared filtered read for /Invoices (all filters use optimised fields)."""
    where = [f'Type=="{doc_type}"']
    if contact_id:
        where.append(f'Contact.ContactID==Guid("{contact_id}")')
    if contact_name:
        where.append(f"Contact.Name=={_str(contact_name)}")
    if date_from:
        where.append(f"Date>={_dt(date_from)}")
    if date_to:
        where.append(f"Date<={_dt(date_to)}")
    if due_date_from:
        where.append(f"DueDate>={_dt(due_date_from)}")
    if due_date_to:
        where.append(f"DueDate<={_dt(due_date_to)}")
    if unpaid_only:
        where.append("AmountDue>0")
    params: dict[str, Any] = {"where": " AND ".join(where), "order": "Date ASC"}
    if status:
        params["Statuses"] = status
    return client.get_all("Invoices", **params)


def _query_credit_notes(
    client: XeroClient,
    doc_type: str,
    status: str | None,
    contact_id: str | None,
    date_from: str | None,
    date_to: str | None,
) -> list[dict]:
    """Shared filtered read for /CreditNotes (optimised where fields only)."""
    where = [f'Type=="{doc_type}"']
    if status:
        where.append(f'Status=="{status}"')
    if contact_id:
        where.append(f'Contact.ContactID==Guid("{contact_id}")')
    if date_from:
        where.append(f"Date>={_dt(date_from)}")
    if date_to:
        where.append(f"Date<={_dt(date_to)}")
    return client.get_all("CreditNotes", where=" AND ".join(where), order="Date ASC")


# -- bills (ACCPAY invoices) -------------------------------------------------

def create_bill(
    client: XeroClient,
    contact_id: str,
    line_items: list[dict],
    date: str | None = None,
    due_date: str | None = None,
    invoice_number: str | None = None,
    status: str = "DRAFT",
    line_amount_types: str = "Exclusive",
    currency_code: str | None = None,
    planned_payment_date: str | None = None,
    idempotency_key: str | None = None,
) -> dict:
    """Create an ACCPAY bill (strict create via PUT).

    status: DRAFT (default, lines may be incomplete), SUBMITTED or AUTHORISED.
    PAID is never set directly (Xero sets it when a payment is applied).
    ACCPAY quirks: InvoiceNumber is non-unique (shown as Reference in the UI)
    and line discounts are not supported. Dates are "YYYY-MM-DD".
    """
    if status not in BILL_CREATE_STATUSES:
        raise XeroError(f"Bills can only be created as {BILL_CREATE_STATUSES}, not {status!r}")
    bill: dict[str, Any] = {
        "Type": "ACCPAY",
        "Contact": {"ContactID": contact_id},
        "LineItems": line_items,
        "Status": status,
        "LineAmountTypes": line_amount_types,
    }
    if date:
        bill["Date"] = date
    if due_date:
        bill["DueDate"] = due_date
    if invoice_number:
        bill["InvoiceNumber"] = invoice_number
    if currency_code:
        bill["CurrencyCode"] = currency_code
    if planned_payment_date:
        bill["PlannedPaymentDate"] = planned_payment_date
    resp = client.put("Invoices", {"Invoices": [bill]}, idempotency_key=idempotency_key)
    return resp["Invoices"][0]


def get_bills(
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
    """Fetch ACCPAY bills (all pages, full line items), ordered by Date.

    status: single or comma-separated statuses (e.g. "AUTHORISED,PAID").
    Dates are "YYYY-MM-DD" and inclusive. unpaid_only keeps AmountDue > 0
    (combine with status="AUTHORISED" for approved bills awaiting payment).
    """
    return _query_invoices(client, "ACCPAY", status, contact_id, contact_name,
                           date_from, date_to, due_date_from, due_date_to, unpaid_only)


def update_bill(client: XeroClient, invoice_id: str, changes: dict) -> dict:
    """POST partial changes to a bill.

    If "LineItems" is included it REPLACES the whole line set: lines keep their
    LineItemID to be updated, lines without one are added, omitted lines are
    deleted, always send the full set. Fully editable only while DRAFT /
    SUBMITTED / AUTHORISED-unpaid; with payments applied only the fields listed
    in the module docstring may change (see correct_bill_coding). No updates in
    locked periods.
    """
    return client.post(f"Invoices/{invoice_id}", changes)["Invoices"][0]


def void_bill(client: XeroClient, invoice_id: str) -> dict:
    """Void an AUTHORISED bill (allowed only when no payments/credit
    allocations are applied, remove them first). DRAFT/SUBMITTED bills must be
    deleted with Status=DELETED instead. VOIDED is terminal and reverses the
    posted journals."""
    return client.post(f"Invoices/{invoice_id}", {"Status": "VOIDED"})["Invoices"][0]


def approve_bill(client: XeroClient, invoice_id: str) -> dict:
    """Move a DRAFT or SUBMITTED bill to AUTHORISED (posts journals).

    No-op if already AUTHORISED; raises for PAID/VOIDED/DELETED. DRAFT bills
    with incomplete lines (e.g. missing account codes) fail Xero validation.
    """
    bill = client.get(f"Invoices/{invoice_id}")["Invoices"][0]
    current = bill["Status"]
    if current == "AUTHORISED":
        return bill
    if current not in ("DRAFT", "SUBMITTED"):
        raise XeroError(
            f"Bill {invoice_id} is {current}; only DRAFT/SUBMITTED can be approved"
        )
    return client.post(f"Invoices/{invoice_id}", {"Status": "AUTHORISED"})["Invoices"][0]


# -- supplier credit notes (ACCPAYCREDIT) ------------------------------------

def create_supplier_credit_note(
    client: XeroClient,
    contact_id: str,
    line_items: list[dict],
    date: str | None = None,
    reference: str | None = None,
    status: str = "DRAFT",
    line_amount_types: str = "Exclusive",
    currency_code: str | None = None,
    idempotency_key: str | None = None,
) -> dict:
    """Create an ACCPAYCREDIT supplier credit note (strict create via PUT).

    Same status lifecycle and line schema as bills (discounts not supported on
    the ACCPAY side). Must be AUTHORISED before it can be allocated, creating
    and allocating cannot happen in one call.
    """
    if status not in BILL_CREATE_STATUSES:
        raise XeroError(
            f"Credit notes can only be created as {BILL_CREATE_STATUSES}, not {status!r}"
        )
    note: dict[str, Any] = {
        "Type": "ACCPAYCREDIT",
        "Contact": {"ContactID": contact_id},
        "LineItems": line_items,
        "Status": status,
        "LineAmountTypes": line_amount_types,
    }
    if date:
        note["Date"] = date
    if reference:
        note["Reference"] = reference
    if currency_code:
        note["CurrencyCode"] = currency_code
    resp = client.put("CreditNotes", {"CreditNotes": [note]}, idempotency_key=idempotency_key)
    return resp["CreditNotes"][0]


def get_supplier_credit_notes(
    client: XeroClient,
    status: str | None = None,
    contact_id: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> list[dict]:
    """Fetch ACCPAYCREDIT supplier credit notes (all pages), ordered by Date.

    Useful read fields: RemainingCredit, Allocations[], FullyPaidOnDate.
    """
    return _query_credit_notes(client, "ACCPAYCREDIT", status, contact_id, date_from, date_to)


# -- purchase orders ---------------------------------------------------------

def create_purchase_order(
    client: XeroClient,
    contact_id: str,
    line_items: list[dict],
    date: str | None = None,
    delivery_date: str | None = None,
    reference: str | None = None,
    status: str = "DRAFT",
    line_amount_types: str = "Exclusive",
    currency_code: str | None = None,
    idempotency_key: str | None = None,
) -> dict:
    """Create a purchase order (strict create via PUT).

    The contact must already exist. PurchaseOrderNumber is auto-generated.
    Statuses: DRAFT/SUBMITTED/AUTHORISED on create; BILLED means copied to a
    bill (there is no API call to convert a PO to a bill, create the ACCPAY
    bill separately, then mark the PO BILLED).
    """
    if status not in ("DRAFT", "SUBMITTED", "AUTHORISED"):
        raise XeroError(f"Purchase orders are created as DRAFT/SUBMITTED/AUTHORISED, not {status!r}")
    po: dict[str, Any] = {
        "Contact": {"ContactID": contact_id},
        "LineItems": line_items,
        "Status": status,
        "LineAmountTypes": line_amount_types,
    }
    if date:
        po["Date"] = date
    if delivery_date:
        po["DeliveryDate"] = delivery_date
    if reference:
        po["Reference"] = reference
    if currency_code:
        po["CurrencyCode"] = currency_code
    resp = client.put("PurchaseOrders", {"PurchaseOrders": [po]}, idempotency_key=idempotency_key)
    return resp["PurchaseOrders"][0]


def get_purchase_orders(
    client: XeroClient,
    status: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> list[dict]:
    """Fetch purchase orders (all pages) via the endpoint's native
    Status/DateFrom/DateTo filters. Dates "YYYY-MM-DD"."""
    return client.get_all("PurchaseOrders", Status=status, DateFrom=date_from, DateTo=date_to)


def update_purchase_order_status(client: XeroClient, purchase_order_id: str,
                                 status: str) -> dict:
    """Set a purchase order's status (DRAFT/SUBMITTED/AUTHORISED/BILLED/DELETED).

    DELETED removes the PO; Xero publishes no PO transition matrix, so invalid
    moves surface as validation errors.
    """
    if status not in PURCHASE_ORDER_STATUSES:
        raise XeroError(f"Invalid PO status {status!r}; expected one of {PURCHASE_ORDER_STATUSES}")
    resp = client.post(f"PurchaseOrders/{purchase_order_id}", {"Status": status})
    return resp["PurchaseOrders"][0]


# -- expense claims (legacy, read-only) ---------------------------------------

def get_expense_claims(client: XeroClient, status: str | None = None) -> list[dict]:
    """Read legacy expense claims (statuses SUBMITTED/AUTHORISED/PAID/VOIDED).

    Only available to organisations that used classic expense claims before
    10 July 2018; claims cannot be paid via the API. Record NEW expenses as
    ACCPAY bills (create_bill), do not build on this endpoint.
    """
    where = f'Status=="{status}"' if status else None
    return client.get("ExpenseClaims", where=where).get("ExpenseClaims", [])


# -- correction helpers --------------------------------------------------------

def correct_bill_coding(client: XeroClient, invoice_id: str,
                        new_line_items: list[dict]) -> dict:
    """Re-code a bill's line items, respecting what its status permits.

    Unpaid DRAFT/SUBMITTED/AUTHORISED: replaces the full line set with
    new_line_items (POST semantics, lines without a LineItemID are created,
    omitted existing lines are deleted).

    Bills with any payment/credit applied (incl. PAID): Xero only allows
    per-line Description, AccountCode (non-CIS) and Tracking to change, with
    amounts untouched. new_line_items are matched to existing lines (by
    LineItemID when given, else by position, same line count required) and only
    those three fields are applied. Amount/quantity/tax changes raise XeroError
    with the remediation: delete the payment(s) via POST /Payments/{id}
    {"Status": "DELETED"} (and DELETE any credit-note allocations), edit the
    reverted AUTHORISED bill or void (Status=VOIDED) and recreate, then
    re-apply payments. VOIDED/DELETED bills raise (terminal). Locked-period
    bills fail with Xero's validation error.

    Returns {"action", "detail", "invoice"} describing what was done.
    """
    bill = client.get(f"Invoices/{invoice_id}")["Invoices"][0]
    if bill.get("Type") != "ACCPAY":
        raise XeroError(f"Invoice {invoice_id} is {bill.get('Type')}, not an ACCPAY bill")
    status = bill["Status"]
    if status in ("VOIDED", "DELETED"):
        raise XeroError(f"Bill {invoice_id} is {status} (terminal), recreate it instead")

    has_payments = bool(bill.get("Payments")) or bool(bill.get("CreditNotes")) \
        or float(bill.get("AmountPaid") or 0) or float(bill.get("AmountCredited") or 0)

    if status != "PAID" and not has_payments:
        updated = client.post(f"Invoices/{invoice_id}", {"LineItems": new_line_items})
        return {
            "action": "lines_replaced",
            "detail": f"Bill is {status} with no payments; full line set replaced "
                      f"({len(new_line_items)} lines).",
            "invoice": updated["Invoices"][0],
        }

    # Paid / partly paid: only Description, AccountCode, Tracking are editable.
    existing = bill.get("LineItems", [])
    by_id = {ln.get("LineItemID"): ln for ln in existing}
    if len(new_line_items) != len(existing):
        raise XeroError(
            f"Bill {invoice_id} has payments applied: the line count is fixed "
            f"({len(existing)} lines, got {len(new_line_items)}). To restructure "
            "lines, delete the payment(s) (POST /Payments/{PaymentID} "
            '{"Status": "DELETED"}), remove credit allocations, then edit, or '
            "void and recreate the bill, and re-apply payments."
        )
    merged: list[dict] = []
    changed: list[str] = []
    for pos, new in enumerate(new_line_items):
        old = by_id.get(new.get("LineItemID")) or existing[pos]
        for field in _PAYMENT_LOCKED_LINE_FIELDS:
            if field in new and new[field] != old.get(field):
                raise XeroError(
                    f"Bill {invoice_id} has payments applied: line {field} cannot "
                    "change (only Description, AccountCode and Tracking can). To "
                    "change amounts/tax, delete the payment(s) (POST "
                    '/Payments/{PaymentID} {"Status": "DELETED"}) and edit the '
                    "reverted AUTHORISED bill, or void and recreate it, then "
                    "re-apply payments."
                )
        line = {k: v for k, v in old.items() if k not in ("ValidationErrors", "Warnings")}
        for field in ("Description", "AccountCode", "Tracking"):
            if field in new and new[field] != old.get(field):
                line[field] = new[field]
                changed.append(f"line {pos + 1} {field}")
        merged.append(line)

    if not changed:
        return {
            "action": "no_change",
            "detail": "Bill has payments applied; the requested lines carry no "
                      "editable differences (Description/AccountCode/Tracking).",
            "invoice": bill,
        }
    updated = client.post(f"Invoices/{invoice_id}", {"LineItems": merged})
    return {
        "action": "recoded_in_place",
        "detail": "Bill has payments applied; updated editable fields only: "
                  + ", ".join(changed) + ". Amounts, tax and payments untouched.",
        "invoice": updated["Invoices"][0],
    }
