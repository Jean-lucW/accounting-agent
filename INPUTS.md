# Inputs: everything you need to provide

This is the complete list of information the agent needs about your group.
Each line says where the information goes.

- **R** = required for a first run.
- **O** = optional. Leave it out if it does not apply; the agent asks an
  admin when the situation comes up.

[ADAPTING.md](ADAPTING.md) has the setup path, the section-by-section map
and the questions to ask about unusual situations.

You do not write any code. Entity names, account codes, currencies, bank
accounts, Slack permissions and intercompany pairs all come from
`config/group.toml` and `rules/`, and every script, report, reconciliation
and Slack prompt reads them from there.

> **AI agents setting this up for someone:** go through this list with the
> person item by item. Ask for every **R** item. Ask whether each **O** item
> applies. Never fill in a fact about their group yourself. If an answer is
> missing, leave the section as `<!-- FILL IN -->`.

---

## 1. Services and credentials (`.env` and token folders)

| | Service | What you need | Guide |
|---|---|---|---|
| R | Anthropic | an API key, or a Claude subscription token, for headless `claude -p` runs | [docs/setup/SERVER.md](docs/setup/SERVER.md) |
| R | Xero | an OAuth app (client ID and secret), authorised for every organisation in the group | [docs/setup/XERO.md](docs/setup/XERO.md) |
| R | Slack | a Socket Mode app: bot token (`xoxb-`) and app token (`xapp-`) | [docs/setup/SLACK.md](docs/setup/SLACK.md) |
| R | Gmail | an OAuth client for the accounting inbox. **Gmail or Google Workspace only for now**; no other mail provider is supported | [docs/setup/GMAIL.md](docs/setup/GMAIL.md) |
| R | Server | a small Linux server (Ubuntu 22.04 or 24.04) and its ssh address | [docs/setup/SERVER.md](docs/setup/SERVER.md) |
| O | Google Drive | an OAuth client (the Gmail one can be reused), to publish workbooks and read payroll documents | [docs/setup/GOOGLE_DRIVE.md](docs/setup/GOOGLE_DRIVE.md) |
| O | Revolut Business | a read-only API certificate per account, per bank slug | [docs/setup/REVOLUT.md](docs/setup/REVOLUT.md) |
| O | Mercury | a read-only API token, per bank slug | [docs/setup/MERCURY.md](docs/setup/MERCURY.md) |
| O | Any other bank | nothing to connect: statement CSV exports dropped in `data/statements/` | [docs/BANKING.md](docs/BANKING.md) |

Tools needed on the laptop: Python 3.11+, Claude Code, git and `jq`. The
payment guard refuses every command without `jq`.

---

## 2. The group: `config/group.toml` `[company]`

- [ ] **R** Group name, as reports should show it → `name`
- [ ] **R** Reporting currency, any ISO code → `reporting_currency` (config
      refuses to load without it)
- [ ] **R** Timezone, any IANA name, used for report headings and run stamps → `timezone`
- [ ] **R** The Gmail address suppliers send invoices to → `accounting_inbox`
- [ ] **R** The Gmail label applied once an invoice is in Xero → `processed_label`
- [ ] **O** Financial year end, as `MM-DD` (default `12-31`); reports,
      quarters and look-back windows follow it → `financial_year_end`
- [ ] **O** The earliest date bank and accounts payable checks look back to →
      `records_from` (default: the start of the previous financial year)

## 3. Entities: `[[entities]]`, one block per Xero organisation

- [ ] **R** The list of every legal entity you keep books for.
- [ ] **R** A short, stable key for each entity, such as `HOLDCO` → `key`.
      Never rename it: renaming orphans the bank feed files.
- [ ] **R** The Xero organisation name, exactly as Xero shows it → `xero_name`
- [ ] **R** Base currency, any ISO code → `base_currency`
- [ ] **O** Display name, and the aliases people use for it in Slack
      ("parent", "us") → `short`, `aliases`
- [ ] **O** Country (ISO code) → `country`
- [ ] **R** Where its rules file lives → `rules_file`, default
      `rules/entities/<KEY>.md`. The file must exist.

## 4. Cash: `[[entities.bank_accounts]]`, per entity, per account

- [ ] **R** Every bank account, and the bank that holds it → `label`
- [ ] **R** How it is read: Revolut Business API, Mercury API or statement
      CSVs → `provider` = `revolut` / `mercury` / `csv`
