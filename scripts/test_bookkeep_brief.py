"""Offline checks for scripts/bookkeep_brief.py and scripts/bookkeep_extract.py.
No network, no Xero, no bank, no Claude.

Fakes stand in for the Slack and Gmail APIs, the feed commands and the
`claude` CLI; the ledger is a fixture and the group is the example config.
The tests prove the ledger match (by time; the counts are not a test), the
inbox's document count (a signature image or a certificate is not a
document), what is downloaded and what is not, that the briefing names every
message to work with what the run needs to write its ledger line and settle
it, that a failed step is named and the rest still run, that the folder is
emptied on every build, which reads get a second look, how a supplier's
rules/SUPPLIERS.md entry is found, and that an email whose body is the receipt
becomes intake with a PDF to attach.

    .venv/bin/python scripts/test_bookkeep_brief.py
"""
from __future__ import annotations

import base64
import json
import os
import pathlib
import sys
import tempfile
import time
from datetime import datetime, timezone

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "src"))
os.environ["AGENT_GROUP_CONFIG"] = str(REPO / "config" / "group.example.toml")
for k in ("AGENT_READONLY", "AGENT_SMALL_MODEL", "AGENT_QUERY_MODEL", "AGENT_RUN_ID"):
    os.environ.pop(k, None)

import bookkeep_brief as bb  # noqa: E402
import daily_chain as dc  # noqa: E402
from accounting_agent import config  # noqa: E402

