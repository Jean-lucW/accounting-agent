---
name: xero-reconcile
description: Bank-reconciliation workflow for the group's Xero entities, whose bank feeds the Xero API cannot read. Match statement data (the reconstructed bank feed, saved CSVs, the read-only bank APIs or rows a user supplies) to Xero, create correctly coded transactions and unpaid bills for the gaps, and report. Use when asked to reconcile a bank account. Never creates a payment and never marks anything reconciled.
---

# Bank reconciliation (blocked-feed workflow)

Load the `xero` skill first for the API surface, run-code pattern, and SAFETY
RULES: they all apply here. Key constraint: **the Xero API cannot read bank
statement lines or perform the Match action.** Reconciliation via API means
getting the Xero side complete and correctly coded; the human clicks Match in
the UI.

> **NEVER CREATE A PAYMENT** (`xero` skill safety rule 7, enforced by a
> `PreToolUse` hook). A statement line that settles a supplier invoice ends at
> a **bill**: if the bill exists, leave it alone and report the match for the
> user to click; if it does not exist, create the bill (AUTHORISED, invoice
> attached, unpaid) and stop. The user does the matching in the UI. Never
> create or allocate a payment, never mark anything reconciled, never set
> `IsReconciled`.

## Step 0: the bank feed (READ BEFORE ANYTHING ELSE, EVERY RUN)

`data/bankfeed/<slug>.md`, server only, one file per entity covering every
account. Every statement line not yet reconciled. Read it, then
`.venv/bin/python scripts/bankfeed.py refresh all`: it pulls every settled
movement after each account's stamp from the bank API of every account
`config/group.toml` gives an API provider (read-only, server only) and moves
the stamps. Then work. Remove a line the moment the transaction accounting for
it is posted (never batched at the end) and refresh again when the run is
done. Statement data the user supplies is still welcome, but for each account
the source is decided by the movement date: the saved CSV up to the account's
`csv_until`, the API after it; a `csv` account (no API) is read from its CSV
only. Do not ask for rows you can read. Full rules and the
`scripts/bankfeed.py` commands: [docs/BANKING.md](../../../docs/BANKING.md).
Only accounts of group entities in `config/group.toml` are ever reconciled
here.

This is the reconstruction of the feed the Xero API will not give us, and the
goal it serves is that **every cash payment on every bank account ends up as a
bill, spend money or transfer**. Chasing someone does not clear a line.

## Reference: rules/ for the coding decisions

Every company-specific judgement in this workflow is made against `rules/`,
never from memory:

- **Always**: `rules/GROUP.md` (group structure, which entity recognises which
  cost, the transaction-type rule) plus the **entity file** for the entity
  whose bank you are reconciling (`config.entity(<key>).rules_file`, e.g.
  `rules/entities/<KEY>.md`).
- **Expense lines**: `rules/EXPENSES.md` (coding hierarchy, per-entity
  whitelists, staff-expense rules); it directs you to `rules/SUPPLIERS.md` for
  per-supplier treatment. Grep it (`grep -i "<name>" rules/SUPPLIERS.md`)
  rather than reading a large file whole.
- **Transfers, intercompany settlements, cross-entity payments**:
  `rules/INTERCOMPANY.md` for which intercompany account (flavour, codes in
  `config/group.toml` `[[intercompany]]`) the line routes through.
- **Payroll**: `rules/PAYROLL.md`, with the control accounts per entity in
  `config/group.toml` `[entities.payroll_controls]`.
- **Known pitfalls** (subscriptions vs IT costs, capitalisation, paying vs
  recognising entity): `docs/REVIEW_METHOD.md`.

## What this skill is really for

In most groups many transactions arrive in Xero from upstream tools (a receipt
capture tool publishing bills with the document attached, Xero Expenses for
staff claims). Which tools your group uses, and what they publish, is
described in `rules/GROUP.md`. Do NOT re-create what they publish.

So when reconciling, the job is predominantly:

1. **Coding review, not creation**: for items an upstream tool created, the
   entry usually exists; the value-add is checking it landed in the
   **correct account** per `rules/` (GROUP.md principles, the entity's file,
   EXPENSES.md, SUPPLIERS.md, and `docs/REVIEW_METHOD.md`: supplier
   consistency vs the prior 2-3 months, read the attached invoice when in
   doubt). Flag miscodings; correct only with confirmation. **Token
   efficiency**: look suppliers up with grep, and read only the relevant
   entity's section of EXPENSES.md.
2. **The manual items**: chiefly **payroll** (see below), plus transfers,
   intercompany settlements, bank fees and similar items no upstream tool
   creates.

### Payroll journals (the main manual work)

**`rules/PAYROLL.md` is the authority**: control accounts per entity (bank
payroll payments are coded to them), journal shapes, which payroll sources are
bills and which are journals, and the master mapping tables. The user provides
payroll reports (or they exist for prior months). For each entity and month:

1. Map every line with **PAYROLL.md's mapping tables** (read only the sections
   for the entity in scope), cross-checked against the prior 2-3 months
   (`get_manual_journals(c, narration_contains=...)` for journals,
   `get_bills(c, contact_name=...)` for bill sources; the mapping must be
   identical month to month). Do not restate mappings from memory: the tables
   are binding; an unmapped line is a query.
2. Prepare ONE journal for the month's totals (not per employee) with
   `prepare_manual_journal`, and present the line-by-line mapping table to the
   user for confirmation before posting.
3. After posting, **attach the payroll report to the journal**:
   `documents.attach_file(c, "ManualJournals", journal_id, report_path)`.