- [ ] **R** The Xero bank account it reconciles to → `xero_account_code`
- [ ] **O** The credentials key for an API bank → `slug` (it picks
      `REVOLUT_<SLUG>_*` or `MERCURY_<SLUG>_KEY` in `.env`)
- [ ] **O** The currency, when one multi-currency account feeds a separate
      Xero bank account per currency → `currency`
- [ ] **O** For CSV history: the export file and the last date it covers →
      `statement_csv`, `csv_until`
- [ ] **O** Company cards and who holds each one, so a missing receipt is
      chased to the right person → `[slack.chase_routing]`, by cardholder name
- [ ] **O** Your bank's fee price list, if you want fees checked against it →
      `[bank_fees.<provider>]`

## 5. Chart of accounts (per entity)

- [ ] **R** Every cost category you use and the account code it maps to, per
      entity → `rules/EXPENSES.md` § Account table
- [ ] **O** Prepayment, accrual and deposit accounts → the balance-sheet table
      in `rules/EXPENSES.md`, and `[reports.*]` (`account_words` or explicit
      `accounts`)
- [ ] **O** Capitalisation threshold and depreciation life per asset type →
      `rules/EXPENSES.md` § 1. Capitalisation
- [ ] **O** Payroll control accounts (net wages, payroll taxes, pensions) and
      what a legitimate leftover balance on each looks like →
      `[entities.payroll_controls]`
- [ ] **O** Other accounts that are cleared by journal rather than through the
      bank → `[safety] phantom_check_exempt_accounts`
- [ ] **O** Accounts only one entity has → `rules/entities/<KEY>.md` § Accounts

## 6. Tax (per entity)

- [ ] **O** What the entity is registered for (VAT, GST, sales tax or none)
      and in which country →
      `rules/entities/<KEY>.md` § Tax registration and tax types
- [ ] **O** Which input tax is recoverable, and the Xero tax types used →
      the same section
- [ ] **O** Branches or second registrations abroad → the same section
- [ ] **O** Anything that departs from the generic tax method, such as
      reverse charge or foreign tax → `rules/EXPENSES.md` § 5. Sales tax,
      VAT and GST

## 7. Intercompany

**R** if the group has more than one entity.

- [ ] Every intercompany account pair: the two entities, the kind of balance
      (loan, recharge, management fee, cost attribution), the account code on
      each side, and whether it is payments-enabled in Xero →
      `[[intercompany]]` `a`, `b`, `flavour`, `accounts`, `payments_enabled`.
      If one side has not been opened yet, give its code as `""`.
- [ ] What goes through each kind of balance → `rules/INTERCOMPANY.md` § 1. Flavours
- [ ] Who funds whom → `rules/INTERCOMPANY.md` § 2
- [ ] How a cost paid by one entity for another is booked on both sides →
      `rules/INTERCOMPANY.md` § 3, `rules/GROUP.md` § Costs paid by one entity
      for another
- [ ] Recharge policy, including any markup → `rules/INTERCOMPANY.md` § 4
- [ ] How often balances are revalued for FX → `rules/INTERCOMPANY.md` § 5
- [ ] Period-end sweeps from one kind of balance into another →
      `rules/INTERCOMPANY.md` § 6
- [ ] The FX gain or loss account → `[intercompany_settings] fx_gain_loss_account`
- [ ] **O** Accounts that look intercompany but are with a party outside the
      group, such as a shareholder loan → `[[non_group]]`. These are
      reported, never matched.
- [ ] **O** Matching tolerances, small-break threshold, fallback FX rates and
      FX narration wording → `[intercompany_settings]`. Defaults are
      provided.
- [ ] **O** Tracking categories an entity's FX revaluation lines must carry
      → `[intercompany_settings.fx_tracking]`

## 8. Shared costs

- [ ] **O** Each cost shared by more than one entity, and how it is split
      (fixed percentage, headcount, usage) → `rules/ALLOCATION.md` § 3
- [ ] **O** Where the numbers for each split come from (headcount list, usage
      report) → `rules/ALLOCATION.md` § 2
- [ ] **O** Who holds each shared contract, and whether it is recharged at
      cost or with a markup → `rules/ALLOCATION.md` § 4
- [ ] **O** How the split is booked: a journal pair or a recharge invoice →
      `rules/ALLOCATION.md` § 5

