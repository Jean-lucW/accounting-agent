"""Offline checks for the Slack listener's state model.

No Slack, no Claude, no network: a fake web client, a temp data dir and a
temporary config/group.toml with fake member IDs (AGENT_GROUP_CONFIG). Run it
after touching slack_agent.py, on the laptop or the server:

    .venv/bin/python scripts/test_slack_agent.py

It exists because the things most likely to break here are invisible
until someone is affected by them: one admin seeing another's notes,
a run consuming notes that were not its own, a thread digest that comes
back empty so a bare "yes" reaches the next run with nothing attached.
"""
import os
import pathlib
import sys
import tempfile
import types

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "src"))

# -- a throwaway group, written before slack_agent is imported ---------------
tmp = pathlib.Path(tempfile.mkdtemp())
CFG = tmp / "group.toml"
A1, A2, A3 = "UTESTADM01", "UTESTADM02", "UTESTADM03"   # admins
U1 = "UTESTUSR01"                                       # user tier
R1 = "UTESTRDO01"                                       # read-only tier
CFG.write_text(f"""
[company]
name = "Test Group"
reporting_currency = "USD"
timezone = "UTC"

[[entities]]
key = "PARENT"
xero_name = "Test Parent Limited"
short = "Parent"
base_currency = "GBP"

[[entities]]
key = "SUB"
xero_name = "Test Subsidiary Inc."
short = "Sub"
base_currency = "USD"

[slack]
channel_id = "CTESTCHAN1"
channel_name = "test-accounting"

[slack.admins]
"{A1}" = "Avery Admin"
"{A2}" = "Blake Second"
"{A3}" = "Casey Third"

[slack.users]
"{U1}" = "Uma User"

[slack.readonly]
"{R1}" = "Riley Reader"

[slack.nicknames]
"Blake Second" = "bee"

[[domains]]
key = "bookkeeping"
title = "Bookkeeping"
skill = "xero-bills"
docs = ["rules/EXPENSES.md", "docs/bookkept/LEDGER.md"]

[[domains]]
key = "reports"
title = "Standing Reports"
skill = "reports"
docs = []
""")
os.environ["AGENT_GROUP_CONFIG"] = str(CFG)
from accounting_agent import config  # noqa: E402
config.reload()

# stub slack_sdk so the module imports without the dependency shape mattering
for name in ["slack_sdk", "slack_sdk.socket_mode", "slack_sdk.socket_mode.request",
             "slack_sdk.socket_mode.response"]:
    sys.modules.setdefault(name, types.ModuleType(name))
sys.modules["slack_sdk"].WebClient = object
sys.modules["slack_sdk.socket_mode"].SocketModeClient = object
sys.modules["slack_sdk.socket_mode.request"].SocketModeRequest = object
sys.modules["slack_sdk.socket_mode.response"].SocketModeResponse = object

import slack_agent as A  # noqa: E402
A.DATA = tmp; A.NOTES_DIR = tmp/"notes"; A.SHARED_NOTES = A.NOTES_DIR/"shared.md"
A.SHARED_APPLIED = A.NOTES_DIR/"shared-applied.json"   # or it writes into the real data/
A.ARCHIVE = tmp/"arch"; A.REGISTRY = tmp/"runs.jsonl"; A.RUNCOUNT = tmp/"runcount"
A.THREADS = tmp/"threads"

# 0. the tiers come from config, nothing else
assert A.ADMINS == {A1: "Avery Admin", A2: "Blake Second", A3: "Casey Third"}, A.ADMINS
assert A.USERS == {U1: "Uma User"} and A.READONLY == {R1: "Riley Reader"}
assert A._short("Blake Second") == "bee" and A._short("Avery Admin") == "avery"
print("PASS tiers and nicknames loaded from the temporary config")


def wipe(uid):
    """Clean slate for one admin: notes and their taken-markers."""
    A._archive(A._notes_path(uid), uid, remove=True)
    A._applied_path(uid).unlink(missing_ok=True)


sent = []
class FakeWeb:
    def chat_postMessage(self, channel, text, thread_ts=None): sent.append(text)
    def reactions_add(self, **k): pass
    def conversations_replies(self, channel, ts, limit=60):
        return {"messages": [
            {"ts": "100.0", "bot_id": "B1", "text": "queried INV-1001 · 05 Sep · USD 1,250.00 · Contoso Cloud Ltd · which entity?"},
            {"ts": "101.0", "user": A1, "text": "Parent"},
        ]}

ag = A.Agent(FakeWeb(), me="B1")

# 1. notes are per-admin
ag.capture("D1", "200.0", "code Contoso Cloud to 439", A1)
ag.capture("D1", "201.0", "bee's private note", A2)
ag.capture("D2", "202.0", "the card charge is Sub", U1)
assert "Contoso Cloud" in A._read_notes(A._notes_path(A1))
assert "bee's private note" not in A._read_notes(A._notes_path(A1)), "admin leak!"
assert "Contoso Cloud" not in A._read_notes(A._notes_path(A2)), "admin leak!"
assert "card charge" in A._read_notes(A.SHARED_NOTES)
print("PASS per-admin notes isolated; user-tier input shared")

