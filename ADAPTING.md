# Adapting the agent to your group

This is the most important document for a new adopter. It covers:

1. what the agent has to know about your group;
2. where each piece of information goes in the repo;
3. the one setup path, from clone to the first scheduled run.

Nothing in the code names a company, entity, account, bank or person. All of
that comes from the files this guide tells you to fill in.

> **Use a private repository.** Once you fill it in, `config/group.toml` and
> `rules/` describe your group's books, and the server commits admin rulings
> into `rules/`. Clone this template into a **private** repository and keep
> the public one only as an `upstream` remote you pull fixes from. Never push
> a filled-in copy anywhere public.

---

## For an AI agent helping someone adopt this repo

If you are Claude, or any other agent, setting this up for someone, follow
these steps:

1. **Interview first.** Go through [INPUTS.md](INPUTS.md) with the person
   before writing anything. Ask for every item marked required. For optional
   items, ask whether they apply.
2. **Never invent a fact about their group:** an entity, account code,
   supplier rule, split or person. If an answer is missing or vague, ask
   again, or leave the section as `<!-- FILL IN -->`. An unfilled section
   makes the live agent query an admin; a guessed one makes it post wrong
   entries.
3. **Ask about anything unusual.** Part 3 lists situations that need their
   own rule. If the person mentions something that fits nowhere in this
   guide, ask how they want it handled and write the answer into the file
   that owns the subject (Part 2).
4. **Write each answer where Part 2 says it goes**, and nowhere else. One
   subject, one home.
5. **Finish by checking.** Run `python -m accounting_agent.config`. Grep
   `rules/` for `FILL IN` and read back to the person what is still open.
   `deploy/preflight.sh` must end with `preflight clean`.

---

## The three kinds of information

| Kind | Where | Examples | Read by |
|---|---|---|---|
| **Structure** | `config/group.toml` | entities, Xero organisation names, currencies, bank accounts, intercompany account pairs, control accounts, Slack member IDs | every script, through `accounting_agent.config` |
| **Judgement** | `rules/*.md` | which entity bears a cost, account coding, cost splits, supplier rules, payroll handling, intercompany routing | the Claude session, at decision time |
| **Access** | `.env` and token folders (`.xero/`, `.gmail/`, `.gdrive/`, `.revolut/`) | API keys, OAuth tokens | the connectors in `src/accounting_agent/` |

Change structure in config and every feed, report and reconciliation picks it
up on its next run. Change judgement in `rules/` and the next bookkeeping run
decides differently. You should not need to edit any code.

---

## Part 1. What the agent needs: the inputs checklist

The full checklist is [INPUTS.md](INPUTS.md). It lists every item, marks
each one required or optional, says where it goes, and gives the minimum for
a first run. Go through it with the person before writing anything.

---

## Part 2. Where each piece of information goes

Every row is one piece of information and the file and section it goes into.
`rules/` sections are headings in that file. Config sections are TOML tables
in `config/group.toml`, each explained in `config/group.example.toml`.

### Structure: `config/group.toml`

