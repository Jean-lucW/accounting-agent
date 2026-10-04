# Shared-cost allocation

How a cost that benefits more than one entity is split between them, and how
the agent books the split. A cost that benefits one entity is not allocated:
`GROUP.md` names its recogniser and that is the end of it.

The agent reads this file whenever the recognition matrix in `GROUP.md` says
`allocated` for a cost category, or a supplier entry in `SUPPLIERS.md` points
here. If a shared cost has no row in the allocation table, the agent books it
in the paying entity and queries the admins for the key.

> Sections marked `<!-- FILL IN -->` are yours. The keys, the booking shape
> and the review checks are generic method: keep them, edit what does not
> fit. Blocks headed `Illustration (fictional)` use the fictional group in
> `config/group.example.toml`; they are never rules.

## 1. Allocation keys

A key is the measure a cost is split on. Use the one that best reflects who
benefits, and keep it stable: changing keys from month to month makes the
entities' costs incomparable.

| Key | Split in proportion to | Suits |
|---|---|---|
| `headcount` | People employed by each entity at the reference date | Per-seat software, group benefit schemes, HR systems |
| `revenue` | Each entity's revenue for the reference period | Group insurance, group-wide marketing |
| `fixed` | Agreed percentages written in the reference data below | Costs with a contractual or agreed split, a shared office by floor area |
| `by_user` | The named users or seats each entity's staff hold, from the supplier's own invoice or usage report | Seat-based tools where the invoice lists users |
| `usage` | A measured quantity (compute hours, storage, call minutes) from the supplier's usage report | Metered services |

Choosing a key:

- **Measured beats estimated.** Where the invoice or a usage report says who
  used what (`by_user`, `usage`), use it; fall back to `headcount`,
  `revenue` or `fixed` only where nothing is measured.
- **Pooled seats are not by user.** A plan whose seats are shared across the
  group is split on `headcount`, even if the invoice lists names.
- **A key changes only by an admin's ruling**, from a stated period, never
  mid-period and never retrospectively.

## 2. Reference data for the keys

<!-- FILL IN: one row per key you use. Source = where its numbers come from
(headcount from PAYROLL.md's who-employs-whom table or your payroll
provider's reports; revenue from the P&L per entity for a stated period).
Write the current percentages here so every run uses the same ones, and
refresh them on the cadence you state. One column per entity. -->

| Key | Source | Refreshed | <entity key> | <entity key> |
|---|---|---|---|---|
| <key> | | | | |

> Illustration (fictional):
>
> | Key | Source | Refreshed | HOLDCO | OPCO_US | OPCO_EU |
> |---|---|---|---|---|---|
> | headcount | payroll reports at month end | monthly | 20% | 30% | 50% |
> | revenue | prior quarter revenue per entity | quarterly | 0% | 55% | 45% |

## 3. Allocation table

<!-- FILL IN: one row per shared cost, from your contracts and the
supplier's invoices. Paid by = the entity that holds the contract and pays
the supplier. Key = one from §1. Recognising entities = who takes a share.
Frequency = how often the split is booked. The expense account in each
recognising entity comes from EXPENSES.md; write only exceptions in Notes. -->

| Shared cost | Supplier (as in SUPPLIERS.md) | Paid by | Key | Recognising entities | Frequency | Notes |
|---|---|---|---|---|---|---|
| <shared cost> | | <entity key> | | | | |

> Illustration (fictional):
>
> | Shared cost | Supplier (as in SUPPLIERS.md) | Paid by | Key | Recognising entities | Frequency | Notes |
> |---|---|---|---|---|---|---|
> | Group productivity suite | Contoso Cloud | HOLDCO | headcount | all | monthly | Seats are pooled; never split by user |
> | Group insurance policy | Fabrikam Insurance | HOLDCO | revenue | OPCO_US, OPCO_EU | per policy period | Prepaid first (`EXPENSES.md` rule 2); the release is what is split |
> | Group mobile plan | Tailspin Telecom | HOLDCO | by_user | all | monthly | The invoice lists lines; map each to the user's employing entity |

## 4. Who holds the contract and who bills whom

- **One contract, one payer.** The entity the supplier invoices holds the
  bill, recovers any input tax on it (it is the entity the invoice is
  addressed to) and recharges the others. Nobody else books a bill from that
  supplier for the same charge.
- **The payer recharges each recogniser directly.** A share is never passed
  on through a third entity.
- **At cost unless a markup is set.**
  <!-- FILL IN: at cost, or with a markup (what percentage, on which costs).
  Your tax adviser decides where a recharge must carry a markup or tax. -->

## 5. How the agent books an allocated cost

The shape is fixed: the supplier's invoice is booked once, in the entity that
pays, and each other entity's share moves by recharge through intercompany.

1. **Bill in the payer.** The supplier's invoice is a bill in the paying
   entity, document attached, coded and taxed on the payer's own rules
   (`EXPENSES.md`).
2. **Work out the split** on the key and the current reference data, on the
   net amount the payer recognised. Round each share to the cent and put any
   rounding difference on the payer's share, so the shares add back to the
   bill exactly.
3. **Recharge each other entity's share** through the intercompany account the
   pair uses for recharges (`INTERCOMPANY.md` §1 and §4; the pair and its
   account codes are in `config/group.toml`). The default is a journal pair:
   - in the payer: a journal crediting the expense account by the other
     entities' shares and debiting the intercompany account with each of them;
   - in each recogniser: a journal debiting its own expense account for the
     same category (its code from `EXPENSES.md`) and crediting its side of the
     same intercompany pair.

   Where a recharge must be invoiced (because it carries a markup or tax),
   the shape is a sales invoice in the payer and a bill in the recogniser,
   both coded to the pair's intercompany accounts, never to a bank.
   <!-- FILL IN: `journal pair` or `recharge invoice`, per shared cost if it
   varies. -->
4. **Both sides in the same period, at the same amount**, in the payer's
   currency translated at the same rate, so the intercompany reconciliation
   matches line for line. The narration names the cost, the period, the key
   and the share, e.g. `Recharge: group productivity suite, Mar, headcount
   30%`.
5. **Prepaid shared costs are split on the release, not on the invoice.** The
   payer prepays the whole invoice; each monthly release is what is
   recharged.

```
no    OPCO_US's 30% of the group suite is booked as a second bill from the
      supplier in OPCO_US, so the supplier appears twice and OPCO_US claims
      tax on an invoice not addressed to it
yes   one bill in HOLDCO; journal pair through the HOLDCO / OPCO_US recharge
      accounts moving 30% of the net cost into OPCO_US's software account
```

## 6. Review checks

- The shares in each period add back to the payer's net cost exactly.
- Every recharge has its mirror in the recogniser, same period, same amount.
- The key and the percentages match the reference data for that period.
- A shared cost booked in full in one entity with no recharge, or recharged
  on a key other than its row says, is a finding.
