---
name: xero
description: All Xero operations across the group's entities (listed in config/group.toml) - reading, posting and correcting transactions (bills, invoices, bank transactions), manual journals, reports, attachments and reconciliation support. Load this whenever a task touches Xero in any way.
---

# Xero operations

## Safety rules (read first)

These are **LIVE production ledgers** for real companies. There is no sandbox.

1. **Before ANY write** (create / update / void / approve / upload): state the target entity by its **full Xero name** (`config.entity(...).xero_name`, e.g. "Example Holdings Limited"), show the exact payload or change you are about to send, and **get user confirmation in the conversation**, unless the user's instruction in the current message already specifies the operation unambiguously (entity, document, amounts, coding) or a standing workflow (e.g. `xero-bills`) covers it.
2. **Always pass an idempotency key on creates** (`idempotency_key=` is accepted by every create function; use `uuid.uuid4().hex` or a deterministic key for the task).
3. **Never delete or void anything without explicit instruction.** `void_bill`, `void_invoice`, `void_manual_journal`, `void_bank_transaction`, `delete_payment`, `delete_batch_payment` are irreversible or reversing operations.
4. **Never run writes inside a multi-entity loop without per-entity confirmation.** Reads across all entities are fine; writes need a per-entity go-ahead.
5. Documents and journals may be created **posted (AUTHORISED/POSTED) directly**, with no draft-first or manual-approval step, when the coding is confident and follows `rules/` or a standing workflow (e.g. `xero-bills`). Use DRAFT only when genuinely uncertain and awaiting an answer, and prefer not posting at all (query instead) over posting a guess.
6. **NEVER create a bill (or spend/receive money) that already exists. Check first, every time.** Before ANY bill or transaction create, search the target entity for the same supplier + `InvoiceNumber`, and the same supplier + amount within a few days (all statuses incl. DRAFT; a VOIDED/DELETED prior copy is not a duplicate). Invoices often also flow in via a receipt-capture tool or another intake route, so an emailed or Slacked invoice may already have a bill in Xero. A hit means **skip and report "already in Xero"**, never create a second copy. Idempotency keys (rule 2) only stop same-session retries; they do NOT protect against this.

6a. **The bank is the source of truth on cash**: [docs/BANKING.md](../../../docs/BANKING.md).
   Xero is what we think happened; the statement is what happened. Any question
   about whether something was paid, which entity paid it, whether it was paid
   twice, or whether a refund landed is answered from the statement first. If
   the two disagree, the bank wins and Xero is what gets corrected. The
   statement is on the server: saved CSVs under `data/statements/` for
   movements on or before each account's `csv_until` date, and the read-only
   bank API clients for anything after (`config.bank_accounts(key)` says which
   applies to which account). Read it, do not ask for it.

