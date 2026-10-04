# Banking: the bank is the source of truth

## The central rule

**Where cash actually moved is decided by the bank, never by Xero.** Xero is
what we think happened; the bank statement is what happened. Whenever the two
disagree, the bank wins and the Xero side is what gets corrected.

Go to the bank, every time, before concluding anything about:

- **whether something was paid at all**: an AUTHORISED bill is not evidence of
  payment, and neither is a receipt that says PAID;
- **which entity paid it**: the paying entity is whichever account the money
  left, whatever the invoice is addressed to;
- **a suspected double payment**: two bills is not a double payment, two debits
  is. Check the statement before raising it or reversing anything;
- **a refund or credit note**: find the credit on the statement and match it to
  the original debit, by amount and counterparty;
- **an unmatched bank-feed line**: before chasing anyone for a missing invoice,
  confirm the debit is real and is not already bookkept;
- **the date of a cost**: the statement date is the cash date.

This applies across every workflow: bookkeeping, reviews, intercompany,
reports. If a question is about cash and the answer came only from Xero, the
answer is not finished.

## The bank accounts: config/group.toml

Every bank account the agent reads is declared in `config/group.toml`, under
its entity, as an `[[entities.bank_accounts]]` block. That table is the
inventory: read it (or run `.venv/bin/python -m accounting_agent.config`)
rather than keeping a list anywhere else. Each block says:

| Key | Meaning |
|---|---|
| `label` | how reports and feed headings name the account ("Revolut", "Mercury", "Local bank") |
| `provider` | `revolut` or `mercury` (read through the read-only API clients) or `csv` (no API: statements only) |
| `slug` | API providers only: picks the credentials in `.env` (`REVOLUT_<SLUG>_*`, `MERCURY_<SLUG>_KEY`) |
| `statement_csv` | the saved statement export under `data/statements/`, if there is one |
| `csv_until` | the last day read from the CSV; the API takes over the day after. Leave it out to use the API for everything |
| `xero_account_code` | the Xero bank account this feeds, for reconciliation |
| `currency` | optional: restrict the block to one currency sub-account |

An entity with no bank account block has no bank of its own: its costs are
paid by another entity, and every search for one of its bills runs across
every group account (rules/GROUP.md says who funds whom).

### Adding a bank account

1. Add an `[[entities.bank_accounts]]` block under the entity in
   `config/group.toml`.
2. For an API provider, put the credentials in `.env` under the slug and check
   the connection: `scripts/revolut_check.py <slug>` (follow
   `docs/setup/REVOLUT.md` to authorise first) or `scripts/mercury_check.py
   <slug>`. Both list the configured slugs when run without one. Use READ ONLY
   credentials: the clients refuse every non-GET at the transport layer, and
   the bank should refuse a write as well.
3. If you have history before the API was connected, save the export as
   `data/statements/<file>.csv` (mode `0600`) and set `statement_csv` and
   `csv_until` to the last WHOLE day the export holds. An export taken
   part-way through a day leaves that day to the API.
4. Run `scripts/bankfeed.py refresh <entity>` once; the account's heading
   appears in `data/bankfeed/<slug>.md` with its first stamp.

Nothing else needs editing: the bank feed, the bills report, the bank and FX
fees report and the phantom-payment check all read the same table.

## Which source to use, by date

For each account, the date of the movement you are asking about decides the
source, and the two are never mixed for one query.

| Movement date | Source | Where |
|---|---|---|
| on or before the account's `csv_until` | the saved CSV export | `data/statements/<statement_csv>` on the server |
| after `csv_until` (or always, when there is none) | the bank API, read-only, from the server | `RevolutClient(slug)` / `MercuryClient(slug)` |
| any date, `provider = "csv"` | the saved CSV export only | `data/statements/<statement_csv>` |

Never infer a movement after `csv_until` from the saved file: it ends there.
Never answer a question about a date on or before `csv_until` from the API when
the CSV is the authoritative export for that period (the API usually reaches
back further, so it is a fine cross-check, but the CSV is the record).

**The API clients.** Both clients are GET-only at the transport layer
(`docs/REVOLUT_API_REFERENCE.md`, `docs/MERCURY_API_REFERENCE.md`). `statement_lines()` returns the same columns
from both, so the feeds compare like for like: `state` completed (Revolut) /
sent (Mercury) is cash; pending has not settled; declined, reverted, failed,
cancelled, blocked are not cash. Mercury's account list omits the credit card;
the client fetches it from `/credit` and merges it in as `kind="credit"`, so
one call returns checking, savings and card lines (`account_kind` says which).
Debit card spend is on the checking account. Mercury's own `kind`, category
and GL guesses are not authoritative; code per `rules/EXPENSES.md`.