## 9. Suppliers

**O**, but strongly recommended. Seed `rules/SUPPLIERS.md` § Entries with
your 20 to 30 largest or most frequent suppliers. Admins' answers fill in the
rest over time. For each supplier:

- [ ] Name as it appears on the invoice, and the payee name as it appears on
      the bank statement
- [ ] Which entity the supplier bills, and which entity really bears the cost
- [ ] Default account, tax treatment and currency
- [ ] Billing pattern (monthly, annual, usage) and any special handling
- [ ] **O** Words too common among your suppliers to identify one (such as
      "cloud" or "consulting") → `[accounts_payable] generic_supplier_words`

## 10. People and Slack

- [ ] **R** The Slack channel the agent reports into → `[slack] channel_id`, `channel_name`
- [ ] **R** At least one admin (member ID and name). Admins command the agent,
      change the rules and receive reports → `[slack.admins]`
- [ ] **O** Users, who feed in information and invoices but cannot command
      the agent → `[slack.users]`
- [ ] **O** Read-only users, who are users who can also ask questions →
      `[slack.readonly]`
- [ ] **O** Who to chase for a missing invoice or receipt: by cardholder
      name, then cost category, then entity, then a default →
      `[slack.chase_routing]`
- [ ] **O** Who receives the list of unpaid bills → `[accounts_payable] unpaid_notify`
- [ ] **O** Nicknames, keyed by display name → `[slack.nicknames]`
- [ ] **O** Anyone the agent must never message under any rule → `never_message`

## 11. Payroll

- [ ] **O** Who employs whom, by role and location, never by name →
      `rules/PAYROLL.md` § Who employs whom
- [ ] **O** Each payroll source (a provider, a bureau, in-house) and where
      its documents are filed → `rules/PAYROLL.md` § Payroll sources
- [ ] **O** What flows through each control account → `rules/PAYROLL.md` § Control accounts
- [ ] **O** Employer-cost accounts: payroll taxes, social security,
      pensions, benefits → `rules/PAYROLL.md` § Employer taxes, pensions and benefits
- [ ] **O** A line-by-line mapping of each source's report to accounts →
      `rules/PAYROLL.md` § Mapping tables
- [ ] **O** How contractors are treated, and which currency each payroll
      source reports in → `rules/PAYROLL.md` § Contractors versus employees,
      § FX handling

## 12. The group's rules in prose

- [ ] **R** What the group does, in two or three sentences: what it does,
      where revenue is earned, which entity is the parent and why the others
      exist → `rules/GROUP.md` § What the group is
- [ ] **R** What each entity does: its role, the revenue it may carry, the
      staff it employs → `rules/GROUP.md` § What each entity does, plus
      `rules/entities/<KEY>.md` § Role
- [ ] **R** **The recognition matrix**: one row per cost category, one cell
      per entity, each cell one of `recognises` / `allocated` / `pays` / `never`
      or a short condition such as `own staff` →
      `rules/GROUP.md` § Which entity recognises which cost. This single
      input decides the entity on every invoice.
- [ ] **O** How cash moves inside the group → `rules/GROUP.md` § Who pays whom
- [ ] **O** The period costs must be accurate to (month or quarter) →
      `rules/GROUP.md` § Cost recognition principles
- [ ] **O** Which documents become bills, and which are handled another way
      (leases, payroll, card receipts) → `rules/GROUP.md` § Transaction-type
      rule, § Leases
- [ ] **O** Other tools that already post into Xero, such as a receipt-capture
      or expenses app, so nothing is posted twice → `rules/GROUP.md` § Upstream tools
- [ ] **O** When the group prepays and when it accrues → `rules/EXPENSES.md`
      § 2 and § 3
- [ ] **O** How deposits, foreign currency, receipts, refunds and credit notes
      are treated → `rules/EXPENSES.md` § 4, § 6 to § 8
- [ ] **O** Whether a document a person submits presumes their own employing
      entity → `rules/EXPENSES.md` § Intake presumption by sender
- [ ] **O** Office locations and which entity each belongs to →
      `rules/EXPENSES.md` § Office locations
- [ ] **O** Tracking categories (department, location, project) →
      `rules/entities/<KEY>.md` § Tracking categories