7. **NEVER create or allocate a PAYMENT. NEVER mark a bill paid. NEVER touch
   the bank.** A run that creates payments leaves phantom entries sitting
   unreconciled against the bank accounts, each one competing with a real
   statement line. The division of labour is fixed:

   | Step | Who |
   |---|---|
   | Bank feed imports into Xero | automatic |
   | Bank lines read for context | the agent, from the bank feed (`scripts/bankfeed.py`) |
   | Invoices arrive (Slack/email): create the **BILL**, attach the document, leave it **AUTHORISED and unpaid** | **the agent** |
   | Match the statement line to the bill (Find & Match / OK) | **the user, manually, in the UI** |

   Creating a payment produces a phantom bank entry that competes with the
   real statement line, which is exactly the duplicate the user is trying to
   avoid. **An AUTHORISED bill with `AmountDue` equal to its total is the
   correct, finished state.** Do not "helpfully" settle it.

   The payment writers are **not in this library at all**: `xero/banking.py`
   has no `create_payment`, `create_payments_batch`, `create_batch_payment`,
   `allocate_prepayment`, `allocate_overpayment` or `mark_reconciled`, and
   there is no `allocate_credit_note`. Do not write them back, and never send
   `is_reconciled=True` or a raw `PUT/POST Payments`.
   `.claude/hooks/guard-payments.sh` (wired as a `PreToolUse` hook) blocks
   these both in Bash **and** in any `.py` file you try to Write/Edit, so
   moving the code into a script to run later does not work either. Do not
   attempt to evade it, and never reach for `dangerouslyDisableSandbox`.
   Deleting a payment is downgraded to a confirmation prompt rather than
   blocked, because it is sometimes the remedy (see the warning below).

   Detection, not just prevention: `scripts/check_no_phantom_payments.py`
   lists unreconciled payments on bank accounts across every entity in
   `config/group.toml` and is a **mandatory exit check** for every
   invoice-bookkeeping run.

   **Never delete a payment that is already reconciled** (`IsReconciled: true`):
   that is the user's own matching work and undoing it re-opens the bill.
   See also the reconciled-transaction immutability gotcha below.

   *Payroll bills cleared from control accounts:* the agent posts a payroll
   **bill** (AUTHORISED, source documents attached) as normal where
   `rules/PAYROLL.md` says that source is a bill. Clearing it against the
   payroll control accounts (`config.payroll_controls()`) is a payment the
   **user** posts by hand: the agent proposes the clearing with the amount per
   control account, never posts it, even with approval. The reconcile-to-bill
   matching on the bank side is the user's too.

## Running code

Run from the repo root with the project venv; the package lives under `src/`:

```bash
.venv/bin/python - <<'EOF'
import sys; sys.path.insert(0, "src")
from accounting_agent import config
from accounting_agent.xero import XeroClient, client_for, entities
from accounting_agent.xero import banking, purchases, sales, journals, reports, documents, contacts

print([e.title for e in config.entities()])
print(entities())     # what the Xero connection can see
EOF
```

Auth is already set up (token in `.xero/`); 429s and token refresh are handled inside the client. Validation failures raise `XeroValidationError`: its `.messages` lists Xero's per-element errors, so print them.

## The entities

The entities come from `config/group.toml`, never from this file. Resolve one
with `config.entity(name)` (key, slug, alias, short name or Xero name) and get
a client for it with `client_for(entity)`:

```python
from accounting_agent import config
from accounting_agent.xero import client_for, XeroClient

e = config.entity("us")              # Entity(key="OPCO_US", xero_name="Example Operations Inc.", ...)
c = client_for(e)                    # same as XeroClient(e.xero_name)
c = client_for("OPCO_US")            # a key, alias or name also works
```

`XeroClient(x)` takes the organisation's **exact** Xero name or a tenant ID.
Never address an entity by a fuzzy substring: names inside one group usually
share most of their words, so a short substring is ambiguous or, worse,
silently picks the wrong organisation. Go through `config.entity()` and
`client_for()` every time. `entities()` returns `{name: tenantId}` for what
the Xero connection can see (cached in `.xero/connections.json`); an entity
in `group.toml` that is missing there is a connection problem to report, not
an entity to skip silently.

## Bookkeeping criteria: rules/ (load when coding or judging transactions)

Any task that decides *where* something is booked (coding a bill, bank
transaction or journal, choosing an account or the recognising entity, or
judging an existing entry) must be checked against `rules/`, which is where
the group writes its own logic. Route by topic:

- **Always**: `rules/GROUP.md`: group structure, cost-recognition principles,
  any group-split or recharge accounts, and the **transaction-type rule**
  (supplier invoices are bookkept as BILLS; spend money only for payroll
  control payments, leases, naturally invoice-less items, transfers and
  intercompany legs). Read it for any bookkeeping decision.
- **Entity-specific bookkeeping**: also the entity's own file,
  `rules/entities/<KEY>.md` (`config.entity(...).rules_file`): role, revenue
  posting, payroll specifics, critical accounts, tax registrations.