**A Mercury `Intl. Transaction Fee` is a fixed percentage of the charge it
belongs to** (3% when this was written; check the account's own fee
schedule). The fee arrives as its own statement line, next to several other fees of
the same name on the same day, and the only way to say which purchase each one
belongs to is the arithmetic: divide the fee by that rate and the charge it matches
is on the same day. Two consequences worth having. A fee whose purchase is not
yet bookkept still posts on its own, as spend money to bank fees, the moment
the purchase does. And **a foreign-currency purchase with no fee beside it was
billed in dollars by the merchant**, not converted by Mercury: the merchant did
the conversion at its own rate, so the card amount will not reconcile to the
invoice at any rate the bank used, and the bill's own currency is the one to
trust.

**Only group entities.** A bank account belongs in `config/group.toml` only if
its entity is one the agent bookkeeps. An account the company can read but does
not bookkeep (a related party outside the group) is never added there: never
refresh a bank feed from it, never bookkeep from it, never include it in a
run's entity list. A loan to such a company is the lending entity's own bank
line, bookkept from the lender's side only (rules/INTERCOMPANY.md, non-group
accounts in config).

## The reconstructed bank feed: `data/bankfeed/<slug>.md`

Xero has a bank feed and the API cannot read it, so we keep our own. One file
per entity covering every bank account of that entity, one line per statement
line that is **not yet reconciled** and has not been reported as reconciled by
hand.

The goal is the thing Xero's reconcile screen gives a human and gives us
nothing: **every single cash payment on every single bank account ends up as a
bill, spend money or transfer in Xero.** The feed file is the standing list of
what has not got there yet.

**Server only.** `data/` is gitignored and never synced anywhere, so these
files exist on the server and nowhere else. Directory `0700`, files `0600`.

Other scripts may read the feed files (the bills report does, to say whether
the agent still has a line on its list). A read never removes, adds or edits a
feed line: a line leaves the file only through the bookkeeping rules below.

### Every bookkeeping or reconciliation run starts here

Before reading a single invoice:

1. **Read the feed file** for every entity in scope. This is what cash is
   unaccounted for. A run that skips it is working blind.
2. **Read the `last checked` stamp** on each account heading.
3. **Refresh from the bank API**: `scripts/bankfeed.py refresh all`. For every
   account with an API it pulls every settled movement after the account's
   stamp, appends the ones not already in the file (each tagged `rev:<id>` /
   `mer:<id>` so it cannot be added twice) and moves the stamps to now. It
   never removes anything. Pending rows are skipped and picked up once they
   settle. A `csv` account is not refreshed: see "Banks without an API" below.
4. Do the bookkeeping work as normal.
5. **Remove a line the moment the cash is accounted for**: the bill, spend
   money or transfer that covers it is posted. Not batched at the end.
6. **Refresh again when the run is done**, then `status`, and put the count per
   entity in the run report. The file the next run opens must be current: the
   bank feed is rebuilt every time the bookkeeping is completed.

```bash
.venv/bin/python scripts/bankfeed.py status
.venv/bin/python scripts/bankfeed.py refresh all              # or one entity; --dry-run to preview
.venv/bin/python scripts/bankfeed.py list holdco "GBP Main"
.venv/bin/python scripts/bankfeed.py remove holdco "Contoso Cloud" "bill INV-1002 posted"
.venv/bin/python scripts/bankfeed.py stamp holdco "GBP Main"  # manual stamp, rarely needed
```

`<entity>` is any key, slug, alias or short name `config/group.toml` gives the
entity.

A line added by `refresh` has an empty `who` when the bank did not name a
cardholder and an empty `staged` column: the run fills those in as it learns
them, and the `note` column is where chases are recorded.

`remove` refuses on anything but exactly one match, so a run cannot quietly
drop the wrong payment.

### The bill feed: `data/billfeed/<slug>.md`

The other half of the picture: every AUTHORISED, unpaid bill per entity,
pulled from Xero by `scripts/billfeed.py refresh all` and stored as a snapshot
(server only, same permissions as the bank feed). Nothing is inferred: a bill
leaves the feed when Xero shows it paid. Refresh it early in every bookkeeping
run and again before the bill-payments check. `scripts/billfeed.py match`
reads the two feeds from disk and pairs open bills with bank lines by
currency, amount and date (less `accounts_payable.card_lag_days` for card
timing); a pair in a different entity is the `bill-payments` skill's work
(payer's spend money to the pair's intercompany loan, bill reported for the
user), a pair in the same entity is the user's Match in Xero, and neither
reads the bank again.

### What removes a line, and what does not

