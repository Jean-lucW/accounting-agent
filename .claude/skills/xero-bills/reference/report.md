# xero-bills reference: the report, and an exit check that fails

Read on demand from `.claude/skills/xero-bills/SKILL.md`, which says when. The rules here are as binding as the skill's own; they sit here so a run reads them when they apply, not on every turn.

## When an exit check fails

`check_no_phantom_payments.py` scans every entity for unreconciled payments
sitting on **bank** accounts, the phantom entries that duplicate real
statement lines. It must print **PASS**. A FAIL means a payment was created
during the run: remove the ones the agent created, but **never remove a
payment that is already reconciled** (that is the user's own matching work,
see `xero` skill safety rule 7). Payroll control accounts
(`config.payroll_controls()`) and any codes in
`config.phantom_exempt_accounts()` are reported separately as an allowed
exception, not a failure.

**A FAIL is not proof the run did it.** The check reports the state of the
ledger, not the authorship of it, so a person paying staff expense claims in
the Xero Expenses UI produces exactly the same FAIL: a batch of unreconciled
payments on a bank account, sitting there until the bank line arrives and
someone matches it. The tell is the invoice number **Expense Claims** and an
`UpdatedDateUTC` before the run started. Those are the user's own work, they
are API-immutable anyway, and deleting them would destroy a real
reimbursement. So before touching anything: read each flagged payment's
timestamp and invoice, and delete only what this run can be shown to have
created. Where none of it is the run's, **report the FAIL in the headline
exactly as it printed** and say in one line who made the payments and why they
are there. Never soften it to a pass, and never quietly delete to make the
check go green.

`outstanding.py check-queries` must print **PASS** as well. A FAIL names each
bookkeeping query in the register that was never investigated, or was graded
high or medium and never went through the gate: open it with
`investigate_query.py open --from-register <id>` and deal with it like any
other question, then close the line.

## The report

Report in the house style, `docs/COMMS.md`. It is a **hard rule** and it
governs every message this run sends to anyone, not just this report: every
chase, every query, every thread reply. Headline line, then only the sections
that have content: a bold lowercase heading, bullets underneath.
`scripts/bookkeep_report.py` has written `bookkept` and `bank feed`; the
session writes the headline and every other section. The example uses the
fictional group in `config/group.example.toml`:

```
bookkeeping · 14 bookkept · 3 resolved · 1 to confirm · 2 blocked · 1 query · 1 manual · check PASS

*bookkept*
• Contoso Cloud · 04 Sep · EUR 1,240.00 · paid OpCo EU · recognised OpCo EU · INV-1002

*resolved*
• OpCo US, the invoice is billed to it and its last three bills sit there · Fabrikam Travel · 03 Sep · USD 410.00 · paid OpCo US · recognised OpCo US · INV-1007
• 2 to-confirm items accepted, unchallenged for 7 days

*to confirm*
• Software (6300) in OpCo EU, a new supplier selling the same tool as Litware Software. Reply yes, or the right account · Northwind Store · 06 Sep · EUR 180.00 · paid OpCo EU · recognised OpCo EU · INV-1008

*bill payments*
• Adventure Works Hotels · 28 Aug · GBP 240.00 · booked to Travel (5100) · INV-1004 - paid by OpCo US from Mercury on 30 Aug, recognised as a bill in HoldCo, spend money posted in OpCo US to the HoldCo loan account. *Manually reconcile the bill in HoldCo to intercompany.*

*not attempted*
• the July Contoso Cloud invoices for OpCo US, deferred to the August review

*blocked*
• the tax split on the laptop bill cannot post, the 31 Mar lock date rejects it - INV-1001, DR Input VAT (2250) / CR Computer Equipment (1500), GBP 480.00

*queries - could not answer*
• Litware Software, OpCo EU or OpCo US? Proposed OpCo EU (low): billed to OpCo EU, paid by OpCo US's card, no earlier bill to settle it · 05 Sep · USD 3,400.00 · INV-1006

*manual*
• reconcile the HoldCo Adventure Works Hotels bill to intercompany - INV-1004

*chased*
• the travel owner for the 28 Aug Adventure Works Hotels receipt, HoldCo

*from users*
• bookkeeper: the card charge is OpCo US · coded Software (6300) in OpCo US

*wrote back*
• rules/SUPPLIERS.md, Contoso Cloud to Hosting (5320) in the bill-to entity
```

Every rule for these lines lives in `docs/COMMS.md` and nowhere else: read it
before writing the report, do not work from memory. Read it once, with the Read
tool: `cat` spills a file that size and it ends up read twice.

**Register what the report leaves with a person.** Every `queries`, `to
confirm`, `manual` and `blocked` line is added to the outstanding register in
the same step that writes it (`scripts/outstanding.py add --domain bookkeeping
--kind queried|decided|manual|blocked --with <person> --key <ref> --text "..."
--refs "..."`), and every item this run resolved is closed, by the poster's
`settle` list or `scripts/outstanding.py close --key <ref>`. The report and
the register must agree.

## Where the report goes

**The report is posted to the channel**, because a bookkeeping run belongs to
the team, not to the admin who happened to start it. Queue it with the outbox,
never `chat.postMessage` (a hook refuses a direct post to the channel from a
run): the runner posts it once the session has ended, with the run's cost in
brackets after the headline, which only it can know.

```bash
.venv/bin/python scripts/slack_outbox.py channel \
  --headline "BOOKKEEPING RUN (18 Sep 2026, 14:32 UTC)" --thread $RUN/report.md
```

The top-level message is the headline and nothing else; the whole report
(every section, `queries`, `manual` and `bill payments` included) is the first
reply in its thread. The date and time are the run's own start, in the
group's timezone (`config.company("timezone")`), in that shape. The admin
whose thread started the run gets the report's one-line headline there plus
`full report in #<channel_name>`, never the report twice.

**Then the manual items, queued the same way**: a top-level
`MANUAL ITEMS (18 Sep 2026, 14:32 UTC)` and, as the first reply in its thread,
`from the bookkeeping run of ...` followed by every `bill payments` bullet and
every other line telling the user to do something in Xero by hand. Skipped
only when the run has none.

Chases, queries put to a person and answers to anything else asked in a thread
are unaffected: they stay with the person. If the channel refuses the post
(`channel_not_found`, the bot is not in it) the runner adds the queued report
to the admin's thread itself. Rules in `docs/COMMS.md`, "Where a message goes".