| Information | Goes to | Req. | Used by |
|---|---|---|---|
| Group name, reporting currency, timezone | `[company]` `name`, `reporting_currency`, `timezone` | R | every report and run stamp |
| Financial year end (`MM-DD`, default `12-31`) | `[company]` `financial_year_end` | O | report years, quarters, default look-back windows |
| Accounting inbox (Gmail) and processed label | `[company]` `accounting_inbox`, `processed_label` | R | invoice intake |
| Earliest date bank and accounts payable checks look back to | `[company]` `records_from` (default: start of the previous financial year) | O | bills report, bank feed, bank fees |
| Each entity: key, exact Xero name, short name, aliases, base currency, country | `[[entities]]` `key`, `xero_name`, `short`, `aliases`, `base_currency`, `country` | R (`key`, `xero_name`, `base_currency`) | every script |
| Where the entity's prose rules live | `[[entities]]` `rules_file` (default `rules/entities/<KEY>.md`) | R | bookkeeping, preflight |
| Each bank account: provider, slug, CSV file, CSV cut-over date, Xero bank account, currency | `[[entities.bank_accounts]]` `provider`, `slug`, `statement_csv`, `csv_until`, `xero_account_code`, `currency` | R | bank feed, bills report, bank fees, phantom check |
| Payroll control accounts and their legitimate leftover balance | `[entities.payroll_controls]` | O | payroll control check, phantom check exemptions |
| Each intercompany pair: entities, kind, both account codes, payments-enabled | `[[intercompany]]` `a`, `b`, `flavour`, `label`, `accounts`, `payments_enabled` | R if more than one entity | intercompany recon and workbook, FX revaluation |
| Accounts that look intercompany but are not | `[[non_group]]` | O | intercompany recon (reported, never matched) |
| FX gain or loss account, matching tolerances, narration patterns | `[intercompany_settings]` | O | intercompany recon, FX drafts |
| Tracking on an entity's FX revaluation lines | `[intercompany_settings.fx_tracking]` | O | FX revaluation journals |
| Report channel | `[slack]` `channel_id`, `channel_name` | R | every report |
| Admins, users, read-only users | `[slack.admins]`, `[slack.users]`, `[slack.readonly]` (member ID = display name) | R (one admin) | the Slack listener |
| Who to chase for a missing document | `[slack.chase_routing]` (cardholder, then category, then entity, then `default`) | O | chases |
| Nicknames, people never to message | `[slack.nicknames]` (display name = nickname), `never_message` | O | prompts, chases |
| Who gets the unpaid bills list, timing windows | `[accounts_payable]` `unpaid_notify`, `lead_days`, `leg_window_days`, `card_lag_days` | O | bills report |
| Words too common among your suppliers to identify one | `[accounts_payable]` `generic_supplier_words` | O | bills report name matching |
| Bills never settled through the bank | `[[accounts_payable.not_via_bank]]` | O | bills report |
| Prepayment, accrual and deposit accounts | `[reports.prepayments]`, `[reports.accruals]`, `[reports.deposits]` (`account_words` or `accounts`) | O | balance reports |
| Drive publish folder and subfolders | `[drive]` `publish_folder_id`, `[drive.subfolders]`, `[drive.routes]` | O | workbook publishing |
| Your bank's fee price list | `[bank_fees.<provider>]` | O | bank fees report |
| Accounts cleared by journal, not by the bank | `[safety]` `phantom_check_exempt_accounts` | O | phantom payment check |
| Bodies of work and the rules each loads | `[[domains]]` `key`, `title`, `skill`, `docs` | R (copy as shipped) | Slack prompts, outstanding register |

### Judgement: `rules/`

The order to fill these in, and what each file owns, is in
[rules/README.md](rules/README.md). Every section marked `<!-- FILL IN -->`
says what goes there and where the information usually comes from.