# 2. a thread reply carries the thread
ag.capture("D1", "102.0", "yes do it", A1, thread_ts="100.0")
body = A._read_notes(A._notes_path(A1))
assert "## thread 100.0" in body and "bot: queried INV-1001" in body and "avery: Parent" in body
assert "yes do it" in body
print("PASS thread digest attached to the note")

# 3. digest written once per thread, not per reply
ag.capture("D1", "103.0", "and the next one too", A1, thread_ts="100.0")
assert A._read_notes(A._notes_path(A1)).count("## thread 100.0") == 1
assert A._read_notes(A._notes_path(A1)).count("bot: queried INV-1001") == 1
print("PASS digest not repeated per reply")

# 4. notes command shows yours + shared, never another admin's
sent.clear(); ag.cmd_notes("D1", "300.0", "", A1)
assert "bee's private note" not in sent[0] and "Contoso Cloud" in sent[0] and "card charge" in sent[0]
print("PASS notes command:", repr(sent[0][:60]))

# 5. taking notes marks them, and only yours
own, shared = A._take_notes(A1)
assert "Contoso Cloud" in own and "card charge" in shared
assert A._read_notes(A._notes_path(A2)).strip() != "", "A2's notes were consumed by A1's run"
assert "Contoso Cloud" in A._read_notes(A._notes_path(A1)), "own notes deleted, not marked"
assert len(A._read_applied(A._applied_path(A1))) > 0, "own notes not marked taken"
assert A._take_notes(A1)[0].strip() == "", "the same note was fed to a second run"
print("PASS take_notes marks only the caller's pool, and feeds it once")

# 6. per-admin run counts
A._bump_runs(A1); A._bump_runs(A1); A._bump_runs(A2)
assert (A._runs_today(A1), A._runs_today(A2), A._runs_today(A3)) == (2, 1, 0)
print("PASS per-admin run counts")

# 7. cross-user visibility
A._registry_append({"ts": A._now(), "user_id": A3, "user": "Casey Third",
                    "job": A._job(A3), "task": "run today's bookkeeping",
                    "kind": "bookkeeping", "thread_ts": "1", "channel": "D3"})
print("PASS overlap:", repr(A._overlap(A1, "do today's bookkeeping", "bookkeeping")))
assert A._overlap(A1, "do today's bookkeeping", "bookkeeping")
assert not A._overlap(A3, "do today's bookkeeping", "bookkeeping"), "warned about own run"
A._registry_append({"ts": A._now(), "user_id": A2, "user": "Blake Second",
                    "job": A._job(A2), "task": "check the tax split on Parent bills",
                    "kind": "focused", "thread_ts": "1", "channel": "D3"})
assert A._overlap(A1, "check the tax split on Parent bills", "focused").startswith("bee ran")
assert not A._overlap(A1, "post the fixed asset addition FA-0002 in Sub", "focused")
print("PASS focused overlap flagged by nickname; unrelated focused task not flagged")

# 8. jobs are distinct per admin
assert A._job(A1) != A._job(A2) != A._job(A3)
print("PASS distinct jobs:", A._job(A1), A._job(A2))

# 9. status shows everyone, notes only yours
sent.clear(); ag.cmd_status("D1", "400.0", A1)
assert "bee's private note" not in sent[0]
assert "bee: idle" in sent[0] and "casey: idle" in sent[0]
print("PASS status:\n" + sent[0])

# 11. a run started inside a thread carries that thread into its prompt
class _Stub:
    DEVNULL = -3
    @staticmethod
    def Popen(*a, **k): return None
    @staticmethod
    def run(*a, **k): return type("R", (), {"stdout": "", "returncode": 1})()
A.subprocess = _Stub
sent.clear()
ag.on_message({"text": "run apply that fix", "channel": "D1", "ts": "104.0",
               "user": A1, "thread_ts": "100.0"})
prompt = (tmp / f"last-prompt-{A1}.txt").read_text()
assert "THREAD this run was started from" in prompt, "no thread block"
assert "bot: queried INV-1001" in prompt and "avery: Parent" in prompt, "thread digest empty"
print("PASS run from a thread carries the thread")
print("PASS start message:", repr(sent[0]))

# 11b. the prompt is built from config: domain table, channel, entities, users
assert "ONE DOMAIN PER RUN. Decide which of the 2 below" in prompt, "domain count not from config"
assert "Bookkeeping" in prompt and "xero-bills" in prompt and "Standing Reports" in prompt
assert "#test-accounting (CTESTCHAN1)" in prompt, "channel not from config"
assert "Uma User" in prompt and U1 in prompt, "user tier table not from config"
assert "TEST GROUP MANUAL ITEMS" in prompt, "company name not from config"
assert "{" not in prompt.split("THREAD this run")[0].replace("{}", ""), "unfilled placeholder"
print("PASS run prompt built from config (domains, channel, users, company)")