- **Payroll**: anything touching a payroll report, payroll journal or bill,
  an employer-of-record or PEO invoice, or a payroll control account:
  `rules/PAYROLL.md`, with the line-item to account mapping for every payroll
  source, the bills-vs-journals rule per source, the control-account
  inventory and FX handling. Mappings there are binding; an unmapped line item
  is a query, never a guess. Read only the sections for the entity in scope.
- **Expense coding**: `rules/EXPENSES.md`: coding hierarchy, per-entity P&L
  account whitelists, and the rule for which entity bears staff expenses. It
  directs you, when you need a specific supplier's usual treatment, to
  `rules/SUPPLIERS.md`: look suppliers up with
  `grep -i "^| <name>" rules/SUPPLIERS.md` (the file grows large; never read
  it whole for a handful of suppliers).
- **Intercompany and group-cost items**: `rules/INTERCOMPANY.md`: which
  intercompany flavour carries what between each pair, and their strict
  routing. The account codes themselves are in `config.intercompany_pairs()`.
- **Review method and known pitfalls**: `docs/REVIEW_METHOD.md`.

Pure API mechanics (running a report, downloading attachments, listing accounts) don't need these.

## Function reference

All functions take a `XeroClient` as first arg (except builders/helpers). Dates in and out of these functions are `"YYYY-MM-DD"` strings.

**Client core** (`from accounting_agent.xero import XeroClient, XeroError, XeroValidationError, entities, client_for`)
- `entities(refresh=False) -> dict[name, tenantId]`
- `client_for(entity) -> XeroClient`: an `Entity` from `config`, or anything `config.entity()` resolves
- `XeroClient(xero_name_or_tenant_id)`: methods `get(path, **params)`, `post(path, body, idempotency_key=None, **params)` (update / update-or-create), `put(path, body, idempotency_key=None, **params)` (strict create), `get_all(path, collection=None, page_size=1000, **params)` (auto-pagination), `request(...)` returning `(status, body, headers)`, `list_attachments(endpoint, guid)`, `upload_attachment(endpoint, guid, filename, data, content_type=..., include_online=False)`, `download_attachment(endpoint, guid, attachment)`, `iter_journals(payments_only=False)` (**needs the `accounting.journals.read` scope; will 403 if the app does not hold it, see gotchas**)

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
- `get_expense_claims(client, status=None)`: legacy, read-only, likely unauthorised (see gotchas)

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

## Fixed Assets API (scope `assets`)

Base `https://api.xero.com/assets.xro/1.0/` + `xero-tenant-id` header (raw
urllib/curl; the XeroClient helpers target the accounting API). Verified
mechanics:

- `GET Assets?status=DRAFT`: status values MUST be uppercase (`Draft` gives
  403, not a validation error). `GET AssetTypes` returns a bare list.
  `GET Settings` shows the org's FAR start date and last depreciation run.
- **Register a draft**: `POST Assets` with the existing `assetId` +
  `assetNumber` in the body (update-by-id; `PUT Assets/{id}` 404s). Include
  `assetTypeId`, `purchaseDate`, `purchasePrice`, `assetStatus: "Registered"`,
  `bookDepreciationSetting` ({depreciationMethod: "StraightLine",
  averagingMethod: "FullMonth", depreciationRate: N,
  depreciationCalculationMethod: "Rate"}) and `bookDepreciationDetail`
  ({depreciationStartDate}). The start date must be >= purchaseDate.
- **Registering silently drops depreciation settings unless the body is
  complete.** A first `POST Assets` registration can return 200 with
  `assetStatus: "Registered"` while the stored `bookDepreciationSetting`
  comes back as only `{depreciationCalculationMethod: "None"}`, with no
  method and no rate. Re-POSTing the same body **plus `assetName`** (and
  `effectiveFromDate` on the setting) fixes it; without `assetName` the retry
  400s with "AssetName ... can't be blank". Always re-read the asset after
  registering and compare its `bookDepreciationSetting` to a known-good
  registered asset in the same org.
