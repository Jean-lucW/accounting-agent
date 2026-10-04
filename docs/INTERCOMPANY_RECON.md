# Intercompany reconciliation: method, break taxonomy and corrections

Companion to [`rules/INTERCOMPANY.md`](../rules/INTERCOMPANY.md), which holds
the group's own *routing* rules (which flavour of account each kind of item
belongs in) and its resolved break patterns. The account codes themselves live
in `config/group.toml` under `[[intercompany]]`. This file owns the
*reconciliation*: how the two sides of every intercompany relationship are
proved against each other, how a difference is classified, and what correction
each class needs. It is generic: nothing here depends on how many entities the
group has or what its flavours are called.

Driven by the [`xero-interco`](../.claude/skills/xero-interco/SKILL.md) skill
and `scripts/interco_recon.py`; revaluation by
[`xero-interco-fx`](../.claude/skills/xero-interco-fx/SKILL.md) and
`scripts/interco_fx.py`; the workbook by `scripts/interco_matrix.py`.

## 1. The principle

Every intercompany balance is held **twice**, once in each entity, and the two
copies are the same debt seen from opposite sides. So for every pair:

> **A's balance + B's balance = 0**, once both are expressed in one currency.

Anything else is a break, and every break has exactly one of the causes in §3.
The reconciliation is not "do the balances look similar": it is transaction by
transaction, because two sides can agree in total while both being wrong.

### Sign convention used throughout

All balances are normalised to **debit-positive** (asset-natural). A clean pair
sums to zero. Xero's Balance Sheet presents each row naturally within its
section, so an asset row is positive when it is a debit and a liability row is
positive when it is a *credit*: `pull_balances()` flips liability, equity and
revenue rows so both sides of a pair are directly comparable.

### Currency

Each entity has its own base currency (`base_currency` in
`config/group.toml`), and every cross-ledger figure is translated to the
group's reporting currency (`[company] reporting_currency`). A pair spanning
two base currencies can never agree in local currency, so the test is applied
in two layers:

1. **Transaction currency**: did both ledgers record the *same* amount in the
   currency the transaction actually happened in? If yes, the item **agrees**;
   any local-currency gap is translation, not error.
2. **Local carrying value**: the residual after layer 1 is the FX translation
   difference, cleared by a revaluation journal (§3D), never by touching the
   transaction.

Conflating those two is the most common way to "fix" a non-problem and create a
real one.

> **Xero's `CurrencyRate` is stored INVERTED** (foreign units per 1 base
> unit), so base amount = `LineAmount / CurrencyRate`, **never**
> `LineAmount * CurrencyRate`. A GBP 50,000 transfer @0.8000 into a USD-base
> entity ties to the Balance Sheet only as USD 62,500. Getting this backwards
> understates every FX item and manufactures breaks that do not exist. Prove it
> once against one of your own foreign-currency transfers.

The cross rates used to compare ledgers come from Xero's own document rates:
a document in currency C on an entity of base B gives C per B, so once one
currency's value in the reporting currency is known the other follows, and the
rates propagate outwards from the reporting currency (`derive_rates`). A
currency the group's documents never connect to is taken from `default_rates`
in `[intercompany_settings]`, or passed with `--rate CCY=...`; the report warns
loudly when it had to fall back to 1.0.

## 2. Rebuilding the ledger (no journals scope)

`accounting.journals.read` is a premium scope most organisations do not hold,
so there is **no account-transactions report** through the API. The GL for an
account is therefore reassembled from documents:

| Source | Sign on the account |
|---|---|
| Posted manual journals (`JournalLines`) | as stated |
| Bank transactions | SPEND = debit, RECEIVE = credit |
| Bills (ACCPAY) / supplier credit notes | debit / credit |
| Invoices (ACCREC) / credit notes | credit / debit |
| **Payments settled against a payments-enabled loan account** | debit (ACCREC) / credit (ACCPAY) |