# 12. a multi-paragraph note does not swallow the notes after it
long_note = ("contoso cloud invoices should be coded to software,\n"
             "not to general office costs,\n"
             "\n"
             "and split across the entities by headcount.\n"
             "note, this applies from the next run onwards")
wipe(A1)          # runs no longer empty the file, so ask for the slate explicitly
ag.capture("D1", "500.0", long_note, A1)
ag.capture("D1", "501.0", "another thing to add: northwind freight bills need a purchase order number", A1)
items = A._note_items(A._read_notes(A._notes_path(A1)))
assert len(items) == 2, f"expected 2 notes, parser found {len(items)}"
assert "applies from the next run onwards" in items[0][2], "multi-paragraph note truncated"
assert "purchase order" in items[1][2], "the note after a long one was lost"
sent.clear(); ag.cmd_notes("D1", "502.0", "", A1)
assert "yours (2 new of 2)" in sent[0], sent[0]
print("PASS multi-paragraph note counted as one, later notes survive")
sent.clear(); ag.cmd_notes("D1", "503.0", "full", A1)
assert "applies from the next run onwards" in sent[0] and "purchase order" in sent[0]
print("PASS `notes full` still prints everything verbatim")

# 13. EVERY count, for EVERY admin, goes through _note_items
#     (a multi-paragraph note must never inflate a number shown to anyone)
multi = ("contoso cloud is coded to software\n"
         "\n"
         "and split across the entities by headcount\n"
         "note, this applies from the next run onwards")
for uid in (A1, A2, A3):
    wipe(uid)                                    # clean slate per admin
A._archive(A.SHARED_NOTES, "shared", remove=True)
A.SHARED_APPLIED.unlink(missing_ok=True)
for uid in (A1, A2, A3):
    ag.capture("D1", f"6{uid[-2:]}.0", multi, uid)
    ag.capture("D1", f"7{uid[-2:]}.0", "second note, one line", uid)
ag.capture("D2", "800.0", "user-tier multi\n\nsecond para", U1)

for uid, name in ((A1, "you"), (A2, "bee"), (A3, "casey")):
    assert A._count(A._read_notes(A._notes_path(uid))) == 2, f"{name} count wrong"

    sent.clear(); ag.cmd_notes("D1", "900.0", "", uid)
    assert "yours (2 new of 2)" in sent[0], f"{name} notes: {sent[0][:80]}"
    assert "from users (1 new of 1)" in sent[0], f"{name} user count: {sent[0][:120]}"

    sent.clear(); ag.cmd_status("D1", "901.0", uid)
    assert "your notes: 2 · user input: 1" in sent[0], f"{name} status: {sent[0]}"
print("PASS notes + status report the true count for all three admins")

# the run's "N notes attached" too
sent.clear()
ag.on_message({"text": "run check something unrelated to anything", "channel": "D1",
               "ts": "902.0", "user": A2})
assert "3 notes attached" in sent[0], sent[0]   # A2's 2 + 1 shared
print("PASS run start:", sent[0].splitlines()[0])

# and clear
sent.clear(); ag.cmd_clear("D1", "903.0", "", A3)
assert sent[0] == "cleared 2 of your notes.", sent[0]
print("PASS clear reports the true count")

# 14. a run MARKS shared user-tier input, it does not consume it: the other
#     admins keep seeing it, tagged with who took it
applied = A._read_applied()
assert len(applied) == 1, f"A2's run should have marked 1 shared note, marked {len(applied)}"
assert "bee" in list(applied.values())[0], applied
assert A._read_notes(A.SHARED_NOTES).strip(), "shared pool was deleted, not marked"
sent.clear(); ag.cmd_notes("D1", "905.0", "", A1)
assert "from users (0 new of 1)" in sent[0], sent[0]
assert "taken by bee" in sent[0], sent[0]
print("PASS shared input survives another admin's run, visible to the rest:")
print("   " + [l for l in sent[0].splitlines() if "taken by" in l][0].strip())

# and it is not fed to a second run
own2, shared2 = A._take_notes(A1, "avery")
assert shared2 == "", "the same user-tier note was fed to a second run"
print("PASS but it is not applied twice")

sent.clear(); ag.cmd_clear("D1", "906.0", "shared", A3)
assert "cleared 1 user notes" in sent[0], sent[0]
assert not A._read_applied(), "clear shared left stale applied markers"
print("PASS `clear shared` drops the pool and its markers together")

