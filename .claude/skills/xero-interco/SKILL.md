---
name: xero-interco
description: Intercompany reconciliation across every entity and intercompany account pair in config/group.toml. Match each pair on both sides, transaction by transaction and month by month, classify each difference (one-sided entry, wrong flavour, amount, FX translation, transfer-leg FX, timing, missing mirror account) and propose or post the correcting journals. Builds the Intercompany Reconciliation workbook (summary matrix plus one line-by-line tab per pair). Use when asked to reconcile intercompany balances or loans, check that interco accounts agree, investigate an interco difference, and as a standard section of any bookkeeping review.
---

# Intercompany reconciliation

Load the `xero` skill first (API surface, run-code pattern, safety rules).

**Read [`docs/INTERCOMPANY_RECON.md`](../../../docs/INTERCOMPANY_RECON.md)
before running anything**: it holds the method, the break taxonomy and the
correction pattern for each class. Read
[`rules/INTERCOMPANY.md`](../../../rules/INTERCOMPANY.md) for the group's
routing rules (which flavour of account each item belongs in), its recharge and
FX policies and its resolved break patterns. The account map itself (every
entity pair, flavour and account code) is `config/group.toml`
`[[intercompany]]`; never hard-code a code in a script or a note.

A reconciliation is **read-only by default**. Correcting journals are proposed
in the report and posted only on a separate, confirmed admin instruction.

## When this runs

