"""Offline checks for scripts/reconcile_list.py: the list keeps only what the
bank reconcile check holds open on a verified account, ambiguous lines read as
"one of these", wrong matches get their own heading, a match that does not fit
goes to the admin and not the bookkeeper, the DM goes once a day to the
configured member, and a failed check writes nothing. Fictional group from
config/group.example.toml, fake data; no network, nothing written outside a
temporary folder.

    .venv/bin/python scripts/test_reconcile_list.py
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
import types

REPO = pathlib.Path(__file__).resolve().parents[1]
TMP = pathlib.Path(tempfile.mkdtemp(prefix="test_reconcile_list_"))
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "src"))
os.environ["AGENT_GROUP_CONFIG"] = str(REPO / "config" / "group.example.toml")
os.environ["BRC_OUT_DIR"] = str(TMP / "out")
import reconcile_list as rl  # noqa: E402
import bank_reconcile_check as brc  # noqa: E402

rl.OUT_JSON, rl.OUT_MD, rl.DM_STATE = TMP / "list.json", TMP / "list.md", TMP / "dm.json"
FAILS: list[str] = []
KEY = "holdco"
XERO = "Revolut GBP Main"


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("ok   " if cond else "FAIL ") + name + (f" - {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def mv(n: int, amount: float, payee: str, status: str, goes: dict | None = None, *, verified: bool = True,
       state: str = "OPEN", siblings: list | None = None, day: str = "2026-10-05") -> dict:
    return {"id": f"rev:{n:08d}", "entity": KEY, "bank": "Revolut GBP", "xero": XERO, "date": day,
            "currency": "GBP", "amount": amount, "payee": payee, "status": status, "goes_to": goes,
            "confidence": "high" if goes else "", "verified": verified, "state": state, "in_window": True,
            "siblings": siblings or [], "words": sorted(brc.words(payee))}


def doc(number: str, contact: str, due: float, day: str = "2026-10-01") -> dict:
    return {"entity": KEY, "type": "ACCPAY", "number": number, "reference": "", "contact": contact, "date": day,
            "currency": "GBP", "due": due, "total": due}


def goes(*docs: dict) -> dict:
    return {"kind": "document", "text": "bill", "documents": list(docs)}


bakery = mv(1, -200.0, "Corner Bakery", "TO MATCH", goes(doc("CB-1", "Corner Bakery", 200.0)))
market_a = mv(2, -30.0, "Daily Market", "AMBIGUOUS", goes(doc("DM-1", "Daily Market", 30.0)), siblings=["rev:00000003"])
market_b = mv(3, -30.0, "Daily Market", "RECONCILED", None, state="RECONCILED", day="2026-10-03")
offline = mv(4, -55.0, "Harbour Cafe", "TO MATCH", goes(doc("HC-1", "Harbour Cafe", 55.0)), verified=False)
wrong = mv(5, -80.0, "Acme Hosting", "SUSPECT WRONG MATCH",
           {**goes(doc("AH-1", "Acme Hosting", 80.0)), "kind": "wrong match",
            "taken_by": {"xero": XERO, "line": "3 Oct GBP 80.00 payment (Acme Hosting)"}})
reconciled = mv(6, -12.0, "Pet Supplies", "RECONCILED", None, state="RECONCILED")
checked = {"built": "2026-10-07T08:14Z", "entities": {KEY: {
    "movements": [m for m in (bakery, market_a, offline, wrong) if m["state"] == "OPEN"],
    "_all": [bakery, market_a, market_b, offline, wrong, reconciled],
    "suspects": [
        {"kind": "payment does not fit its bill", "why": "bank amount GBP 90.00 against a EUR 100.00 payment; x",
         "xero": XERO, "line": "1 Oct GBP 90.00 payment (Gadget Store)", "bill": "GS-1", "contact": "Gadget Store",
         "doc_currency": "EUR", "doc_amount": 100.0, "entity": KEY, "date": "2026-10-01", "amount": -90.0,
         "currency": "GBP", "payee": "Gadget Store"},
        {"kind": "line matched to the wrong bill", "why": "w", "xero": XERO, "entity": KEY, "date": "2026-10-02",
         "amount": -20.0, "currency": "GBP", "payee": "Pet Supplies", "bill": "PS-0", "line": "2 Oct payment",
         "likely_doc": doc("PS-1", "Pet Supplies", 20.0, "2026-09-30")}]}}}


def item(line: dict, text: str, number: str) -> dict:
    return {"entity": KEY, "date": line["date"], "kind": "bill", "text": text, "refs": f"{line['payee']} · {number}",
            "line": {"entity": KEY, "currency": "GBP", "date": line["date"], "amount": line["amount"],
                     "payee": line["payee"]}}


candidates = [item(bakery, "reconcile the Corner Bakery line to its bill", "CB-1"),
              item(market_a, "reconcile the Daily Market line to its bill", "DM-1"),
              item(market_b, "reconcile the 3 Oct Daily Market line to its bill", "DM-0"),
              item(offline, "reconcile the Harbour Cafe line to its bill", "HC-1"),
              item(reconciled, "reconcile the Pet Supplies line to its bill", "PS-9"),
              item(bakery, "reconcile the same line to another bill", "XX-9")]
candidates[5]["line"] = {**candidates[5]["line"], "amount": -999.0}      # a line the check does not have

items, wrongs, held, dropped = rl.apply_check(candidates, checked)
texts = [i["text"] for i in items]
check("a line the check holds TO MATCH on a verified account is kept", any("Corner Bakery" in t for t in texts), str(texts))
check("an ambiguous line is kept and reads as one of these",
      any("Daily Market" in i["text"] and "One of these" in i["text"] for i in items), str(texts))
check("a line the check calls reconciled is dropped, with the reason",
      any("Pet Supplies" in d and "reconciled in Xero" in d for d in dropped), str(dropped))
check("a line on an account that does not tie is dropped from the list and held for the admin",
      any("Harbour Cafe" in d and "does not tie" in d for d in dropped)
      and any("Harbour Cafe" in h["line"] and "does not tie" in h["why"] for h in held), str((dropped, held)))
check("a line the check has no record of is dropped", any("another bill" in d for d in dropped), str(dropped))
check("a line open in the check and paired with a posted bill it holds TO MATCH goes under the wrong-match heading",
      any("Acme Hosting" in w["text"] and "Undo that match" in w["text"] for w in wrongs), str(wrongs))
check("an open bill that fits a line matched elsewhere is a wrong match with its bill named",
      any("Pet Supplies" in w["text"] and "PS-1" in w["refs"] for w in wrongs), str(wrongs))
check("a match that does not fit, with no better bill, goes to the admin and not the bookkeeper",
      any(h["status"] == "check the match" and "Gadget Store" in h["line"] for h in held)
      and not any("Gadget Store" in w["text"] for w in wrongs), str(held))

block = rl.manual_block(items + wrongs, "7 Oct 2026, 09:14 UTC", held)
check("the manual block names the as-at time and the wrong matches under their own heading",
      block.startswith("*reconcile on the bank feed*\nas at 7 Oct 2026, 09:14 UTC") and "*matched to the wrong bill*" in block, block)
check("the admin-only lines are under their own heading", "*for the admin, not sent to the bookkeeper*" in block)
dm = rl.dm_text(items + wrongs, "7 Oct 2026, 09:14 UTC")
check("the DM carries the list but never the admin's lines", "Corner Bakery" in dm and "Gadget Store" not in dm
      and "for the admin" not in dm, dm)
check("an empty list says none", rl.dm_text([], "now").endswith("none"))

# ---- the pairing from Xero's side ------------------------------------------------------
brc.BANK_MAP[KEY] = {"Revolut GBP": XERO}
line = {"entity": KEY, "account": "GBP Main", "currency": "GBP", "date": "2026-10-05", "payee": "Corner Bakery",
        "amount": -200.0}
check("a Xero account is the line's account only through the configured mapping",
      rl.same_account(KEY, XERO, "GBP", line) and not rl.same_account(KEY, "Revolut USD Main", "GBP", line)
      and not rl.same_account(KEY, XERO, "USD", line))
entry = {"spent": True, "date": "2026-10-07", "amount": 200.0, "account": XERO, "currency": "GBP"}
check("an entry is paired with the line of its amount within the leg window",
      rl.find_line(KEY, entry, [line], set()) is line and rl.find_line(KEY, {**entry, "date": "2026-10-20"}, [line], set()) is None
      and rl.find_line(KEY, {**entry, "amount": 200.01}, [line], set()) is None)

bill = {"id": "b1", "status": "OPEN", "state": "MATCH IN XERO", "supplier": "Corner Bakery", "currency": "GBP",
        "due_amount": 200.0, "date": "2026-10-01", "number": "CB-1", "reference": ""}
store = {"entities": {KEY: {"bills": [bill, {**bill, "id": "b2", "number": "CB-2", "due_amount": 150.0}]}}}
xero = {KEY: {"banks": [{"name": XERO, "currency": "GBP"}], "entries": [], "payments": []}}
built = rl.build(store, {"b1": line, "b2": {**line, "amount": -170.0, "payee": "Corner Bakery 2"}}, [line], xero)
check("a bill the entity's own bank paid is listed; a line at another amount in one currency is not",
      len(built) == 1 and "CB-1" in built[0]["refs"], str(built))

# ---- the DM: once a day, to the configured member ----------------------------------------
posts: list[tuple] = []


class FakeClient:
    def __init__(self, token): pass
    def chat_postMessage(self, channel, text): posts.append((channel, text))


sys.modules["slack_sdk"] = types.SimpleNamespace(WebClient=FakeClient)
sys.modules["slack_sdk.errors"] = types.SimpleNamespace(SlackApiError=Exception)
os.environ["SLACK_BOT_TOKEN"] = "x"
check("no recipient configured: nothing is sent", "blank" in rl.send_dm(items, as_of="now") and not posts)
os.environ["AGENT_GROUP_CONFIG"] = str(TMP / "notify.toml")
(TMP / "notify.toml").write_text(pathlib.Path(REPO / "config" / "group.example.toml").read_text().replace(
    'unpaid_notify = ""', 'unpaid_notify = "U0000000001"'))
config = rl.config
config.reload()
msg = rl.send_dm(items, as_of="now")
check("the DM goes to unpaid_notify when reconcile_notify is blank", posts and posts[0][0] == "U0000000001", msg)
n = len(posts)
check("the same list again the same day is not sent twice", "already have" in rl.send_dm(items, as_of="now") and len(posts) == n)
(TMP / "notify.toml").write_text((TMP / "notify.toml").read_text().replace('reconcile_notify = ""', 'reconcile_notify = "U0000000002"'))
config.reload()
rl.DM_STATE.write_text("{}")
rl.send_dm(items, as_of="now")
check("reconcile_notify wins over unpaid_notify", posts[-1][0] == "U0000000002", str(posts[-1]))

# ---- a failed check writes nothing and sends nothing ---------------------------------------
def boom(*a, **k):
    raise RuntimeError("Xero would not answer")


brc.run_check = boom
rl.OUT_JSON.unlink(missing_ok=True)
posts.clear()
import contextlib, io  # noqa: E402
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rc = rl.cmd_run(True, True, False)
check("a check that fails: exit 1, no list, no DM", rc == 1 and not rl.OUT_JSON.exists() and not posts
      and "Nothing written" in buf.getvalue(), buf.getvalue())

# The register's channel post carries the reconcile section beside the other
# manual items and the queries, under their own headings.
import outstanding  # noqa: E402
_rec = outstanding.reconcile_lines
outstanding.reconcile_lines = lambda: ["• statement line goes to bill RL-9"]
try:
    chunks = outstanding.slack_chunks(
        [{"domain": "bookkeeping", "kind": "queried", "text": "which entity", "refs": "RL-8", "id": "OI-0901",
          "with": "admin", "raised": "2026-03-02", "key": "q-rl-8"}], "03 Mar 2026")
    body = "\n".join(chunks)
    check("the channel post renders the reconcile section with the queries",
          f"*Bookkeeping - {outstanding.RECONCILE_LABEL}*" in body and "queried - could not answer" in body, body)
finally:
    outstanding.reconcile_lines = _rec

print(f"\n{'FAIL' if FAILS else 'PASS'}: {len(FAILS)} failed")
sys.exit(1 if FAILS else 0)
