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
5. Documents and journals may be created **posted (AUTHORISED/POSTED) directly**, with no draft-first or manual-approval step, when the coding is confident and follows `rules/` or a standing workflow (e.g. `xero-bills`), or when `scripts/resolve_gate.py` clears the run's own graded answer (CLAUDE.md, "Queries: answer them before asking them"). Use DRAFT only when genuinely uncertain and awaiting an answer, and prefer not posting at all (query instead) over posting a guess.
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
   See also the reconciled-transaction immutability gotcha in `reference/gotchas.md`.

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

## Reference: read it when the task needs it

The rest of this skill is in `.claude/skills/xero/reference/`, read with the
Read tool when it applies, not on every turn:

- `reference/functions.md`: every helper by module (client core, banking,
  purchases, sales, journals, reports, documents, contacts) and the code
  patterns. **Read it before writing Xero code you have not written in this
  session.**
- `reference/gotchas.md`: the platform's traps (deleted rows in list calls,
  which way `CurrencyRate` runs, edits that fail silently on paid, locked or
  reconciled documents, the daily rate cap). **Read it before any write you
  have not made in this session, and whenever Xero returns an error or a
  result that looks wrong.** A new quirk is written back there.
- `reference/fixed-assets.md`: the Fixed Assets API (scope `assets`):
  registering drafts, depreciation settings, cost changes, what is UI-only.
- `reference/slack.md`: who may be messaged on Slack and how; CLAUDE.md and
  `docs/COMMS.md` govern every message.