- On request ("reconcile intercompany", "do the interco loans agree?", "why is
  HoldCo/OpCo US out?").
- **As a standard section of every bookkeeping review** (`xero-review`), because
  intercompany breaks are invisible in a single entity's books: one entity can
  look perfect while its mirror is wrong.
- Before any period-end recharge or other computation that posts into the
  intercompany accounts, since a pre-existing break contaminates the result.
- After any month end where a cross-entity payment was made.

## Run it

**On the server only**: it is the sole Xero client. The runner below is the
ad-hoc variant; a scheduled read-only run can be chained into the `daily`
job (`deploy/run-scheduled.sh`). It covers every entity and every configured
pair at once.

```bash
deploy/run-interco.sh                       # from a laptop with AGENT_SERVER set: ssh's to the server and runs there
deploy/run-interco.sh --from 2026-07-01 --as-at 2026-08-31
deploy/fetch-interco.sh                     # pull the outputs to data/interco/ on the laptop
```

The runner calls `scripts/interco_recon.py` (window default: first day of the
as-at quarter to the as-at date; start at the **last month end the pair
agreed**, see below), then `scripts/interco_fx.py` (drafts the revaluation
journals, posts nothing; see `xero-interco-fx`), then
`scripts/interco_matrix.py`. It writes `data/interco/interco_recon_<as-at>.txt`
(the report), `interco_recon_<as-at>.json` (full detail),
`intercompany_<as-at>.xlsx` (dated workbook copy) with `latest.*` pointers,
and the current workbook at `data/reports/Intercompany Reconciliation.xlsx`.
The workbook lives on the server and is fetched, never committed, and **every
build publishes it to Drive** (the publish folder in `config.drive_settings()`,
intercompany subfolder), one copy that each build overwrites (the as-at date is
on the summary sheet). The runner publishes after its read-only Xero pull;
`interco_matrix.py` run by hand publishes itself unless `--no-publish` or
`--entities` (a partial build stays on the server). Report the Drive line.

`interco_recon.py` flags, passed straight through or used directly on the server:

| Flag | Meaning |
|---|---|
| `--from` / `--to` | transaction window |
| `--as-at` | balance date, default `--to` |
| `--periods N` | months of balance history (default 6) |
| `--pair HOLDCO:OPCO_US` | one relationship, every flavour; `HOLDCO:OPCO_US:recharge` one flavour. Entity keys, aliases or short names from the config |
| `--rate EUR=1.10` | override a cross rate to the reporting currency (default: derived from Xero's own document rates in the window) |
| `--json PATH` | full machine-readable detail |

**The workbook**, `interco_matrix.py --as-at D [-o PATH] [--entities A,B]`.
Its shape is the config: change `config/group.toml` and the next build has the
new rows, columns and tabs.

- **Summary matrix**: entities on both axes in config order; a cell is the
  **column** entity's books, its total intercompany balance against the
  **row** entity, all flavours summed, in the column entity's base currency.
  Green = both directions agree, red = disagree or no mirror account, grey =
  nil or no account. Below it, one line per configured pair with both
  balances, the break and a status, each linked to its tab.
- **One tab per `[[intercompany]]` block**: the year's transactions grouped by
  month (latest first), the two ledgers side by side, each transaction against
  the one it matches in the other entity and **0 where the mirror is
  missing**. Column B is the **category** (`Interco`, `Reversal`, `FX Reval`,
  `Sweep JE` for consolidation or sweep journals between interco accounts,
  `CT` for amounts under the rounding tolerance) and the tab opens filtered to
  `Interco`, the lines that must reconcile with the counterparty. A **group
  match** (one journal against several payments, several bills against one
  settlement) is one merged block with the group's total in the Diff cell; a
  **probable match** (same supplier or subject within 10 days, amounts up to
  35% apart) is paired with the gap named for investigation; a `WRONG
  FLAVOUR?` note marks a one-sided item whose mirror sits on a different
  flavour of the same two entities. A pair whose mirror account is missing
  still gets its tab, with a STRUCTURAL GAP banner.
- Each tab foots to Opening + Movement = Closing with an
  "Unexplained (should be 0)" line. If that line is not zero the rebuild
  missed a source, so fix it before reading anything else on the sheet.

**The goal of every run: no Interco row on any tab is one-sided.** Reversals,
sweeps, revaluations and cents filtered, every transaction has its
counterpart, one-to-one, in a group, or as a probable match with its
difference explained. A row that is still one-sided is read and explained
(mirror never posted, the recognising side's bill not yet settled against the
intercompany account, a split the group matcher could not see, a wrong
flavour, or a genuine error), never left as a count. Only when a
cross-currency account has none left is it revalued (`xero-interco-fx`), and
the FX drafts at the end of the recon report say which pairs are ready.

**Mind the daily call cap.** This is a call-heavy tool: ~6 endpoints per entity
for the transaction rebuild plus a multi-period Balance Sheet and the chart of
accounts, and Xero allows **5,000 calls per organisation per day**. Hitting it
returns 429 with `X-Rate-Limit-Problem: day` and a `Retry-After` of up to a few
hours; retrying will not help. Diagnose by probing the headers raw
(`X-DayLimit-Remaining`, `X-Rate-Limit-Problem`) rather than re-running, and use
the matrix's `--entities` with the other entity keys to build everything else
while one entity is capped.

The script prints a summary line per (pair, flavour) with the break in the
reporting currency, then STRUCTURAL GAPS (missing mirrors, accounts missing
from a chart, archived or P&L-typed), the non-group accounts, and a detail
block per broken pair splitting items into **one-sided** (with any `WRONG
FLAVOUR?` hint), **group** and **probable** matches, **reversals (labelled
REVERSED)**, **FX revaluation**, **amount/FX difference** and **timing
difference**.

### Always do these things around the run

1. **Verify the account map first.** `[[intercompany]]` in `config/group.toml`
   is the live map of every (entity pair, flavour) to account code on both
   sides. Codes are often reused across entities for *different*
   relationships, and names are rarely consistent, so **match by entity pair,
   never by name or code**. The STRUCTURAL GAPS section checks each code
   against the live chart every run; when an account moves, update the
   config (an admin edit), not the scripts.
2. **Prove the rebuild.** There is no journals scope and no account-transactions
   report, so the GL is reassembled from documents, including settlements made
   directly against payments-enabled accounts, which carry no line item and are
   easy to miss. The rebuilt movement must equal the Balance Sheet movement to
   the cent. If it does not, a source is missing: fix that before reporting.
3. **Use period-aligned windows.** If `rules/INTERCOMPANY.md` §6 says a
   flavour is swept to nil at a period end, a window starting mid-period sees
   none of its activity and will wrongly report it as unused. Check such a
   flavour on the period-end *movement*, not the month-end balance.
4. **Find the last agreed month.** The monthly history shows when each pair last
   summed to zero. Reconcile from there. A break that has run for months is a
   different problem from one that appeared this month, and a correcting journal
   posted without knowing which will just move an unquantified difference around.
5. **Check the resolved patterns** in `rules/INTERCOMPANY.md` §8 and apply them
   without re-asking.

## Scope: a pair is not reconciled until all seven are done

Balance agreement is only the first line. Full detail in
`docs/INTERCOMPANY_RECON.md` §8.

1. Every configured flavour, **both directions**: the pair sums to zero.
2. **Flavour routing**: a pair can sum to zero with every item in the wrong
   account.
3. **Intercompany AR/AP**: an unpaid interco invoice sits in AR/AP and never
   touches an intercompany account, so an account-only check cannot see it.
4. **Periodic sweeps**: they run and reverse inside one day.
5. **FX revaluation completeness**: a missed month end looks exactly like a
   real break until you decompose it.
6. **Non-group interco** (`[[non_group]]` in the config): report, never match;
   they have no mirror in this group.
7. **Account existence and type**: every pair needs a mirror on both sides,
   typed balance sheet; a missing or P&L-typed mirror is reported, never
   routed through.

## Diagnosing a break: before proposing any journal

Full playbook with worked examples: `docs/INTERCOMPANY_RECON.md` §9.

- **Prove the decomposition; never plug to FX.** Show that
  `movement in the break = one-sided items + per-transaction translation`, to
  the cent, using each transaction's own booked rate. Only the remainder is
  genuinely translation; an "FX" correction posted without this step swallows
  the posting error it should have found.
- **`IsReconciled` is evidence.** On a bank transfer, a reconciled paying leg
  with an **unreconciled receiving leg** means no real statement line on the
  receiving side: the money left the entity and the "internal transfer" is a
  miscoding. Fix bank-side in the UI, not by journal.
- **Step change vs drift.** Walk the monthly break history first: smooth drift
  is FX, a step change is an event.
- **Voided document replaced by a journal**: check which entity the supplier
  actually billed; absence in the entity allegedly paying is itself the finding.
- **Check the cost's standing treatment** in `rules/SUPPLIERS.md` and
  `rules/GROUP.md`. If an entity recharges a cost that normally sits with it,
  the *recharge* is the error, not the missing mirror.
- **Cost recognition decides the benefiting entity**, per `rules/GROUP.md` and
  `rules/EXPENSES.md`, never who paid. If the recognising entity also paid,
  there should be no interco leg at all.

## Reading the output

| Line | Means | Action |
|---|---|---|
| `AGREED` | pair sums to zero within `tolerance_abs` | none |
| `translation difference only (agrees at N transaction currency)` | both sides booked the same amount; local carrying values differ | revaluation journal (§3D), never touch the transaction |
| `ONE-SIDED in X` | X posted it, the mirror never did | **real break**: post the missing side in the other entity |
| `WRONG FLAVOUR?` | a one-sided item's mirror sits on another flavour of the same two entities | reclass the side that breaks `rules/INTERCOMPANY.md` |
| `AMOUNT / FX DIFFERENCE` | matched, but the transaction-currency amounts genuinely differ | check both against the source document |
| `GROUP MATCH n:m` | several lines one side against one or several the other, netting within tolerance | none; the gap shown is translation |
| `PROBABLE MATCH n:m` | same supplier or subject within 10 days, amounts up to 35% apart | read both source documents; the gap is a real difference to explain |
| `REVERSED` | a posting and its own reversal inside one ledger | ignore, nets to nil; flagged in column B and filtered out of the tab |
| `FX REVALUATION` | a one-sided revaluation journal | ignore, correct by design |
| `TIMING DIFFERENCE` | same item, different dates | ignore unless it straddles a period end that matters |
| `NO MIRROR ACCOUNT in X` | the flavour exists one side only (empty code in the config) | create the mirror in Xero and add its code to the config before the flavour is first used |
| STRUCTURAL GAPS: not in chart / ARCHIVED / typed P&L | the config points at an account that cannot hold the balance | fix the chart or the config |

**A zero break does not prove the pair is right.** It proves the totals agree;
it does not prove each item is in the correct flavour. **Every run checks
transaction mapping as well as balance agreement**: both sides can agree
perfectly while sitting in the wrong account. Check routing against
`rules/INTERCOMPANY.md`.

## Standing policy

The group's own policies (revaluation frequency, materiality floor, who bears
a transfer-leg FX difference) are in `rules/INTERCOMPANY.md` §5. Absent a
rule there, the defaults are:

- **Revalue monthly** on every cross-base pair, not ad hoc; a skipped month end
  reappears as a break.
- **No materiality floor.** Report every difference and propose a correction for
  it, however small. The admin decides what actually gets posted.
- **Cross-currency transfer legs: case by case**: the difference goes to
  whichever entity's bank performed the conversion (`docs/INTERCOMPANY_RECON.md`
  §3E).