**The last row is the one that gets missed.** An interco loan account with
`EnablePaymentsToAccount = true` lets a bill be settled *from the loan
account* with no line item anywhere touching it. Omit payments and the rebuild
silently under-reports. Mark such accounts `payments_enabled = true` in the
config so the reader knows to expect them (the rebuild reads payments for every
account either way).

**Always prove the rebuild before trusting it**: the sum of the rebuilt
transactions over a period must equal the Balance Sheet movement over the same
period, to the cent. If it does not, a source is missing: fix that before
reporting any break. Each pair tab in the workbook ends with an
"Unexplained (should be 0)" line that is exactly this proof.

**Both ends of the window must be month ends, or the proof cannot tie.** The
monthly Balance Sheet returns *month-end* columns whatever day is asked for, so
a balance date mid-month is really the month end, and a rebuild that stops
mid-month omits anything dated in the rest of that month, including journals
dated forward to the month end, which is where an accrual or a recharge is
routinely put. The gap then looks exactly like a missing source. Run the
reconciliation to the month or quarter end, and take the opening balance from
the month end immediately *before* the window, never from whatever the oldest
column happens to be: the extra column is a month of movement, and on a flavour
that is swept to nil at a period end, a month either side of that sweep can
differ by the whole period's activity.

## 3. Break taxonomy

Classify every difference into exactly one of these. The class determines the
fix; guessing the fix without classifying is how phantom entries get created.

### A. One-sided transaction: the real error

One entity recorded the item, the other never did. Nearly always the
"X pays an expense that belongs to Y" pattern:

```
X (the payer):    Dr  X<>Y intercompany account     Cr  Bank          <- posted
Y (the benefit):  Dr  Expense                       Cr  X<>Y account  <- MISSING
```

**Fix:** post the missing side in Y, in Y's base currency, at the rate implied by
the transaction. Code the expense per `rules/EXPENSES.md` and
`rules/SUPPLIERS.md` as if Y had paid it directly. Which flavour of account
the two legs use is decided by `rules/INTERCOMPANY.md`.

### B. Wrong-flavour routing

Two entities often hold more than one intercompany account between them (a
loan, a recharge account, a management-fee account: whatever flavours the
config lists). The item is on both sides but in the wrong flavour on one or
both. The pair may still sum to zero across flavours, so **a zero break does
not prove correct routing**: check flavours separately against
`rules/INTERCOMPANY.md`.

The reconciler flags the common shape automatically: an item one-sided on one
flavour whose equal-and-opposite mirror sits, also one-sided, on a *different*
flavour of the same two entities is marked `WRONG FLAVOUR?` on both legs.

**Fix:** reclass journal in whichever side(s) is wrong. Never net a
misrouting off against a genuine break.

### C. Amount difference

Both sides recorded it, at genuinely different amounts in the transaction
currency (not an FX artefact). Usually a transposition, a gross/net split, or one
side booking a part-payment as the whole.

**Fix:** establish which side matches the source document, correct the other.

### D. FX translation difference

Both sides agree in transaction currency; the local carrying values diverge
because each entity translated at its own date's rate. **Expected and
unavoidable** on any cross-base pair, and it accumulates.

**Fix:** a **one-sided** revaluation journal in the entity carrying the
foreign-currency balance:

```
Dr  X<>Y intercompany account      Cr  FX gain/loss account     (or the reverse)
```

The FX account is `fx_gain_loss_account` in `[intercompany_settings]`. The
journal is narrated `FX Adjustment for Intercompany Balance with
<counterparty>` (override with `fx_narration`), dated the month end, and
posted by the entity whose base currency is **not** the currency the loan is
denominated in: the ledger in the loan's own currency carries it at face
value, the other ledger's carrying value floats. It is drafted by
`scripts/interco_fx.py` and posted only through the `xero-interco-fx` skill,
and **only once every transaction on the account has its counterpart**
(one-to-one or a group match, reversals filtered out). A revaluation posted
over an open one-sided item books that item as FX and hides it; the pair then
agrees at the month end while both P&Ls are wrong. Same-currency pairs are
never revalued.

