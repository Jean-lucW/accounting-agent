# Slack setup

The agent is driven from Slack. A small Python listener
(`scripts/slack_agent.py`) holds a Socket Mode connection to your workspace,
reads direct messages sent to the bot, and starts a Claude session only when
someone with the right to do so types `run` or `query`. Everything else
(capturing notes, `status`, `notes`, `clear`) is handled by the listener
itself and costs nothing.

Socket Mode means the server dials out to Slack. No inbound port, no public
URL, no webhook to secure.

This guide covers:

1. [Create the Slack app](#1-create-the-slack-app) (from a manifest, or by hand)
2. [Install it and put the tokens in `.env`](#2-install-the-app-and-store-the-tokens)
3. [Create the reporting channel](#3-create-the-reporting-channel)
4. [Fill in the three tiers](#4-fill-in-the-three-tiers)
5. [The permission model](#5-the-permission-model)
6. [Commands](#6-commands)
7. [Run the listener](#7-run-the-listener) (locally, then as a systemd service)
8. [Verify](#8-verify)

---

## 1. Create the Slack app

You need to be allowed to create apps in the workspace (a workspace admin
can approve it otherwise).

Go to <https://api.slack.com/apps> and click **Create New App**.

### Option A: from a manifest (recommended)

Choose **From an app manifest**, pick the workspace, select the **YAML** tab,
paste the manifest below, then **Next** and **Create**.

```yaml
display_information:
  name: Accounting Agent
  description: Bookkeeping, reports and read-only answers from the books.
  background_color: "#2c2d30"
features:
  app_home:
    home_tab_enabled: false
    messages_tab_enabled: true
    messages_tab_read_only_enabled: false
  bot_user:
    display_name: accounting-agent
    always_online: true
oauth_config:
  scopes:
    bot:
      - chat:write          # every reply, report, question and chase (chat.postMessage)
      - reactions:write     # the tick / eyes acks, and the intake "done" marker (reactions.add)
      - im:history          # receive DMs (message.im) and read DM threads (conversations.replies, conversations.history)
      - im:read             # the intake sweep lists the bot's DMs (conversations.list types=im)
      - users:read          # the intake sweep names who sent an invoice (users.info)
      - files:read          # download invoices people DM to the bot (url_private_download)
      - files:write         # send a workbook or extract back into a thread (files.getUploadURLExternal + files.completeUploadExternal)
      - channels:history    # the outstanding-items list re-reads the reporting channel (public channel)
      - groups:history      # the same, if the reporting channel is private
settings:
  event_subscriptions:
    bot_events:
      - message.im
  interactivity:
    is_enabled: false
  org_deploy_enabled: false
  socket_mode_enabled: true
  token_rotation_enabled: false
```

Keep only one of `channels:history` / `groups:history` if you like: the first
for a public reporting channel, the second for a private one.

### Option B: by hand

Choose **From scratch**, name the app, pick the workspace, then:

1. **Socket Mode** (left menu): turn **Enable Socket Mode** on. When asked,
   create an app-level token named e.g. `socket` with the scope
   `connections:write`. Copy the `xapp-...` token; it is `SLACK_APP_TOKEN`.
2. **OAuth & Permissions**, **Bot Token Scopes**: add exactly the scopes in
   the manifest above (`chat:write`, `reactions:write`, `im:history`,
   `im:read`, `users:read`, `files:read`, `files:write`, `channels:history`
   and/or `groups:history`).
3. **Event Subscriptions**: turn **Enable Events** on (no request URL is
   needed in Socket Mode). Under **Subscribe to bot events** add
   `message.im`.
4. **App Home**: under **Show Tabs**, enable the **Messages Tab** and tick
   **Allow users to send Slash commands and messages from the messages tab**.
   Without it nobody can DM the bot.

### Why these scopes and no others

Each scope maps to a call the code or a skill actually makes:

| Scope | Used by | Call |
|---|---|---|
| `chat:write` | listener, runners, every session | `chat.postMessage` (thread replies, channel reports, DMs to a bare member ID) |
| `reactions:write` | listener; bookkeeping intake | `reactions.add` (tick on a captured note, eyes on a queued follow-up, the intake's processed marker) |
| `im:history` | listener; bookkeeping intake | the `message.im` event, `conversations.replies` for the thread digest, `conversations.history` on DMs |
| `im:read` | bookkeeping intake | `conversations.list?types=im` for the 7-day completeness sweep |
| `users:read` | bookkeeping intake | `users.info` for the sender of an invoice |
| `files:read` | bookkeeping intake | downloading `url_private_download` for an invoice someone DMed |
| `files:write` | `scripts/slack_query.py upload` | `files.getUploadURLExternal`, `files.completeUploadExternal` |
| `channels:history` / `groups:history` | outstanding-items skill | `conversations.history` on the reporting channel |

Deliberately left out:

- `im:write`: nothing calls `conversations.open`. Posting with
  `chat.postMessage` to a bare member ID (`U...`) lands in the app's DM with
  that person without it. The skills and prompts say so, so a session never
  burns a turn on `missing_scope`.
- `app_mentions:read`, `message.channels`, `message.groups`: the listener
  acts only on direct messages (`channel_type == "im"`). Messages in the
  reporting channel are never commands.
- `chat:write.public`: the bot is invited to the reporting channel instead,
  which also makes `channel_not_found` a clear signal that it is not.
- `users:read.email`, `channels:read` and the rest: unused.

The app-level token needs only `connections:write` (it opens the Socket Mode
WebSocket).

## 2. Install the app and store the tokens

1. **OAuth & Permissions** (or **Install App**): click **Install to
   Workspace** and approve. Copy the **Bot User OAuth Token** (`xoxb-...`).
2. **Basic Information**, **App-Level Tokens**: if you used the manifest,
   click **Generate Token and Scopes**, name it `socket`, add
   `connections:write`, generate, and copy the `xapp-...` token.
3. Put both in `.env` at the repo root (never in `config/group.toml`, which
   holds no secrets):

   ```bash
   SLACK_BOT_TOKEN=xoxb-...
   SLACK_APP_TOKEN=xapp-...
   ```

Every time you add a scope later, Slack asks you to **reinstall** the app;
the bot token usually stays the same, but check and update `.env` if it
changed.

## 3. Create the reporting channel

Runs report into one channel: bookkeeping run reports, manual items (the
things someone has to do by hand in Xero) and the outstanding-items list.
Nothing else is ever posted there.

1. Create a channel, e.g. `#accounting-agent` (public or private).
2. In the channel, type `/invite @accounting-agent` (the bot's display name).
3. Copy its channel ID: open the channel, click its name, and the ID
   (`C...`) is at the bottom of the **About** tab.
4. Put it in `config/group.toml`:

   ```toml
   [slack]
   channel_id = "CEXAMPLE001"
   channel_name = "accounting-agent"
   ```

`channel_name` is only how prompts and messages name the channel. If
`channel_id` is left empty (or `SLACK_CHANNEL_ID` is not set in the
environment), runs put what would have gone to the channel into the thread
they were started from, and say so.

## 4. Fill in the three tiers

Anyone not listed in one of the three tables is ignored entirely: the
listener logs their member ID and does nothing else.

**Find a member ID:** in Slack, open the person's profile, click the **⋮**
(three dots) menu, **Copy member ID**. It looks like `UEXAMPLE001`.

```toml
[slack.admins]
"UEXAMPLE001" = "Finance Lead"
"UEXAMPLE002" = "Financial Controller"

[slack.users]
"UEXAMPLE003" = "Bookkeeper"
"UEXAMPLE004" = "Office Manager"

[slack.readonly]
"UEXAMPLE005" = "Analyst"

# Optional: how a person is named in messages (house style is lowercase
# first names; this overrides the default first name).
[slack.nicknames]
"Financial Controller" = "fc"
```

The value is the person's display name as the agent should use it. A member
may be in only one tier; `config/group.toml` refuses to load otherwise.

The tiers are read when the listener starts. **Restart the listener after
editing them** (`sudo systemctl restart accounting-agent-slack`).

Who answers which question (travel receipts, a team's card spend, one
entity's invoices) is company logic: write it in `rules/GROUP.md`. Runs read
it when they decide whom to ask.

## 5. The permission model

Three tiers. The difference between them is who can spend money (start a
Claude session), who can write to the books and change the rules, and who
gets the report.

| | Admin | User | Read-only |
|---|---|---|---|
| Messages captured as input | yes, as private notes | yes, into the shared pool | yes, into the shared pool |
| Input is treated as | an instruction the run acts on | evidence a run weighs | evidence a run weighs |
| `run <instruction>` | yes | no (captured, not executed) | no (captured, not executed) |
| `run consensus <task>` | yes | no | no |
| Follow-ups in an agent's thread | yes | no | no |
| `query <question>` | yes | no | yes |
| `status`, `notes`, `clear` | yes | no | no |
| Receives run reports | yes, their own runs | never | never |
| May change rules (write-back to `rules/` or a skill) | yes | never | never |
| May lift a safety rule (payment ban, duplicate guard, this whitelist) | only by an explicit, separate instruction | never | never |

What this means in practice:

- **Admins** start runs. A run is attributed to whoever started it, and that
  is who gets its questions and its report. Their notes are private: no
  admin ever sees another admin's notes, and a run only takes the notes of
  the admin who started it. A ruling in an admin's notes can be written back
  into `rules/` by the run.
- **Users** feed information in: explanations, data, answers to the agent's
  questions. Every message is acked with a tick and goes into a shared pool
  that the next run of any admin folds in, because an answer about an
  invoice is team data. Their input is never an instruction and never a rule
  change: where it conflicts with a document or a rule, the run queries it.
  They never see a report; the admin's report says what they said and what
  the run did with it.
- **Read-only** members are users plus `query`. A query is a Claude session
  that can read everything the agent can read (every Xero entity, the bank
  and bill feeds, the workbooks, Drive, the accounting inbox) and write
  nothing. Read-only is enforced three ways, none of them prose:
  1. `AGENT_READONLY=1` makes the Xero client refuse every non-GET call, and
     the Drive writers refuse too (`src/accounting_agent/readonly.py`);
  2. the session runs with the Write, Edit and NotebookEdit tools disallowed;
  3. the PreToolUse hook `.claude/hooks/guard-readonly.py` denies every
     other write (file edits outside `data/query/`, redirects, git, mail
     changes, Slack posts, the repo's loader and build scripts, unsetting the
     flag). Every denial is logged to `data/logs/readonly-denials.log`.

Daily caps stop a runaway loop from spending money. Defaults, overridable in
`.env`:

| Variable | Default | Counts |
|---|---|---|
| `SLACK_AGENT_MAX_RUNS` | 12 | new `run` agents per admin per day |
| `SLACK_AGENT_MAX_QUERIES` | 20 | `query` sessions per person per day |
| `SLACK_AGENT_MAX_TURNS` | 40 | follow-up turns in one thread, for its life |

## 6. Commands

All commands are sent as a **direct message to the bot**, at the top level of
the DM (not in the reporting channel). Examples use the fictional group in
`config/group.example.toml` (HoldCo, OpCo US, OpCo EU).

### `run <instruction>` (admins)

Starts a new agent for that instruction. It replies in the thread of your
message, and **that thread is the agent**: one Claude session per thread.

```
run today's bookkeeping
run check the tax coding on the HoldCo bills from last month
run send me the prepayments report
run give me all outstanding items
```

- The listener answers at once with a one-line ack, e.g.
  `started · bookkeeping · 3/12 · 2 notes attached`, plus a line if another
  admin already did the same work in the last 12 hours.
- Anything you post **in that thread** afterwards (with or without `run`) is
  an instruction for the same agent. It is acked with 👀, queued if the agent
  is mid-turn, and delivered the moment the turn ends: the runner resumes the
  same session (`claude --resume`), so the agent remembers the whole thread.
  Ten messages in one thread are one agent working through ten instructions.
- Two top-level `run` messages are two independent agents, side by side.
- `run` with nothing after it shows the help.
- One domain per run: the prompt carries the domain table from
  `[[domains]]` in `config/group.toml`, and the run loads one domain's skill
  and docs only.

Routing inside `run`:

| The instruction mentions | The run gets | Where the report goes |
|---|---|---|
| bookkeeping, intake, invoice run, daily run | the full bookkeeping workflow | the reporting channel: a top-level header `<COMPANY> BOOKKEEPING RUN (date, time)`, then the report as the first reply in its thread; your thread gets the headline and a pointer |
| a standing report (prepayments, accruals, deposits) and asks for it | the reports workflow | your thread |
| outstanding items, open queries, manual actions, what is still open | the outstanding-items list | the reporting channel, top level; your thread gets the headline |
| anything else | a focused task, read-only by default | your thread |

Whatever the run, any manual item (something a person has to do by hand in
Xero) is also posted to the channel in the same two-message shape:
`<COMPANY> MANUAL ITEMS (date, time)` at top level, the items as the first
reply.

A bookkeeping run is refused while another bookkeeping run is in flight
(another admin's, or the scheduled one), because two would race for the same
invoices. Focused runs never block each other.

### `run consensus <task>` (admins)

The same as `run`, through the two-agent consensus runner
(`deploy/run-consensus.sh`): the evidence is gathered once, two agents
reason independently, they reconcile, and only then is anything written.
About 2.5x the cost; use it for batches with real judgement in them.

```
run consensus code last week's OpCo US card bills
```

The report comes back through `deploy/notify-queried.sh`. A follow-up in its
thread starts a fresh session with the thread as context.

### `query <question>` (admins and read-only)

A read-only answer from the books, posted back into your thread. Files
(workbooks, CSV extracts) can come back too.

```
query total revenue in OpCo US for June
query HoldCo unpaid bills right now, by supplier
query send me the deposits workbook
```

One query at a time per person; a second one while yours is running is
refused with a note, not queued.

### `status` (admins)

Every admin's running agents (by task and age) or last run, the scheduled
bookkeeping if it is running, query counts for the day, and your own note
counts.

```
status
```

### `notes` / `notes full` (admins)

Your own notes and the shared user pool, counted and listed, with which ones
a run has already taken (`[taken by <name> 14:02]`). `notes full` prints the
whole text verbatim.

### `clear` / `clear shared` (admins)

`clear` discards your own notes (archived under
`data/slack/notes-archive/`). `clear shared` discards the shared user pool.

### Anything else

Captured as a note and acked with ✅. No Claude, no cost. A reply in a thread
that has no agent behind it is captured together with a digest of the thread,
so a bare "yes, do it" still means something to the next run.

## 7. Run the listener

### Prerequisites

- `.venv` with the requirements installed (`slack_sdk` among them).
- `.env` with `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN` and a Claude credential
  (`ANTHROPIC_API_KEY` or `CLAUDE_CODE_OAUTH_TOKEN`) for the runs.
- The `claude` CLI on the PATH (usually `~/.local/bin`).
- `config/group.toml` with `[slack]` filled in.
- Linux for runs: the runners use `flock`, `setsid` and `pgrep`. The
  listener itself starts anywhere, but a run started from a laptop without
  those tools will not run.

### Locally (foreground, for a first test)

```bash
set -a; . ./.env; set +a
.venv/bin/python scripts/slack_agent.py
```

It logs `connected as U..., listening only to: ...` and
`socket mode connected`. Stop it with Ctrl-C.

### On the server, as a systemd service

`deploy/accounting-agent-slack.service` has three placeholders: `__USER__` (the
Linux user that owns the checkout), `__USER_HOME__` (that user's home
directory) and `__REPO__` (the checkout's absolute path). `deploy/install.sh` fills them in and installs the unit; by hand:

```bash
sed -e "s|__USER_HOME__|$HOME|g" -e "s|__USER__|$USER|g" -e "s|__REPO__|$PWD|g" deploy/accounting-agent-slack.service \
    | sudo tee /etc/systemd/system/accounting-agent-slack.service
sudo systemctl daemon-reload
sudo systemctl enable --now accounting-agent-slack
```

Day to day:

```bash
sudo systemctl status accounting-agent-slack
journalctl -u accounting-agent-slack -f
sudo systemctl restart accounting-agent-slack     # after editing [slack] or slack_agent.py
```

The unit uses `KillMode=process`: restarting the listener never kills the
runs it has spawned. With systemd's default, a restart would kill a
bookkeeping run mid-flight.

### What runs where

| Piece | Role |
|---|---|
| `scripts/slack_agent.py` | the listener; never calls Claude |
| `deploy/run-thread.sh` | one turn of a thread's agent; resumes the session, drains queued follow-ups, posts the final message into the thread, then the write-back commit and the phantom-payment check |
| `deploy/run-query.sh` | one read-only `query` session; sets `AGENT_READONLY=1`, posts the answer |
| `deploy/run-consensus.sh` + `deploy/notify-queried.sh` | `run consensus` and its report |
| `scripts/slack_query.py` | `reply` (runner posts the answer) and `upload` (session sends a file), into the asking thread only |
| `data/slack/` | notes, the run registry, per-day counts, thread state and inboxes |
| `data/logs/<job>-<date>.log` | one log per job per day |

## 8. Verify

1. **Offline tests** (no Slack, no network):

   ```bash
   .venv/bin/python scripts/test_slack_agent.py
   .venv/bin/python scripts/test_readonly_guard.py
   ```

   Both end with `ALL PASS`.
2. **Token check**:

   ```bash
   set -a; . ./.env; set +a
   curl -s -H "Authorization: Bearer $SLACK_BOT_TOKEN" https://slack.com/api/auth.test
   ```

   `"ok":true` and the bot's `user_id`. `deploy/preflight.sh` does the same
   among its other checks.
3. **Listener**: start it (section 7) and DM the bot `status` from an admin
   account. You should get a few lines back within a second. From an account
   in no tier you should get nothing, and the listener logs
   `ignoring message from U...`.
4. **Notes**: DM the bot any sentence. It gets a ✅, and `notes` lists it.
5. **Query**: from an admin or read-only account, `query how many entities
   are connected`. You get `querying · read-only · 1/20`, then the answer in
   the thread.
6. **Channel**: run a bookkeeping run (`run today's bookkeeping`). The report
   should arrive in the reporting channel as a header message with the
   report in its thread. If the session reports `channel_not_found`, the bot
   is not in the channel: `/invite` it.
7. **Uploads**: `query send me the deposits workbook`. If the answer quotes
   `missing_scope`, add `files:write` and reinstall the app.

Troubleshooting:

| Symptom | Cause |
|---|---|
| The bot never answers a DM | Messages Tab not enabled in App Home, `message.im` not subscribed, or the sender is in no tier |
| `missing_scope` in a run's report | the named scope is missing: add it under OAuth & Permissions and reinstall |
| `invalid_auth` / `not_authed` | wrong or rotated token in `.env`; restart the service after fixing |
| Listener exits at start with a config error | `config/group.toml` missing, or a member in two tiers |
| A new admin is ignored | the listener was not restarted after editing `[slack]` |
| `that failed on my side` in the DM | the listener hit an exception: `journalctl -u accounting-agent-slack` |
