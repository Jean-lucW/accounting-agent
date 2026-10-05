---
name: xero-interco-fx
description: Intercompany FX revaluation. Once every transaction on a cross-currency intercompany account has been matched to its counterpart, draft the one-sided FX journal that brings the pair's two balances to zero, and post every ready draft at the last month end when an admin asks from Slack ("run post the fx interco journals", in any wording), then rebuild the workbook. Never from cron. Use when asked to post the FX interco journals, revalue an intercompany loan, post the month-end FX adjustment on an interco account, or make a reconciled pair balance. Never for a pair with one-sided items still open, never for two entities in the same currency.
---

# Intercompany FX revaluation

Load the `xero` skill first (safety rules, run-code pattern). This skill is the
last step of the `xero-interco` reconciliation and is only reached from it: the
pair must already be reconciled transaction by transaction.

## The rule

A cross-currency pair (two ledgers in different base currencies, per
`config/group.toml`) can never agree in local currency: each side carries the
same balance translated at its own dates' rates. Once **every transaction on
the account has a counterpart** on the other side (one-to-one, in a group, or
a confirmed match), the whole remaining break is translation, and it is
cleared by **one one-sided journal in the entity carrying the
foreign-currency balance**:

```
Dr / Cr  <intercompany account>     the movement that makes A + B = 0 in the reporting currency
Cr / Dr  <fx_gain_loss_account>     from [intercompany_settings] in config/group.toml
```

dated the month end, narrated `FX Adjustment for Intercompany Balance with
<counterparty's full name>` (override with `fx_narration` in
`[intercompany_settings]`), line description `FX Adjustment`. Where
`[intercompany_settings.fx_tracking]` names tracking for the posting entity,
both lines carry it.

**Which entity posts.** The loan is denominated in one currency. The ledger
whose base is that currency carries it at face value; the other ledger's
carrying value floats, and that is the entity that revalues. `interco_fx.py`
infers the denomination from the documents on the account (the currency most
of the value was booked in). If that is inconclusive and exactly one side is
in the reporting currency, the other side revalues. Otherwise the draft says
`NO POSTING SIDE` and the admin names it with `--entity`. Where
`rules/INTERCOMPANY.md` §5 fixes the posting side for a pair, follow it (pass
`--entity` if the inference disagrees). Only one side revalues a given month,
so the two ledgers never both revalue the same difference.