Revaluation journals are legitimately one-sided **by design**: they have no
counterpart in the other ledger and must never be reported as a break. The
reconciler tags any manual journal whose narration carries an FX marker
(`fx_markers` in the config; by default "fx adjustment", "revaluation",
"exchange difference" and similar) and sets it aside.

How often to revalue, and whether there is a materiality floor, is group
policy: `rules/INTERCOMPANY.md` §FX revaluation. Revaluing some month ends and
not others, or one side and not the other, is itself a source of breaks.

### E. FX rate difference on the two legs of one transfer

A cross-currency cash transfer: the sender books what left, the receiver books
what arrived, and the bank's conversion sits between them. Both entries are
correct as records of their own bank line, yet they differ. Amounts are small
relative to the transfer.

**Fix:** a journal in one entity taking the difference to FX gain/loss.
Which entity is group policy (`rules/INTERCOMPANY.md`). The usual answer is
the entity whose bank actually performed the conversion, which is the entity
genuinely bearing the cost: read the two bank lines before proposing; the
converting side is the one whose document currency differs from its own base.

### F. Timing difference

Both sides will record it, but in different periods. No correction: it clears
itself.

**Escalate only when it straddles a period that matters:**

- A period end the group reports or recharges on (a quarter end, a year end)
  is material: a break straddling it distorts whatever is computed at that
  date. Fix by re-dating the later side into the correct period (subject to the
  lock date).
- A break that clears within the period is **noted, not corrected**.

A recurring shape: one entity pays a large periodic supplier invoice for
another and debits the pair's account the same day; the other entity books its
side by journal weeks later. The pair breaks by the invoice amount mid-period
every period and returns to zero by the period end. Record such a pattern in
`rules/INTERCOMPANY.md` §Resolved patterns once the admin has ruled on it, so
it is not "fixed" every run.

### G. Missing mirror account (structural gap)

One entity holds an account for a flavour the other has never created, so the
mirror cannot exist. In the config this is an empty account code for one side
of an `[[intercompany]]` block. Not a break in itself, but it guarantees one
the moment the flavour is first used. The reconciliation reports it as
`NO MIRROR ACCOUNT` under STRUCTURAL GAPS, and the workbook still gives the
pair its own tab, with a STRUCTURAL GAP banner and the existing side's
transactions shown one-sided.

**Fix:** create the mirror account in Xero before the first posting, matching
the counterpart's naming and type, then add its code to `config/group.toml`.

The same class covers a mapped account that is **typed as a P&L account**
(EXPENSE or REVENUE), archived, or missing from the entity's chart: a posting
to a P&L-typed account lands in the P&L and the balance-sheet reconciliation
cannot see it. The reconciler reads every entity's chart (`GET Accounts`) each
run and lists these under STRUCTURAL GAPS. Retype to a balance-sheet type
before first use.

## 4. Method

0. **Check the routing, not just the totals.** Every run verifies that each
   transaction is mapped to the *correct flavour* per `rules/INTERCOMPANY.md`
   and to the correct entity pair. **A pair summing to zero proves nothing
   about routing**: both sides can agree perfectly while sitting in the wrong
   account. Balance agreement and mapping correctness are two separate tests
   and both run every time.
1. **Build the account map.** Both sides of every (entity pair, flavour), from
   `config/group.toml`. Match **by the pair of entities, never by name or by
   code**: naming is rarely consistent across entities, and the same code is
   often reused in different entities for different relationships. Re-verify
   the config against the live charts each run (the STRUCTURAL GAPS section
   does most of this) and update it when an account moves.
2. **Pull balances** monthly (Balance Sheet, `periods=N, timeframe="MONTH"`),
   normalised debit-positive.
3. **Pull transactions** for the window and prove them against the Balance Sheet
   movement (§2).
