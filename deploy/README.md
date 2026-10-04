# Running the accounting agent on a server

The operations manual for this repo once it is deployed. Provisioning a
server from nothing is in `docs/setup/SERVER.md`; this file is what you need
day to day after that.

## What is different about this project

Most things run by cron are Python scripts. This repo is two things:

1. **A Python library and some scripts** (`src/`, `scripts/`). Ordinary,
   cron-able: the bank feed, the bill feed, the bills payable report, the
   balance reports, the bank and FX fees.
2. **A set of Claude Code skills** (`.claude/skills/`). The bookkeeping run,
   the intercompany reconciliation and the review are not scripts at all. They
   only happen when a Claude session reads a skill and does the work.
   Automating those means running Claude Code headless, which is what
   `deploy/run-agent.sh` does.

## The Xero token is single-homed. This matters more than anything else here.

Xero rotates the refresh token on **every** use. The moment the server
refreshes, any other copy of `.xero/tokens.json` is dead, and vice versa.

**The server is the only machine that touches Xero.** Once it is set up,
nothing runs against Xero from a laptop. If you ever need to go back to
running locally, stop the cron jobs and the listener first, then re-run the
OAuth bootstrap there rather than copying the file around. After the first
full run on the server is confirmed working, delete the laptop's
`.xero/tokens.json` so nothing local can silently steal the refresh token.

Gmail and Google Drive are not affected. Google does not rotate its refresh
token, so the same `.gmail/tokens.json` and `.gdrive/tokens.json` work in two
places. Both are bootstrapped on the laptop (a browser is needed) and copied to
the server:

```bash
.venv/bin/python -m accounting_agent.gdrive_auth   # approve as a member of the shared drive
scp .gdrive/tokens.json "$AGENT_SERVER:$AGENT_SERVER_DIR/.gdrive/"
```

Drive access is `drive.readonly` plus `drive.file`. `drive.file` lets the
agent create files and update the files it created, nothing else: that is how
workbooks are published into the Drive publish folder (`[drive]
publish_folder_id` in `config/group.toml`). The first publish cannot
overwrite a copy that was uploaded by hand (the token may only touch files it
created): it creates its own alongside. The Drive API must be enabled on the
same Google Cloud project as the Gmail OAuth client. A token consented with
`drive.readonly` alone cannot publish: re-run the bootstrap on the laptop and
copy the file to the server once (`docs/setup/GOOGLE_DRIVE.md`).

The Revolut tokens in `.revolut/` (and the signing key, if it is a PEM file
there) are bootstrapped once (`docs/setup/REVOLUT.md`) and copied to the
server. Treat the server as their home too, so a bank feed is only ever
refreshed from one place.

## First-time setup

```bash
ssh "$AGENT_SERVER"
cd "$AGENT_SERVER_DIR"
./deploy/bootstrap.sh          # jq/flock/git/python 3.11+ check; installs the claude CLI (via node) only if missing
./deploy/install.sh            # venv, requirements, exec bits, secret check, systemd unit
./deploy/preflight.sh          # read-only checks; must be clean before scheduling
./deploy/install.sh --enable   # installs the crontab block, starts the Slack listener
```

`preflight.sh` verifies that `config/group.toml` loads, the Python deps, the
Xero token (`scripts/smoke_test.py`, read-only), the Gmail token, the Drive
token when a publish folder is configured, the Slack tokens and channel, every
Revolut and Mercury slug in the config (`scripts/revolut_check.py`,
`scripts/mercury_check.py`), the CLI and its credential, and that the payment
guard and the read-only guard actually fire. Re-run it after any Claude Code
upgrade and after any change to `config/group.toml`.

## Keeping laptop, git remote and server equal: `deploy/sync-all.sh`

```bash
./deploy/sync-all.sh --dry-run   # what each copy has that the others lack
./deploy/sync-all.sh             # collect, merge, push, deliver, verify
```