| | |
|---|---|
| **Removes it** | the bill / spend money / transfer accounting for it is posted in Xero |
| **Removes it** | an admin says on Slack it was reconciled by hand |
| **Removes it** | Xero already holds a live AUTHORISED bank transaction, or a payment on a bill, of the same date, amount, currency and direction, whoever put it there |
| **Removes it** | it is a transfer between two group entities' own accounts, when rules/INTERCOMPANY.md says a person matches those by hand |
| **Does NOT** | chasing someone for the receipt: the cash is still unaccounted for |
| **Does NOT** | a `staged` coding sitting in the Xero UI: staged is not reconciled |
| **Does NOT** | deciding it is immaterial |

**Intercompany transfers.** Money moving between two group entities' own
accounts shows up twice, once as a payment out and once as a receipt in.
Whether the agent posts those legs or a person reconciles both in the Xero UI
is the group's own rule, in `rules/INTERCOMPANY.md`. Where a person does it,
an interco transfer leaves the feed on sight, with the reason recorded, and is
not chased and not bookkept. What this does **not** excuse is the
intercompany *reconciliation*: agreeing the two entities' loan accounts
transaction by transaction is a separate piece of work
(`docs/INTERCOMPANY_RECON.md`) and it is unaffected by how the bank lines get
matched.

Two things that look like transfers and are not: a loan to or from a party
outside the group, and a payment by one
entity of another entity's supplier cost. The second is a real supplier bill
in the entity that bears the cost (rules/GROUP.md says which entity recognises
what), plus an interco settlement leg: see the `bill-payments` skill.

**Look the line up in Xero before chasing it.** A person coding the feed in
the Xero interface, or paying a bill there, accounts for the cash without the
agent knowing: the line is still on the feed because the feed is our own
reconstruction. So before a chase goes out, read the entity's live AUTHORISED
bank transactions and the supplier's bills around that date and amount. Two
shapes turn up every run. A charge with no invoice that a person has already
coded to spend money means the cash is accounted for and the chase is dropped,
though a documented cost sitting in spend money rather than a bill is still
reported. A supplier who bills early in the month and collects at the end of
it means the month-end debit settles a bill already in the books, so nothing
is missing at all.

**This list is what the chases are for.** A line sitting here with no invoice
is a person to message, routed per the `xero-bills` skill. The feed tells you
who to chase and about what; the note column records that you did.

### Line format

```
<date> | <payee> | spent|received | <CCY> <amount> | <who> | <staged> | <note>
```

`payee` exactly as the bank wrote it: that mangled string is what you match
on. `staged` is coding already prepared in the Xero UI by a human: a strong
hint, not an instruction, and the invoice still decides. `note` carries chases
sent and why a line is stuck.

## Banks without an API (`provider = "csv"`)

Some banks have no API the agent can read. Such an account is declared with
`provider = "csv"` and a `statement_csv`, and its statements are dropped into
`data/statements/` as CSV, as often as the bookkeeping needs (monthly at
least, weekly for a busy account). Replace the file with a fuller export each
time rather than adding a second one: the scripts read one file per account.

- `bankfeed.py refresh` leaves a csv account alone. When a new statement
  lands, add its unreconciled lines to the feed with `bankfeed.py add <entity>
  "<label>" "<line>"` in the line format above (the first `add` opens the
  account's heading under the `label` config gives it), then `stamp` the
  account with the statement's last date.
- The bills report and every other bank question read the CSV directly.
- The data is only as fresh as the last export. A run report must say the date
  a csv account's statement runs to, and a bill paid after that date is
  "not yet visible", never "unpaid".

**The generic CSV schema.** A bank whose export is neither Revolut's nor
Mercury's is read with these headers (case-insensitive; save or convert the
export to them):

| Column | Required | Use |
|---|---|---|
| `Date` | yes | ISO `YYYY-MM-DD`, the settled (cash) date |
| `Description` | yes | the counterparty as the bank wrote it (`Payee` is accepted too) |
| `Amount` | yes | signed: negative is money out |
| `Currency` | no | ISO code; defaults to the block's `currency`, else the entity's base currency |
| `Reference` | no | the payment reference: an invoice number here is strong evidence |
| `Original amount`, `Original currency` | no | what was charged before conversion |
| `Account` | no | sub-account name; defaults to the block's `label` |
| `Payer` / `Card` | no | who spent it |
| `Status` | no | rows marked pending, failed, cancelled, declined or reverted are not cash and are skipped |

## Where the statements live

**On the server, `data/statements/<file>.csv`**, one file per account named in
its `statement_csv`. `0600`, in a `0700` directory, under `data/`, which is
gitignored and never synced, so they never reach the repository.

**Read them as a matter of course.** They are the source of truth on cash and
they are local to the agent: a run that needs to know whether something was
paid reads the CSV instead of asking.