| Information | Goes to | Req. |
|---|---|---|
| What the group does, in two or three sentences | `rules/GROUP.md` § What the group is | R |
| What each entity does, and its revenue | `rules/GROUP.md` § What each entity does | R |
| Which entity recognises which cost (recognition matrix) | `rules/GROUP.md` § Which entity recognises which cost | R |
| Standing arrangements where one entity always pays for another | `rules/GROUP.md` § Costs paid by one entity for another | O |
| How cash moves inside the group | `rules/GROUP.md` § Who pays whom, and how the entities are funded | O |
| The period costs must be accurate to | `rules/GROUP.md` § Cost recognition principles | O |
| Which documents become bills; leases | `rules/GROUP.md` § Transaction-type rule, § Leases | O |
| Other tools that post into Xero | `rules/GROUP.md` § Upstream tools | O |
| One entity's role and what it recognises | `rules/entities/<KEY>.md` § Role, § What it recognises | R |
| Accounts only that entity has | `rules/entities/<KEY>.md` § Accounts | O |
| Tax registration and tax types (VAT, GST, sales tax) | `rules/entities/<KEY>.md` § Tax registration and tax types | O |
| Tracking categories (department, location, project) | `rules/entities/<KEY>.md` § Tracking categories | O |
| Bank feed quirks of that entity | `rules/entities/<KEY>.md` § Bank data | O |
| Leases, deposits, loans and recurring journals of that entity | `rules/entities/<KEY>.md` § Standing arrangements and balance sheet | O |
| Cost category to account code, per entity | `rules/EXPENSES.md` § Account table | R |
| Balance-sheet accounts the treatment rules use | `rules/EXPENSES.md` § Account table (balance-sheet table) | O |
| Capitalisation threshold, depreciation lives | `rules/EXPENSES.md` § 1. Capitalisation | O |
| Prepayment and accrual policy | `rules/EXPENSES.md` § 2. Prepayments, § 3. Accruals | O |
| Deposits, generic tax method, foreign currency, receipts, credit notes | `rules/EXPENSES.md` § 4 to § 9 | O |
| Whether a submitted document presumes the sender's entity | `rules/EXPENSES.md` § Intake presumption by sender | O |
| Office locations and which entity each belongs to | `rules/EXPENSES.md` § Office locations | O |
| One entry per supplier | `rules/SUPPLIERS.md` § Entries | O |
| What goes through each intercompany flavour | `rules/INTERCOMPANY.md` § 1. Flavours | R if more than one entity |
| Funding direction | `rules/INTERCOMPANY.md` § 2. Who funds whom | O |
| How each side records a cost paid for another entity | `rules/INTERCOMPANY.md` § 3 | O |
| Recharge policy and markup | `rules/INTERCOMPANY.md` § 4. Recharge policy | O |
| FX revaluation frequency and policy | `rules/INTERCOMPANY.md` § 5. FX revaluation policy | O |
| Period-end sweeps between flavours | `rules/INTERCOMPANY.md` § 6 | O |
| Allocation keys and their source data | `rules/ALLOCATION.md` § 1, § 2 | O |
| Each shared cost and its split | `rules/ALLOCATION.md` § 3. Allocation table | O |
| Who holds each shared contract, at cost or with a markup | `rules/ALLOCATION.md` § 4 | O |
| Journal pair or recharge invoice | `rules/ALLOCATION.md` § 5 | O |
| Who employs whom | `rules/PAYROLL.md` § Who employs whom | O |
| Each payroll source and where its documents are | `rules/PAYROLL.md` § Payroll sources, § Where the payroll documents come from | O |
| What flows through each control account | `rules/PAYROLL.md` § Control accounts | O |
| Employer-cost accounts | `rules/PAYROLL.md` § Employer taxes, pensions and benefits | O |
| Line-by-line mapping of each payroll source | `rules/PAYROLL.md` § Mapping tables, one per source | O |
| Contractor treatment; payroll currencies | `rules/PAYROLL.md` § Contractors versus employees, § FX handling | O |

### Access: `.env` and token folders

| Information | Goes to | Guide |
|---|---|---|
| Anthropic API key or Claude subscription token | `.env` `ANTHROPIC_API_KEY` or `CLAUDE_CODE_OAUTH_TOKEN` | [docs/setup/SERVER.md](docs/setup/SERVER.md) §5 |
| Xero app client ID and secret | `.env` `XERO_*`; token in `.xero/` | [docs/setup/XERO.md](docs/setup/XERO.md) |
| Slack bot and app tokens | `.env` `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN` | [docs/setup/SLACK.md](docs/setup/SLACK.md) |
| Gmail OAuth client | `.env` `GMAIL_*`; token in `.gmail/` | [docs/setup/GMAIL.md](docs/setup/GMAIL.md) |
| Drive OAuth client (optional) | `.env` `GDRIVE_*`; token in `.gdrive/` | [docs/setup/GOOGLE_DRIVE.md](docs/setup/GOOGLE_DRIVE.md) |
| Revolut keys, per bank slug | `.env` `REVOLUT_<SLUG>_*`; token in `.revolut/` | [docs/setup/REVOLUT.md](docs/setup/REVOLUT.md) |
| Mercury read-only token, per bank slug | `.env` `MERCURY_<SLUG>_KEY` | [docs/setup/MERCURY.md](docs/setup/MERCURY.md) |
| Server address and directory | `.env` `AGENT_SERVER`, `AGENT_SERVER_DIR` (laptop only) | [docs/setup/SERVER.md](docs/setup/SERVER.md) |

