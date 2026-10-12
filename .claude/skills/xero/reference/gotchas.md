Part of the `xero` skill, read when its SKILL.md says it applies. It is as
binding as the skill itself, and the skill's safety rules apply to
everything here. A new quirk, error shape or workaround is
written back here.

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
