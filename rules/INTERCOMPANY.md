# Intercompany routing rules

The group's own intercompany logic, in prose: which kind of transaction goes
through which flavour of intercompany account, who funds whom, how a cost one
entity pays for another is booked on both sides, the recharge policy and the
FX revaluation policy. The agent reads this file on every bookkeeping run, every
review and every intercompany reconciliation.

**Account codes do not live here.** Every intercompany account pair (the two
entities, the flavour, and each side's Xero account code) is declared in
`config/group.toml` under `[[intercompany]]`, and the reconciliation and the
workbook are built from that list. This file holds only the routing rules: what
each flavour is *for*. When you add a flavour to the config, add a row for it
here; when you write a rule here that needs an account that does not exist yet,
add the block (with an empty code for the missing side) to the config so the
reconciliation reports the gap.

> **Reconciling the two sides** (do they agree, and why not) is a separate
> discipline with its own method and break taxonomy:
> [`docs/INTERCOMPANY_RECON.md`](../docs/INTERCOMPANY_RECON.md), driven by the
> `xero-interco` skill. This file owns *routing* and the group's resolved
> break patterns; that file owns the method.

> Sections marked `<!-- FILL IN -->` are yours. Blocks headed
> `Illustration (fictional)` use the fictional group from
> `config/group.example.toml` (HOLDCO, GBP; OPCO_US, USD; OPCO_EU, EUR) and
> fictional suppliers; they are never rules.

## 1. Flavours: what goes through which account

Two entities can hold more than one intercompany account between them, one per
flavour. **What goes through which is a strict routing rule**: an item in the
wrong flavour is a review finding (break class B) even when the pair's totals
agree. The flavour names are your own (they are the `flavour` strings in the
config); the table says what each one carries and, just as important, what it
must never carry.

<!-- FILL IN: one row per flavour used in config/group.toml. Say what goes
through it and what never does. If a flavour is swept to nil periodically, say
so here and in §6. -->

| Flavour | What goes through it | Never |
|---|---|---|
| <flavour> | | |

> Illustration (fictional):
>
> | Flavour | What goes through it | Never |
> |---|---|---|
> | `loan` | Cash funding between the entities, and every cross-entity payment that is not a recharge under §4 (one entity paying a supplier invoice that belongs to another) | Recharges of shared costs |
> | `recharge` | The monthly recharge of shared costs under §4, raised by HOLDCO to OPCO_US | Cash transfers; one-off payments on behalf |

Review check: an item in the wrong flavour is a finding, e.g. a cash transfer
through `recharge`, or the monthly recharge journal through `loan`.

## 2. Who funds whom

<!-- FILL IN: the direction of funding in normal operation, which entity holds
the group's cash, how funding is requested and evidenced, whether loans bear
interest, and in which currency each loan is denominated (this decides which
side revalues, §5). From the intercompany loan agreements or board minutes. -->

> Illustration (fictional): HOLDCO holds the group's cash and funds both
> operating companies by bank transfer on request. Funding is booked on the
> pair's `loan` flavour, interest free, repayable on demand. The HOLDCO /
> OPCO_US loan is denominated in USD; the HOLDCO / OPCO_EU loan in EUR.
> OPCO_US and OPCO_EU do not fund each other in normal operation; any balance
> between them is a cost one paid for the other (§3).

## 3. A cost paid by one entity for another

The most common source of intercompany entries, and of breaks. The general
shape is always the same: the **payer** books the cash against the pair's
intercompany account, and the **benefiting** entity books the cost against the
same relationship, so both sides move together.

```
Payer X:        Dr  X<>Y intercompany (flavour per §1)    Cr  Bank
Benefiting Y:   Dr  Expense (coded per rules/EXPENSES.md)  Cr  X<>Y intercompany
```

Which entity is the benefiting one is decided by `rules/GROUP.md` and
`rules/EXPENSES.md` (cost recognition), never by who happened to pay.

<!-- FILL IN: how each side is recorded in your group. Who holds the supplier
bill (the payer, coded to the intercompany account, or the benefiting entity,
settled against it)? Is the benefiting side a bill, a manual journal or a
supplier credit? Who attaches the source document where? At what rate is a
foreign-currency cost booked on the second side? What happens to recoverable
tax (VAT, GST or sales tax) on such a bill? -->

> Illustration (fictional): when HOLDCO pays a supplier invoice addressed to
> OPCO_EU (say a Fabrikam Travel invoice paid from HOLDCO's card), HOLDCO
> books a spend money coded to the HOLDCO / OPCO_EU `loan` account, gross.
> OPCO_EU keeps the supplier's invoice as an ordinary bill (it holds the
> document and the recoverable tax) and is settled against its side of the
> `loan` account rather than its bank (the admin does that in Xero; the agent
> never creates a payment). The bill is never also paid from OPCO_EU's bank.
>
> If the supplier billed the payer instead, the payer's bill is coded to the
> intercompany account and the benefiting entity books a manual journal:
> Dr expense, Cr the pair's account, at the payer's transaction-currency
> amount, with the invoice attached.

## 4. Recharge policy

<!-- FILL IN: which shared costs are recharged, by whom to whom, on what basis
(headcount, revenue, fixed percentage, actual use: link rules/ALLOCATION.md
rather than repeating it), how often, with or without a markup, by invoice or
by journal, and which flavour carries it. Say what is NOT recharged. -->

> Illustration (fictional): HOLDCO recharges a share of group software
> subscriptions and office costs to OPCO_US monthly, on the headcount split in
> `rules/ALLOCATION.md`, at cost with no markup, by a manual journal in each
> entity on the last day of the month, through the `recharge` flavour. OPCO_EU
> is not recharged. A cost that already sits in the entity that recognises it
> is never recharged.

## 5. FX revaluation policy

A cross-currency pair can never agree in local currency: each side carries the
same balance translated at its own dates' rates. The difference is cleared by a
one-sided revaluation journal (`docs/INTERCOMPANY_RECON.md` §3D, drafted by
`scripts/interco_fx.py`). The FX gain/loss account it uses is
`fx_gain_loss_account` in `config/group.toml` `[intercompany_settings]`.
The journals are posted only when an admin asks from Slack (`run post the fx
interco journals`), never by a scheduled run (skill `xero-interco-fx`).

<!-- FILL IN: how often to revalue (every month end is the safe default),
which side revalues each cross-currency pair (default: the entity whose base
is not the loan's currency), whether there is a materiality floor or every
difference is reported, and who bears the conversion difference on the two
legs of a cross-currency transfer (default: the entity whose bank performed
the conversion). -->

> Illustration (fictional): revalue every cross-currency pair at every month
> end, never ad hoc: a skipped month end reappears as a break. The entity
> whose base currency is not the loan's currency revalues (OPCO_US never
> revalues the USD-denominated HOLDCO loan; HOLDCO does). There is no
> materiality floor: every difference is reported with a proposed correction,
> and the admin decides what is posted. On a cross-currency transfer the
> conversion difference goes to the entity whose bank did the conversion,
> decided case by case from the two bank lines.

## 6. Periodic sweeps and consolidation journals

<!-- FILL IN: if any flavour is cleared into another at a period end, say which,
when, and how the journal is narrated (its words must match sweep_markers in
config/group.toml so the reconciliation files it as "Sweep JE"). Delete this
section if you have none. -->

> Illustration (fictional): no sweeps in the example group; the `recharge`
> balance is settled by bank transfer each quarter.

## 7. Non-group balances

Balances with parties outside the group (a shareholder loan, a loan to or
from another company) are listed under `[[non_group]]` in
`config/group.toml` and are reported by the reconciliation, never matched as
intercompany.

## 8. Resolved break patterns

The learning loop in `docs/INTERCOMPANY_RECON.md` §6: when the admin rules on
a break, the ruling is written here as a rule and every later run applies it
without re-asking. Only add a pattern once the admin has actually ruled on it.

The rows below are sound defaults most groups adopt. Keep, change or delete
each one; they bind the agent only while they are here.

<!-- FILL IN: add your own rulings as rows. -->

| Pattern | Trigger | Agreed treatment |
|---|---|---|
| Recurring periodic timing | A large periodic supplier invoice paid by one entity for another, the other side booked weeks later; the pair breaks mid-period and clears by the period end | **Not a break.** Classify as timing (`docs/INTERCOMPANY_RECON.md` §3F), report as noted-only, never correct. Escalate only if it fails to clear by the period end |
| Monthly FX revaluation | Any cross-base pair at a month end | Revalue that month end, one-sided, in the entity carrying the foreign-currency balance (§5) |
| Cross-currency transfer legs | Two legs of one transfer differ | Charge the difference to the entity whose bank performed the conversion |
| Unreconciled bank-transfer leg | A Xero bank transfer whose receiving leg is unreconciled while the paying leg is reconciled | The money left the entity: it was **not** an internal transfer. Recode the paying leg as a spend to the counterparty's intercompany account. Bank-side fix, done by the admin in the UI |
| Voided bill replaced by an interco journal | A supplier bill is voided and a manual journal posted crediting an intercompany account for the same cost | Verify the counterparty actually paid it. If the supplier billed *this* entity, the credit to the intercompany account is unsupported: reverse it and reinstate the bill |
| Recharge of a cost the entity already owns | An entity recharges a cost whose standing treatment in `rules/SUPPLIERS.md` puts it in that same entity | The recharge is the error, not the missing mirror. Reverse it to the supplier's normal account |
| Payer charged the account, benefiting entity still holds the supplier bill | The same invoice is recognised twice: the payer coded its bill to the pair's intercompany account, and the benefiting entity also has the supplier's invoice open as an ordinary payable | Correct in the benefiting entity, never reverse the payer. Either the admin settles the bill against the intercompany account in Xero (the agent never creates a payment), or void it and post the mirror journal (`docs/INTERCOMPANY_RECON.md` §3A) in its place (Dr expense, Cr the pair's account, at the payer's transaction-currency amount, invoice attached), as §3 above prescribes. No payment and no allocation is created by the agent |
| Benefiting entity's bill carries recoverable tax | The pattern above, where the bill to be voided carries recoverable input tax (VAT, GST or sales tax) | Do not void it on a routine run: voiding turns the recoverable tax into cost unless the replacement journal carries an explicit `TaxAmount`. Report the pair, name the tax at risk, and hold |
| The admin will settle the bill by hand | The same situation, where the admin wants to settle the benefiting entity's bill against the intercompany account in person | Keep the bill AUTHORISED and unpaid with the document attached; leave the payer's leg one-sided until they do, and report it as a manual action. The pair's break carries that item until then, which is correct and is said in the report rather than chased on the next run |
| Swept flavours nil at month end | A flavour that §6 says is swept every period | **Not a finding.** Check it on the period-end movement, never the month-end balance |
