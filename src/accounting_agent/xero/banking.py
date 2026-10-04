"""Banking & reconciliation: bank transactions, transfers, payment and batch
payment reads, prepayment/overpayment reads, and reconciliation helpers.

The agent never pays: there is deliberately no helper here that creates a
payment or batch payment, allocates a prepayment or overpayment, or force-marks
a transaction reconciled. Bills stay AUTHORISED and unpaid and a person matches
the bank statement line to them in Xero. Payments are read (get_payments) and,
with a person's approval, reversed (delete_payment).

Xero rules that shape this module:
- PUT = create, POST = update (or update-or-create); "delete" is a POST
  setting Status "DELETED".
- The public API cannot read or match bank statement lines. IsReconciled is
  a flag only, setting it never matches a statement line.
- Prepayments/Overpayments are created as BankTransactions (types
  SPEND-/RECEIVE-PREPAYMENT, SPEND-/RECEIVE-OVERPAYMENT); allocating them to
  invoices is a separate call, done by a person in Xero.

Dates go to Xero as "YYYY-MM-DD" strings.
"""

from __future__ import annotations

from typing import Any

from .client import XeroClient, XeroError

GUID_DASHES = (8, 13, 18, 23)


def _is_guid(value: str) -> bool:
    return (
        len(value) == 36
        and all(value[i] == "-" for i in GUID_DASHES)
        and all(c in "0123456789abcdefABCDEF" for i, c in enumerate(value) if i not in GUID_DASHES)
    )