**Confidentiality.** Anyone with root on the server can read them; file
permissions are hygiene, not a boundary, so keep the set of server admins
small. Do not copy the statements anywhere further: not into the repo, not
into `docs/`, not into a Slack message or a run report. Quote the individual
rows a question needs rather than the file.

## The files

### Revolut CSV columns

Revolut exports share one schema, 33 columns. The entity is **not** in the
filename, so identify it by the `Account` column and the IBAN. The ones that
matter:

| Column | Use |
|---|---|
| `Date started (UTC)` / `Date completed (UTC)` | Initiated vs settled. **Use completed for the cash date**; they differ across month ends. |
| `Type` | `CARD_PAYMENT`, `TRANSFER`, `EXCHANGE`, `TOPUP`, `FEE`, `REFUND`, `CARD_REFUND`, `REV_PAYMENT` |
| `State` | Only `COMPLETED` is cash. Ignore anything else when reconciling. |
| `Description` | The counterparty as the bank saw it: merchant names are often mangled |
| `Payer`, `Card label` | Who spent it. `Card label` often names the entity and the person |
| `Orig currency` / `Orig amount` | What was actually charged, before conversion |
| `Payment currency` / `Amount` / `Fee` | The account-side movement. Negative is money out. |
| `Balance` | Running balance, per account: use it to prove nothing is missing |
| `Account` | Which sub-account, e.g. `EUR Main`. **This is how the account is identified.** |
| `Related transaction id` | Links a refund to its original, and both legs of an `EXCHANGE` |
| `MCC` | Merchant category, useful when the description is unreadable |

An `EXCHANGE` is two rows, one per side, sharing a `Related transaction id`.
Counting both as spend double counts.

### Mercury CSV columns

22 columns, a different schema. Note the dates are **US format `MM-DD-YYYY`**,
which sorts wrong as a string: parse before comparing.

| Column | Use |
|---|---|
| `Date (UTC)`, `Timestamp` | `MM-DD-YYYY`; `Timestamp` is the precise one |
| `Description`, `Bank Description` | Counterparty; `Bank Description` is the raw one |
| `Amount` | Negative is money out |
| `Status` | `Sent` is cash. **`Failed` and `Cancelled` are not**: a failed payment is a common cause of a bill that looks unpaid. `Pending` has not settled. |
| `Source Account` | the Mercury checking account (`Mercury Checking xx1234`) or `Mercury Credit` |
| `Last Four Digits`, `Name On Card`, `Cardholder Email` | Who spent it |
| `GL Code`, `Category` | Mercury's own guess. Not authoritative; code per `rules/EXPENSES.md`. |

## Proving an account is complete: tie the balance out

The fastest way to know whether `data/bankfeed/<slug>.md` is right about an
account is not to walk the lines, it is to tie the closing balances:

```
statement closing balance  +/-  the feed's outstanding lines  ==  Xero closing balance
```

Xero's `bank_summary` reports every account in the **org's base currency**, so
convert before comparing. When the difference equals exactly the sum of the
feed's outstanding lines for that account, the feed is correct and the account
is complete.

A difference that is **not** explained by the feed's own lines is the finding:
either a statement line has no Xero counterpart, or Xero holds something the
bank never saw. Do this before concluding an account is clean.

## Method

- Match on **amount + date window + counterparty**, in that order. Descriptions
  are unreliable; amounts are not.
- A card payment settles one to three days after the purchase. Widen the window
  before concluding a debit is missing.
- Check `Balance` continuity per account when a period looks incomplete.
- For a suspected duplicate: two debits, same amount, same counterparty, within
  a few days, with no `REFUND` or `CARD_REFUND` between them. One bill posted
  twice is a Xero problem, not a bank one.
- **Never create a payment in Xero from a statement line.** The agent creates
  the AUTHORISED bill, the user matches the feed line by hand.

## The scripts that read the bank

| Script | What it does |
|---|---|
| `scripts/bankfeed.py` | the reconstructed bank feed (above) |
| `scripts/billfeed.py` | the bill feed and `match` (above) |
| `scripts/bills_report.py` | every bill, paid or open, and for each open one the bank's answer (`report-bills` skill) |
| `scripts/bank_fees.py` | every bank and FX fee and every conversion on the revolut and mercury accounts, by month, in the reporting currency (`[company] reporting_currency`), each fee checked against your own price list in `[bank_fees.<provider>]` when you give one |
| `scripts/check_no_phantom_payments.py` | closes every bookkeeping run: no unreconciled payment may sit on a bank account |
| `scripts/revolut_check.py`, `scripts/mercury_check.py` | the staged connection checks for one API account |
