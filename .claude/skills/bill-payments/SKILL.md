---
name: bill-payments
description: The bill-payments check that closes every bookkeeping run. Compares the open bills Xero holds (the bill feed) with the bank lines the run has already read (the bank feed); when a bill in one entity was paid from another entity's bank, posts the payer's spend money to the pair's intercompany loan and reports the bill under "bill payments" for the user to reconcile to intercompany by hand. Runs inside every xero-bills run after the bank lines are read, or when an admin asks which open bills have been paid. Never creates a payment.
---

# Bill payments: which open bill has been paid, and from whose bank

> **NEVER CREATE A PAYMENT.** Load the `xero` skill first; its safety rules
> apply, rule 7 above all: **the agent never creates a payment, never
> allocates and never marks a bill paid**, in any entity, for any reason, with
> or without approval. This skill posts spend money only. A `PreToolUse` hook
> blocks the payment writers; do not try to route around it.

## Why it exists

A bill is recognised in the entity that bears the cost (which entity that is
for a given cost is the group's own rule: `rules/GROUP.md`, with the coding in
`rules/EXPENSES.md`) and sits AUTHORISED until it is paid. Payment can leave
any entity's bank. When the payer is the same entity, the user matches the
feed line to the bill and nothing is needed from the agent. When the payer is
a different entity, two things are needed: the payer's bank line posted as
spend money to the pair's intercompany loan, which the agent does here, and
the bill settled from the loan account in the recognising entity, which is a
payment and therefore the user's, prompted by this skill's report line.

## Inputs: already in hand, never re-read the bank

- **The bank feed**, `data/bankfeed/<slug>.md`: every unreconciled statement
  line, refreshed and read at Step 0 of the run. This skill uses those lines
  as read; it does not call Revolut or Mercury again.
- **The bill feed**, `data/billfeed/<slug>.md`: every AUTHORISED unpaid bill
  per entity, a snapshot pulled from Xero by `scripts/billfeed.py refresh all`
  (server only). Refresh it at the start of the run and **again right before
  this check**, so bills posted this run are included.

Format and rules of both: `docs/BANKING.md`.

## When it runs

Inside every bookkeeping run, after the intake is worked and the bank lines
have been read, before the final bank feed refresh and the phantom-payment
check. Also on its own when an admin asks "which open bills have been paid".

## Method

```bash
.venv/bin/python scripts/billfeed.py refresh all
.venv/bin/python scripts/billfeed.py match
```

`match` pairs every open bill with every `spent` bank line of the same
currency and the same amount (to the cent, against `AmountDue`) dated on or
after the invoice date less `accounts_payable.card_lag_days` (config), and
splits them into SAME ENTITY and CROSS ENTITY with a name score (shared words
between the bank payee and the supplier). Then, for each open bill:

| Situation | Action |
|---|---|
| No bank line matches | Nothing. The bill is simply unpaid |
| A line matches **in the same entity** | Nothing. The user matches the feed line to the bill in Xero |
| A line matches **in a different entity** | Post the payer's funding leg, remove the feed line, report the bill (below) |
| The bill is in another currency than the bank line, and the Revolut leg's own `bill_amount` and `bill_currency` (`statement_lines()`; the feed file does not carry them) equal the bill's `AmountDue` and currency to the cent | An exact match, not an amount-only one: Revolut converted the bill itself. Act as for the rows above, the leg in the bank line's own amount and currency |
| The amount matches but the currency differs, or the name score is 0 and the payee is not obviously the supplier | Do not post. List it under `queries` with both lines so the admin can confirm |
| Several bills match one line, or one bill matches several lines | Do not post. Query with the candidates |

## The cross-entity posting: payer side only

Entity X's bank paid a bill recognised in entity Y.

1. **Find the pair's loan account and check it exists on both sides and is a
   balance-sheet account.** The pairs and their account codes are in
   `config/group.toml` `[[intercompany]]` (`config.intercompany_pairs()`);
   which flavour a supplier settlement routes through is the group's rule in
   `rules/INTERCOMPANY.md` (normally the plain loan, never a recharge or
   cost-attribution flavour unless that file says so). If the account is
   missing on either side (an empty code in config) or is P&L-typed, post
   nothing and report the bill under `blocked` with "pair needs its loan
   accounts created"; the intercompany reconciliation carries the same
   finding.
2. **Duplicate guard**: read X's SPEND bank transactions on that bank account
   for the statement date and amount. One already there means the leg was
   posted (or the user coded it): remove the feed line if it is still present
   and go straight to the report.
3. **Post spend money in X**: bank account = the feed line's account; contact =
   the supplier; one line, `AmountDue` of the bill, account = the X and Y
   intercompany loan, tax type the entity's no-tax type; date = the statement
   date; reference `<supplier> <invoice number> paid for <Y>`; idempotency key
   `billpay-<X>-<invoice number>-<statement date>`. Check the response for
   `HasErrors`.