# 15. starting a run must not empty your side of `notes`. A note handed to a
#     run and then unlinked makes the queue read as empty, and the admin
#     reasonably concludes it was dropped and re-posts it.
wipe(A1)
A._archive(A.SHARED_NOTES, "shared", remove=True)
A.SHARED_APPLIED.unlink(missing_ok=True)

feedback = ("fabrikam supplies\n"
            "each invoice should appear once in the ledger\n"
            "with its document attached.\n"
            "\n"
            "for the refund, check all the\n"
            "transactions on xero across the entities for that")
ag.capture("D1", "1000.0", feedback, A1)
sent.clear(); ag.cmd_notes("D1", "1001.0", "", A1)
assert "yours (1 new of 1)" in sent[0], sent[0]

own, _ = A._take_notes(A1, "avery")           # the run starts
assert "for the refund" in own, "the run did not receive the note"

sent.clear(); ag.cmd_notes("D1", "1002.0", "", A1)
assert "yours (0 new of 1)" in sent[0], f"note vanished from the queue: {sent[0]}"
assert "taken by avery" in sent[0], f"no sign of who took it: {sent[0]}"
assert "for the refund" in A._read_notes(A._notes_path(A1)), "note deleted from the file"
print("PASS a run does not empty `notes`:")
print("   " + [l for l in sent[0].splitlines() if "taken by" in l][0].strip()[:100])

# a note posted while that run is still live belongs to the NEXT run,
# and is listed as new straight away
ag.capture("D1", "1003.0", "one more thing about the fabrikam credit note", A1)
sent.clear(); ag.cmd_notes("D1", "1004.0", "", A1)
assert "yours (1 new of 2)" in sent[0], sent[0]
sent.clear(); ag.cmd_status("D1", "1005.0", A1)
assert "your notes: 1" in sent[0], sent[0]
own2, _ = A._take_notes(A1, "avery")
assert "fabrikam credit note" in own2 and "for the refund" not in own2, "the next run got the wrong notes"
print("PASS a note posted mid-run is listed as new and goes to the next run")

# 16. pruning: a note is dropped only when it is BOTH taken and older than
#     KEEP_TAKEN_DAYS. An untaken note is kept however old it is, or the queue
#     silently loses work nobody has run yet.
import datetime as _dt  # noqa: E402
wipe(A1)
old_ts = (_dt.datetime.now(_dt.timezone.utc)
          - _dt.timedelta(days=A.KEEP_TAKEN_DAYS + 5)).strftime("%Y-%m-%dT%H:%M:%SZ")
for text in ("an old note a run already took", "an old note nobody ran"):
    A._append_note(A._notes_path(A1), "## direct", "",
                   f"- ({old_ts}) [Avery Admin, admin, {A1}] {text}\n")

blocks = A._note_blocks(A._read_notes(A._notes_path(A1)))
assert len(blocks) == 2, blocks
taken_key = next(b[0] for b in blocks if "already took" in b[3])
A._mark_applied([taken_key], "avery",
                path=A._applied_path(A1), pool=A._notes_path(A1))

A._prune_taken(A._notes_path(A1), A._applied_path(A1))
body = A._read_notes(A._notes_path(A1))
assert "an old note nobody ran" in body, "an untaken note was pruned"
assert "already took" not in body, "a taken note older than the window survived"
print("PASS pruning drops old taken notes, keeps untaken ones at any age")

# and a taken note inside the window stays listed
wipe(A1)
ag.capture("D1", "1100.0", "recent, and taken", A1)
A._take_notes(A1, "avery")
assert "recent, and taken" in A._read_notes(A._notes_path(A1)), "pruned too eagerly"
print("PASS a note taken today is still listed")


# 17. the helpers must resolve module globals at CALL time. Binding
#     SHARED_APPLIED as a default argument would make _read_applied() ignore
#     the rebinding above and read the real data/ tree, which this harness
#     would then write test markers into.
assert A._read_applied() == A._read_applied(A.SHARED_APPLIED)
assert str(A.SHARED_APPLIED).startswith(str(tmp)), "harness is pointed at the real data dir"
import inspect as _inspect  # noqa: E402
for fn in (A._read_applied, A._mark_applied):
    for name, prm in _inspect.signature(fn).parameters.items():
        if name in ("path", "pool"):
            assert prm.default is None, (
                f"{fn.__name__}({name}=) binds a module global as a default; "
                "resolve it inside the function instead")
print("PASS applied-marker paths resolve at call time, not at def time")

print()
print("all slack_agent checks passed")


# 18. the READ-ONLY tier: user-tier behaviour plus `query`
assert R1 in A.READONLY and R1 in A.ALLOWED_USERS and R1 not in A.ADMINS and R1 not in A.USERS
assert set(A.READONLY) == {R1}, "only the configured member is read-only"
spawned = []
class _StubQ(_Stub):
    @staticmethod
    def Popen(args, **k): spawned.append(args); return None
A.subprocess = _StubQ