- **The re-POST workaround does not always work, and the write fails
  silently.** Registrations have returned 200 with `assetStatus:
  "Registered"`, some even echoing the full setting back in the response,
  while the re-read had dropped it; a second POST with the complete body
  (`assetName`, `effectiveFromDate`, `depreciationCalculationMethod` as
  `Rate` / `Life` rather than `None`) returned 200 again and left the setting
  emptier still. So the asset is registered at the right cost and type but
  depreciates at nothing, and the response is not evidence either way.
  **Re-read after every registration; if the setting is still empty after one
  retry, stop and report it as a manual action**: setting the depreciation
  method on a registered asset is UI-only from that point, and a silent retry
  loop just writes nothing repeatedly.
- **The retry usually does work, and `depreciationCalculationMethod: "None"`
  on the re-read is not evidence of failure.** Make the retry before
  reporting a manual action. Read the right field when judging the result:
  compare the asset's `depreciationMethod`, `averagingMethod` and
  `depreciationRate` or `effectiveLifeYears` against the **asset type's own**
  setting from `GET AssetTypes`. Asset types can themselves carry
  `depreciationCalculationMethod: "None"`, so an asset echoing `None` there
  while holding the method and the rate is depreciating exactly as its type
  says. Only a setting still empty of method and rate after the retry is the
  UI-only case above.
- **Create an asset type**: `POST AssetTypes` with assetTypeName +
  fixedAssetAccountId / depreciationExpenseAccountId /
  accumulatedDepreciationAccountId (AccountIDs from the accounting API) +
  bookDepreciationSetting.
- **`purchasePrice` CAN be changed on a REGISTERED asset that already has
  posted depreciation**: `POST Assets` with a **complete** body (`assetId`,
  `assetNumber`, `assetName`, `purchaseDate`, the new `purchasePrice`,
  `assetTypeId`, `assetStatus: "Registered"`, a full
  `bookDepreciationSetting` and `bookDepreciationDetail`) returns 200 and the
  new cost sticks. This is the route for pulling recoverable tax or any other
  journalled-out amount back out of an asset's cost. **Xero then recomputes
  accumulated depreciation on the new cost and leaves the depreciation already
  posted to the GL alone.** That GL/FAR gap is real, it is UI-only to close
  (roll back and re-run depreciation), and the run that creates it has to say
  so.
- **Cannot change depreciation settings on a REGISTERED asset that already
  has posted depreciation**: `POST Assets` with a full body returns
  **HTTP 500**, with a minimal `{assetId, assetNumber,
  bookDepreciationSetting}` body returns **HTTP 403**. Neither alters the
  asset. Switching an asset to "No depreciation", and **rolling back
  already-posted depreciation**, are both **UI-only**.
- **No DELETE**: junk or orphaned drafts can only be deleted in the Xero UI.
- A bill line with Quantity N creates **N drafts at unit price**.
- Depreciation policy per entity (rates, useful lives, capitalisation
  threshold): `rules/EXPENSES.md` and the entity's `rules/entities/<KEY>.md`.
- FAR start date (Settings) is read-only via API; first-time setup is
  UI-only. Running depreciation is also UI-only.

## Talking to people on Slack

Runs may be triggered from Slack and questions go back the same way. Full model
in the `xero-bills` skill, section 0. The short version:

- **Admins** (`config.slack().admins`) start runs and receive the report. A run
  belongs to whoever started it: that is who gets its questions and its
  summary.
- **Users** (`config.slack().users`) supply information and answer questions,
  but cannot start a run and never see the report. DM them directly for the
  thing only they know; then tell the admin what they said and what you did
  with it. **Read-only** members (`config.slack().readonly`) may also ask
  `query <question>`, answered by a read-only session.
- Who to chase for a missing invoice: `config.section("slack").get("chase_routing", {})`,
  applied per the `xero-bills` skill, "Missing invoices". Never message anyone
  in `config.section("slack").get("never_message", [])`.
