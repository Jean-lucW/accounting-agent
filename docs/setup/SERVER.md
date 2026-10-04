# Setting up the server

The agent's scheduled runs and its Slack listener live on one small Linux
server. This guide takes you from nothing to a server that bookkeeps every
night. Day-to-day operations after that are in `deploy/README.md`.

Why a server at all: the Xero refresh token rotates on every use, so exactly
one machine may hold it, and that machine must be on at 02:00 UTC. A laptop
is not.

## 1. Provision the machine

Anything that runs Ubuntu 22.04 or 24.04 LTS works: an AWS EC2 `t3.small`
(2 vCPU, 2 GiB), a Lightsail or DigitalOcean instance, any VPS. Sizing:

- 2 GiB of memory is enough; 1 GiB is not comfortable once a Claude session
  and a workbook build overlap. Add a 2 GiB swap file on the smallest sizes.
- 20 GB of disk covers the repo, the virtualenv, logs and downloaded invoices.
- Outbound HTTPS to Xero, Slack, Google, your banks and Anthropic. **No inbound
  port is needed** except ssh: the Slack listener dials out over Socket Mode.

Lock ssh down to your own addresses (a security group on EC2, `ufw` on a VPS):

```bash
sudo ufw allow from <your-ip>/32 to any port 22 proto tcp
sudo ufw enable
```

Create a dedicated user to own the agent, and give it your ssh key:

```bash
sudo adduser --disabled-password --gecos "" agent
sudo mkdir -p /home/agent/.ssh
sudo cp ~/.ssh/authorized_keys /home/agent/.ssh/
sudo chown -R agent:agent /home/agent/.ssh && sudo chmod 700 /home/agent/.ssh
```

The deploy scripts call `sudo` only to install and restart the systemd unit.
Either give `agent` sudo, or run those two steps yourself as an admin user.

Set the server's clock to UTC (cron times in `deploy/crontab.example` are
UTC):

```bash
sudo timedatectl set-timezone Etc/UTC
```

On your laptop, add an ssh alias so every script and command below can say
`accounting-server`:

```
# ~/.ssh/config
Host accounting-server
    HostName 203.0.113.10
    User agent
    IdentityFile ~/.ssh/accounting-server.pem
```

and in the laptop's `.env`:

```
AGENT_SERVER=accounting-server
AGENT_SERVER_DIR=/opt/accounting-agent
```

## 2. Install Python, Node and Claude Code

As an admin user on the server:

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip git jq curl util-linux
python3 -V                      # must be 3.11 or newer (24.04 ships 3.12)
```

On 22.04, whose `python3` is 3.10, install a newer one (for example
`sudo add-apt-repository ppa:deadsnakes/ppa && sudo apt-get install -y python3.12 python3.12-venv`).
`bootstrap.sh` and `install.sh` find `python3.11`, `python3.12` or
`python3.13` on their own.

Node is needed only to install Claude Code through npm. If you install Claude
Code another way (see Anthropic's install docs) and `claude` is on the PATH,
skip Node. Otherwise, Node 18 or newer from NodeSource:

```bash
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt-get install -y nodejs
node -v
```

Claude Code, as the `agent` user:

```bash
sudo -iu agent
mkdir -p ~/.local && npm config set prefix ~/.local
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.profile && . ~/.profile
npm install -g @anthropic-ai/claude-code
claude --version
```

The npm prefix `~/.local` puts `claude` in `~/.local/bin`, which is where
the deploy scripts and the systemd unit look for it (cron's own PATH has
neither). `deploy/bootstrap.sh` does this last part for you if `claude` is
missing.

## 3. Get the repo onto the server

Create the directory:

```bash
sudo mkdir -p /opt/accounting-agent && sudo chown agent:agent /opt/accounting-agent
```

The repository must be **private**: once running, it holds your group's
rules. **If the server can reach your git host**, clone it:

```bash
sudo -iu agent
git clone git@gitlab.example.com:you/accounting-agent.git /opt/accounting-agent
```

**If it cannot** (for example the git host is IP-allowlisted, or you do not
want a key for it on the server), push from the laptop instead. On the server:

```bash
sudo -iu agent
cd /opt/accounting-agent && git init -b main
```

and on the laptop:

```bash
git remote add server accounting-server:/opt/accounting-agent
git push server main:refs/remotes/laptop/main
ssh accounting-server 'cd /opt/accounting-agent && git checkout -B main laptop/main'
```

Either way, add the `server` remote on the laptop if you have not (the
command above). From now on `deploy/sync-all.sh` keeps the three copies equal
(section 9).

## 4. Bootstrap and install

On the server, as `agent`:

```bash
cd /opt/accounting-agent
./deploy/bootstrap.sh     # checks jq, flock, git, python 3.11+, node 18+; installs claude if missing
./deploy/install.sh       # .venv, requirements, exec bits, data/ dirs, secrets check, systemd unit
```

`install.sh` installs `deploy/accounting-agent-slack.service` into
`/etc/systemd/system/` but does not start it, and does not touch the crontab.
Both happen in step 7, once preflight is clean.

## 5. Configuration and secrets

**`config/group.toml`** holds no secrets but describes your group, so it is
gitignored. Copy it from the laptop:

```bash
scp config/group.toml accounting-server:/opt/accounting-agent/config/
```

**`.env`** carries every key. Create it on the server from the example and
fill it in there, or copy the laptop's and then fix the server-only values:

```bash
scp .env accounting-server:/opt/accounting-agent/.env
ssh accounting-server 'chmod 600 /opt/accounting-agent/.env'
```

`ANTHROPIC_API_KEY` must be a real key (console.anthropic.com > API keys). A
laptop's `.env` often has it blank, because Claude Code on a laptop logs in
through the keychain; headless runs on the server cannot. `CLAUDE_CODE_OAUTH_TOKEN`
from `claude setup-token` works instead if you prefer to bill a subscription.

**Token directories.** Every OAuth bootstrap needs a browser, so the
standard path is to run them on the laptop (`docs/setup/XERO.md`, `GMAIL.md`, `GOOGLE_DRIVE.md`,
`REVOLUT.md`) and copy the results over ssh. Never through email, chat or a
shared drive.

```bash
cd ~/path/to/accounting-agent
for d in .xero .gmail .gdrive .revolut; do
    [ -d "$d" ] && rsync -a --chmod=D700,F600 "$d/" "accounting-server:/opt/accounting-agent/$d/"