# a plain message from them is shared input, tagged readonly, acked, no session
sent.clear(); spawned.clear()
ag.on_message({"text": "the hosting invoice is in my inbox", "channel": "D9", "ts": "1000.0", "user": R1})
shared = A._read_notes(A.SHARED_NOTES)
assert "hosting invoice" in shared and f"[Riley Reader, readonly, {R1}]" in shared
assert not spawned and not sent
print("PASS read-only: plain message captured as shared input, role readonly")

# run / status / notes / clear from them: captured, refused, nothing spawned
for cmd in ("run today's bookkeeping", "status", "notes", "clear shared"):
    sent.clear(); spawned.clear()
    ag.on_message({"text": cmd, "channel": "D9", "ts": "1001.0", "user": R1})
    assert not spawned, cmd
    assert sent and "query <question>" in sent[0], (cmd, sent)
assert "today's bookkeeping" in A._read_notes(A.SHARED_NOTES)
assert A._runs_today(R1) == 0 and not A._notes_path(R1).exists(), "read-only user got an admin's notes file"
print("PASS read-only: run/status/notes/clear refused and captured, no session, no admin state")

# query: spawns run-query.sh with the job, user, channel, thread and a read-only prompt
sent.clear(); spawned.clear()
ag.on_message({"text": "query total revenue in Sub for June", "channel": "D9", "ts": "1002.0", "user": R1})
assert len(spawned) == 1, spawned
args = spawned[0]
assert args[2].endswith("deploy/run-query.sh") and args[3] == "query-" + R1
assert args[4] == R1 and args[5] == "D9" and args[6] == "1002.0"
prompt = args[7]
assert prompt.startswith("total revenue in Sub for June\n")
assert "READ-ONLY QUERY" in prompt and "Riley Reader" in prompt and "read-only\ntier" in prompt
assert "AGENT_READONLY=1" in prompt and "slack_query.py upload" in prompt
assert "ONE DOMAIN PER RUN" in prompt and "docs/COMMS.md" in prompt
assert "entities by short name (Parent, Sub)" in prompt, "entity names not from config"
assert "UNATTENDED RUN" not in prompt and "NOTES from" not in prompt, "query got a run's brief or notes"
assert (tmp / f"last-query-{R1}.txt").read_text() == prompt
assert sent == ["querying · read-only · 1/%d" % A.MAX_QUERIES_PER_DAY], sent
assert A._runs_today("query-" + R1) == 1 and A._runs_today(R1) == 0
rec = A._registry_read(1)[-1]
assert rec["kind"] == "query" and rec["job"] == "query-" + R1 and rec["user_id"] == R1
print("PASS read-only: query spawns run-query.sh with the right args and brief")

# no notes leak into a query: the shared pool and admin notes are never attached
assert "hosting invoice" not in prompt and "bee" not in prompt.lower().split("read-only query")[0]
print("PASS read-only: no notes of anyone attached to a query")

# empty query gets the help, nothing spawned or counted
sent.clear(); spawned.clear()
ag.on_message({"text": "query", "channel": "D9", "ts": "1003.0", "user": R1})
assert not spawned and sent and sent[0].startswith("`query <question>`")
assert A._runs_today("query-" + R1) == 1
print("PASS read-only: bare `query` shows help")

# a query from inside a thread carries the thread
sent.clear(); spawned.clear()
ag.on_message({"text": "query and for July?", "channel": "D9", "ts": "1004.0", "user": R1, "thread_ts": "100.0"})
assert "THREAD this question was asked in" in spawned[0][7] and "bot: queried INV-1001" in spawned[0][7]
assert spawned[0][6] == "100.0", "reply must land in the thread root"
print("PASS read-only: query from a thread carries the thread, answers into it")

# the daily query cap is separate from the run cap
for _ in range(A.MAX_QUERIES_PER_DAY):
    A._bump_runs("query-" + R1)
sent.clear(); spawned.clear()
ag.on_message({"text": "query anything", "channel": "D9", "ts": "1005.0", "user": R1})
assert not spawned and "daily query cap reached" in sent[0], sent
print("PASS read-only: daily query cap enforced:", sent[0])

# a query never counts as overlap for an admin's run, and status shows the count
assert not A._overlap(A1, "total revenue in Sub for June", "focused")
sent.clear(); ag.cmd_status("D1", "1006.0", A1)
assert "queries today: riley" in sent[0], sent[0]
print("PASS read-only: queries invisible to overlap, visible in status")

# admins can query too; the user tier still cannot
sent.clear(); spawned.clear()
ag.on_message({"text": "query Parent unpaid bills", "channel": "D1", "ts": "1007.0", "user": A2})
assert spawned and spawned[0][3] == "query-" + A2 and "admin\ntier" in spawned[0][7]
sent.clear(); spawned.clear()
ag.on_message({"text": "query Parent unpaid bills", "channel": "D2", "ts": "1008.0", "user": U1})
assert not spawned and "I can't start runs" in sent[0]
assert "Parent unpaid bills" in A._read_notes(A.SHARED_NOTES)
print("PASS read-only: admins may query, users may not")