def _esc(value: str) -> str:
    """Escape a literal for a Xero where clause (backslashes, then quotes)."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _date_expr(field: str, op: str, date: str) -> str:
    """Render "YYYY-MM-DD" as a where-clause DateTime(y,m,d) comparison."""
    y, m, d = (int(part) for part in date.split("-"))
    return f"{field}{op}DateTime({y},{m},{d})"


def _account_ref(account: str | dict) -> dict:
    if isinstance(account, dict):
        return account
    return {"AccountID": account} if _is_guid(account) else {"Code": account}


def _contact_ref(contact: str | dict) -> dict:
    if isinstance(contact, dict):
        return contact
    return {"ContactID": contact} if _is_guid(contact) else {"Name": contact}


def _where(clauses: list[str]) -> str | None:
    return " AND ".join(clauses) or None


def _bank_account_clause(bank_account: str, field_prefix: str = "BankAccount") -> str:
    if _is_guid(bank_account):
        return f'{field_prefix}.AccountID==Guid("{bank_account}")'
    return f'{field_prefix}.Code=="{_esc(bank_account)}"'


# -- bank transactions (spend / receive money) -------------------------------

def create_bank_transaction(
    client: XeroClient,
    txn_type: str,
    bank_account: str | dict,
    line_items: list[dict],
    contact: str | dict | None = None,
    date: str | None = None,
    reference: str | None = None,
    is_reconciled: bool = False,
    line_amount_types: str | None = None,
    currency_code: str | None = None,
    url: str | None = None,
    idempotency_key: str | None = None,
) -> dict:
    """Create a spend/receive money transaction (PUT /BankTransactions).

    txn_type: SPEND, RECEIVE, SPEND-OVERPAYMENT, RECEIVE-OVERPAYMENT,
    SPEND-PREPAYMENT or RECEIVE-PREPAYMENT (transfer types are read-only;
    use create_bank_transfer). Required by Xero: Type, BankAccount (must be a
    bank-type account, by Code or AccountID), LineItems (each needs a
    Description plus UnitAmount/LineAmount and an account); Contact is
    required for SPEND/RECEIVE. Reference only applies to SPEND/RECEIVE.
    Created with Status AUTHORISED, there is no draft state.
    IsReconciled=true only flags the transaction; it matches no statement
    line, so use it only where the account has no feed or imports.
    """
    txn: dict[str, Any] = {
        "Type": txn_type,
        "BankAccount": _account_ref(bank_account),
        "LineItems": line_items,
        "IsReconciled": is_reconciled,
    }
    if contact is not None:
        txn["Contact"] = _contact_ref(contact)
    if date:
        txn["Date"] = date
    if reference:
        txn["Reference"] = reference
    if line_amount_types:
        txn["LineAmountTypes"] = line_amount_types
    if currency_code:
        txn["CurrencyCode"] = currency_code
    if url:
        txn["Url"] = url
    data = client.put("BankTransactions", {"BankTransactions": [txn]},
                      idempotency_key=idempotency_key)
    return data["BankTransactions"][0]


def get_bank_transactions(
    client: XeroClient,
    bank_account: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    reconciled: bool | None = None,
    status: str | None = None,
    where: str | None = None,
    order: str | None = None,
) -> list[dict]:
    """Fetch spend/receive transactions with line detail (paged, all pages).

    bank_account is a Code or AccountID; status is AUTHORISED, DELETED or
    VOIDED; dates are inclusive "YYYY-MM-DD" bounds on the transaction Date.
    """
    clauses = [where] if where else []
    if bank_account:
        clauses.append(_bank_account_clause(bank_account))
    if from_date:
        clauses.append(_date_expr("Date", ">=", from_date))
    if to_date:
        clauses.append(_date_expr("Date", "<=", to_date))
    if reconciled is not None:
        clauses.append(f"IsReconciled=={str(reconciled).lower()}")
    if status:
        clauses.append(f'Status=="{_esc(status)}"')
    return client.get_all("BankTransactions", where=_where(clauses), order=order)


def update_bank_transaction(client: XeroClient, bank_transaction_id: str,
                            updates: dict, idempotency_key: str | None = None) -> dict:
    """Update one transaction (POST /BankTransactions/{id}) with the changed fields."""
    data = client.post(f"BankTransactions/{bank_transaction_id}",
                       {"BankTransactions": [updates]}, idempotency_key=idempotency_key)
    return data["BankTransactions"][0]


def void_bank_transaction(client: XeroClient, bank_transaction_id: str) -> dict:
    """Void/delete a bank transaction by POSTing Status DELETED (not editable back)."""
    return update_bank_transaction(client, bank_transaction_id, {"Status": "DELETED"})


# -- bank transfers -----------------------------------------------------------

def create_bank_transfer(
    client: XeroClient,
    from_account: str | dict,
    to_account: str | dict,
    amount: float,
    date: str | None = None,
    reference: str | None = None,
    idempotency_key: str | None = None,
) -> dict:
    """Create a transfer between two bank accounts (PUT /BankTransfers).

    Required: FromBankAccount, ToBankAccount, Amount. Xero generates a
    SPEND-TRANSFER and RECEIVE-TRANSFER bank transaction pair (IDs come back
    as From/ToBankTransactionID). Transfers are create/delete only, no update.
    """
    transfer: dict[str, Any] = {
        "FromBankAccount": _account_ref(from_account),
        "ToBankAccount": _account_ref(to_account),
        "Amount": amount,
    }
    if date:
        transfer["Date"] = date
    if reference:
        transfer["Reference"] = reference
    data = client.put("BankTransfers", {"BankTransfers": [transfer]},
                      idempotency_key=idempotency_key)
    return data["BankTransfers"][0]


def get_bank_transfers(
    client: XeroClient,
    from_date: str | None = None,
    to_date: str | None = None,
    where: str | None = None,
    order: str | None = None,
) -> list[dict]:
    """Fetch bank transfers (not paged, returned in full)."""
    clauses = [where] if where else []
    if from_date:
        clauses.append(_date_expr("Date", ">=", from_date))
    if to_date:
        clauses.append(_date_expr("Date", "<=", to_date))
    return client.get("BankTransfers", where=_where(clauses),
                      order=order).get("BankTransfers", [])


# -- payments -----------------------------------------------------------------

def delete_payment(client: XeroClient, payment_id: str) -> dict:
    """Reverse a payment (POST /Payments/{id} with Status DELETED); this
    restores the target document's outstanding amount. Only with a person's
    explicit approval: the project hook asks before it runs."""
    data = client.post(f"Payments/{payment_id}", {"Status": "DELETED"})
    return data["Payments"][0]


def get_payments(
    client: XeroClient,
    from_date: str | None = None,
    to_date: str | None = None,
    status: str | None = None,
    where: str | None = None,
    order: str | None = None,
) -> list[dict]:
    """Fetch payments (paged, all pages). status is AUTHORISED or DELETED;
    PaymentType distinguishes invoice payments from credit/refund payments."""
    clauses = [where] if where else []
    if from_date:
        clauses.append(_date_expr("Date", ">=", from_date))
    if to_date:
        clauses.append(_date_expr("Date", "<=", to_date))
    if status:
        clauses.append(f'Status=="{_esc(status)}"')
    return client.get_all("Payments", where=_where(clauses), order=order)


# -- batch payments -----------------------------------------------------------

def get_batch_payments(client: XeroClient, where: str | None = None,
                       order: str | None = None) -> list[dict]:
    """Fetch batch payments (Type PAYBATCH for bills, RECBATCH for sales)."""
    return client.get("BatchPayments", where=where,
                      order=order).get("BatchPayments", [])


def delete_batch_payment(client: XeroClient, batch_payment_id: str) -> dict:
    """Delete a batch payment (POST /BatchPayments/{id} with Status DELETED).

    Delete support is per the current OpenAPI spec; older docs said batches
    could not be deleted via the API, verify against the org before relying
    on it in a pipeline.
    """
    data = client.post(f"BatchPayments/{batch_payment_id}", {"Status": "DELETED"})
    return data["BatchPayments"][0]


# -- prepayments & overpayments -----------------------------------------------

def get_prepayments(client: XeroClient, status: str | None = None,
                    where: str | None = None, order: str | None = None) -> list[dict]:
    """Fetch prepayments (paged). Status: AUTHORISED (credit remaining),
    PAID (fully allocated/refunded), VOIDED. RemainingCredit drives allocation."""
    clauses = [where] if where else []
    if status:
        clauses.append(f'Status=="{_esc(status)}"')
    return client.get_all("Prepayments", where=_where(clauses), order=order)


def get_overpayments(client: XeroClient, status: str | None = None,
                     where: str | None = None, order: str | None = None) -> list[dict]:
    """Fetch overpayments (paged). Same status/RemainingCredit semantics as
    prepayments. Both documents are read+allocate only on these endpoints;
    they are created as bank transactions (see create_bank_transaction)."""
    clauses = [where] if where else []
    if status:
        clauses.append(f'Status=="{_esc(status)}"')
    return client.get_all("Overpayments", where=_where(clauses), order=order)


# -- reconciliation helpers -----------------------------------------------------

def find_unreconciled(client: XeroClient, bank_account: str | None = None,
                      since: str | None = None) -> list[dict]:
    """AUTHORISED bank transactions not yet matched to a statement line.

    Statement lines themselves are not exposed by the public API, this is
    the Xero-side half of reconciliation only.
    """
    return get_bank_transactions(client, bank_account=bank_account,
                                 from_date=since, reconciled=False,
                                 status="AUTHORISED")


def reconciliation_summary(client: XeroClient, from_date: str | None = None,
                           to_date: str | None = None) -> list[dict]:
    """Per-bank-account movement from the BankSummary report: one dict per
    account with the report's columns (opening/closing balance, cash
    received/spent) plus AccountID. No per-line reconciliation status."""
    report = client.get("Reports/BankSummary", fromDate=from_date,
                        toDate=to_date)["Reports"][0]
    headers: list[str] = []
    out: list[dict] = []

    def walk(rows: list[dict]) -> None:
        nonlocal headers
        for row in rows:
            if row.get("RowType") == "Header":
                headers = [c.get("Value", "") for c in row.get("Cells", [])]
            elif row.get("RowType") == "Section":
                walk(row.get("Rows", []))
            elif row.get("RowType") == "Row":
                cells = row.get("Cells", [])
                entry: dict[str, Any] = {}
                for header, cell in zip(headers, cells):
                    entry[header or "Account"] = cell.get("Value")
                    for attr in cell.get("Attributes", []):
                        if attr.get("Id") == "accountID":
                            entry["AccountID"] = attr.get("Value")
                out.append(entry)

    walk(report.get("Rows", []))
    return out


def list_bank_accounts(client: XeroClient) -> list[dict]:
    """BANK-type accounts from /Accounts (includes credit cards and PayPal
    accounts, BankAccountType distinguishes them)."""
    return client.get("Accounts", where='Type=="BANK"').get("Accounts", [])


def find_account(client: XeroClient, name_or_code: str) -> dict:
    """Resolve any account by exact Code, exact name, or unique name substring.

    Raises XeroError if nothing matches or the substring is ambiguous.
    """
    accounts = client.get("Accounts").get("Accounts", [])
    for a in accounts:
        if a.get("Code") == name_or_code or a["Name"].lower() == name_or_code.lower():
            return a
    needle = name_or_code.lower()
    matches = [a for a in accounts if needle in a["Name"].lower()]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise XeroError(f"No account matches {name_or_code!r} in {client.entity_name}")
    raise XeroError(
        f"Ambiguous account {name_or_code!r}, matches {[a['Name'] for a in matches]}"
    )