4. **Filter out reversals first.** A posting undone by its own reversal inside
   one ledger nets to nil and has no counterpart anywhere; left in, each leg
   shows as a phantom one-sided item. A reversal is inferred from amount,
   date and wording: equal and opposite within 60 days, and either a journal
   whose narration says it reverses or reclasses something, or two documents
   sharing a reference. It is **never two cash lines** (money out and money
   back are two transfers, each with its own mirror), and not an
   attribution-then-sweep pair or a settlement and the sweep that clears it,
   which mirror line by line across the pair. Column B of the pair tab is the
   **category**: `Interco` (ordinary transactions with the counterparty, which must
   reconcile line by line), `Reversal`, `FX Reval` (one-sided by design,
   §3D), `Sweep JE` (consolidation or sweep journals moving a balance from one
   interco account into another, mirrored both sides; recognised by
   `sweep_markers` in the config), `CT` (under `tolerance_abs`, rounding
   true-ups). The tab opens filtered to `Interco` so what is on screen is what
   has to reconcile; the rest is set aside by kind, never deleted.
5. **Match one to one**, tightest test first: same transaction currency and
   equal-and-opposite, then shared base currency and equal-and-opposite, then
   cross-currency within FX tolerance (3%, relaxed to 10% when the narrations
   name the same counterparty or reference). Widen the date window in steps
   (same day, 3 days, 10 days, 35 days): interco transfers repeat the same
   round amounts, so a wide first pass pairs them arbitrarily.
