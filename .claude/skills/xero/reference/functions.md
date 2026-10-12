Part of the `xero` skill, read when its SKILL.md says it applies. It is as
binding as the skill itself, and the skill's safety rules apply to
everything here.

## Function reference

All functions take a `XeroClient` as first arg (except builders/helpers). Dates in and out of these functions are `"YYYY-MM-DD"` strings.

**Client core** (`from accounting_agent.xero import XeroClient, XeroError, XeroValidationError, entities, client_for`)
- `entities(refresh=False) -> dict[name, tenantId]`
- `client_for(entity) -> XeroClient`: an `Entity` from `config`, or anything `config.entity()` resolves
- `XeroClient(xero_name_or_tenant_id)`: methods `get(path, **params)`, `post(path, body, idempotency_key=None, **params)` (update / update-or-create), `put(path, body, idempotency_key=None, **params)` (strict create), `get_all(path, collection=None, page_size=1000, **params)` (auto-pagination), `request(...)` returning `(status, body, headers)`, `list_attachments(endpoint, guid)`, `upload_attachment(endpoint, guid, filename, data, content_type=..., include_online=False)`, `download_attachment(endpoint, guid, attachment)`, `iter_journals(payments_only=False)` (**needs the `accounting.journals.read` scope; will 403 if the app does not hold it, see `gotchas.md`**)

**Banking** (`from accounting_agent.xero.banking import ...`)
- `create_bank_transaction(client, txn_type, bank_account, line_items, contact=None, date=None, reference=None, is_reconciled=False, line_amount_types=None, currency_code=None, url=None, idempotency_key=None)`: txn_type SPEND/RECEIVE/SPEND-PREPAYMENT/RECEIVE-PREPAYMENT/SPEND-OVERPAYMENT/RECEIVE-OVERPAYMENT. Leave `is_reconciled` False, always (safety rule 7)
- `get_bank_transactions(client, bank_account=None, from_date=None, to_date=None, reconciled=None, status=None, where=None, order=None)`
- `update_bank_transaction(client, bank_transaction_id, updates, idempotency_key=None)` / `void_bank_transaction(client, bank_transaction_id)`
- `create_bank_transfer(client, from_account, to_account, amount, date=None, reference=None, idempotency_key=None)` / `get_bank_transfers(client, from_date=None, to_date=None, where=None, order=None)`
- `get_payments(client, from_date=None, to_date=None, status=None, where=None, order=None)` / `delete_payment(client, payment_id)` (hook asks first; never on a reconciled payment)
- `get_batch_payments(client, where=None, order=None)` / `delete_batch_payment(client, batch_payment_id)`
- `get_prepayments(client, status=None, where=None, order=None)` / `get_overpayments(...)`: read only
- `find_unreconciled(client, bank_account=None, since=None)` / `reconciliation_summary(client, from_date=None, to_date=None)` / `list_bank_accounts(client)`
- No payment creation or allocation function exists (safety rule 7)

**Purchases** (`...xero.purchases`)
- `make_line(description, quantity, unit_amount, account_code, tax_type=None, tracking=None)`: shared line builder (also re-exported by `sales`)
- `create_bill(client, contact_id, line_items, date=None, due_date=None, invoice_number=None, status="DRAFT", line_amount_types="Exclusive", currency_code=None, planned_payment_date=None, idempotency_key=None)`: ACCPAY
- `get_bills(client, status=None, contact_id=None, contact_name=None, date_from=None, date_to=None, due_date_from=None, due_date_to=None, unpaid_only=False)`
- `update_bill(client, invoice_id, changes)` / `void_bill(client, invoice_id)` / `approve_bill(client, invoice_id)`
- `correct_bill_coding(client, invoice_id, new_line_items)`: status-aware recode; on paid bills only Description/AccountCode/Tracking change
- `create_supplier_credit_note(client, contact_id, line_items, date=None, reference=None, status="DRAFT", line_amount_types="Exclusive", currency_code=None, idempotency_key=None)` / `get_supplier_credit_notes(client, status=None, contact_id=None, date_from=None, date_to=None)`. Credit notes are left unallocated
- `create_purchase_order(client, contact_id, line_items, date=None, delivery_date=None, reference=None, status="DRAFT", line_amount_types="Exclusive", currency_code=None, idempotency_key=None)` / `get_purchase_orders(client, status=None, date_from=None, date_to=None)` / `update_purchase_order_status(client, purchase_order_id, status)`
- `get_expense_claims(client, status=None)`: legacy, read-only, likely unauthorised (see `gotchas.md`)