Run from the laptop, the one machine that reaches both. In order: commit
anything uncommitted in the server's working tree (a stale copy of a laptop
commit is restored instead), merge `server/main` and `origin/main` into
`main`, push origin, push `main` to the server as `refs/remotes/laptop/main`
and `git merge --ff-only` it there, then print the three HEADs, which must be
one hash. It runs `install.sh` on the server if dependencies changed and
restarts the listener if `scripts/slack_agent.py` changed (`KillMode=process`,
runs are not touched). It refuses on a dirty laptop tree, an unreachable host,
a run in flight, a merge conflict, or a server-side deletion of a file the
laptop still has. It never force-pushes or resets. `.claude/skills/sync-all`
is the same procedure as a skill, with every refusal and what to do about it.

`deploy/sync-back.sh` is the collect-and-push half only, kept for when that
is all you want.

### Why not rsync

Copying the tree over with `rsync --delete` looks simpler and loses work:

- It cannot see a write-back the server committed, and `--delete` removes it.
  Anything a run edits in place (a new rule in `rules/`, and the gitignored
  registers in `docs/bookkept/`) is overwritten or deleted.
- It leaves the server's git index disagreeing with its working tree.
- `rsync -a` propagates the laptop's file modes. A script committed 644
  arrives without its exec bit and cron fails. Keep `deploy/*.sh` committed
  755.
- Its exclude list drifts from `.gitignore`, and then `data/`, `.env` or a
  token directory gets overwritten. `.env` in particular: a laptop `.env` with
  an empty `ANTHROPIC_API_KEY` copied over the server's breaks every
  unattended run with no error until the next cron fire.

## Two agents and a consensus

```bash
./deploy/run-consensus.sh <job> "<task>"
```

Three `claude -p` passes against one evidence pack in `data/consensus/`:
gather (read-only), a second independent reading that consults the pack
before re-reading any source (read-only), then a reconciliation that actions
the agreed items, resolves what the evidence settles, and escalates the rest
by DM. Only the third pass may write. About 2.5x a single run. Worth it for a
batch with judgement in it, not for routine intake.

## Running a job by hand

```bash
./deploy/run-scheduled.sh daily          # any scheduled job, exactly as cron runs it
./deploy/run-agent.sh <job-name> "<prompt>"
./deploy/run-agent.sh bookkeep "Run the daily invoice pull"
```

`run-agent.sh` is the single place that decides locking, logging, env loading
and permission mode. Logs land in `data/logs/<job>-<date>.log`; what cron
itself printed is in `data/logs/cron.log`.

## Scheduling

`deploy/crontab.example` has the lines, with `__HOME__` substituted by
`install.sh --enable`, which keeps them between `# BEGIN accounting-agent <repo path>` and
`# END accounting-agent` markers and leaves any other crontab lines alone.
Times are UTC.

| When (UTC) | Job | What |
|---|---|---|
| Mon-Sat 02:00 | `daily` | a chain of three sessions: the bookkeeping run (intake from the Slack app and the accounting inbox, bank feed, bill-payments check), the intercompany reconciliation (read-only, plus the matrix workbook to Drive), the accounts payable check |
| Mon-Fri 02:00 | `bills` | waits for `daily`; `scripts/bills_report.py`, no Claude: the bills payable workbook to Drive, the unpaid list DM'd to `[accounts_payable] unpaid_notify` |
| Mon-Sat 02:00 | `balance-reports` | waits for `daily`; prepayments, accruals and deposits reports, bank and FX fees refresh, then `scripts/balance_reports_summary.py` posts one summary to the channel. No Claude |
| Wed + Sun 10:00 | `review` | full bookkeeping review of every entity against `docs/REVIEW_METHOD.md` and `rules/`, intercompany reconciliation as a standard check |
| (optional) | `outstanding` | the outstanding-items list posted to the channel; off by default, an admin asks for it in Slack |
| every 10 min | retry sweep | re-runs a job deferred on the Xero daily rate limit |
| 23:00 | phantom check | `scripts/check_no_phantom_payments.py` to `data/logs/phantom-check.log` |
| Sun 04:30 | log trim | deletes `data/logs/*.log` older than 30 days |

`bills` and `balance-reports` fire the same minute as `daily` and wait on
`data/daily-chain.lock` (up to six hours), so they read Xero after the day's
bills are posted and never alongside a session. Each job takes a per-job
`flock`, so a slow run never gets double-fired into posting the same bill
twice.

Every scheduled run reports into the channel in `config/group.toml`
`[slack]`: the headline at top level, the report in its thread, and anything
left for a person in a separate MANUAL ITEMS thread. The prompts are in
`deploy/run-scheduled.sh`; the entity list, channel, inbox and timezone in
them are read from `config/group.toml` at run time, so a new entity is
picked up on the next fire with no change here.