- Ask questions *as you hit them*, not batched at the end, and never block
  waiting for an answer. Replies arrive as context on the next run.
- Post with `chat.postMessage` and a bare user ID as `channel`, using
  `SLACK_BOT_TOKEN` from `.env`. `conversations.open` may fail with
  `missing_scope`; posting to a bare user ID works either way.
- **The channel** (`config.slack().channel_id`, named
  `config.slack().channel_name`) takes a bookkeeping run's report (headline top
  level, whole report in its thread) and any manual action the user has to
  take by hand, the `bill-payments` reconcile-to-intercompany line above all,
  in the same shape from every run: a `MANUAL ITEMS (date, time)` headline
  top level, the items as the first reply in its thread. Nothing else goes
  there: a query, a focused run, a status reply and a chase all stay with the
  person who asked. Rules in `docs/COMMS.md`, "Where a message goes".

## Platform gotchas

- **`GET BankTransactions` returns DELETED and VOIDED transactions too, and
  `IsReconciled` is `false` on every one of them.** A deleted spend therefore
  looks exactly like a live unreconciled one unless you read `Status`, and a
  human's clean-up reads as a duplicate. **Always filter `Status` to
  `AUTHORISED` before concluding anything from a bank-transaction list**, and
  never judge "is this outstanding?" from `IsReconciled` alone. The same
  applies to any `where=` query: Xero does not exclude deleted rows for you.
- **`GET Invoices` / `GET BankTransactions` / `GET CreditNotes` list
  endpoints DO return `LineItems`.** Sweeping a whole account's transactions
  over a period therefore needs one paged list call per collection, not a
  `GET {id}` per document; the per-document loop is what burns the 5,000/day
  tenant cap. `GET ManualJournals` likewise returns `JournalLines`. Note the
  list form of `ManualJournals` omits `DateString` (only `Date` as
  `/Date(ms)/`), so parse the epoch rather than assuming the string variant is
  there.
- **`CurrencyRate` is foreign units per unit of base, so the base amount is
  `AmountDue / CurrencyRate`.** Multiplying inflates every foreign-currency
  figure and the error is silent: a large foreign-currency travel bill can
  come out at tens of millions in base currency, and a whole entity's payables
  at hundreds of millions. It holds the same way round in every entity,
  whatever the base: a GBP bill in a USD-based entity carries a rate near
  0.75, a EUR bill in a GBP-based entity a rate near 1.17. Sanity-check any
  converted total against the spot rate before reporting it.