done
```

Then, **on the laptop, delete `.xero/tokens.json` and the `.revolut/` token
files**. Xero and Revolut rotate the refresh token on use: the first refresh
on the server kills the laptop's copy, and a refresh on the laptop would kill
the server's. (The alternative is to run a bootstrap on the server through an
ssh port forward, as each guide describes; then the token never exists on the
laptop at all.)

`install.sh` re-applies `chmod 600` to `.env` and the token files each time it
runs.

## 6. Preflight

```bash
./deploy/preflight.sh
```

Read-only. It checks that `config/group.toml` loads, the Python dependencies,
Xero (a read-only smoke test of every entity), the Gmail token, the Drive
token if `[drive] publish_folder_id` is set, the Slack bot and app tokens and
the report channel, every Revolut and Mercury slug in the config, the Claude
CLI and its credential, and that the payment guard and read-only guard fire.
It must end with `preflight clean`. Fix every FAIL before scheduling; each
line says where to look.

Then try one session by hand and read its log:

```bash
./deploy/run-agent.sh smoke "Say hello in one line. Do not call any API."
tail -50 data/logs/smoke-$(date -u +%F).log
```

## 7. Enable cron and the Slack listener

```bash
./deploy/install.sh --enable
```

This installs the lines from `deploy/crontab.example` into the `agent` user's
crontab between `# BEGIN accounting-agent <repo path>` /
`# END accounting-agent <repo path>` markers (any other lines in that crontab are kept), and enables and starts
the `accounting-agent-slack` service. Check both:

```bash
crontab -l
systemctl status accounting-agent-slack
```

Then say something to the agent in Slack as an admin. The schedule (UTC):
the daily bookkeeping chain Mon-Sat 02:00, the bills report and balance
reports straight after it, the review Wed and Sun 10:00, the retry sweep
every 10 minutes, the phantom-payment check at 23:00, a log trim on Sundays.
The full table is in `deploy/README.md`.

To pause everything: `crontab -e` and comment out the block, and
`sudo systemctl stop accounting-agent-slack`.

## 8. Logs

```bash
ls data/logs/
tail -f data/logs/cron.log                       # what cron fired and the plain-Python jobs
less data/logs/bookkeep-$(date -u +%F).log       # one session, prompt to phantom check
./deploy/retry-sweep.sh --list                   # jobs waiting on the Xero rate limit
journalctl -u accounting-agent-slack -f          # the Slack listener
cat data/logs/phantom-check.log                  # the nightly safety net
```

## 9. The git model: laptop, remote, server

Three copies of the repo:

```
laptop  --ssh-->  origin (any git host; a PRIVATE repository)
laptop  --ssh-->  server  (git remote `server`)
server  --x-->    origin  (when the git host is IP-allowlisted, or the server has no key)
```

The server edits the repo itself: an admin's ruling sent over Slack is
written into `rules/`. After every run `deploy/commit-writeback.sh` commits those
edits on the server (`rules/`, `docs/` except `docs/bookkept/`, and
`.claude/skills/` only; never `config/group.toml` or `data/`; never pushed).
The intake ledger and the outstanding register in `docs/bookkept/` are
gitignored and stay on the server.

If the server cannot push to your git host, the laptop is the bridge. Run,
from the laptop:

```bash
./deploy/sync-all.sh --dry-run    # read it first
./deploy/sync-all.sh
```

It commits anything left uncommitted on the server, merges the server's and
origin's commits into the laptop's `main`, pushes origin, fast-forwards the
server, and checks the three are one commit. It never force-pushes, never
resets, refuses while a run is in flight, and never moves `data/`, `.env` or
a token directory. The `sync-all` skill (`.claude/skills/sync-all/`) runs it
for you and explains every refusal.

Never `rsync --delete` the laptop's tree onto the server: it silently deletes
whatever the server wrote back.

## 10. Updating

Code changes are made on the laptop, committed, and delivered with
`./deploy/sync-all.sh`. If `requirements.txt` or `pyproject.toml` changed it
runs `deploy/install.sh` on the server; if the listener changed it restarts
the service (`KillMode=process`, so a run in flight is untouched).

After a change to `config/group.toml`, run `./deploy/preflight.sh` on the
server. After a Claude Code upgrade (`npm install -g @anthropic-ai/claude-code`
as `agent`), run it too: if check 7 (the payment guard) ever fails, stop the
cron block until it is fixed.

Operating system updates: `sudo apt-get update && sudo apt-get upgrade` from
time to time, outside 02:00-06:00 UTC. A reboot is safe; cron and the listener
come back on their own.
