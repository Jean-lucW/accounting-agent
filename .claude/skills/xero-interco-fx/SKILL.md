---
name: xero-interco-fx
description: Intercompany FX revaluation. Once every transaction on a cross-currency intercompany account has been matched to its counterpart, draft and (only on a separate, explicit admin instruction) post the one-sided FX journal that brings the pair's two balances to zero. Use when asked to revalue an intercompany loan, post the month-end FX adjustment on an interco account, or make a reconciled pair balance. Never for a pair with one-sided items still open, never for two entities in the same currency.
---

# Intercompany FX revaluation

Load the `xero` skill first (safety rules, run-code pattern). This skill is the
last step of the `xero-interco` reconciliation and is only reached from it: the
pair must already be reconciled transaction by transaction.

## The rule

A cross-currency pair (two ledgers in different base currencies, per
`config/group.toml`) can never agree in local currency: each side carries the
same balance translated at its own dates' rates. Once **every transaction on
the account has a counterpart** on the other side (one-to-one, or a group
match), the whole remaining break is translation, and it is cleared by **one
one-sided journal in the entity carrying the foreign-currency balance**:

```
Dr / Cr  <intercompany account>     the movement that makes A + B = 0 in the reporting currency
Cr / Dr  <fx_gain_loss_account>     from [intercompany_settings] in config/group.toml
```

dated the month end, narrated `FX Adjustment for Intercompany Balance with
<counterparty's full name>` (override with `fx_narration` in
`[intercompany_settings]`), line description `FX Adjustment`.

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

## Run it

Always from a reconciliation run as at the month end being revalued:

```bash
deploy/run-interco.sh --as-at 2026-09-30                 # recon + FX drafts + workbook, on the server
.venv/bin/python scripts/interco_fx.py --json data/interco/interco_recon_2026-09-30.json
```

`interco_fx.py` reads the recon's JSON and prints, per (pair, flavour): the
break in the reporting currency, then `same currency` / `NOT READY (n
one-sided items, listed)` / `agreed` / `NO POSTING SIDE` / `NO FX ACCOUNT` /
`DRAFT`. A draft shows the entity in full and why it is the posting side, the
date, rates used, the two lines, the narration, and who last revalued that
account. **Drafting is the default and posts nothing.**

## Posting: one journal, one admin instruction

Only after an admin has seen the draft and said to post **that** journal:

```bash
.venv/bin/python scripts/interco_fx.py --json <recon json> --pair HOLDCO:OPCO_EU --flavour loan \
    --post --confirm "Example Holdings Limited" [--dry-run] [--entity HOLDCO]
```

- `--post` requires `--pair` and `--flavour` and exactly one ready draft: one
  journal per instruction.
- `--confirm` must be the posting entity's **full Xero name**; anything else is
  refused.
- A read-only session (`AGENT_READONLY`) refuses to post.
- The lock date is read live from `GET Organisation`; a journal dated on or
  before it is refused rather than silently dropped.
- The idempotency key is deterministic per (entity, account, date), so a
  retry after a rate-limit 429 cannot double-post.
- The journal is posted POSTED and **re-read**; the report line is the entity,
  date and narration (never a GUID).
- Re-run the reconciliation afterwards and confirm the pair now agrees.

Reporting follows `docs/COMMS.md`: what was posted in words, references at the
end of the line. `revalued the HoldCo/OpCo EU loan to 30 Sep in HoldCo - DR Intercompany
Loan OpCo EU (1620) / CR FX Gain/(Loss) (4900), GBP 270.00`.
