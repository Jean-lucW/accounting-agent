# Accounting Agent

An AI bookkeeper for a group of companies on Xero. Headless
[Claude Code](https://docs.anthropic.com/en/docs/claude-code) sessions run
on a small Linux server. Slack messages and a cron schedule start them, and
they do the day-to-day bookkeeping across every Xero organisation in the
group:

- **Bookkeeping.** Supplier invoices arriving in a Gmail inbox or sent to a
  Slack app are read, assigned to the right entity and account from your
  rules, and posted to Xero as AUTHORISED bills with the document attached.
- **Bank-led reconciliation.** A reconstructed bank feed per entity, from
  read-only bank APIs (Revolut Business, Mercury) or statement CSVs, so every
  cash movement ends up as a bill, spend money or transfer in Xero.
- **Accounts payable.** Every open bill is checked against the bank: unpaid,
  paid by its own entity, paid by another entity, or a duplicate. The result
  is an Excel workbook, and the unpaid list goes to whoever pays.
- **Intercompany reconciliation.** Every intercompany account pair is matched
  line by line, with one workbook tab per pair and draft FX revaluations.
- **Standing reports.** Prepayments, accruals and deposits are rebuilt from
  source documents and tied to the Balance Sheet.

It never creates a payment. A person always matches the bank line to the bill
in Xero.

> **Start with [INPUTS.md](INPUTS.md), then [ADAPTING.md](ADAPTING.md).**
> INPUTS.md is the comprehensive list of every piece of information you need
> to provide: required or optional, and where each piece goes. ADAPTING.md
> maps each piece to its file and section, and gives the one setup path from
> clone to first scheduled run.

> **Run it from a private repository.** Once filled in, `config/group.toml`
> and `rules/` describe your group's books, and the server commits admin
> rulings into `rules/`. Treat this public repository as an upstream template:
> clone it into a private repository and never push a filled-in copy anywhere
> public.

![How data gets in, gets processed, and gets out](docs/images/architecture-overview.png)

## How it works

1. **Trigger.** An admin writes `run ...` in Slack, or cron fires a job. The
   Slack listener (`scripts/slack_agent.py`) checks a three-tier whitelist
   (admin, user, read-only) and ignores everyone else. One agent per Slack
   thread.
2. **Start a session.** A runner in `deploy/` takes a lock, checks the Xero
   rate limit and starts `claude -p` for **one domain** (bookkeeping, or a
   standing report), so a decision is never made on the wrong rules.
3. **Read the group.** The session reads `CLAUDE.md` (the rulebook), the
   domain's skill in `.claude/skills/`, your group's **structure** from
   `config/group.toml` (entities, banks, intercompany pairs, Slack people)
   and your **judgement** from `rules/` (which entity bears which cost,
   coding, splits, suppliers, payroll).
4. **Gather.** Connectors in `src/accounting_agent/` read Xero, Gmail,
   Drive and the banks; `scripts/` builds the bank feed (statement lines not
   yet in Xero) and the bill feed (open bills). An `invoice-extract`
   subagent transcribes documents in parallel and decides nothing.
5. **Decide and post.** The session picks entity and account from `rules/`,
   checks Xero for a duplicate, and posts an AUTHORISED bill with the document
   attached. If the rules do not answer, it asks an admin in Slack.
6. **Close the loop.** Open bills are compared with the bank lines; a bill
   paid by another entity gets the intercompany entry and a person reconciles
   it. Every question or manual task goes on an outstanding-items register.
7. **Report.** One message per run in the Slack channel, the report in its
   thread; workbooks to `data/reports/` and optionally Google Drive.
8. **Learn.** An admin's answer ("from now on, X is recognised in OPCO_EU")
   is written into the `rules/` file that owns the subject.

Schedule (UTC, `deploy/crontab.example`), chained so two sessions are never
in Xero at once: Mon to Sat 02:00 bookkeeping, intercompany recon and
accounts payable check, then the bills and balance reports; Wednesday and
Sunday a full review; nightly a phantom-payment check.

## Where Claude is used

The model is only reached through the Claude Code CLI (`claude -p`). Five
runners in `deploy/` start sessions:

| Runner | Used for |
|---|---|
| `run-agent.sh` | scheduled jobs |
| `run-thread.sh` | an admin's Slack thread |
| `run-query.sh` | read-only questions |
| `run-consensus.sh` | two independent passes plus a reconciling pass, for judgement-heavy work |
| `notify-queried.sh` | the run summary sent to whoever started a run |

The server needs an `ANTHROPIC_API_KEY` (or `CLAUDE_CODE_OAUTH_TOKEN`).
Everything else (listener, connectors, feeds, reports, reconciliation,
safety checks) is plain Python with no model calls and runs without a key.

## What you provide

| Kind | File | What |
|---|---|---|
| Structure | `config/group.toml` | entities, Xero names, currencies (any ISO code), bank accounts, intercompany pairs, control accounts, Slack people |
| Judgement | `rules/*.md` | recognition matrix, account coding, cost allocation, supplier database, payroll, intercompany routing |
| Access | `.env` and token folders | Anthropic, Xero, Slack, Gmail, Drive, Revolut and Mercury credentials |

Nothing in the code names an entity, account or person. **[INPUTS.md](INPUTS.md)
is the comprehensive list of everything to provide**, with the minimum for a
first run. [ADAPTING.md](ADAPTING.md) maps each item to its file and section.

## Integrations

| Service | Required | Notes | Setup |
|---|---|---|---|
| Xero | yes | every organisation in the group, one OAuth app | [docs/setup/XERO.md](docs/setup/XERO.md) |
| Slack | yes | Socket Mode app: commands, reports, invoice files | [docs/setup/SLACK.md](docs/setup/SLACK.md) |
| Gmail | yes | the accounting inbox. **Built for Gmail and Google Workspace only for now**; other mail providers are not supported yet | [docs/setup/GMAIL.md](docs/setup/GMAIL.md) |
| Anthropic | yes | API key or Claude subscription token, for `claude -p` | [docs/setup/SERVER.md](docs/setup/SERVER.md) |
| Google Drive | no | publish workbooks, read payroll documents | [docs/setup/GOOGLE_DRIVE.md](docs/setup/GOOGLE_DRIVE.md) |
| Revolut Business | no | read-only bank feed | [docs/setup/REVOLUT.md](docs/setup/REVOLUT.md) |
| Mercury | no | read-only bank feed | [docs/setup/MERCURY.md](docs/setup/MERCURY.md) |
| Any other bank | no | statement CSVs dropped in `data/statements/` | [docs/BANKING.md](docs/BANKING.md) |

## Requirements and quick start

- **Laptop:** Python 3.11+, Claude Code, git and `jq`. The payment guard hooks
  refuse every command when `jq` is missing.
- **Server:** Ubuntu 22.04 or 24.04 with 2 GiB of memory. Outbound HTTPS
  only; Slack uses Socket Mode, so no inbound port is needed.

```sh
python3 -m venv .venv && source .venv/bin/activate
pip install -e .                                  # editable install is required
cp .env.example .env                              # credentials (docs/setup/)
cp config/group.example.toml config/group.toml    # your group (ADAPTING.md)
python -m accounting_agent.config                 # what the agent sees
python -m accounting_agent.xero_auth              # connect Xero
python scripts/smoke_test.py                      # read-only check of every entity
```

Then follow [ADAPTING.md](ADAPTING.md) Part 4 to fill in `rules/`, connect
the other services and move to the server. Tests run without credentials:
`python scripts/test_config.py`, `scripts/test_readonly_guard.py` and
`scripts/test_slack_agent.py`.

## Repository layout

```
INPUTS.md                    START HERE: every piece of information you provide
ADAPTING.md                  where each piece goes, and the setup path
ARCHITECTURE.md              diagrams of every component and data flow
CLAUDE.md                    the rulebook every agent session reads
config/group.example.toml    template for YOUR structure (copy to group.toml, gitignored)
rules/                       YOUR judgement; rules/README.md explains each file
.claude/skills/              one workflow per domain, plus support skills
.claude/hooks/, settings.json  safety hooks: payment ban, read-only lock
.claude/agents/              invoice-extract: one document to JSON
src/accounting_agent/        connectors: Xero, Gmail, Drive, Revolut, Mercury; config loader
scripts/                     runners the skills call: feeds, reports, interco, register, tests
docs/                        method (comms, banking, review, intercompany); Xero, Revolut, Mercury API references
docs/setup/                  one guide per integration, plus the server
docs/bookkept/               runtime registers, created on the server, never committed
deploy/                      session runners, schedule, systemd, install, preflight, sync
data/                        runtime state on the server (gitignored)
```

## Safety

These rules are enforced in code, not only in prompts:

- **No payments.** Hooks deny payment calls, the Xero library has no payment
  writers, and every run ends with `scripts/check_no_phantom_payments.py`.
- **Read-only sessions.** `AGENT_READONLY=1` makes the Xero client and a hook
  refuse every write.
- **Bank APIs are GET-only**: the API that reads the feed also moves money.
- **Duplicate guard** on supplier, number and amount before every create.
- **Slack cannot lift a safety rule**; only an edit to the repository can.

Never commit `.env`, `config/group.toml`, `.xero/`, `.gmail/`, `.gdrive/`,
`.revolut/` or `data/`. All of them are in `.gitignore`.
