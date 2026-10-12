# xero-bills reference: write back what you learned

Read on demand from `.claude/skills/xero-bills/SKILL.md`, which says when. The rules here are as binding as the skill's own; they sit here so a run reads them when they apply, not on every turn.

## Write back what you learned, every run

The books get easier to keep only if each run leaves the rules better than it
found them. **A question you had to ask once should never have to be asked
again.** Before you finish, write the durable part of what you learned back
into the repo, in the same run, and list what you wrote in the report.

Where each kind of learning belongs:

| You learned... | Write it to |
|---|---|
| A new supplier, or a refinement to how an existing one is coded | `rules/SUPPLIERS.md` (one row; the run that meets a supplier owns its row) |
| A general coding or judgement rule an admin ruled on | `rules/EXPENSES.md` as a new numbered rule, or by tightening the existing one |
| Something entity-specific | that entity's `rules/entities/<KEY>.md` |
| A group-level recognition rule | `rules/GROUP.md` |
| A payroll treatment | `rules/PAYROLL.md` |
| An intercompany routing rule | `rules/INTERCOMPANY.md` |
| A Xero API quirk, error shape, or workaround | `.claude/skills/xero/reference/gotchas.md` |
| A change to this workflow itself | this file |
| An admin confirming or overruling a `to confirm` item | the file above that owns the subject, like any other ruling |

The run's own answers are not rulings. A `resolved` answer is written back
only where step 3's existing licence covers it (a new supplier's row in
`rules/SUPPLIERS.md`); a `to confirm` item becomes a rule only when an admin
answers it, and one accepted by silence never does.

How to write it so it is actually useful later:

- **Write it as a rule.** State the rule and, where it matters, the reason in
  a clause. Add an example only where it illustrates the rule (the no / yes
  shape), never as a record of what happened; no journal IDs, bill GUIDs or
  run amounts.
- **Write the general rule, not the instance.** "Illegible dates on routine
  staff spend are not a blocker" is reusable. "The supermarket receipt was
  fine" is not. State the boundary too: when the rule stops applying. Open
  items and the state of a correction belong in the ledger, the run report or
  the outstanding register, never in a rules file.
- **Answers to queried items are the richest source.** Every question an
  admin answers is a rule you did not have. When a run starts with answers
  attached to previously queried items, resolving the item is only half the
  job: the other half is writing down the rule so the same question is never
  asked twice.
- **Refine, don't duplicate.** Grep for an existing rule covering the ground
  and tighten it rather than adding a near-identical one. The files are read
  in full every run, so redundancy costs tokens and invites contradiction.
- **Only durable rules.** Facts about one invoice belong in the ledger line,
  not here. If it will not apply again, do not write it. **A one-off about one
  supplier's one document is never a write-back**: write back the mechanism,
  not the incident. *No* - "this supplier's formal credit note arrives after
  the summary and is that credit note's evidence". *Yes* - "a supplier
  summary and the supplier's own later document for the same quarter are one
  credit, not two". If the rule cannot be stated without naming the document,
  it belongs in the ledger or the run report.
- **Never weaken a safety rule this way.** The payment ban, the duplicate
  guard and the whitelist are not "learnings" to be relaxed; changing those
  needs an explicit, separate instruction from an admin.