The deploy scripts source `.env` with bash, so put quotes around any value
that contains a space.

---

## Part 3. Situations to ask about

These need a rule of their own. Ask about each one. If the answer is yes,
write the rule into the file shown.

| Ask | If yes, write it in |
|---|---|
| Does one entity pay costs that belong to another, routinely? | `rules/GROUP.md` § Costs paid by one entity for another, `rules/INTERCOMPANY.md` § 3 |
| Are any contracts (software, office, insurance) shared between entities? | `rules/ALLOCATION.md` |
| Does any entity have a branch or a second tax registration abroad? | `rules/entities/<KEY>.md` § Tax registration and tax types |
| Do staff of one entity work for another, or get paid through an employer of record? | `rules/PAYROLL.md` § Who employs whom |
| Are there bank accounts with no API and no regular statement export? | `config` `provider = "csv"`, plus a person who drops the CSVs |
| Are invoices sent somewhere other than the Gmail inbox, such as Slack, a portal or post? | `rules/GROUP.md` § Upstream tools; Slack files are read automatically |
| Does another tool already post bills or receipts into Xero? | `rules/GROUP.md` § Upstream tools |
| Are there suppliers billed in a currency that is not the entity's own? | `rules/SUPPLIERS.md` entry, `rules/EXPENSES.md` § 6 |
| Are any costs recharged with a markup? | `rules/INTERCOMPANY.md` § 4, `rules/ALLOCATION.md` § 4 |
| Are there loans with shareholders or other parties outside the group? | `config` `[[non_group]]` |
| Are there leases, deposits or long prepayments? | `rules/GROUP.md` § Leases, `rules/EXPENSES.md` § 2 and § 4 |
| Is any entity dormant, or about to be opened or closed? | `rules/entities/<KEY>.md` § Role |
| Is anyone allowed to approve or pay bills whom the agent should never chase? | `config` `never_message` |
| Is there anything about your books that would surprise a new bookkeeper? | the file that owns the subject; ask if unsure |

---

## Part 4. The setup path, clone to first scheduled run

Follow it in this order. Each guide ends with a check command.

**On your laptop**

