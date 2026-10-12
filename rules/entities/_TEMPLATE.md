# <KEY>: <Xero organisation name>

<!-- Copy this file to rules/entities/<KEY>.md for each entity in
config/group.toml (the file name must match the entity's `rules_file`).
Fill in every section; delete a section that genuinely does not apply rather
than leaving it empty. A section left as FILL IN means "no rule": the agent
asks an admin. Write rules, not history: no dates of rulings, no names, no
invoice numbers. Structure (currency, bank accounts, intercompany pairs,
payroll control codes) stays in config/group.toml; describe here only what
it means for bookkeeping. This file is the only home for the entity's tax
registration, tax types and tracking categories. -->

## Role

<!-- FILL IN: what the entity is for, in two or three sentences; what revenue
it may carry (anything else in its revenue lines is a finding); what its cost
base mostly is; whether it has staff. Matches its row in GROUP.md. -->

## What it recognises

<!-- FILL IN: the entity's column of the recognition matrix in GROUP.md, in
words, plus anything entity-specific the matrix cannot say: costs it pays for
others, costs others pay for it, anything it must never carry. -->

## Accounts

The entity's account column is in `EXPENSES.md` § Account table; read that
column only.

<!-- FILL IN: any account only this entity has, or a category it codes
differently from the table. Delete if none. -->

## Tax registration and tax types

<!-- FILL IN: what the entity is registered for (VAT, GST, sales tax or none,
as your jurisdiction applies) and in which country; the Xero tax type it uses
for domestic purchases, for zero-rated or exempt purchases, for reverse
charge, and for no-tax items; any recoverable-tax account kept outside the
normal return; and the traps (a foreign tax that is never recoverable here,
a tax that is always part of the cost so bill lines carry the gross). From
the entity's tax registration and its tax rates in Xero. Examples only in
no / yes shape. -->

## Tracking categories

<!-- FILL IN: each Xero tracking category the entity uses (department,
branch, cost centre), its options, which lines need it, how the option is
chosen, and what is exempt (FX lines, for example). From Xero's tracking
settings for the entity. Delete if none. -->

## Payroll

<!-- FILL IN: which rows of the payroll sources table in PAYROLL.md apply to
this entity (by number), and how health care and benefits reach it (inside
the payroll document or billed separately). -->

## Bank data

<!-- FILL IN: quirks of this entity's bank feeds and statements that affect
matching: rows that are not cash (failed, cancelled), how foreign card
charges appear, separate fee lines, statement date formats. The accounts
themselves are in config/group.toml. -->

## Standing arrangements and balance sheet

<!-- FILL IN: leases, deposits, loans, recurring journals, anything a
reviewer of this entity must know is correct by design. -->