**Sales** (`...xero.sales`)
- `create_invoice(client, contact_id, line_items, date=None, due_date=None, invoice_number=None, reference=None, status="DRAFT", line_amount_types="Exclusive", currency_code=None, branding_theme_id=None, expected_payment_date=None, idempotency_key=None)`: ACCREC
- `get_invoices(client, status=None, contact_id=None, contact_name=None, date_from=None, date_to=None, due_date_from=None, due_date_to=None, unpaid_only=False)`
- `update_invoice(client, invoice_id, changes)` / `void_invoice(client, invoice_id)` / `approve_invoice(client, invoice_id)`
- `email_invoice(client, invoice_id)` / `get_online_invoice_url(client, invoice_id)`
- `create_credit_note(client, contact_id, line_items, date=None, reference=None, credit_note_number=None, status="DRAFT", line_amount_types="Exclusive", currency_code=None, idempotency_key=None)` / `get_credit_notes(client, status=None, contact_id=None, date_from=None, date_to=None)`
- `get_quotes(client, status=None, contact_id=None, date_from=None, date_to=None, expiry_date_from=None, expiry_date_to=None, quote_number=None)` / `create_quote(client, contact_id, line_items, date, expiry_date=None, quote_number=None, reference=None, title=None, summary=None, terms=None, line_amount_types="Exclusive", currency_code=None, idempotency_key=None)`

**Journals** (`...xero.journals`)
- `make_journal_line(account_code, amount, description=None, tax_type=None, tracking=None)`: debits positive, credits negative
- `prepare_manual_journal(narration, lines, date=None, status="DRAFT", line_amount_types="NoTax")`: validates balance locally, raises `ValueError` on imbalance
- `create_manual_journal(client, payload, idempotency_key=None)` (always DRAFT) / `post_manual_journal(client, payload_or_id, idempotency_key=None)` (payload: create as POSTED; ManualJournalID: flip DRAFT to POSTED)
- `get_manual_journal(client, manual_journal_id)` / `get_manual_journals(client, status=None, date_from=None, date_to=None, narration_contains=None)`
- `update_manual_journal(client, manual_journal_id, changes)` / `void_manual_journal(client, manual_journal_id)` (DRAFT to DELETED, POSTED to VOIDED)

**Reports** (`...xero.reports`), all read-only
- `run_report(client, name, **params)` / `report_to_rows(report_json)` (flatten to list of dicts) / `save_report_csv(report_json, path)`
- `trial_balance(client, date=None)` / `profit_and_loss(client, from_date=None, to_date=None, periods=None, timeframe=None, tracking=None)` / `balance_sheet(client, date=None, periods=None, timeframe=None)`
- `aged_payables(client, contact_id=None, date=None)` / `aged_receivables(client, contact_id=None, date=None)`: Xero 400s without contactID
- `bank_summary(client, from_date=None, to_date=None)` / `budget_summary(client)` / `executive_summary(client)`

**Documents** (`...xero.documents`)
- `attach_file(client, endpoint, guid, file_path, include_online=False)`: endpoint one of `ATTACHMENT_ENDPOINTS` (Invoices, CreditNotes, BankTransactions, BankTransfers, Contacts, Accounts, ManualJournals, PurchaseOrders, Quotes, Receipts, RepeatingInvoices)
- `get_attachments(client, endpoint, guid)` / `download_all(client, endpoint, guid, out_dir)`

**Contacts** (`...xero.contacts`)
- `find_contacts(client, search_term)` (optimised searchTerm) / `get_contact(client, contact_id)`
- `create_contact(client, name, email=None, is_supplier=None, is_customer=None)` / `archive_contact(client, contact_id)` (no delete or merge via API)

## Patterns

The examples use the fictional group in `config/group.example.toml`. In a
real run the entity comes from `config`, never a literal.

### Read bills / invoices with filters

```python
from accounting_agent.xero import client_for
from accounting_agent.xero.purchases import get_bills
from accounting_agent.xero.sales import get_invoices

c = client_for("OPCO_US")
unpaid = get_bills(c, status="AUTHORISED", unpaid_only=True)
q2 = get_invoices(c, date_from="2026-04-01", date_to="2026-06-30", contact_name="Contoso Cloud")
for b in unpaid:
    print(b["InvoiceNumber"], b["Contact"]["Name"], b["AmountDue"], b["DateString"][:10])
```

### Create + post a manual journal