- **Report first, then learn.** Present every break with a proposed correction
  and let the admin rule on it. Write each ruling into `rules/INTERCOMPANY.md`
  §8 as a reusable pattern, and apply recorded patterns automatically on later
  runs instead of re-asking.

## Two traps

- **`CurrencyRate` is stored inverted**: base = `LineAmount / CurrencyRate`.
  Multiplying instead of dividing understates every FX item and manufactures
  breaks that do not exist.
- **Balance Sheet rows are presented naturally within their section**: a
  liability row is positive when it is a *credit*. Normalise everything
  debit-positive (the script does) or the two sides of a pair look like they
  agree when they are both wrong in the same direction.

## Posting corrections

Only on a separate confirmed admin instruction, and per `xero` safety rule 1
(name the entity in full, show the payload, confirm, idempotency key).

- **Read each entity's lock date live** (`GET Organisation`): journals dated
  on or before it are silently dropped (HTTP 200, change discarded). Date every
  correction after the lock and **re-read it to confirm it stuck**.
- Corrections are **manual journals**. No cash moves, so an interco correction is
  never a payment, an allocation or a bank transaction: `xero` safety rule 7
  applies here exactly as everywhere else.
- Post into the entity that is **missing** the entry; do not reverse the entity
  that got it right.
- Narration must name the counterpart entity, the account and what it corrects,
  so the next run's matcher pairs it.
- Re-run the reconciliation afterwards and confirm the pair now agrees.

## Output

Summary table first:

| Pair | Flavour | A side | B side | Break (reporting ccy) | Class | Since | Proposed correction |
|---|---|---|---|---|---|---|---|

then one detail block per break: the unmatched items on each side, the class
per `docs/INTERCOMPANY_RECON.md` §3, and the exact journal proposed (entity,
account codes, amounts, date, narration). End with the pairs that agree, the
structural gaps, and anything needing an admin decision. Reporting follows
`docs/COMMS.md`.

**When the admin gives new context** (a break's real cause, a rate policy, a
new account, which entity bears an FX difference), write it down so it
persists: routing, policy and rulings in `rules/INTERCOMPANY.md`; a new or
corrected account code in `config/group.toml` (`[[intercompany]]`); a change
to the method itself in `docs/INTERCOMPANY_RECON.md`.