6. **Match groups.** What one-to-one leaves behind is tried many-to-many: one
   side books one journal for what the other paid in several bank lines (a
   month's rent in four payments against one month-end journal), or one
   settlement covers several bills. Candidates are drawn together by what
   their narrations share (supplier, reference, subject word; generic
   intercompany words and the group's own entity names are ignored) within 45
   days across the two sides, and a cluster is a match when its sides net to
   within FX tolerance in the reporting currency; otherwise its subsets are
   tried, smallest first. A group is one merged block on the pair tab with the
   group's total in the Diff column. Last, a **probable** pass: the same
   supplier or subject on both sides within 10 days, opposite-signed, amounts
   up to 35% apart (two payments to one supplier the same day for 5,200 and
   4,500) are the same purchase with a difference to investigate, so they are
   paired and the gap is shown in red, never absorbed.
   **Every transaction is expected to end up with a counterpart.** One that
   does not is read, not assumed: the mirror was never posted, the
   recognising side's bill has not been settled against the loan account, the
   payer split what the other side booked whole, or a genuine error.
7. **Classify** each residual per §3 and propose the correction.
8. **Revalue** (`xero-interco-fx`) only when the account has no one-sided
   items left: the remaining break is then translation, and one one-sided
   journal in the entity carrying the foreign-currency balance brings the pair
   to zero (§3D).
9. **Walk the break back through the monthly history** to find the last month the
   pair agreed. A break present for months is a different problem from one that
   appeared this month, and the fix must not re-correct an already-corrected
   period.

### Periodic sweeps: why some flavours look dead

Many groups run a periodic cycle in which a secondary flavour (a recharge or
cost-attribution account, a management-fee account) is used during the
period and then swept into the pair's main loan by a consolidation journal at
the period end, returning it to nil. If yours does, `rules/INTERCOMPANY.md`
says so. Consequences for the reconciliation:

- **A nil balance at every month end is the expected end state**, not proof
  the flavour is unused. Check the flavour on the period-end **movement**,
  never the month-end balance.
- **Run period-aligned windows.** A window starting mid-period sees none of
  the earlier activity and will wrongly report the flavour as unused.
- A settlement leg against such an account is often a **payment against a
  payments-enabled account**, with no line item: exactly the source §2 warns
  is easy to miss.
- The sweep journals themselves are categorised `Sweep JE` and set aside;
  name them so `sweep_markers` recognises them.

### Where to start the window

Start from **the last month end at which the pair agreed**, not from an arbitrary
date. A pair that has never agreed needs a full-history reconciliation before any
correcting journal; otherwise the journal just moves an unquantified break
around.

## 5. Posting corrections

Everything in `xero` skill safety rule 1 applies: name the entity in full, show
the payload, get confirmation, use an idempotency key.

- **Read each entity's lock date live** (`GET Organisation`); it moves.
  Journals dated on or before it are silently dropped (HTTP 200, change
  discarded). Every correction must be dated after the lock and **re-read to
  confirm it stuck**.
- Correct **forward**, never by editing a reconciled or locked historical entry.
  Never delete a payment to fix a bill (`xero` skill safety rule 7).
- Post the correction in the entity that is *missing* the entry, not by reversing
  the entity that got it right.
- One journal per break, narration naming the counterpart entity, the account and
  what it corrects, so the next run's matcher can pair it.
- Corrections are **manual journals**, not payments and not bank transactions:
  they move an intercompany balance, no cash is involved.

## 6. Resolved break patterns: the learning loop

The admin reviews each break and decides the treatment; **that decision is
written into `rules/INTERCOMPANY.md` §Resolved patterns, and future runs apply
it automatically** rather than re-asking.

So the loop is: run, report every difference with a proposed correction,
admin decides, record the decision as a rule, next run applies it without
asking. Only add a pattern once the admin has actually ruled on it. A pattern
needs the trigger and the agreed treatment, written as a rule. Check the
patterns at the start of every run.

## 7. The workbook

`scripts/interco_matrix.py --as-at YYYY-MM-DD` writes
`data/reports/Intercompany Reconciliation.xlsx`. Its shape follows
`config/group.toml` exactly, so it updates itself when the config changes:

- **"summary matrix"** puts every configured entity on both axes, in config
  order. A cell is read from the **column** entity's books: its total
  intercompany balance against the **row** entity, all flavours summed,
  debit-positive in the column entity's base currency. Green = the two
  directions agree, red = they disagree (or one side has no mirror account),
  grey = nil or no account; the diagonal is blank.
- Below the matrix, **one line per configured pair** (flavour by flavour):
  accounts, both balances, the break in the reporting currency and a status,
  each linked to its tab.
- **One tab per `[[intercompany]]` block**, named from the two entities' short
  names and the flavour (sanitised to Excel's 31-character limit and made
  unique). The year's transactions grouped by month, the two ledgers side by
  side. Column B is the `Category` and the tab opens filtered to `Interco`, so
  reversals, FX revaluations, sweep journals and cents are out of the way.
  Each remaining row pairs a transaction with its counterpart, or is a
  **merged block** for a group or probable match (the Diff cell merged across
  the block with the group's total; a probable match names the gap to
  investigate), or is `ONE-SIDED` with 0 on the empty side, or carries a
  `WRONG FLAVOUR?` note. A pair with a missing mirror still gets its tab, with
  a STRUCTURAL GAP banner.

Same-base pairs must agree to `tolerance_abs`; cross-base pairs allow
`tolerance_rel` (0.5% by default) of the larger side, because the comparison
passes through an estimated cross rate and would otherwise show red for pure
rounding.

The reconciliation is finished when no row on any tab is one-sided; the FX
drafts at the end of the recon report say which pairs are ready to revalue.

The workbook is built on the server by `deploy/run-interco.sh` (which also
runs the full reconciliation), with dated copies under `data/interco/`; a
laptop copy comes from `deploy/fetch-interco.sh`. It is a run artefact, never
committed. Every build also publishes it to the Drive publish folder
(intercompany subfolder), one copy overwritten each time, so the current
workbook is always the one on Drive.

## 8. What to reconcile: scope checklist

A pair is not reconciled until all of these are done. Balance agreement alone
covers only the first line.

| # | What | Why it is separate |
|---|---|---|
| 1 | **Every configured flavour, both directions** | The headline test: each pair sums to zero in one currency |
| 2 | **Flavour routing** | A pair can sum to zero with every item in the wrong account. Totals agreeing proves nothing about routing |
| 3 | **Intercompany AR/AP** | An **unpaid** interco invoice sits in AR/AP and never touches an intercompany account, so an account-only reconciliation cannot see it. Check for interco invoices and bills still outstanding at the reporting date |
| 4 | **Periodic sweeps** | Run and reverse inside one day; invisible in month-end balances and in any mid-period window |
| 5 | **FX revaluation completeness** | A missed month end is indistinguishable from a real break until you decompose it (§9) |
| 6 | **Non-group interco** (`[[non_group]]` in the config) | Real intercompany-looking balances with **no mirror in this group** (a loan to or from a party outside the group, such as a shareholder loan): report them, never try to match them |
| 7 | **Account existence and type** | Every pair needs a mirror on both sides, typed balance sheet (§3G). A pair with a missing mirror or a P&L-typed account is reported as such, and any bookkeeping rule that routes cash through it is on hold until the chart is fixed |

## 9. Diagnostic playbook

Techniques that resolve real breaks. Reach for these before proposing any
journal.

### Prove the decomposition: never plug to FX

**A residual is not "FX" because nothing else explains it.** Show that

```
movement in the break  =  one-sided items  +  per-transaction translation
```

ties, to the cent, using each transaction's own booked rate. Only then is the
remainder genuinely translation.

Example shape: a break that moved by -36,500 in the month decomposes into one
-40,000 one-sided receipt plus +3,500 of translation across the matched
transfers. It ties, so the residual after correcting the one-sided item is
pure translation. Skipping the arithmetic produces an "FX correction" that has
silently absorbed the posting error.

### A cross-currency break headline is rate-dependent: say so

For a pair spanning two base currencies the headline break is
`side A x rate A + side B x rate B`, so it **moves with whatever rate you
translate at**: a year-to-date median rate, a rate near the balance date and
the rates the transactions were actually booked at give three different
headline figures from the same two balances. None is wrong; they answer
different questions. **Translate a balance at a rate contemporaneous with its
balance date** (what `derive_rates(txns, as_at)` does): a year-long median
silently values an August balance at January's rate.

The only rate-independent statement is the one-sided item itself (*entity X
is missing an entry for N*). Quote that as the finding; quote the FX residual
as a consequence of the revaluation policy, never as a hard number.

### `IsReconciled` is evidence, not decoration

On a Xero **bank transfer**, compare the two legs. If the paying leg is
reconciled and the **receiving leg is not**, there is no real statement line on
the receiving side: the money left the group entity and the "internal transfer"
is a miscoding, confirmed when the counterparty entity's matching receipt *is*
reconciled. The fix is bank-side, in the UI: recode the paying leg to the
intercompany account.

### Step change vs drift in the monthly break history

Plot the break month by month before diagnosing. **Smooth drift is FX; a step
change is an event.** A series that creeps by a few thousand a month and then
jumps by tens of thousands has one FX story and one posting event; diagnose
the step, not the average.

### A voided document replaced by a journal

When a supplier bill is voided and a manual journal appears for the same cost
crediting an intercompany account, **check which entity the supplier actually
billed**. If the entity the journal says paid has no such charge anywhere, the
credit is unsupported; a journal amount that does not even equal the voided
bill is the second tell.

### Confirm the counterparty from the supplier's own document

Before accepting "X paid this for Y", search **both** ledgers for the supplier
around that date, including voided and draft documents. Absence in the entity
allegedly paying is itself the finding.

### Check the cost's standing treatment

If an entity recharges a cost whose standing treatment in `rules/SUPPLIERS.md`
or `rules/GROUP.md` already puts it in that same entity, the *recharge* is the
error, not the missing mirror. Likewise, a staff-attributable cost follows
whatever entity `rules/EXPENSES.md` says recognises it; if that entity also
paid, there should be no intercompany leg at all.

### Rate limits: probe, do not retry

Persistent 429s are usually the **daily** cap (5,000 calls per organisation),
not the per-minute one. Retrying cannot clear it. Probe the response headers
raw (`X-DayLimit-Remaining`, `X-Rate-Limit-Problem`, `Retry-After`) and build
the workbook for the remaining entities with `--entities` while one is capped.