1. Clone into a **private** repository. Requirements: Python 3.11+,
   [Claude Code](https://docs.anthropic.com/en/docs/claude-code), git and
   `jq`. The payment guard refuses every command if `jq` is missing.
   ```sh
   python3 -m venv .venv && source .venv/bin/activate
   pip install -e .                      # editable install is required
   cp .env.example .env
   cp config/group.example.toml config/group.toml
   ```
2. Gather the information in [INPUTS.md](INPUTS.md).
3. Fill in `config/group.toml` (Part 2, structure). Check it with
   `python -m accounting_agent.config`.
4. Connect Xero ([docs/setup/XERO.md](docs/setup/XERO.md)), then run
   `python scripts/smoke_test.py`.
5. Fill in `rules/` (Part 2, judgement). Replace or delete the three example
   entity files: each carries an example marker, and preflight fails while
   one remains.
6. **Optional local trial.** Run `AGENT_READONLY=1 claude` in the repo and ask
   a question about your books. Nothing can be written in this mode.
7. Create the Slack app ([docs/setup/SLACK.md](docs/setup/SLACK.md)).
8. Connect the Gmail inbox ([docs/setup/GMAIL.md](docs/setup/GMAIL.md)).
   This is required.
9. Optional: connect Google Drive
   ([docs/setup/GOOGLE_DRIVE.md](docs/setup/GOOGLE_DRIVE.md)), Revolut
   ([docs/setup/REVOLUT.md](docs/setup/REVOLUT.md)) and Mercury
   ([docs/setup/MERCURY.md](docs/setup/MERCURY.md)).

**On the server** ([docs/setup/SERVER.md](docs/setup/SERVER.md))

10. Provision a small Linux server and get the repo onto it.
11. Run `deploy/bootstrap.sh`, then `deploy/install.sh`.
12. Copy `config/group.toml`, `.env` and the token folders across with
    `scp` or `rsync`. Then **delete the laptop's Xero and Revolut tokens**:
    both rotate on use, and only one machine may hold them.
13. Run `deploy/preflight.sh` until it ends with `preflight clean`.
14. Do the supervised first runs:
    - From Slack, ask `query what bills are open in <KEY>?`.
    - Then `run bookkeep the invoices in the inbox from this week`. Read the
      report, answer its queries, and watch the rulings land in `rules/`.
    - Then `run reconcile intercompany` and `run give me the bills report`.
15. Once a few runs read cleanly, turn on the schedule and the Slack
    listener with `deploy/install.sh --enable`.

### The minimum to a first run

These are enough for supervised bookkeeping:

- `[company]`.
- `[[entities]]` with `key`, `xero_name` and `base_currency`.
- One bank account per entity.
- `[slack]` with a channel and one admin.
- `[[domains]]` as shipped.
- Xero, Slack, Gmail and an Anthropic credential.
- `rules/GROUP.md`, `rules/EXPENSES.md` § Account table, and one file per
  entity.

Everything else can wait. Whatever the agent cannot decide becomes a Slack
query, and each admin answer is written back into the right `rules/` file, so
the rules grow from use.

---

## What updates automatically from config

| You change | Automatically updated |
|---|---|
| Add an entity | bank feed and bill feed files, bills report rows, balance reports, intercompany matrix rows and columns, smoke test, review scope, Slack run prompts, phantom check |
| Add a bank account | bank feed refresh, bills report bank search, bank fees report, preflight connector checks |
| Add an intercompany pair | intercompany recon, a new summary cell and line-by-line tab in the workbook, FX revaluation candidates |
| Fill in a missing mirror account | the pair moves from "structural gap" to matched |
| Add a payroll control account | payroll control check, phantom payment exemptions |
| Add a Slack member | listener permissions, on restart |
| Add a domain | Slack prompts, outstanding register sections |

## Adding an entity later

1. Connect it in Xero: re-run `python -m accounting_agent.xero_auth` and tick
   the new organisation.
2. Add an `[[entities]]` block, its bank accounts and its payroll controls.
3. Add an `[[intercompany]]` block for every account it shares with another
   entity.
4. Copy `rules/entities/_TEMPLATE.md` to `rules/entities/<KEY>.md` and fill it
   in. Add its column to the recognition matrix in `rules/GROUP.md`.
5. Run `python -m accounting_agent.config`, then `scripts/smoke_test.py`, then
   `deploy/preflight.sh` on the server.

## Changing bookkeeping rules later

There are two ways:

- Edit the file in `rules/` and commit it.
- As an admin, tell the agent in Slack, for example: "from now on, software
  subscriptions for the EU team are recognised in OPCO_EU". The run writes
  the ruling into the file that owns the subject and commits it on the
  server. `deploy/sync-all.sh` then brings it back to the laptop and your
  private remote.

## Adding a new kind of work

**A new domain**, for example a monthly management pack, needs:

- a new skill under `.claude/skills/`;
- its rules, in the `rules/` file that owns the subject;
- a `[[domains]]` block in config;
- if it runs unattended, a job in `deploy/run-scheduled.sh` and a line in
  `deploy/crontab.example`.

**A new standing report** needs a new `report-*` skill, a runner and a line in
the `reports` catalogue. Most balance accounts fit
`scripts/balance_report.py` as the runner.
