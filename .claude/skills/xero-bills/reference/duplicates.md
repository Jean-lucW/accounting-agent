# xero-bills reference: is it a duplicate, and which copy goes

Read on demand from `.claude/skills/xero-bills/SKILL.md`, which says when. The rules here are as binding as the skill's own; they sit here so a run reads them when they apply, not on every turn.

**Is it a duplicate at all? Two questions, in this order.** A supplier name
that looks like another one is not evidence of anything; the bank is.

1. **Are there two identical payments?** Search the paying entity's statement
   for a second debit of the same amount within a few days. One debit means
   there is no duplicate, whatever the two records are called, and the answer
   is simply which record the line belongs to.
2. **Only if there are two: is there an invoice behind each?** Two debits with
   two invoices are two purchases. Two debits with one invoice between them is
   a duplicate payment and is flagged, never quietly cleared.

**The amounts do not have to be equal.** A supplier collected through a card
processor is charged the invoice plus that processor's percentage, so the debit
sits a few per cent above the invoice total and an exact-amount guard sees two
unrelated costs. Work the ratio before concluding: a debit that is the invoice
total times a flat percentage is that invoice, and a second invoice for the base
amount is the duplicate. The tell is a bill already in the books for the odd
figure, often under a number the processor's own confirmation supplied and
therefore stale. Query the pair and post neither.

The common false positive is an acquirer or processor name against the
merchant's: *no* - two records, same amount, a day apart, so a duplicate;
*yes* - one debit on the statement, so the processor line and the merchant
bill are one payment described twice.

**Clearing a duplicate: which copy goes, and check the other one is alive.**
Removing the wrong half of a pair is as damaging as the duplicate. Three
tests, in order, before any void:

- **The counterpart must be LIVE.** A DELETED or VOIDED sibling is not a
  record of anything, so voiding against it leaves the cost in no ledger at
  all. Re-read the counterpart's `Status`; never conclude "already recorded"
  from a list row or an earlier run's report.
- **Keep the copy that carries the document and the tax.** Where the pair is a
  documented bill against a gross card spend, the bill is the record: it holds
  the attachments and any tax split, and voiding it turns recoverable tax into
  cost. The reconciled spend is the one to remove, and the agent cannot, so
  the item is reported for the user to unmatch in the UI, not voided to tidy
  the list.
- **Reconciled means untouchable.** Never void or unreconcile a
  statement-matched transaction to resolve a duplicate; that is the user's own
  matching work (`xero` skill safety rule 7).

A duplicate that fails any of these tests is reported and held, even where the
void was already approved: the approval was given on the premise the run has
just disproved.