4. If an admin's mapping instruction differs from prior months, update
   `rules/PAYROLL.md` (or the entity's rules file) so the new mapping becomes
   the standard.

A payroll bill cleared from a control account (rather than by a bank payment)
is the user's to clear: propose the amounts, never post the clearing payment.

## Workflow

### 1. Take the statement data

From the bank feed, the saved CSV, the API, or a file the user supplies (CSV,
PDF or pasted text). Normalise into rows of: date (YYYY-MM-DD), description,
amount (signed: money out negative), reference. Note the statement's
opening/closing balance if present: use it as a checksum at the end.

### 2. Establish the Xero side

```python
from accounting_agent import config
from accounting_agent.xero import XeroClient
from accounting_agent.xero.banking import list_bank_accounts, find_unreconciled

ent = config.entity("us")                        # key, slug, alias or short name
c = XeroClient(ent.xero_name)                    # exact organisation name, never a fuzzy short code
bank = config.bank_accounts(ent.key)[0]          # the account being reconciled
accounts = list_bank_accounts(c)                 # match by Code when config gives one, else by Name; use its AccountID
acct = next(a for a in accounts if a.get("Code") == bank.xero_account_code or bank.label in a["Name"])
existing = find_unreconciled(c, bank_account=acct["AccountID"], since="2026-07-01")
```

Also pull already-reconciled transactions in the statement window
(`get_bank_transactions(c, bank_account=..., from_date=..., to_date=...)`) and
`get_payments(c, from_date=..., to_date=...)` so statement lines that were
settled as invoice or bill payments are not wrongly re-created as spend or
receive money.

### 3. Match statement lines to existing Xero transactions

Heuristics, in order of confidence:
- **Exact amount + exact date**: near-certain match.
- **Exact amount + date within a few days** (timing differences; a card
  settles one to three days after purchase): likely; confirm if several
  candidates.
- **Amount + contact/description similarity** (statement descriptor contains
  the contact name or vice versa) as tie-breaker.
- One statement line covering several Xero payments may be a batch payment
  total: check `get_batch_payments`.

Each statement line ends in exactly one bucket: **matched** (Xero transaction
exists: run the coding review above), **unmatched** (needs creating: expected
mainly for payroll and other manual items), or **queried** (ambiguous: ask the
user, never guess). Xero-side unreconciled transactions with no statement line
are also findings (possible duplicates or not-yet-cleared items): list them,
do not delete anything.

### 4. Create coded transactions for unmatched lines

**Pick the transaction type per rules/GROUP.md's transaction-type rule.** A
statement line that settles a supplier invoice should end up matched to a
**bill**: create the bill if it is missing and leave the matching to the user.
Do NOT default it to spend money. Spend money (BankTransactions) is only for
the exception categories: payroll payments coded to the entity's payroll
control accounts (per `rules/PAYROLL.md` and config), lease payments per the
group's lease workings, naturally invoice-less items (bank fees, interest,
card spend with no invoice), and intercompany cash legs (per
`rules/INTERCOMPANY.md`); own-account movements are bank transfers.

**First present a coding table to the user and get confirmation** (safety
rule: these are live ledgers). One row per transaction to create: date,
description, amount, SPEND/RECEIVE, contact, account code, tax treatment. Look
up account codes from `c.get("Accounts")` and contacts via `find_contacts`;
reuse the coding of similar historical transactions for the same counterparty
as the default suggestion.

After confirmation:

```python
from accounting_agent.xero.banking import create_bank_transaction
from accounting_agent.xero.purchases import make_line

for row in confirmed_rows:
    create_bank_transaction(
        c, "SPEND" if row.amount < 0 else "RECEIVE", acct["AccountID"],
        [make_line(row.description, 1, abs(row.amount), row.account_code)],
        contact=row.contact, date=row.date, reference=row.reference,
        idempotency_key=f"recon-{acct['AccountID'][:8]}-{row.date}-{row.reference or abs(row.amount)}",
    )
```

Statement lines that are actually settlements of open invoices or bills are
**left for the user to match**: make sure the bill exists (create it, unpaid,
if missing) and list the line as "matches bill X, match in the UI". Never post
a payment against the document.

### 5. Mark reconciled: DON'T

**Never set the reconciled flag** on anything. Every bank account the agent
works on receives feed or statement lines, so force-flagging strands the real
statement line unmatched and produces exactly the phantom-entry duplicates
this workflow exists to prevent. Creating correctly coded transactions and
unpaid bills is the whole job; the human clicks Match/OK.

Know this about Xero: a transaction already reconciled against a statement
line is **API-immutable**. Update, unreconcile and void return 200 with the
change silently dropped; only `ValidationErrors` in the response reveal it,
and unreconcile is UI-only. Always check the response.

**Direct debits that settle several bills** (an insurer or benefits provider
collecting a quarter's premiums in one debit, split across monthly bills):
these are never spend money. They are settled by the user as bill payments
split across the bills they cover. The agent confirms the bills exist with
the relevant portion unpaid and reports the split per bill; it never posts
the payment, with or without approval. Which such debits your group has is in
`rules/PAYROLL.md` or `rules/SUPPLIERS.md`.

### 6. Report

Finish with:

```python
from accounting_agent.xero.banking import reconciliation_summary
summary = reconciliation_summary(c, from_date="2026-07-01", to_date="2026-07-31")
```

and present: per-account opening/closing movement from the summary, plus
tables of (a) created transactions (with Xero references and coding, never
GUIDs in a message), (b) matched lines, (c) unmatched/queried statement lines,
(d) Xero-side unreconciled transactions with no statement counterpart. If the
statement closing balance was supplied, state whether Xero's closing balance
now agrees and by how much it differs (tie-out method: `docs/BANKING.md`).
Finish a run that posted anything with
`.venv/bin/python scripts/check_no_phantom_payments.py`; it must print PASS.