TMP = pathlib.Path(tempfile.mkdtemp(prefix="test_bookkeep_brief_"))
FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("ok   " if cond else "FAIL ") + name + (f" - {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def ts(s: str) -> float:
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp()


# ------------------------------------------------------------ the ledger

LEDGER = """# ledger

## Entries

## Run 2026-10-08
2026-10-08 08:54 | slack | Sam Okafor | 1/1 | Example Cafe | USD 13.70 | OpCo US | posted, 4900
2026-10-08 09:10 | email | Northwind Logistics Inc | 2/1 | Northwind Data | ND-40417 | HoldCo | queried: which entity
2026-10-07 (already in xero) | slack | Priya Nandakumar 14:30 | 1/1 | Contoso Rides | 77120934 | OpCo EU | already-in-xero
2026-10-08 (run footer) | n/a | run complete | 9 bills | - | - | all | PASS
"""
rows = bb.ledger_rows(LEDGER)
check("slack and email lines are read, run lines are not", len(rows) == 3 and {r["source"] for r in rows} == {"slack", "email"})
check("a time in the sender column is read when the date column has none",
      any(abs(s - ts("2026-10-07T14:30:00")) < 1 for r in rows for s in r["stamps"]))


def msg(source, when, ndocs):
    t = ts(when)
    return {"source": source, "stamps": [t], "ts": t, "ndocs": ndocs}


check("a message with its line, all bookkept, is done", bb.ledger_state(msg("slack", "2026-10-08T08:55:30", 1), rows)[0] == "done")
check("outside the tolerance it is not in the ledger",
      bb.ledger_state(msg("slack", "2026-10-08T09:30:00", 1), rows) == ("new", "not in the ledger"))
check("an invoice with its receipt, written 2/1, is done: the counts are not a test",
      bb.ledger_state(msg("email", "2026-10-08T09:10:00", 2), rows)[0] == "done")
check("the source has to match", bb.ledger_state(msg("email", "2026-10-08T08:54:00", 1), rows)[0] == "new")


# ------------------------------------------------------------ fake Slack and Gmail

ME, PRIYA, BOT_DM, PRIYA_DM = "UBOT", "U0PRIYA", "DBOT", "D0PRIYA"
NOW = time.time()
SINCE = NOW - 20 * 3600
NEW_TS = NOW - 3600                      # new since the last session
OLD_DONE = ts("2026-10-08T08:54:00")     # in the ledger


def slack_get(method, **p):
    if method == "auth.test":
        return {"user_id": ME}
    if method == "users.info":
        return {"user": {"real_name": {"U0SAM": "Sam Okafor", PRIYA: "Priya Nandakumar"}[p["user"]]}}
    if method == "conversations.replies":
        return {"messages": [{"ts": p["ts"], "user": ME, "bot_id": "B1", "text": "chase"},
                             {"ts": f"{NOW - 1800:.6f}", "user": PRIYA, "text": "it was the client dinner, OpCo EU",
                              "files": [{"id": "F9", "name": "receipt.jpg", "size": 9000,
                                         "url_private_download": "https://files/F9"}]}]}
    raise AssertionError(method)


def slack_pages(method, key, **p):
    if method == "conversations.list":
        return [{"id": "D0SAM", "user": "U0SAM"}, {"id": PRIYA_DM, "user": PRIYA}, {"id": BOT_DM, "user": "USLACKBOT"}]
    if p["channel"] == "D0SAM":
        return [{"ts": f"{NEW_TS:.6f}", "user": "U0SAM", "text": "lunch, team offsite",
                 "files": [{"id": "F1", "name": "PXL 2026.jpg", "size": 120_000, "url_private_download": "https://files/F1"}]},
                {"ts": f"{OLD_DONE:.6f}", "user": "U0SAM", "text": "",
                 "files": [{"id": "F0", "name": "IMG.jpg", "size": 1, "url_private_download": "https://files/F0"}]},
                {"ts": f"{NOW - 600:.6f}", "user": "U0SAM", "text": "is the cafe one in?"}]
    if p["channel"] == PRIYA_DM:
        return [{"ts": f"{NOW - 30 * 86400:.6f}", "user": ME, "bot_id": "B1", "text": "chase: the dinner receipt",
                 "latest_reply": f"{NOW - 1800:.6f}", "reply_users": [PRIYA]}]
    raise AssertionError(p)


def b64(s: bytes) -> str:
    return base64.urlsafe_b64encode(s).decode()


GMAIL = {
    "m1": {"id": "m1", "threadId": "t00000000001", "internalDate": str(int((NOW - 7200) * 1000)),
           "labelIds": ["INBOX"],
           "payload": {"headers": [{"name": "From", "value": "Northwind Logistics Inc <billing@northwind.example>"},
                                   {"name": "Subject", "value": "Invoice ND-40488"},
                                   {"name": "Date", "value": "Thu, 08 Oct 2026 15:34:00 +0000"}],
                       "parts": [{"mimeType": "text/plain", "body": {"data": b64(b"Please find your invoice. Terms 30 days.")}},
                                 {"mimeType": "application/pdf", "filename": "ND-40488.PDF",
                                  "body": {"attachmentId": "a1", "size": 88000}},
                                 {"mimeType": "image/png", "filename": "image001.png",
                                  "headers": [{"name": "Content-Disposition", "value": "inline; filename=image001.png"}],
                                  "body": {"attachmentId": "a2", "size": 4000}},
                                 {"mimeType": "application/pkcs7-signature", "filename": "smime.p7s",
                                  "body": {"attachmentId": "a3", "size": 5000}}]}},
    "m2": {"id": "m2", "threadId": "t00000000002", "internalDate": str(int((NOW - 5000) * 1000)), "labelIds": ["INBOX"],
           "payload": {"headers": [{"name": "From", "value": "Fabrikam Workspace <noreply@fabrikam.example>"},
                                   {"name": "Subject", "value": "Your invoice is ready"}],
                       "mimeType": "text/html",
                       "body": {"data": b64(b"<html><body><p>Your invoice is in the portal.</p></body></html>")}}},
}


def gmail_get(token, path, **q):
    if path == "messages":
        return {"messages": [{"id": "m1"}] + ([{"id": "m2"}] if "has:attachment" not in q["q"] else [])}
    if path.startswith("messages/") and "/attachments/" in path:
        return {"data": b64(b"%PDF-1.4 northwind")}
    return GMAIL[path.split("/")[1]]


COMMANDS: list[list[str]] = []


def quiet(cmd, timeout=1800):
    COMMANDS.append(cmd)
    joined = " ".join(cmd)
    if "billfeed.py refresh" in joined:
        return 1, "Traceback\nXeroError: 429 daily limit"
    if "bankfeed.py status" in joined:
        return 0, "holdco          4 outstanding\n"
    if "xero-check" in joined:
        pathlib.Path(cmd[cmd.index("--out") + 1]).write_text("[PAID IN XERO] ...\n")
        return 0, "PAID IN XERO 2 · LIKELY 1 · NOTHING FOUND 1\n"
    if "outstanding.py list" in joined:
        return 0, "OI-0101 queried admin | northwind-entity | which entity bears the freight charge\n"
    if "investigate_query.py due" in joined:
        return 0, "nothing due\n"
    return 0, ""


def slack_download(m, f):
    p = bb.unique(bb.DOCS / f"S-x-{bb.safe(m['who'].split()[0])}-{bb.safe(f['name'])}")
    p.write_bytes(b"jpg")
    return p


ledger_file = TMP / "LEDGER.md"
ledger_file.write_text(LEDGER)
dc.slack_get, dc.slack_pages, dc.gmail_get = slack_get, slack_pages, gmail_get
dc.LEDGER, dc.GATES = ledger_file, TMP / "gates"
dc.write_json(dc.GATES / "bookkeep.json", {"session_started": SINCE, "feed": ["x"]})
dc.load_env = lambda: None
FEED_LINE = {"entity": "holdco", "account": "USD Main", "date": "2026-10-08", "payee": "Example Cafe",
             "direction": "spent", "ccy": "USD", "amount": 13.70, "marker": "rev:1a2b3c4d",
             "raw": "2026-10-08 | Example Cafe | spent | USD 13.70 | | rev:1a2b3c4d"}
dc.feed_lines = lambda folder=None: [FEED_LINE]          # never the server's real feeds
dc.bill_feed = lambda folder=None: {k: [] for k in dc.shorts()}
bb.quiet, bb.slack_download = quiet, slack_download
bb.DIR = TMP / "briefings" / "bookkeep"
bb.DOCS, bb.BRIEF = bb.DIR / "docs", bb.DIR / "brief.md"
import accounting_agent.gmail_auth as ga  # noqa: E402
ga.get_access_token = lambda: "tok"

bb.DIR.mkdir(parents=True)
(bb.DIR / "left-over.txt").write_text("yesterday")
path = bb.build(refresh_bank=False, extract=False)      # the reads are tested on their own, below
brief = path.read_text()
docs = sorted(p.name for p in bb.DOCS.iterdir())

check("the folder is emptied before every build", not (bb.DIR / "left-over.txt").exists())
check("the bank feed is not refreshed twice when the gate has done it",
      not any("refresh" in c and "bankfeed.py" in " ".join(c) for c in COMMANDS))
check("a failed step is named with the command to run instead, and the rest still run",
      "## not done here" in brief and "the bill feed refresh failed" in brief and "billfeed.py refresh all" in brief
      and "PAID IN XERO 2" in brief, brief[-600:])
check("the new Slack document is downloaded and the one in the ledger is not",
      any("PXL_2026.jpg" in d for d in docs) and not any("IMG.jpg" in d for d in docs), str(docs))
check("a thread reply with a file, to an old chase, is in the intake",
      any("receipt.jpg" in d for d in docs) and "in thread" in brief, str(docs))
check("the inbox invoice is downloaded with its body, the signature and the certificate are not",
      any(d.endswith("ND-40488.PDF") for d in docs) and any(d.endswith("-body.txt") for d in docs)
      and not any("image001" in d or "p7s" in d for d in docs), str(docs))
check("an email with no ledger line is in the intake", "Northwind Logistics Inc <billing@northwind.example>" in brief
      and "not in the ledger" in brief)
check("each message to work carries its ledger when and its settle reference",
      "ledger when " in brief and "email thread t00000000001" in brief and "slack D0SAM ts" in brief, brief[:1500])
check("a message with no document since the last session is listed with its words",
      "is the cafe one in?" in brief and "Your invoice is in the portal." in brief)
check("the thread replies since the last session are listed", "it was the client dinner, OpCo EU" in brief)
check("the done messages are counted, not listed", "already in the ledger, nothing to do: 1 Slack messages" in brief)
check("the register is saved for a grep, not printed, and the investigations due are in the briefing",
      "OI-0101 queried admin" in (bb.DIR / "register.txt").read_text() and "OI-0101" not in brief
      and "register.txt" in brief and "nothing due" in brief)
check("the xero-check file is saved beside the briefing and named for reading",
      (bb.DIR / "xero-check.txt").exists() and "--from " in brief)
check("a feed line new since the last session is listed", "new since the last session (1)" in brief
      and "rev:1a2b3c4d" in brief)
check("the open bills are counted per entity by short name", "HoldCo 0 · OpCo US 0 · OpCo EU 0" in brief, brief[-800:])
check("the briefing is short", len(brief) < 6000, str(len(brief)))
check("a fresh briefing is fresh", bb.is_fresh())
bb.wipe()
check("wipe leaves nothing behind", not bb.DIR.exists())

# No accounting inbox in config: the inbox is not swept, and nothing says it failed.
real_company = config.company
config.company = lambda key=None, default=None: "" if key == "accounting_inbox" else real_company(key, default)
brief = bb.build(refresh_bank=False, extract=False).read_text()
config.company = real_company
check("with no accounting inbox the inbox is neither swept nor named as failed",
      "Northwind" not in brief and "inbox sweep failed" not in brief and "sweep of the Slack app" in brief, brief[:800])
bb.wipe()


# ------------------------------------------------------------ the reads

import bookkeep_extract as bx  # noqa: E402

check("a fenced answer is parsed", bx.parse_answer('```json\n{"supplier": "Example Cafe"}\n```') == {"supplier": "Example Cafe"})
check("prose around the JSON is skipped", bx.parse_answer('Here: [{"a": 1}] done') == [{"a": 1}])
check("no JSON is None", bx.parse_answer("I could not open it") is None)

MSG = ts("2026-10-09T09:00:00")
ok_receipt = {"document_type": "receipt", "supplier": "Example Cafe", "invoice_date": "2026-10-08", "currency": "USD",
              "net": None, "tax": None, "gross": 13.7, "legibility": {"supplier": "ok", "amount": "ok", "date": "ok"},
              "notes": "upright, one page"}
check("a read that holds together needs no second look", bx.second_look(ok_receipt, MSG, False) == ([], []))
f, w = bx.second_look({**ok_receipt, "notes": "the photo is rotated 90 degrees"}, MSG, False)
check("a rotated photo gets a second look at supplier, total and date", f == ["supplier", "total", "date"], str(f))
for calm in ("Single page, upright, not rotated or at an angle.", "Upright, no rotation needed.",
             "The page is not skewed and is not upside down."):
    check(f"the reader saying all is well is no reason for a second look: {calm}",
          bx.second_look({**ok_receipt, "notes": calm}, MSG, False) == ([], []))
f, w = bx.second_look({**ok_receipt, "invoice_date": "2026-09-08"}, MSG, False)
check("a receipt dated a month before its message gets a second look at the date", f == ["date"], str(f))
check("an invoice dated the month before is normal, no second look",
      bx.second_look({**ok_receipt, "document_type": "invoice", "invoice_date": "2026-09-30",
                      "payment_terms": "Net 30"}, MSG, False) == ([], []))
f, w = bx.second_look({**ok_receipt, "net": 11.0, "tax": 1.0, "gross": 13.7}, MSG, False)
check("a net and tax that do not make the gross get a second look", "net" in f and "total" in f, str(f))
f, w = bx.second_look({**ok_receipt, "legibility": {"amount": "illegible"}}, MSG, False)
check("an illegible amount gets a second look at the total", f == ["total"], str(f))
f, w = bx.second_look({**ok_receipt, "document_type": "invoice", "due_date": None, "payment_terms": None,
                       "payment_evidence": None}, MSG, False)
check("an unpaid invoice with no due date and no terms gets a second look at both",
      f == ["due date", "payment terms"], str(f))

SUPPLIERS_MD = """# Supplier database

## Entry format

```
### <Supplier name as on the invoice>

- **Aliases / bank payee names:** -
```

## Entries

> **Quoted Illustration Bakery**
>
> - **Recognised in:** HOLDCO

### Example Cafe

- **Aliases / bank payee names:** EXAMPLECAFE, Example Coffee Bar
- **Recognised in:** per employing entity
- **Default account:** Staff meals
- **Tax treatment:** gross, no tax
- **Prepays:** -

### Sample Financial Insurance

- **Aliases / bank payee names:** SAMPLE FIN
- **Recognised in:** HOLDCO
- **Default account:** Hosting

### Tailspin Leasing

- **Aliases / bank payee names:** Tailspin Financial Services
- **Recognised in:** OPCO_US
- **Default account:** Equipment lease
- **Special handling:** two documents per charge: the schedule and the
  invoice; book the invoice
"""
got = bx.supplier_rows("EXAMPLECAFE STORE 12", SUPPLIERS_MD)
check("an entry is found by an alias, one line with its fields, a blank field left out",
      got == ["Example Cafe · recognised per employing entity · account Staff meals · tax gross, no tax"], str(got))
got = bx.supplier_rows("Tailspin Financial Services LLC", SUPPLIERS_MD)
check("a word every finance firm shares does not match another supplier; a wrapped field is joined",
      len(got) == 1 and got[0].startswith("Tailspin Leasing") and "book the invoice" in got[0], str(got))
check("the format block and the quoted illustrations are not entries",
      bx.supplier_rows("Quoted Illustration Bakery", SUPPLIERS_MD) == []
      and [e["name"] for e in bx.supplier_entries(SUPPLIERS_MD)] == ["Example Cafe", "Sample Financial Insurance",
                                                                       "Tailspin Leasing"])
check("2500 and 2500.0 are the same total", bb.same(2500, 2500.0) and not bb.same(2500, 2499) and bb.same("Cafe", "cafe"))

# The CLI call itself, against a stand-in for `claude` that echoes what it was given.
fake = TMP / "fake-claude"
fake.write_text("#!/usr/bin/env python3\nimport json, os, sys\n"
                "a = sys.argv\n"
                "print(json.dumps({'result': '```json\\n' + json.dumps({'document_type': 'receipt', 'supplier': 'Example Cafe', "
                "'readonly': os.environ.get('AGENT_READONLY'), 'model': a[a.index('--model') + 1], "
                "'agent': a[a.index('--agent') + 1], 'prompt': a[a.index('-p') + 1]}) + '\\n```', "
                "'total_cost_usd': 0.0012, 'session_id': 'sid-1'}))\n")
fake.chmod(0o755)
bx.CLAUDE_BIN, bx.COST_DIR = str(fake), TMP / "briefings_cost"
small = config.briefing()["reader_model"]
ans, err = bx.call(TMP / "doc.jpg", "", small, 0.5, TMP, 60)
check("the read is the invoice-extract agent on the [briefing] reader model, read-only, given the path",
      err == "" and ans["agent"] == "invoice-extract" and ans["model"] == config.model("small")
      and ans["readonly"] == "1" and ans["prompt"].endswith("doc.jpg"), str(ans))
costs = [json.loads(p.read_text()) for p in (TMP / "briefings_cost").glob("*/*.json")]
check("every call's cost is recorded for the run's bracket", len(costs) == 1 and costs[0]["total_cost_usd"] == 0.0012
      and costs[0]["job"] == "bookkeep-brief")

# The whole pass inside a build: canned reads, no CLI, no Xero.
GMAIL["m3"] = {"id": "m3", "threadId": "t00000000003", "internalDate": str(int((NOW - 4000) * 1000)),
               "labelIds": ["INBOX"],
               "payload": {"headers": [{"name": "From", "value": "Contoso Rides <receipts@contoso.example>"},
                                       {"name": "Subject", "value": "Your Thursday trip"}],
                           "mimeType": "text/html",
                           "body": {"data": b64("<p>Total €21.60</p><p>Trip for Priya Nandakumar, Harbour St</p>".encode())}}}
_old_gmail_get = gmail_get


def gmail_get3(token, path, **q):
    if path == "messages" and "has:attachment" not in q["q"]:
        return {"messages": [{"id": "m1"}, {"id": "m2"}, {"id": "m3"}]}
    return _old_gmail_get(token, path, **q)


dc.gmail_get = gmail_get3
CANNED = {
    "PXL": {"document_type": "receipt", "supplier": "Example Cafe", "invoice_date": "2026-10-08", "currency": "USD",
            "gross": 17.2, "notes": "photographed at an angle", "printed_notes": ["Eat in", "Card ending 5521"]},
    "receipt.jpg": {"document_type": "receipt", "supplier": "Le Petit Bistro", "invoice_date": "2026-10-08",
                    "currency": "EUR", "gross": 96.0, "printed_notes": ["TVA 10% 8.73"]},
    "ND-40488": {"document_type": "invoice", "supplier": "Northwind Logistics Inc", "invoice_number": "ND-40488",
                 "invoice_date": "2026-09-30", "currency": "USD", "gross": 2640.0, "payment_terms": "Net 30",
                 "printed_notes": ["Account NWD-7781", "September 2026 freight, 14 pallets"]},
    "Contoso": {"document_type": "receipt", "supplier": "Contoso Rides", "invoice_date": "2026-10-08",
                "currency": "EUR", "gross": 21.6, "printed_notes": ["Trip for Priya Nandakumar, Harbour St"]},
    "Fabrikam": {"document_type": "not a bill", "supplier": "Fabrikam Workspace",
                 "printed_notes": ["Your invoice is in the portal."]},
}
CALLS = []


def fake_call(path, brief, model, budget, scratch, timeout):
    CALLS.append((path.name, brief[:40], model))
    x = next((dict(v) for k, v in CANNED.items() if k in path.name), {"document_type": "unreadable"})
    if brief:
        x.update({"supplier": "Example Coffee Bar", "notes": "upright copy read",
                  "recheck": {"supplier": "EXAMPLE COFFEE BAR"}})
    return x, ""


def fake_lookups(items, history_days, say=print):
    for i in items:
        i["in_xero"] = (["HoldCo bill 2026-09-30 · Northwind Logistics Inc · USD 2,640.00 · ND-40488 [number]"]
                        if i["x"].get("invoice_number") == "ND-40488" else [])
        i["history"] = (["HoldCo · 5 bills since 9 Apr · coded Freight (6420) x5"]
                        if "Northwind" in str(i["x"].get("supplier")) else [])


bx.call, bx.xero_lookups = fake_call, fake_lookups
COMMANDS.clear()
brief = bb.build(refresh_bank=False, extract=True).read_text()
docs = sorted(p.name for p in bb.DOCS.iterdir())
second = config.briefing()["second_look_model"]
check("every document is read once, and only the angled photo again, on the second-look model",
      sum(1 for c in CALLS if not c[1]) == 5 and [(c[1][:30], c[2]) for c in CALLS if c[1]] == [
          ("second look: supplier, total, ", second)], str(CALLS))
check("the second read stands in the briefing and names what the first read had",
      "Example Coffee Bar" in brief and "the first read had supplier Example Cafe" in brief
      and "EXAMPLE COFFEE BAR" in brief, brief[:3000])
check("every printed note is in the briefing", "Card ending 5521" in brief and "Account NWD-7781" in brief)
check("a document already in Xero is shown with its record", "in Xero? HoldCo bill 2026-09-30" in brief)
check("the supplier's coding history is shown", "Freight (6420) x5" in brief)
check("an email whose body is a receipt becomes intake, rendered to a PDF to attach",
      "receipt is the email itself" in brief and any(d.endswith("-receipt.pdf") for d in docs), str(docs))
check("a notice that is not a bill stays a message, not intake",
      "Your invoice is in the portal." in brief.split("## new since the last session")[-1])
check("each read is kept whole beside the briefing", any((bb.DIR / "extract").glob("*PXL*.json")))
pdf = next(p for p in bb.DOCS.iterdir() if p.name.endswith("-receipt.pdf"))
check("the receipt PDF is a real PDF", pdf.read_bytes()[:5] == b"%PDF-" and pdf.stat().st_size > 1000)
bb.wipe()

print(f"\n{'FAIL' if FAILS else 'PASS'}: {len(FAILS)} failed")
sys.exit(1 if FAILS else 0)