# a running query blocks only that person's next query
_rj_real = A._running_jobs
A._running_jobs = lambda: {"query-" + R1: 1}
sent.clear(); spawned.clear()
A.RUNCOUNT = tmp / "runcount2"           # fresh counters, cap not in the way
ag.on_message({"text": "query again", "channel": "D9", "ts": "1009.0", "user": R1})
assert not spawned and "still running" in sent[0]
sent.clear(); spawned.clear()
ag.on_message({"text": "query something", "channel": "D1", "ts": "1010.0", "user": A3})
assert spawned, "another person's query was blocked by the read-only member's"
A._running_jobs = _rj_real
print("PASS read-only: one query at a time per person, not across people")

# _running_jobs sees both runners, so a query cannot be started twice
class _StubP(_StubQ):
    @staticmethod
    def run(*a, **k):
        return type("R", (), {"stdout":
            f"111 bash /x/deploy/run-agent.sh run-{A1} prompt text\n"
            f"222 bash /x/deploy/run-query.sh query-{R1} {R1} D9 1.0 prompt\n",
            "returncode": 0})()
A.subprocess = _StubP
assert A._running_jobs() == {f"run-{A1}": 111, f"query-{R1}": 222}, A._running_jobs()
print("PASS read-only: _running_jobs parses run-agent.sh and run-query.sh")
print("all read-only checks passed")


# 19. ONE AGENT PER SLACK THREAD. Separate top-level messages are separate
#     agents; everything inside an agent's thread goes to that agent and is
#     answered there; a thread never spawns a second agent.
A.RUNCOUNT = tmp / "runcount3"
A.subprocess = _StubQ
A._running_jobs = lambda: {}

# two top-level runs from the same admin -> two agents, two jobs, two threads
sent.clear(); spawned.clear()
ag.on_message({"text": "run a check for x", "channel": "D5", "ts": "2000.100", "user": A3})
ag.on_message({"text": "run a check for y", "channel": "D5", "ts": "2001.200", "user": A3})
assert len(spawned) == 2, spawned
j1, j2 = spawned[0][3], spawned[1][3]
assert j1 == f"run-{A3}-2000-100" and j2 == f"run-{A3}-2001-200", (j1, j2)
for args, root in ((spawned[0], "2000.100"), (spawned[1], "2001.200")):
    assert args[2].endswith("deploy/run-thread.sh"), args[2]
    assert args[4] == A3 and args[5] == "D5" and args[6] == root and args[7] == root.replace(".", "-")
    assert args[8].startswith("a check for "), args[8][:40]
    assert "THIS SESSION IS ONE SLACK THREAD" in args[8] and "REPORT SHAPE" in args[8]
assert A._thread_state("2000.100")["job"] == j1 and A._thread_state("2001.200")["job"] == j2
assert all(s.startswith("started · focused") for s in sent), sent
assert A._runs_today(A3) == 2
print("PASS two top-level messages are two agents:", j1, j2)

# a plain message inside an agent's thread -> that agent's inbox, runner
# re-spawned for THAT thread with an empty prompt, eyes not tick, no note
sent.clear(); spawned.clear()
ag.on_message({"text": "also split it by entity", "channel": "D5", "ts": "2002.0",
               "user": A3, "thread_ts": "2000.100"})
assert len(spawned) == 1 and spawned[0][3] == j1, spawned
assert spawned[0][6] == "2000.100" and spawned[0][8] == "", spawned[0]
inbox = A._thread_inbox_path("2000.100").read_text()
assert "also split it by entity" in inbox and f"[Casey Third, admin, {A3}]" in inbox
assert "split it by entity" not in A._read_notes(A._notes_path(A3)), "thread message became a note"
assert not sent, sent
print("PASS a message in an agent's thread is queued for that agent, not a note")

# `run ...` inside an agent's thread is a follow-up too, never a new agent
sent.clear(); spawned.clear()
ag.on_message({"text": "run and add the totals", "channel": "D5", "ts": "2003.0",
               "user": A3, "thread_ts": "2000.100"})
assert len(spawned) == 1 and spawned[0][3] == j1 and spawned[0][8] == "", spawned
assert "and add the totals" in A._thread_inbox_path("2000.100").read_text()
assert not A._thread_state("2003.0"), "a `run` inside a thread made a new agent"
assert A._runs_today(A3) == 2, "a follow-up counted as a new run"
assert not sent, sent
print("PASS `run` inside an agent's thread goes to that agent")

# the other thread's agent is untouched by all of that
assert not A._thread_inbox_path("2001.200").exists()
print("PASS the other thread's inbox is untouched")

