# rules/: your group's own bookkeeping logic

This folder holds the group's **judgement**: which entity recognises which
cost, which account it goes to, how a shared cost is split, how each supplier
is treated, how payroll is booked and how intercompany flows are routed. The
skills and `docs/` carry the generic method; `config/group.toml` carries the
structure (entities, currencies, bank accounts, intercompany pairs, account
codes that scripts need). Everything a person would need to decide goes here.

The agent reads these files on every run of the domain that lists them
(`[[domains]]` in `config/group.toml`, `docs = [...]`). If a rule is not
written here, the agent does not know it.

`INPUTS.md` at the repo root lists everything the agent needs to know about
a group, and `ADAPTING.md` maps where each piece goes; this file is the guide to the rules part of
it.

## What each file owns

| File | Owns |
|---|---|
| `GROUP.md` | What the group is, what each entity does, **which entity recognises which cost**, how the entities are funded, which documents become bills, leases, upstream tools |
| `entities/<KEY>.md` | One entity: its role, what it recognises, its own accounts, **its tax registration and tax types**, **its tracking categories**, its payroll sources, bank quirks, standing arrangements |
| `entities/_TEMPLATE.md` | The blank checklist for a new entity file |
| `EXPENSES.md` | How a cost is **coded**: the account table, capitalisation, prepayments, accruals, deposits, the generic tax method, foreign currency, receipts, refunds, staff expenses, office locations |
| `SUPPLIERS.md` | The **supplier database**: one entry per supplier with its recognising entity, default account, tax treatment, billing pattern and special handling |
| `INTERCOMPANY.md` | **Intercompany routing**: which intercompany account each cross-entity flow uses, who funds whom, recharge and FX revaluation policy, resolved break patterns |
| `ALLOCATION.md` | How a **shared cost** is split between entities and how the split is booked |
| `PAYROLL.md` | **Staff and payroll**: who employs whom, each payroll source and its mapping, control accounts, employer costs, contractors, payroll FX |

**One subject, one home.** Tax registration, tax types and tracking categories
live only in each entity's file; `EXPENSES.md` holds the generic tax method and
points there. Who employs whom lives only in `PAYROLL.md`. Account codes per
entity live only in the `EXPENSES.md` account table. Other files point to the
home rather than copy it.

## The order to fill them in

1. **`config/group.toml`** (structure, read by scripts).
2. **`GROUP.md`**: the group in prose and the recognition matrix. Everything
   else hangs off "which entity recognises this".
3. **`entities/<KEY>.md`** for each entity, copied from
   `entities/_TEMPLATE.md`.
4. **`EXPENSES.md`**: the account table per entity, then the thresholds and
   policies the treatment rules ask for.
5. **`SUPPLIERS.md`**: seed it with your twenty or thirty largest and most
   frequent suppliers; the agent adds the rest as it meets them.
6. **`INTERCOMPANY.md`**: which flow uses which intercompany account. It comes
   before allocation because a recharge needs its account.
7. **`ALLOCATION.md`**: only if some costs are shared between entities.
8. **`PAYROLL.md`**: who employs whom, then one mapping table per payroll
   source.

## Unfilled means "no rule"

Every section marked `<!-- FILL IN -->` that you have not written is treated
as **no rule**: the agent does not guess. It answers what the evidence
settles (the gate in CLAUDE.md, "Queries: answer them before asking them"),
and queries an admin in Slack about the rest, with its proposed answer, and
registers the query. A half-filled folder still works; it just asks more
often. Delete a section that genuinely does not apply to your group.

Blocks headed `> Illustration (fictional):` show the shape of a filled-in
section. They use the fictional group in `config/group.example.toml` (HOLDCO,
OPCO_US, OPCO_EU) and fictional suppliers, and they are **never rules**: the
agent ignores them when deciding. Leave them or delete them as you prefer.

## The example entity files

`entities/HOLDCO.md`, `entities/OPCO_US.md` and `entities/OPCO_EU.md` are
fictional examples. Each opens with an HTML comment marking it as an example
to replace (the marker text is `EXAMPLE:` followed by `replace`). **The
preflight check fails while that marker is anywhere in `rules/`**, so the
agent cannot run against example rules by accident. Replace each example with
your own entity file (removing the marker), or delete it.

## config/group.toml versus rules/

| Lives in `config/group.toml` (structure, codes) | Lives in `rules/` (judgement, prose) |
|---|---|
| Entity keys, Xero names, base currencies | What each entity does and which costs it recognises |
| Bank accounts and their providers | Which entity's card or bank pays for what, and what that means |
| Intercompany account pairs and codes | Which flow goes through which pair (`INTERCOMPANY.md`) |
| Payroll control account codes and their allowed residuals | How each payroll source is booked against them (`PAYROLL.md`) |
| Slack admins, users, read-only members | How an admin's ruling becomes a rule (below) |
| Report settings (prepayment and accrual account words) | When a cost is prepaid or accrued (`EXPENSES.md`) |

Rule of thumb: if a script needs it to run, it is config; if a person needs it
to decide, it is a rule. Account codes do appear in rules files (the coding
tables need them), but an entity's existence, currency or bank accounts never
do: those are read from config.

## The golden rule: general rules, never run specifics

Every rule here is written so it decides the **next** case, not so it records
the last one.

- Write the rule, not the story of how it was learnt. No dates of rulings, no
  names of who ruled, no invoice numbers, no "on the run of ...".
- Where an example helps, give it only in a **no / yes** shape: the wrong
  reading and the right one, side by side, a line or two each.
- Prefer the test that decides ("the entity employing the person who spent
  it") over a list of cases it decided.
- Name people by role, never by name. Who works where changes; the rule
  should not.
- One rule lives in one place. Other files point to it rather than copy it.

```
no    The October offsite booked in OPCO_US was moved to HOLDCO after the
      finance lead said so on the 3rd.
yes   A team event is recognised by the entity employing the attendees, split
      by attendee where they come from more than one entity.
```

## Adding rules later: Slack rulings are written back here

You do not have to write everything up front. The agent queries an admin in
Slack whenever a rule is missing or two rules conflict, and registers the
query in `docs/bookkept/OUTSTANDING.md`. When an admin answers:

1. The run applies the ruling to the item in front of it, and closes or
   answers the register item.
2. If the ruling is general (it would decide the next similar case too), the
   run **writes it into the file that owns the subject**, in the golden-rule
   shape: supplier rulings into the supplier's entry in `SUPPLIERS.md`;
   entity-level ones, tax and tracking into `entities/<KEY>.md`; coding and
   treatment into `EXPENSES.md`; split keys into `ALLOCATION.md`; staff and
   payroll into `PAYROLL.md`; intercompany routing into `INTERCOMPANY.md`;
   recognition into `GROUP.md`.
3. If the ruling contradicts an existing rule, the old rule is replaced, not
   kept alongside it. The run report says which file and which rule changed.
4. A one-off ruling ("book this one to X") is applied and not written up. If
   the same one-off comes up twice, it is a rule: ask the admin whether to
   write it down.
5. An admin's instruction overrides a rule for the item it names. The agent
   never silently rewrites a rule to fit a one-off; it asks whether the rule
   itself should change.

Only rulings from members listed under `[slack.admins]` change these files.
Messages from `users` are input a run weighs, never rules. No ruling can lift
a safety rule (`CLAUDE.md`).