- [ ] **O** Bank feed quirks of an entity → `rules/entities/<KEY>.md` § Bank data
- [ ] **O** Leases, deposits, loans and recurring journals of an entity →
      `rules/entities/<KEY>.md` § Standing arrangements and balance sheet

Fill the `rules/` files in this order, because each leans on the one
before: `GROUP.md`, then the entity files, then `EXPENSES.md`,
`SUPPLIERS.md`, `INTERCOMPANY.md`, `ALLOCATION.md` and `PAYROLL.md`. Replace
or delete the three example entity files; preflight fails while one remains.

## 13. Accounts payable and reports (defaults are provided)

- [ ] **O** How many days a card charge may come before its invoice →
      `[accounts_payable] lead_days`
- [ ] **O** The posting lag allowed on an intercompany funding leg → `leg_window_days`
- [ ] **O** How many days a card payment takes to settle → `card_lag_days`
- [ ] **O** Bills never settled through the bank, such as payroll cleared by
      journal → `[[accounts_payable.not_via_bank]]`
- [ ] **O** The financial year from which report rows are always shown, and
      how far back the first build reads documents → `[reports]` `from_year`,
      `first_build_from` (defaults: the current financial year, and the start
      of the previous one)
- [ ] **O** Words that identify, or exclude, prepayment, accrual and deposit
      accounts → `[reports.prepayments]`, `[reports.accruals]`,
      `[reports.deposits]`

## 14. Outputs (optional)

- [ ] **O** The Google Drive folder workbooks are published to, and a
      subfolder per workbook kind → `[drive]` `publish_folder_id`,
      `[drive.subfolders]`, optionally `[drive.routes]`
- [ ] **R** The bodies of work the agent runs → `[[domains]]`. Copy the two
      shipped blocks (bookkeeping, reports) as they are.

---

## Situations to ask about

These are the items most often missed. Ask about each one. Every yes needs a
rule in the file shown.

| Ask | If yes, write it in |
|---|---|
| Does one entity routinely pay costs that belong to another? | `rules/GROUP.md`, `rules/INTERCOMPANY.md` § 3 |
| Are any contracts (software, office, insurance) shared between entities? | `rules/ALLOCATION.md` |
| Does any entity have a branch or a tax registration abroad? | `rules/entities/<KEY>.md` § Tax registration |
| Do staff of one entity work for another, or get paid through an employer of record? | `rules/PAYROLL.md` § Who employs whom |
| Is there a bank account with no API and no regular statement export? | `provider = "csv"`, and name who drops the files |
| Do invoices arrive anywhere other than the Gmail inbox, such as a portal, post or another mailbox? | forward them to the inbox or send them to the Slack app; note it in `rules/GROUP.md` § Upstream tools |
| Does another tool already post bills or receipts into Xero? | `rules/GROUP.md` § Upstream tools |
| Is any supplier billed in a currency the entity does not use? | the supplier's entry, and `rules/EXPENSES.md` § 6 |
| Are any costs recharged with a markup? | `rules/INTERCOMPANY.md` § 4, `rules/ALLOCATION.md` § 4 |
| Are there loans with shareholders or other parties outside the group? | `[[non_group]]` |
| Are there leases, deposits or long prepayments? | `rules/GROUP.md` § Leases, `rules/EXPENSES.md` § 2 and § 4 |
| Is any entity dormant, or about to be opened or closed? | `rules/entities/<KEY>.md` § Role |
| Is there anyone the agent must never chase? | `never_message` |
| Is there anything about the books that would surprise a new bookkeeper? | the file that owns the subject; ask if unsure |

---

## Day one versus later

**Minimum for a supervised first run:**

- §1: the five required services;
- §2;
- §3: `key`, `xero_name` and `base_currency` per entity, plus its rules file;
- §4: one bank account per entity;
- §7, if there is more than one entity;
- §10: the channel and one admin;
- §14: `[[domains]]` as shipped;
- §12: what the group does, what each entity does and the recognition
  matrix;
- §5: the account table.

**Everything else can wait.** Every section left as `<!-- FILL IN -->`
means "no rule". The agent does not guess: it asks an admin in Slack and
records the question in the outstanding-items register. When the admin
answers, the run writes the ruling into the file that owns the subject, so
the rules grow from use. A half-filled `rules/` folder is normal for the
first weeks.

**What never goes in these files:** secrets. API keys and tokens go in `.env`
and the token folders (§1), which are gitignored.