# ten messages in one thread: ten inbox lines, ten runner spawns for the SAME
# job, still one agent and zero new runs
spawned.clear()
for i in range(10):
    ag.on_message({"text": f"instruction {i}", "channel": "D5", "ts": f"2010.{i}",
                   "user": A3, "thread_ts": "2000.100"})
assert len(spawned) == 10 and {a[3] for a in spawned} == {j1}, {a[3] for a in spawned}
assert A._thread_inbox_path("2000.100").read_text().count("- (") == 12
assert A._runs_today(A3) == 2
print("PASS ten messages in one thread stay one agent")

# a reply in a thread that has NO agent is still a note, with the digest
sent.clear(); spawned.clear()
ag.on_message({"text": "yes do it", "channel": "D1", "ts": "2020.0", "user": A3, "thread_ts": "150.0"})
assert not spawned
assert "yes do it" in A._read_notes(A._notes_path(A3)) and "## thread 150.0" in A._read_notes(A._notes_path(A3))
print("PASS a reply in a non-agent thread is still a note")

# `run` in a thread with no agent starts an agent keyed on that thread's root,
# carrying the thread as context
sent.clear(); spawned.clear()
ag.on_message({"text": "run apply that", "channel": "D1", "ts": "2021.0", "user": A3, "thread_ts": "150.0"})
assert spawned and spawned[0][3] == f"run-{A3}-150-0" and spawned[0][6] == "150.0"
assert "THREAD this run was started from" in spawned[0][8]
assert A._thread_state("150.0")["job"] == f"run-{A3}-150-0"
print("PASS `run` in a non-agent thread starts that thread's agent with the thread as context")

# the per-thread turn cap
A._write_thread_state("2001.200", turns=A.MAX_TURNS_PER_THREAD)
sent.clear(); spawned.clear()
ag.on_message({"text": "one more", "channel": "D5", "ts": "2030.0", "user": A3, "thread_ts": "2001.200"})
assert not spawned and "reached its cap" in sent[0], sent
print("PASS per-thread turn cap:", sent[0])

# a running turn in one thread does not block a new top-level agent, nor the
# other thread; only the same thread says "still going"
A._running_jobs = lambda: {j1: 1}
sent.clear(); spawned.clear()
ag.on_message({"text": "run a check for z", "channel": "D5", "ts": "2040.0", "user": A3})
assert spawned and spawned[0][3] == f"run-{A3}-2040-0", spawned
print("PASS a running agent does not block a new top-level agent")

# status lists each running thread by task
sent.clear(); ag.cmd_status("D5", "2050.0", A3)
assert "you: running · focused · a check for x" in sent[0], sent[0]
print("PASS status shows the running thread:\n" + sent[0])

# user tier in an admin's agent thread still cannot spawn anything
sent.clear(); spawned.clear()
ag.on_message({"text": "run it", "channel": "D5", "ts": "2060.0", "user": U1, "thread_ts": "2000.100"})
assert not spawned and "I can't start runs" in sent[0]
print("PASS user tier cannot drive an agent thread")

# bookkeeping still blocks across threads while one bookkeeping run is live
A._registry_append({"ts": A._now(), "user_id": A2, "user": "Blake Second",
                    "job": f"run-{A2}-3000-0", "task": "run today's bookkeeping",
                    "kind": "bookkeeping", "thread_ts": "3000.0", "channel": "D7"})
A._running_jobs = lambda: {f"run-{A2}-3000-0": 5}
sent.clear(); spawned.clear()
ag.on_message({"text": "run today's bookkeeping", "channel": "D5", "ts": "3001.0", "user": A3})
assert not spawned and "bookkeeping already running (bee)" in sent[0], sent
sent.clear(); spawned.clear()
ag.on_message({"text": "run check the tax on Parent", "channel": "D5", "ts": "3002.0", "user": A3})
assert spawned, "a focused run was blocked by someone's bookkeeping"
# and the scheduled bookkeeping job blocks it too
A._running_jobs = lambda: {A.CRON_BOOKKEEPING_JOB: 9}
sent.clear(); spawned.clear()
ag.on_message({"text": "run today's bookkeeping", "channel": "D5", "ts": "3003.0", "user": A3})
assert not spawned and "bookkeeping already running (a scheduled run)" in sent[0], sent
A._running_jobs = _rj_real
print("PASS bookkeeping blocks across threads and against cron; focused runs do not")

# _running_jobs sees run-thread.sh and run-consensus.sh too
class _StubT(_StubQ):
    @staticmethod
    def run(*a, **k):
        return type("R", (), {"stdout":
            f"111 bash /x/deploy/run-thread.sh run-{A1}-1-0 {A1} D1 1.0 1-0 prompt\n"
            f"112 bash /x/deploy/run-thread.sh run-{A1}-1-0 {A1} D1 1.0 1-0 \n"
            f"222 bash /x/deploy/run-consensus.sh run-{A2}-2-0 prompt\n"
            "333 bash /x/deploy/run-agent.sh bookkeep prompt\n",
            "returncode": 0})()