**Refuse while anything is one-sided.** A revaluation sized to the gap between
two balances books every unmatched item as FX and hides it
(`docs/INTERCOMPANY_RECON.md` §9, "never plug to FX"). Clear the item first,
then revalue. The one documented exemption: `interco_fx.py` ignores unmatched
items under `tolerance_abs` (the recon's CT rounding true-ups).

**Same-currency pairs are never revalued.** A break between two ledgers in one
currency is a posting error or a transfer-leg difference (§3E), and it is
corrected at the transaction, not by an FX journal.

**No FX account, no draft.** If `fx_gain_loss_account` is blank in the config,
the script says so and drafts nothing.

## Drafts

Every reconciliation drafts, as at its own date:

```bash
deploy/run-interco.sh --as-at 2026-09-30                 # recon + FX drafts + workbook, on the server
.venv/bin/python scripts/interco_fx.py --json data/interco/interco_recon_2026-09-30.json
```

`interco_fx.py` reads the recon's JSON and prints, per (pair, flavour): the
break in the reporting currency, then `same currency` / `NOT READY (n
one-sided items, listed)` / `agreed` / `NO POSTING SIDE` / `NO FX ACCOUNT` /
`DRAFT`. A draft shows the entity in full and why it is the posting side, the
date, rates used, the two lines, the narration, and who last revalued that
account. **Drafting posts nothing.**

## Posting: an admin starts it from Slack, never cron

The revaluations are posted only once the rest of the intercompany
reconciliation is good, so a person decides when: an admin reviews the
workbook and, when happy, says `run post the fx interco journals` in any
wording. That one instruction covers every draft that is ready at the last
month end; it is the confirmation `xero` safety rule 1 asks for, for those
journals and nothing else. No cron job and no scheduled run ever posts one.
The daily `xero-interco` run only says in its report, after a month end,
which pairs are ready and that an admin posts them with that command.

The run, in order. It calls every part it needs, so the admin's one
instruction is enough:

1. **Reconcile as at the last month end**, read-only and without touching the
   workbook on Drive:
   `AGENT_READONLY=1 .venv/bin/python scripts/interco_recon.py --from <first day of the financial year> --to <month end> --as-at <month end> --json data/interco/interco_recon_<month end>.json`
2. **Settle what is still open at that date.** A line that is one-sided at
   the month end blocks its pair, even where the live reconciliation shows it
   settled: a correction dated the month end sits too far from an earlier
   month's line for the matcher to pair them.
   `.venv/bin/python scripts/interco_breaks.py list --json <that file>` lists
   each open item with its key. Diagnose each per `xero-interco`, "Diagnosing
   a break", then:
   - **The lines are one transaction** (posted on dates too far apart, or a
     correction already posted against an earlier line): record it with
     `scripts/interco_breaks.py confirm --key <key> --all-lines` (or
     `--doc-id` for each line, adding the correction's own line so it nets
     with the pair it fixes), `--reason` naming the documents that prove it
     and `--by` the admin who ruled. `--by agent` only where the documents
     settle it on their own: equal and opposite amounts, or a correction
     whose narration names the line it fixes. Anything less is a query.
   - **An entry is missing or wrong**: the correcting journal is proposed
     per `xero-interco`, "Posting corrections". This instruction does not
     cover it; it is posted only where the admin's instruction also says so.
   Then run step 1 again.
3. **Draft**: `.venv/bin/python scripts/interco_fx.py --json data/interco/interco_recon_<month end>.json`.
   A pair still `NOT READY` after step 2 is left alone and reported with the
   lines that hold it back, and each is registered as a query.
4. **Post** each `DRAFT`, `--dry-run` first, with the command below.
5. **Check**: run step 1 again and confirm every revalued pair reads
   `AGREED` at the month end.
6. **Rebuild the workbook**: `deploy/run-interco.sh` (the live workbook, as
   at today, published to Drive), then the month-end copy that shows the
   revaluations in their month:
   `AGENT_READONLY=1 .venv/bin/python scripts/interco_matrix.py --as-at <month end> -o data/interco/intercompany_<month end>.xlsx --no-publish`.
7. **Report** each journal as entity, date and narration with the amount and
   the accounts by name and code, the pairs not revalued and why, and the
   workbook line. Close the revaluation lines the register holds for that
   month (`scripts/outstanding.py close --key <reference>`).

Each journal is posted with:

```bash
.venv/bin/python scripts/interco_fx.py --json <recon json> --pair HOLDCO:OPCO_EU --flavour loan \
    --post --confirm "Example Holdings Limited" [--dry-run] [--entity HOLDCO]
```

- `--post` requires `--pair` and `--flavour` and exactly one ready draft: one
  journal per call.
- `--confirm` must be the posting entity's **full Xero name**; anything else is
  refused.
- A read-only session (`AGENT_READONLY`) refuses to post.
- The lock date is read live from `GET Organisation`; a journal dated on or
  before it is refused rather than silently dropped.
- The idempotency key is deterministic per (entity, account, date), so a
  retry after a rate-limit 429 cannot double-post.
- The journal is posted POSTED and **re-read**; the report line is the entity,
  date and narration (never a GUID).

A revaluation for a single pair, asked for on its own ("revalue the
HoldCo/OpCo EU loan to 30 Sep"), runs the same steps for that pair only.

Reporting follows `docs/COMMS.md`: what was posted in words, references at the
end of the line. `revalued the HoldCo/OpCo EU loan to 30 Sep in Example
Holdings Limited, DR Intercompany Loan OpCo EU (1620) CR FX Gain/(Loss) (4900),
GBP 270.00`.