### The Xero rate limit

Xero allows 5000 calls a day per tenant. `run-agent.sh` probes it with
`scripts/xero_rate_limit.py` before starting a session (and again after one
that failed or mentioned a 429). A capped job does not start: it writes
`data/retry/<job>.retry` with the reset time and its prompt, and
`deploy/retry-sweep.sh` re-runs it the moment the cap resets.
`deploy/retry-sweep.sh --list` shows what is waiting.

## Standing reports

Read-only against Xero, plain Python, built on the server because the server
is the only machine that calls Xero:

```bash
.venv/bin/python scripts/prepayments_report.py refresh all
.venv/bin/python scripts/accruals_report.py refresh all
.venv/bin/python scripts/deposits_report.py refresh all
.venv/bin/python scripts/bank_fees.py refresh
.venv/bin/python scripts/bills_report.py refresh all
```

Each keeps ONE copy of its workbook at `data/reports/`, overwritten in place,
alongside a JSON store that makes the next refresh incremental, and publishes
the copy to the Drive publish folder (subfolders per `[drive.subfolders]`).
`--full` rebuilds from scratch and is for a difference against the Balance
Sheet, an edited old journal, or a lost store, never for routine work. Exit
code 1 from a balance report means a month does not agree with the Balance
Sheet. With no publish folder configured, the workbooks stay on the server
and an admin asks for them in Slack.

## The Slack listener

`scripts/slack_agent.py` runs as the systemd service
`accounting-agent-slack` (`deploy/accounting-agent-slack.service`), dialling
out over Socket Mode, so the server needs no inbound port beyond ssh.

```bash
sudo systemctl status accounting-agent-slack
journalctl -u accounting-agent-slack -f
sudo systemctl restart accounting-agent-slack
```

The unit uses `KillMode=process`: restarting the listener stops only the
listener, never the runs it has spawned. systemd's default
(`KillMode=control-group`) kills every process in the cgroup, and `setsid`
does not escape a cgroup, so the default kills a bookkeeping run mid-flight
on a restart.

Who may do what is the `[slack]` table in `config/group.toml`: admins (run,
status, notes, query; their rulings are written back into `rules/`), users
(their messages are captured as input a run weighs; no commands) and
read-only members (users plus `query <question>`, answered by a read-only
session). Anyone not listed is ignored with a log line and no reply. Each
top-level `run` message is its own agent with its own lock, log and Claude
session; follow-ups in the thread are further turns of that session. The
caps are `SLACK_AGENT_MAX_RUNS`, `SLACK_AGENT_MAX_QUERIES` and
`SLACK_AGENT_MAX_TURNS` in `.env`. The full setup is in `docs/setup/SLACK.md`.

A second bookkeeping run while one is in flight is refused: two sessions
racing for the same invoices would post duplicates, and the Xero duplicate
guard is not a substitute for not racing. Focused runs never block each other.

## Permissions, and why the hook is the real safety net

Unattended runs use `--permission-mode bypassPermissions`, because cron has no
human to answer a prompt. So the permission system is **not** protecting the
ledgers. The `PreToolUse` hooks in `.claude/settings.json` are, and they still
fire in that mode. `run-agent.sh` also runs `check_no_phantom_payments.py`
after every session as an independent check that does not trust what the
session reported, and cron runs it again nightly to catch anything done by
hand in Xero.

If preflight check 7 ever fails, unattended runs must stop until it is fixed.

Query sessions run with `AGENT_READONLY=1`, which is three independent locks:
the transport layer (`src/accounting_agent/readonly.py`: the Xero client
refuses any non-GET), the CLI's disallowed tools, and the PreToolUse hook
`.claude/hooks/guard-readonly.py`, which is inert unless the flag is set.
Preflight check 8 proves the first and the third.

### Gotcha: `/bin/sh` is dash on Debian and Ubuntu

A hook invoked as `sh .../guard-payments.sh` works on macOS, where `/bin/sh`
is bash. On Debian and Ubuntu `/bin/sh` is **dash**, which rejects `set -o pipefail`, so
the guard dies on its first line and denies nothing. The hooks are invoked
with `bash` (or `python3`), and with `|| exit 2` so that a guard that cannot
run **blocks** the tool call instead of silently allowing it. Hook paths fall
back to `$PWD`, never to an absolute path from someone's laptop.