A.subprocess = _StubT
assert A._running_jobs() == {f"run-{A1}-1-0": 111, f"run-{A2}-2-0": 222, "bookkeep": 333}, A._running_jobs()
print("PASS _running_jobs parses every runner, one entry per job")

# consensus goes to the consensus runner plus the notifier
A.subprocess = _StubQ
A._running_jobs = lambda: {}
sent.clear(); spawned.clear()
ag.on_message({"text": "run consensus code last week's bills", "channel": "D5", "ts": "3100.0", "user": A1})
assert len(spawned) == 2 and spawned[0][2].endswith("deploy/run-consensus.sh"), spawned
assert spawned[1][2].endswith("deploy/notify-queried.sh") and spawned[1][4] == A1
assert spawned[0][4].startswith("code last week's bills\n"), "consensus prefix not stripped"
assert "consensus" in sent[0], sent
sent.clear(); spawned.clear()
ag.on_message({"text": "run consensus", "channel": "D5", "ts": "3101.0", "user": A1})
assert not spawned and "needs a task" in sent[0]
A._running_jobs = _rj_real
print("PASS consensus prefix: two-agent runner plus notifier; empty task refused")
print("all one-agent-per-thread checks passed")


# 20. the outstanding-items list: any admin, through `run`, routed to the
#     skill and told to post in the channel; a report noun keeps the request
#     with its report instead.
for t in ("give me all outstanding items", "what is still outstanding",
          "list the open queries and manual actions",
          "which questions have not been answered yet",
          "everything outstanding please", "what hasn't been done",
          "outstanding items since 1 Sep"):
    assert A.outstanding_gate(t), t
for t in ("send me the outstanding bills", "what deposits are open",
          "run today's bookkeeping", "outstanding invoices on the card account",
          "check the tax split on the Parent bills",
          "refresh the prepayments report"):
    assert not A.outstanding_gate(t), t
A.subprocess = _Stub
A._running_jobs = lambda: {}
sent.clear()
ag.on_message({"text": "run give me all outstanding items", "channel": "D1",
               "ts": "1900.0", "user": A3})
prompt = (tmp / f"last-prompt-{A3}.txt").read_text()
assert "OUTSTANDING ITEMS" in prompt and "outstanding-items" in prompt, "rules not attached"
assert "SCOPE: this is not the daily bookkeeping run" in prompt, "focused rules dropped"
assert "#test-accounting (CTESTCHAN1)" in prompt.split("OUTSTANDING ITEMS")[-1]
for other in ("STANDING REPORT", "DOMAIN: bookkeeping"):
    assert other not in prompt, f"{other} attached to the outstanding list"
assert sent[0].startswith("started · outstanding items"), sent[0]
sent.clear()
ag.on_message({"text": "run send me the outstanding bills report", "channel": "D1",
               "ts": "1901.0", "user": A3})
prompt = (tmp / f"last-prompt-{A3}.txt").read_text()
assert "OUTSTANDING ITEMS" not in prompt, "the bills report was taken for the list"
assert sent[0].startswith("started · focused"), sent[0]
sent.clear()
ag.on_message({"text": "run bookkeep today and list the open queries", "channel": "D1",
               "ts": "1902.0", "user": A3})
prompt = (tmp / f"last-prompt-{A3}.txt").read_text()
assert "DOMAIN: bookkeeping" in prompt and "OUTSTANDING ITEMS" not in prompt, "bookkeeping did not win"
assert "TEST GROUP BOOKKEEPING RUN" in prompt and "full report in #test-accounting" in prompt
print("PASS outstanding items routed to the skill and the channel; reports and bookkeeping left alone")

# 21. standing reports: noun AND verb, never with bookkeeping
sent.clear()
ag.on_message({"text": "run send me the deposits report", "channel": "D1",
               "ts": "1950.0", "user": A3})
prompt = (tmp / f"last-prompt-{A3}.txt").read_text()
assert "STANDING REPORT" in prompt and "(Parent, Sub)" in prompt, "report rules or entities missing"
assert sent[0].startswith("started · report"), sent[0]
sent.clear()
ag.on_message({"text": "run bookkeep the office deposit invoice", "channel": "D1",
               "ts": "1951.0", "user": A3})
prompt = (tmp / f"last-prompt-{A3}.txt").read_text()
assert "STANDING REPORT" not in prompt and "DOMAIN: bookkeeping" in prompt
A._running_jobs = _rj_real
print("PASS standing reports routed on noun plus verb; bookkeeping wins")

# 22. the help is built from config too
assert "#test-accounting" in A.run_help() and "Parent" in A.run_help()
assert "Parent" in A.query_help()
print("PASS help text names the configured channel and entities")
print("ALL PASS")