- **Dates come back as `/Date(1787011200000+0000)/`** on most fields; prefer the `*String` variants (`DateString`, `DueDateString`, `FullyPaidOnDate` etc.) or parse the ms epoch. Dates you *send* are plain `"YYYY-MM-DD"`.
- **Bank accounts may have no `Code`**: reference them by `AccountID` (all helpers accept either; a 36-char GUID is auto-detected).
- **Payments are create/delete only**: no updates. Deleting shows as "Payment Reversed" and recreating as "Payment made" in the document's history (allocation records only, no bank movement). Recreating is the user's job, never the agent's (safety rule 7).
- **Batch payments only work from base-currency bank accounts**: a foreign-currency bank account returns "Bank accounts must use the same currency as the organisations base currency". Relevant when reading why a user settled a one-debit-many-bills case with individual payments sharing a Reference.
- **No statement-line or reconcile API.** `is_reconciled=True` only sets a flag; it matches no statement line, and it is banned here anyway. Real reconciliation strategy: pre-create fully coded transactions so a human just clicks Match/OK in the UI. See the `xero-reconcile` skill.
- **The `accounting.journals.read` scope is premium** and may not be held: `client.iter_journals()` then 403s. Use reports and per-endpoint reads for GL review.
- **ManualJournals carry no `JournalNumber`.** The `#` number the Xero UI shows on a journal lives only on `/Journals` (see above), so without that scope it cannot be read. When reporting a journal, follow `docs/COMMS.md`: entity, date and narration, never the GUID.
- **ExpenseClaims is legacy**: usually returns 401. Record expenses as ACCPAY bills via `create_bill`.
- **Rate limits: 60 calls/min and 5,000/day per entity** (429s auto-retried once with backoff, but budget daily sweeps). An uncertified Xero app has a small connection cap; connecting more organisations than it allows means dropping one, so check the cap before adding an entity to `config/group.toml`.
- **The 5,000/day cap is reachable in one task, and a day-throttle 429 looks
  exactly like a minute-throttle 429.** Iterating `GET Invoices/{id}` /
  `GET BankTransactions/{id}` over a whole quarter to read line items burns a
  tenant's daily quota, after which *every* call, even `GET Organisation`,
  returns 429 and the client's built-in retry cannot clear it. **Read the
  429's headers before assuming it will pass**: `X-Rate-Limit-Problem` is
  `minute` or `day`, and `Retry-After` is hours for the day cap vs seconds
  for the minute cap. `client.request()` returns `(status, body, headers)`,
  so:

  ```python
  status, body, headers = c.request("GET", "Organisation")
  if status == 429:
      print(headers.get("X-Rate-Limit-Problem"), headers.get("Retry-After"))
  ```

  Budget accordingly: prefer list endpoints and reports (one call) over
  per-document GETs, and when you do need line items for a whole period,
  pull them once and cache to a local JSON file rather than re-querying.

  **The cap is per tenant per day and an earlier run can have spent it before
  yours starts, which turns a write batch into an unverifiable one**: if the
  cap hits partway through a batch, the read that would say which writes
  landed also 429s, and nothing can be confirmed for hours. Two habits fix
  it: **pass `idempotency_key` on every write** (safety rule 2; Xero honours
  the key for 24 h, so a re-post after the cap lifts is safe rather than a
  duplicate; `create_manual_journal` and `post_manual_journal` both take one),
  and **probe the cap before a batch of writes** with one cheap
  `GET Organisation`, reading `X-Rate-Limit-Problem` on any 429. A write batch
  begun on a nearly-spent tenant should be deferred, not attempted.
