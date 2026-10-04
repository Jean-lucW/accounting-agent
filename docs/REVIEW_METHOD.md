# Bookkeeping review: method and checklist

How to review bookkeeping already in Xero, whether a full review or a check
of specific comments. The method and checklist here are generic. What
"right" means for your group (which entity, which account, which split) is
in `rules/`; this file says how to test the books against it.

## Review method

When asked to "review the bookkeeping", or to assess specific review
comments:

1. **Load the criteria selectively.** Always `rules/GROUP.md` and the entity
   file(s) in scope (`rules/entities/<KEY>.md`). Load `rules/EXPENSES.md`,
   `rules/ALLOCATION.md`, `rules/PAYROLL.md` or `rules/INTERCOMPANY.md` only
   when that topic is in play. Read `rules/SUPPLIERS.md` only by searching
   for the supplier, never whole.
2. **Pull the data through the Xero layer**: manual journals, bills, bank
   transactions, the P&L by month, and attachments where needed. Filter
   every read; never page a whole ledger to check one line.
3. **Consistency with prior months is the primary test.** For every item
   under review, look at the last two or three months of the same supplier,
   account or pattern, further back if needed, and assess whether it is
   bookkept the same way. Deviations need a reason.
4. **Look at the invoice, not just the supplier.** The coding method in
   `rules/EXPENSES.md` applies to reviews too: a qualitatively different
   invoice from a known supplier is investigated before assuming the usual
   account.
5. **Apply the benefit principle**: the cost belongs in the entity that
   benefits, except where `rules/ALLOCATION.md` splits it on a key.
6. **Output findings per entity**, each with a severity, the evidence (the
   transaction and the prior pattern it breaks), and the suggested
   correction. Findings that need a person go into the outstanding-items
   register (`.claude/skills/outstanding-items/SKILL.md`).

## Common issues checklist

- **Wrong transaction type.** Supplier invoices must be bills matched to the
  bank feed, not spend money. Exceptions are listed in `rules/GROUP.md`
  (transaction-type rule): payroll bank lines to control accounts, leases per
  their schedules, naturally invoice-less items, transfers and intercompany
  cash. A supplier invoice sitting as spend money outside these, or a bill
  raised for bank fees, is a finding.
- **Supplier in the wrong account or entity**, against its entry in
  `rules/SUPPLIERS.md` and its prior months.
- **Wrong entity.** Against the recognition matrix in `rules/GROUP.md`. Two
  failure modes: flagging a cross-paid cost that follows the established
  pattern (it is not a miscoding), and a cost quietly left in the paying
  entity when the pattern says it belongs elsewhere.
- **One intercompany leg missing.** A cost recognised in one entity and paid
  by another needs both legs: the payer's bank line to intercompany and the
  recogniser's bill settled from the mirror account. Check both sides mirror
  each other and use the account `rules/INTERCOMPANY.md` names for that flow.
- **Shared cost not allocated, or allocated on the wrong key**, against
  `rules/ALLOCATION.md`. The shares must add back to the payer's net cost
  and every recharge must have its mirror.
- **Payroll not bookkept consistently**: provider fees not in payroll fees;
  per-employee posting instead of totals; a report line drifting between
  accounts month to month; any deviation from the mapping tables in
  `rules/PAYROLL.md`; a payroll document in the wrong currency or at a rate
  of 1.0.
- **Payroll controls not clearing.** Run `scripts/check_payroll_controls.py`;
  any balance beyond the residual config allows is a finding.
- **A staff benefit on the wrong account, or twice.** Where each entity has
  its own account for a benefit, the cost sits on the booking entity's own
  account; a separately billed invoice for a benefit an entity already pays
  through payroll double counts it.
- **Meals and welfare versus travel.** Hotels and transport are always
  travel, whatever platform billed them; a hotel or flight on a welfare
  account is a finding.
- **Subscriptions versus IT costs.** Anything paid routinely each month at a
  small, steady amount is a subscription unless it belongs to a specific
  account. The coding rule is the supplier's entry; this line is the check.
- **Capitalisation.** Against the threshold in `rules/EXPENSES.md`; an asset
  account adjusted by journal without the register following is a finding.
- **Missing or wrong tracking**, where an entity file requires a tracking
  category on every cost line.
- **Tax wrongly recovered.** Foreign tax taken as input tax, tax derived on a
  bank fee, tax split out of travel consumed abroad, a tax type code carried
  across entities (`rules/EXPENSES.md` rule 5).
- **Costs in the wrong period.** Against the accuracy period in
  `rules/GROUP.md`: a cost or release spanning a period boundary unsplit, or
  expensed in the period before or after the one it covers. Staff expense
  claims are never prepayments, so a multi-month claim is not a period error.
- **Prepayment schedules.** A release worked out on the account balance
  rather than the period covered; a recurring release still posting after the
  term; a one-off fee spread; a foreign figure released in a base-currency
  ledger.
- **Duplicates.** Two documents for one charge (a card reference and the
  supplier's own invoice; a booking platform's invoice and the merchant's):
  open both attachments and compare the invoice number printed inside, keep
  the one matched to the charge, void the unpaid twin. Equally, **not
  duplicates**: suppliers that legitimately issue several same-amount
  invoices with distinct numbers (usage-threshold billing, fixed recurring
  fees). Distinct invoice numbers from such a supplier are not a finding;
  record the supplier's pattern in its `SUPPLIERS.md` entry.
- **A card charged twice for one item** is a real double charge to recover,
  not a bookkeeping error. Report it separately.
- **Revenue an entity may not carry**, against the entity's role in
  `rules/GROUP.md`.
- **Agent-created payments.** The agent never pays; run
  `scripts/check_no_phantom_payments.py` where it exists and treat any hit as
  the top finding.