### A credential the server must never have

Never put a write credential on the server that the guard hooks do not know
about. The payment guard only matches Xero payment calls; a database DSN with
write rights, an API key with write scope to some other system, is something
nothing stops an unattended session from using. If the server ever needs such
a write, the order of work is: add a `PreToolUse` matcher that denies the
write call the way `guard-payments.sh` denies payment calls, add a preflight
check that proves it fires, *then* add the credential. Not the other way
round. The bank APIs follow the same rule: Revolut is configured with READ
scope only and Mercury with a read-only token (`scripts/mercury_check.py`
proves Mercury itself refuses a write).

## When SSH hangs: the port-22 allowlist

If the server's firewall or cloud security group allows port 22 from fixed
addresses only (recommended), a changed public IP on your side locks you out
with a plain timeout and no error worth reading. Diagnose before assuming the
server is broken:

```bash
curl -s "${AGENT_IP_CHECK_URL:-https://api.ipify.org}"   # is your IP on the allowlist?
```

Then check, in your cloud provider's console or CLI, that the server is
running and that its firewall or security group allows port 22 from that
address (on AWS, for example, the instance state and the security group's
inbound rules; other providers have the equivalent firewall view). Add the
new address, or connect from a machine that is on the list. Most providers
also offer a browser or serial console that does not depend on SSH, as a
fallback when you cannot reach the server at all.

**Losing SSH does not touch a running job.** Anything launched with `setsid`
keeps its own session ID and survives the disconnect, and outbound calls
(Xero, Slack, Gmail) are unaffected by an inbound firewall problem. Launch
long runs detached and let them report by Slack:

```bash
setsid nohup ./deploy/run-agent.sh bookkeep "<prompt>" \
  > /dev/null 2>&1 < /dev/null &
```

Verify detachment with `ps -eo pid,sid,cmd`: the job's SID must be its own PID,
not an `sshd` session.

## When the git host times out

If your git host is IP-allowlisted, a timeout on port 22 means *this*
address is not on the list, not that SSH is unavailable. It can fail from one
network and succeed minutes later from another with no configuration change.
Check the address before concluding anything:

```bash
curl -s "${AGENT_IP_CHECK_URL:-https://api.ipify.org}"
ssh -T git@<your-git-host>      # a welcome or "successfully authenticated" line = fine
```

Do **not** "fix" it by switching `origin` to HTTPS. That trades a transient
network condition for a permanent credential problem.

If the server cannot reach your git host (an allowlist, or no deploy key),
the laptop is the bridge: `deploy/sync-all.sh`
collects from the server and delivers to it over the `server` git remote, and
the server's own `origin` is only a label. Never give the server a push
credential to work round it.

## Claude authentication

There is no keychain and no interactive login on the server, so the
credential has to be in the environment. `run-agent.sh` accepts either
`ANTHROPIC_API_KEY` or `CLAUDE_CODE_OAUTH_TOKEN` and aborts with a clear log
line if neither is set.

An API key is the usual choice: scheduled runs are billed as API usage and
stay off any Claude subscription quota, so a long bookkeeping run cannot eat
into interactive capacity.

Watch for an empty `ANTHROPIC_API_KEY=` placeholder in `.env`. Claude Code on
a laptop authenticates from the OS keychain and ignores that variable, so it
is never noticed locally. Headless does not, which is why a first smoke test
on a new server typically aborts. Preflight check 6 catches it.

The model is `opus` by default; set `AGENT_MODEL` in `.env` to change it for
every unattended run.

## Logs

| Where | What |
|---|---|
| `data/logs/<job>-<date>.log` | one per job per day: the prompt, the session's output, rate-limit decisions, the write-back commit, the phantom check |
| `data/logs/cron.log` | what cron and the plain-Python jobs printed |
| `data/logs/retry-sweep.log` | deferred jobs and their re-runs |
| `data/logs/phantom-check.log` | the nightly standalone phantom-payment check |
| `journalctl -u accounting-agent-slack` | the Slack listener |

Logs older than 30 days are deleted every Sunday.