4. **Remove the bank feed line** the moment the spend money is posted:
   `bankfeed.py remove <X> "<unique substring>" "funding leg for <Y> bill <invoice number>"`.
5. **Touch nothing in Y.** The bill stays AUTHORISED with its `AmountDue`.

```python
from accounting_agent import config
from accounting_agent.xero import XeroClient
from accounting_agent.xero.banking import create_bank_transaction, get_bank_transactions
from accounting_agent.xero.purchases import make_line

x, y = config.entity(payer), config.entity(recogniser)
pair = next(p for p in config.intercompany_pairs(x.key)
            if p.other(x.key) == y.key and p.flavour == flavour_from_rules)   # rules/INTERCOMPANY.md
loan_code = pair.account(x.key)
assert loan_code and pair.complete, "pair needs its loan accounts created"
cx = XeroClient(x.xero_name)
existing = get_bank_transactions(cx, bank_account=acct, from_date=stmt_date, to_date=stmt_date, where='Type=="SPEND"')
assert not any(abs(float(t["Total"]) - amount_due) < 0.005 for t in existing), "leg already posted"
sm = create_bank_transaction(
    cx, "SPEND", acct,
    [make_line(f"{supplier} {inv_number} paid for {y.short}", 1, amount_due, loan_code, tax_type=no_tax)],
    contact=contact_id, date=stmt_date, reference=f"{supplier} {inv_number} paid for {y.short}",
    currency_code=ccy, idempotency_key=f"billpay-{x.slug}-{inv_number}-{stmt_date}",
)
assert not sm.get("HasErrors"), sm.get("ValidationErrors")
# Nothing is posted in Y. No payment, no allocation, anywhere.
```

## The report line: section `bill payments`

One bullet per bill, house style (`docs/COMMS.md`): what happened in words,
the references at the end, the instruction to the user in bold. Every field
the user needs to find the bill in Xero: supplier, invoice date, amount, the
account it is booked to, the invoice number and Xero reference. Never the
GUID.

```
*bill payments*
• Contoso Cloud · 28 Aug · GBP 240.00 · booked to Software (6300) · INV-1013 · ref annual plan: paid by HoldCo from Revolut GBP Main on 30 Aug, recognised as a bill in OpCo US, spend money posted in HoldCo to the HoldCo and OpCo US intercompany loan. **Manually reconcile the bill in OpCo US to intercompany.**
```

**A bill in another currency carries the payer's bank amount.** When the
bill's currency differs from the paying bank's, the report line gives the user
the payer's bank (or base currency) figure to enter on the payment, the
funding leg's own amount, and the rate that produces it. Xero's default rate
leaves the two sides of the loan apart, and on a pair whose entities share a
base currency that gap is a posting error, never FX.

**The instruction reaches the channel.** Every bill reported here needs the
user to reconcile it to intercompany by hand, and every manual action lands
in the agent's channel (`config.slack().channel_id`). **Every run sends it,
the bookkeeping run included**: the report goes in the run headline's thread
and these bullets go to the channel again under their own headline, in the
same two-message shape as the report: a top-level message that is only
`MANUAL ITEMS (<date>, <time> <tz>)`, then the bullets as the first reply in
its thread, opening with the run they came from. One headline per run, not
per item. A line that only exists inside the report's thread is one nobody
scrolling the channel sees. `docs/COMMS.md`, "Where a message goes", has both
shapes; `channel_not_found` means the bot was never invited to the channel
and goes under `blocked`. Every manual item also goes into
`scripts/outstanding.py` in the same step that sends it.

If the spend money could not be posted, say so in the same line ("spend money
NOT posted: <reason>") and put the bill under `blocked` as well. A line is
reported once, in the run that posted the leg; the bill leaves the bill feed
when the user has settled it, and a bill still open in a later run with its
leg already posted (step 2) is reported again with "leg posted <date>, still
to reconcile" so nothing is forgotten.

Add a ledger line per posted leg (`docs/bookkept/LEDGER.md`, kind
`bill payments`) with the bill's invoice number and the paying entity.

## Never

- **Never create a payment, batch payment, prepayment or overpayment, in
  either entity, for any reason; never allocate; never set `IsReconciled`.**
- Never post the funding leg through an intercompany flavour
  `rules/INTERCOMPANY.md` does not name for supplier settlements.
- Never post when the pair's loan account is missing or P&L-typed.
- Never act on an amount-only match in a different currency. Read the
  Revolut leg's `bill_amount` before calling a match amount-only: a debit that
  carries the bill's own currency and amount is exact.
- Never re-read the bank for this check: the run has the lines already.