```python
import uuid
from accounting_agent.xero import client_for
from accounting_agent.xero.journals import (
    make_journal_line, prepare_manual_journal, create_manual_journal, post_manual_journal)

c = client_for("HOLDCO")
lines = [
    make_journal_line("<audit fees code>", 1250.00, "Accrue August audit fee"),   # debit
    make_journal_line("<accruals code>", -1250.00, "Accrue August audit fee"),    # credit
]
payload = prepare_manual_journal("August audit fee accrual", lines, date="2026-08-31")
# ^ raises ValueError with the imbalance if lines don't net to zero; fix before any API call
key = uuid.uuid4().hex
draft = create_manual_journal(c, payload, idempotency_key=key)   # DRAFT, safe default
# After user confirmation to post:
posted = post_manual_journal(c, draft["ManualJournalID"], idempotency_key=key + "-post")
```

Account codes come from the entity's chart (`c.get_all("Accounts")`) and `rules/`, never from memory. System accounts (AR/AP/retained earnings) and bank accounts cannot be journalled to. Journals dated on or before the org's lock date are rejected.

### Spend / receive money

```python
import uuid
from accounting_agent.xero import client_for
from accounting_agent.xero.banking import create_bank_transaction, list_bank_accounts
from accounting_agent.xero.purchases import make_line

c = client_for("OPCO_US")
bank = next(a for a in list_bank_accounts(c) if "Mercury" in a["Name"])
txn = create_bank_transaction(
    c, "SPEND", bank["AccountID"],          # bank accounts often lack a Code: use AccountID
    [make_line("Card fee August", 1, 12.50, "<bank fees code>")],
    contact="Example Bank", date="2026-08-15", reference="Card fee Aug",
    idempotency_key=uuid.uuid4().hex,
)
```

Created AUTHORISED (no draft state). Contact is required for SPEND/RECEIVE. Spend money is only for what `rules/GROUP.md`'s transaction-type rule allows; a documented supplier cost is a bill.

### Payments (read only)

```python
from accounting_agent.xero.banking import get_payments
pays = get_payments(c, from_date="2026-08-01", where='Status=="AUTHORISED"')
for p in pays:
    print(p["Date"], p["Amount"], p["IsReconciled"], p.get("Invoice", {}).get("InvoiceNumber"))
```

Payments are read to answer questions (what is settled, what is reconciled) and for the phantom check. They are never created or allocated by the agent (safety rule 7). A customer receipt with no invoice, or a supplier refund, is recorded by a person; the agent reports it.

### Bank transfer

```python
from accounting_agent.xero.banking import create_bank_transfer
xfer = create_bank_transfer(c, bank["AccountID"], savings["AccountID"], 50000.00,
                            date="2026-08-18", reference="Sweep", idempotency_key=uuid.uuid4().hex)
```

Transfers are create/delete only; Xero generates the SPEND-TRANSFER/RECEIVE-TRANSFER pair.

### Attach / download documents

```python
from accounting_agent.xero.documents import attach_file, get_attachments, download_all

attach_file(c, "Invoices", bill["InvoiceID"], "data/intake/receipt.pdf")   # bills live under Invoices
files = get_attachments(c, "Invoices", bill["InvoiceID"])
download_all(c, "BankTransactions", txn_id, "data/intake/attachments")
```

Re-uploading the same filename replaces the attachment. Max 10 per document; keep files under 3 MB to be safe (Xero's limits vary by endpoint).

### Run + flatten reports

```python
from accounting_agent.xero.reports import profit_and_loss, trial_balance, report_to_rows, save_report_csv

pl = profit_and_loss(c, from_date="2026-01-01", to_date="2026-06-30", periods=6, timeframe="MONTH")
rows = report_to_rows(pl)          # list of dicts keyed by header row; AccountID attached where present
save_report_csv(pl, "data/reports/opco_us_pl.csv")
```

Report cell values are strings; join on `AccountID` (from cell Attributes), not on display text. Aged payables/receivables need a `contact_id`.

### Multi-entity loop (reads)

```python
from accounting_agent import config
from accounting_agent.xero import client_for
from accounting_agent.xero.reports import profit_and_loss, save_report_csv

for e in config.entities():
    pl = profit_and_loss(client_for(e), from_date="2026-07-01", to_date="2026-07-31")
    save_report_csv(pl, f"data/reports/pl_{e.slug}.csv")
    print("done", e.title)
```

Never put writes in such a loop without per-entity confirmation (safety rule 4).