- **Status lifecycles**: DRAFT/SUBMITTED are deleted (not voided); AUTHORISED posts journals and can only go to VOIDED (needs payments/allocations removed first); PAID is set by Xero, never directly. Paid documents allow only Reference/DueDate/Contact/per-line Description+AccountCode+Tracking edits; `correct_bill_coding` handles this automatically.
- **AUTHORISED documents require a DueDate**: creating a bill/invoice with `status="AUTHORISED"` and no `due_date` fails validation ("The document DueDate field must be specified"); DRAFT doesn't require it. Convention: due date = invoice date for card/receipt-type bills.
- **Element-level errors don't raise**: the client sends `summarizeErrors=false`, so a failed create can return HTTP 200 with `InvoiceID` all-zeros, `HasErrors: true` and `StatusAttributeString: "ERROR"` instead of raising `XeroValidationError`. **Always check `HasErrors`/a real GUID on the returned element** before treating a write as done.
- **Locked periods** reject all updates; writes dated on or before the lock date fail with a validation error.
- **`update_*` with `LineItems`/`JournalLines` REPLACES the whole line set**: always send every line (keep `LineItemID`s), or lines get deleted.
- **Contact-name variants:** the same supplier can exist as several contacts (a short business name, the full legal name, a "Ltd"/"Inc." variant, a per-entity or per-service variant). `get_bills(contact_name=...)` resolves to ONE ContactID, so it silently misses the other variants. Before concluding a document doesn't exist: run `find_contacts` for the family and query every variant, or pull bills unfiltered and match on a fuzzy name. Never declare "not posted" from a single contact_name filter.
- **Updating LineItems on a foreign-currency invoice can silently RESET its CurrencyRate:** a POST with only `LineItems` has been seen to reset a bill's rate to that day's rate, in the inverted convention, inflating every base-currency GL value on the bill, while identical sibling updates did not. Do not rely on it not happening. After any line update on a non-base-currency document, re-read `CurrencyRate` and compare to the pre-edit value. Restoring it requires the document's payments to be deleted, `{"CurrencyRate": <old>}` POSTed, and the payments recreated (the rate edit is silently ignored while payments exist); payments are the user's, so report this as a manual action rather than doing it.
- **On a PAID bill, line-AMOUNT edits fail SILENTLY.** A POST that changes
  `UnitAmount`/`LineAmount` on a paid bill's existing lines returns HTTP 200
  with `HasErrors: false` and no ValidationErrors, but only the Description /
  AccountCode / Tracking changes persist; the amounts come back unchanged on
  re-read (even when the invoice total is left identical). Adding a line to a
  paid bill DOES raise a proper element error ("To update fields on a paid
  invoice line item, you must supply a LineItemID"). Consequence: a bill can
  only be re-split (e.g. pulling recoverable tax onto its own line) BEFORE it
  is paid, so where payments exist and are off-limits, fix it with a reclass
  manual journal instead. Always re-read after any paid-bill edit.
- **Manual journals honour an explicit `TaxAmount`**: a JournalLine with
  `LineAmountTypes: "Exclusive"`, a tax-bearing `TaxType` and
  `TaxAmount: <actual>` stores that exact tax rather than recomputing the
  rate on the line. That is the way to journal input tax where part of the
  spend is outside the scope of the tax (service charges). Debits and
  credits must balance INCLUDING the tax, so `prepare_manual_journal`'s local
  balance check will reject it: build the payload dict directly for
  tax-bearing journals.
- **A bill with ANY payment rejects structural line edits** ("To update fields on a paid invoice line item, you must supply a LineItemID"): adding/removing lines or changing amounts would require deleting ALL its payments first. Payments are the user's; report it, or use a reclass journal. Account-code/description-only recodes are fine on paid bills.
- **Bank transactions reconciled against a real bank statement line are API-immutable:** update/unreconcile/void all return HTTP 200 whose element even echoes `"Status": "DELETED"`, but carries `StatusAttributeString: "ERROR"` + ValidationErrors "cannot be edited as it has been reconciled with a Bank Statement", and nothing persists; `void_bank_transaction`'s return value alone is NOT proof. Always re-read after voiding. Unreconcile/remove is UI-only. `IsReconciled: true` on a fetched transaction does not distinguish statement-matched (immutable) from flag-only (editable), and an account you believe has no feed may still have statement lines.
- **Xero Expenses items are API-IMMUTABLE:** bills created
  by Xero Expenses (staff expense claims) reject every modification with
  `HasErrors: true`, `StatusAttributeString: "ERROR"` and ValidationErrors
  "Sorry this was created by Xero Expenses and can't be modified via the API".
  The helper (`correct_bill_coding`) returns without raising and its returned
  element even echoes the NEW AccountCode, so the return value is not proof:
  always re-read. **Tell-tale: `InvoiceNumber == "Expense Claims"`.** Recodes,
  reclassifications and cross-entity moves of these items are UI-only. Check
  for this marker BEFORE planning a batch of expense-claim recodes.
- **Lock dates make edits fail SILENTLY:** POSTing changes to a document dated on or before the org's lock date returns HTTP 200 with the change dropped; the only signal is a `ValidationErrors` message inside the returned document ("cannot be edited as it is currently dated before the end of year lock date"). Lock dates move in both directions: read them live from `GET Organisation` (`PeriodLockDate` / `EndOfYearLockDate`) per entity, never from a doc. After ANY recode/update, re-read the document and verify the change actually stuck; treat locked-period items as immutable and fix forward (e.g. a reclass journal dated after the lock) only when instructed.
