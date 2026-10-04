---
name: sync-all
description: Bring the three copies of this repo (the laptop, the git remote `origin` on GitLab or GitHub, and the server) to one identical commit without losing anything on any of them. Collects uncommitted edits and write-backs from the server, merges by git history, pushes to origin, fast-forwards the server. Use when asked to sync, deploy, push, pull, "get the server's changes", "is X on the remote / on the server", or before any other deploy step.
---

# sync-all: laptop = git remote = server

One command, run from the laptop, never from the server:

```bash
./deploy/sync-all.sh --dry-run    # report only: what each copy has that the others lack
./deploy/sync-all.sh              # collect, merge, push, deliver, verify
```

Always run the dry run first and read it. It is the "which is most up to
date" answer: three `git log -1` lines, then every commit each remote has that
the laptop lacks, then every uncommitted file on the server and what it is.

The script reads `AGENT_SERVER` (ssh target), `AGENT_SERVER_DIR` (repo path on
the server, default `/opt/accounting-agent`) and optionally `AGENT_SSH_KEY`
from the environment or the laptop's `.env`, and expects two git remotes:
`origin` (the git host: GitLab, GitHub or any other) and `server`
(`$AGENT_SERVER:$AGENT_SERVER_DIR`). Setup is in `docs/setup/SERVER.md`.

**For a real deployment `origin` must be a private repository.** Once a group
fills in `config/group.toml` and `rules/`, the repo holds the group's entity
names, account codes, bank accounts, suppliers and staff: the shape of its
books. Never push a filled-in copy to a public remote.

## Why git history, not timestamps

Three copies, one bridge. The server often cannot reach the git host (an IP
allowlist, or no key for it), so the laptop carries changes both ways. "Most
up to date" is not a question of mtime: a copy tool stamps every file it
touches, and the same fix can exist on two copies. The commit graph is the
only reliable record, so the script:

1. **collects** the server's working tree: a dirty file byte-identical to the
   laptop's commit is a stale copy and is restored; anything else is
   committed on the server as its own work so it can be merged, never
   overwritten.
2. **merges** `server/main` and `origin/main` into the laptop's `main`.
3. **pushes** `main` to origin.
4. **delivers** `main` to the server as a plain ref and fast-forwards the
   server's checkout onto it. Runs `deploy/install.sh` there if dependencies
   changed and restarts the Slack listener if `scripts/slack_agent.py` or its
   unit changed (`KillMode=process`, so a run in flight is not touched).
5. **verifies** the three HEADs are one hash and the server tree is clean.

Only tracked files move. `data/`, `.env`, `.xero/`, `.gmail/`, `.gdrive/`,
`.revolut/` and the runtime registers (`docs/bookkept/LEDGER.md`,
`docs/bookkept/OUTSTANDING.md`, `docs/bookkept/outstanding.json`) are
gitignored and never cross in either direction, which is what
keeps the server's Xero token, bank feed, bill feed, intake ledger,
outstanding register, reports and Slack state where they are: they live on
the server only and are never committed. Workbooks reach people through the Drive publish folder and
Slack, not through git.

What a run writes back (a rule in `rules/`, a doc, a skill edit) is committed on the server by
`deploy/commit-writeback.sh`, and this sync is how it reaches origin and the
laptop.

## Answering "is change X everywhere?"

Do not read files on the server over ssh and compare by eye. Grep each ref:

```bash
git fetch --multiple origin server
for r in HEAD origin/main server/main; do echo "== $r"; git grep -n "<text of the change>" "$r" -- rules/EXPENSES.md; done
```

If the three outputs agree, it is everywhere. If not, run the sync and grep
again. After a successful run all three refs are the same commit, so a single
grep on `HEAD` answers it.

## When it refuses

The script stops rather than guess. Each stop and what to do:

| It says | Do |
|---|---|
| laptop working tree is dirty | commit the laptop's work first (it is the user's; ask what the message should be if unclear), then re-run |
| origin unreachable from `<ip>` | if the git host is IP-allowlisted, the laptop's address is not on the list. Report the IP. Do **not** change `origin` to HTTPS to get round it |
| server unreachable | the same problem on the server's firewall or security group, `deploy/README.md` "When SSH hangs" |
| a run is in flight on the server | wait for it to report in Slack, re-run. Its write-back is not committed until it finishes |
| merging `<ref>` conflicts | `git merge <ref>` on the laptop, resolve, commit, re-run. Both sides are real work; keep both unless one is plainly superseded |
| deleted on the server but the laptop still has it | someone removed a tracked file on the server by hand. Ask before deleting anything |
| looks like a secret and is not gitignored | a `.env*`, key, cert or `tokens.json` file sits untracked on the server. It is never committed: have it renamed to a `.keep` name (covered by `.env.*` in .gitignore) or removed on the server, then re-run. Once committed and pushed, a secret is in the remote's history for good |
| server-only edits outside rules/, docs/ and .claude/ | NOTE, not a stop. Code changed on the server, which is not supposed to happen. It is committed and merged all the same; tell the user which files |
| RESTART THE LISTENER by hand | `sudo -n` was refused on the server. `ssh` in and `sudo systemctl restart accounting-agent-slack` |

## Never

- `git push --force`, `git reset --hard`, or `git checkout -- .` on any copy.
  If the script cannot fast-forward the server, something is wrong upstream
  of it.
- `rsync --delete` to the server. It cannot see a write-back and deletes it.
- Touch `data/`, `.env` or any token directory on the server.
- Run this on the server, or against a branch other than `main`.

## Reporting

`docs/COMMS.md` applies. Sections only where more than one applies, terse:

```
**synced**
- laptop = origin = server at abc1234

**collected from the server**
- the new supplier rule for Contoso Cloud, rules/SUPPLIERS.md
- a coding rule for staff meals, rules/EXPENSES.md

**pushed**
- the Slack notes fix, def5678, to origin and server; listener restarted

**blocked**
- (only if the script stopped; the line it stopped on and what the user must decide)
```
