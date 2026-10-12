"""Offline checks for the three tiers of the bank reconcile check: what an
open bank line goes to, by code when the exact amount is unique or a
discriminator separates a repeat, then by the bank-line-match agent, every
answer verified by code. Synthetic lines and documents for the fictional group
in config/group.example.toml, a fake agent; no Xero, no bank, no network,
nothing written.

    .venv/bin/python scripts/test_bank_line_match.py
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "src"))
os.environ["AGENT_GROUP_CONFIG"] = str(REPO / "config" / "group.example.toml")
os.environ["BRC_OUT_DIR"] = tempfile.mkdtemp(prefix="test_bank_line_match_")
import bank_reconcile_check as brc  # noqa: E402

# Two staff for the who-paid signal, and an agent-made bill number that carries
# the card holder's initials, the way a group would configure them.
brc._PEOPLE = {"names": {"Alex Sample": ["alex", "sample"], "Sam Tester": ["sam", "tester"],
                         "Kim Rowan": ["kim", "rowan"]},
               "initials": {"ST": "Sam Tester"}}
brc.INITIALS_NUMBER = re.compile(r"^[A-Z0-9]+-([A-Z]{2,4})-")

FAILS: list[str] = []
KEY = "holdco"
DAY = "2026-10-05"
ACCT = "Revolut GBP Main"


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("ok   " if cond else "FAIL ") + name + (f" - {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def line(i: int, amount: float, payee: str, who: str = "", day: str = DAY, ref: str = "") -> dict:
    return {"id": f"rev:{i:08d}:Revolut GBP", "entity": KEY, "bank": "Revolut GBP", "xero": ACCT, "date": day,
            "currency": "GBP", "amount": amount, "payee": payee, "alt": "", "who": who, "reference": ref,
            "orig_amount": None, "orig_currency": "", "source": "revolut api", "settled_at": f"{day}T12:00"}


def bill(i: int, amount: float, contact: str, text: str = "", number: str = "", day: str = DAY) -> dict:
    d = {"id": f"doc-{i}", "entity": KEY, "type": "ACCPAY", "number": number, "reference": "",
         "contact": contact, "date": day, "currency": "GBP", "base": "GBP", "rate": 1.0, "total": amount,
         "due": amount, "status": "AUTHORISED", "text": text, "direction": "spent"}
    d["words"] = sorted(brc.words(contact, number, "", text))
    brc._people_of(d, contact, "", text, number=number)
    return d


def run(moves: list[dict], docs: list[dict]) -> dict:
    out = brc.classify(KEY, moves, [], {KEY: docs}, {d["id"]: d for d in docs},
                       brc.Rates(docs, "2026-01-01"), {}, "2026-09-01", "2026-08-20")
    for m in out["movements"]:
        m["verified"] = True
    return out


def settle(out: dict, ask=None, agent: bool = True) -> dict:
    return brc.settle_tier3({KEY: {"out": out}}, write=False, agent=agent, say=lambda *a: None, ask=ask)


def by_id(out: dict) -> dict:
    return {m["id"]: m for m in out["movements"]}


def fake(answers_for):
    """A stand-in for the agent: answers_for(question) -> (pick index or a raw id, confidence)."""
    calls = []

    def ask(payload: str) -> dict:
        qs = json.loads(payload.split("\n", 1)[1])["lines"]
        calls.append(qs)
        out = []
        for q in qs:
            pick, conf = answers_for(q)
            if isinstance(pick, int):
                pick = q["candidates"][pick]["id"]
            out.append({"line_id": q["line_id"], "pick": pick, "confidence": conf, "reason": "test"})
        return {"answers": out, "error": "", "cost_usd": 0.0012, "usage": {"input_tokens": 900, "output_tokens": 80},
                "seconds": 0.1}
    ask.calls = calls
    return ask


# 1. two same-day GBP 25.00 lines at one cafe, different cardholders, two bills
# naming those two people (one in full, one by the initials in its number)
moves = [line(1, -25.0, "Harbour Cafe", "Alex Sample"), line(2, -25.0, "Harbour Cafe", "Sam Tester")]
docs = [bill(1, 25.0, "Harbour Cafe", "Dinner, Alex Sample"), bill(2, 25.0, "Harbour Cafe", "Dinner", "CAFE-ST-05OCT2026")]
out = run(moves, docs)
m = by_id(out)
check("who paid: the first line goes to the bill naming its cardholder",
      m[moves[0]["id"]]["status"] == "TO MATCH"
      and m[moves[0]["id"]]["goes_to"]["documents"][0]["number"] == "")
check("who paid: tier 2 by code", all(m[x["id"]].get("match_by") == "code" and m[x["id"]].get("match_how") == "who paid"
                                      for x in moves), str([(m[x["id"]].get("match_by"), m[x["id"]].get("match_how")) for x in moves]))
pair = {m[x["id"]]["who"]: m[x["id"]]["goes_to"]["documents"][0]["number"] for x in moves}
check("who paid: the second line goes to the bill whose number carries the initials",
      pair.get("Sam Tester") == "CAFE-ST-05OCT2026", str(pair))
check("who paid: initials ST read as Sam Tester", docs[1]["people"] == ["Sam Tester"], str(docs[1]["people"]))

# 2. the same with no cardholder: a tie code cannot break, tier 3
moves = [line(1, -25.0, "Harbour Cafe"), line(2, -25.0, "Harbour Cafe")]
docs = [bill(1, 25.0, "Harbour Cafe", "Dinner, Alex Sample"), bill(2, 25.0, "Harbour Cafe", "Dinner, Sam Tester")]
out = run(moves, docs)
m = by_id(out)
check("no cardholder: both lines tied for tier 3",
      all(m[x["id"]].get("_t3", {}).get("tied") and len(m[x["id"]]["_t3"]["candidates"]) == 2 for x in moves))
check("no cardholder: nothing matched by code", not any(m[x["id"]].get("goes_to") for x in moves))
ask = fake(lambda q: (0, "certain"))
lm = settle(out, ask)
check("tie: a 'certain' pick between twins is refused by code",
      all(m[x["id"]]["status"] == "NO DOCUMENT" and m[x["id"]]["awaiting"].get("refused") for x in moves),
      str([m[x["id"]].get("awaiting") for x in moves]))
check("tie: refused lines are awaiting information with both candidates",
      all(brc._label(m[x["id"]]) == "AWAITING INFORMATION" and len(m[x["id"]]["awaiting"]["candidates"]) == 2 for x in moves))
check("tie: one call for the run", len(ask.calls) == 1 and len(ask.calls[0]) == 2)

out = run(moves, docs)
settle(out, fake(lambda q: (None, "unsure")))
m = by_id(out)
check("agent unsure: awaiting information, candidates on the note",
      all(m[x["id"]]["status"] == "NO DOCUMENT" and "awaiting information, possible: Harbour Cafe" in m[x["id"]]["note"]
          for x in moves), m[moves[0]["id"]].get("note", ""))

# 3. an estimate against the actual charge, same vendor, within 1%
moves = [line(3, -958.01, "WAYFARER TRAVEL")]
docs = [bill(3, 966.30, "Wayfarer Travel", "Flight estimate", "WAYF-AS-02OCT2026", "2026-10-02")]
out = run(moves, docs)
m = by_id(out)[moves[0]["id"]]
check("estimate: not code-certain, a tier 3 candidate", not m.get("goes_to") and len(m.get("_t3", {}).get("candidates", [])) == 1)
lm = settle(out, fake(lambda q: (0, "certain")))
check("estimate: a verified certain pick is TO MATCH, high, by the agent",
      m["status"] == "TO MATCH" and m["confidence"] == "high" and m["match_by"] == "agent", str(m.get("awaiting")))
check("estimate: tier count", lm["tiers"].get("agent, certain") == 1, str(lm["tiers"]))

# 4. GBP 61.00 bill from a different supplier, nothing else agreeing: tier 3
moves = [line(4, -61.0, "CORNER BAKERY")]
docs = [bill(4, 61.0, "Acme Widgets Ltd", "Widgets")]
out = run(moves, docs)
m = by_id(out)[moves[0]["id"]]
check("same amount, other supplier: not code-certain", not m.get("goes_to") and m.get("_t3"), m.get("status"))
settle(out, fake(lambda q: (None, "unsure")))
check("same amount, other supplier: awaiting information", brc._label(m) == "AWAITING INFORMATION")

# 5. no candidates at all: NO DOCUMENT, the native feed's chase
moves = [line(5, -36.85, "SOME CAFE")]
out = run(moves, [bill(5, 300.0, "Other Supplier", "x", "", "2026-09-01")])
m = by_id(out)[moves[0]["id"]]
settle(out, fake(lambda q: (0, "certain")))
check("no candidates: NO DOCUMENT, nothing awaiting", m["status"] == "NO DOCUMENT" and not m.get("_t3")
      and brc._label(m) == "NO DOCUMENT")

# 6. the agent picks something that is not a candidate: refused
moves = [line(6, -61.0, "ACME WIDGETS")]
docs = [bill(6, 64.0, "Acme Widgets Ltd", "Widgets")]
out = run(moves, docs)
m = by_id(out)[moves[0]["id"]]
settle(out, fake(lambda q: ("L1.C9", "certain")))
check("non-candidate pick: refused, awaiting information",
      m["status"] == "NO DOCUMENT" and m["awaiting"].get("refused") == "not one of its candidates", str(m.get("awaiting")))

# 7. the agent fails: awaiting information, the check completes
out = run(moves, docs)
m = by_id(out)[moves[0]["id"]]
lm = settle(out, lambda payload: {"answers": None, "error": "the agent timed out after 300 seconds", "cost_usd": None,
                                  "usage": {}, "seconds": 300})
check("agent failure: awaiting information", m["status"] == "NO DOCUMENT" and m["awaiting"]["why"] == "the agent failed"
      and lm["error"], str(m.get("awaiting")))

# 8. --no-agent: awaiting information, not assessed
out = run(moves, docs)
m = by_id(out)[moves[0]["id"]]
settle(out, None, agent=False)
check("--no-agent: not assessed", "not assessed" in m["awaiting"]["why"])

# 9. tier 1: the exact amount, unique, the payee named
moves = [line(9, -14.5, "CORNER BAKERY")]
docs = [bill(9, 14.5, "Corner Bakery", "Lunch")]
out = run(moves, docs)
m = by_id(out)[moves[0]["id"]]
check("unique and named: TO MATCH by code", m["status"] == "TO MATCH" and m["match_how"] == "unique")

# 10. tier 1 refused where the bill names someone other than the cardholder
moves = [line(10, -14.5, "CORNER BAKERY", "Alex Sample")]
docs = [bill(10, 14.5, "Corner Bakery", "Lunch, Sam Tester")]
out = run(moves, docs)
m = by_id(out)[moves[0]["id"]]
check("who paid disagrees: not code-certain", not m.get("goes_to") and m.get("_t3"))

# 11. a merchant branch tells two same-amount bills apart
moves = [line(11, -8.2, "DAILY MARKET2 CENTRAL")]
docs = [bill(11, 8.2, "Daily Market2", "Groceries"), bill(12, 8.2, "Daily Market3", "Groceries")]
out = run(moves, docs)
m = by_id(out)[moves[0]["id"]]
check("merchant detail: the Market2 bill", m.get("goes_to") and m["goes_to"]["documents"][0]["contact"] == "Daily Market2",
      str(m.get("goes_to")))

# 12. the budget drops the furthest candidates, then whole lines
qs = [{"line_id": f"L{i}", "date": DAY, "amount": "spent GBP 1.00", "payee": "x" * 60,
       "candidates": [{"id": f"L{i}.C{j}", "contact": "y" * 40, "desc": "z" * 80} for j in range(6)]}
      for i in range(200)]
kept, dropped = brc._budget(qs)
check("budget: under the token cap", len(brc._payload_text(kept)) / 4 <= brc.LINE_MATCH_BUDGET_TOKENS)
check("budget: candidates cut to 3 before lines go", all(len(q["candidates"]) == 3 for q in kept) and dropped)

# 13. the feed note replaces an earlier one and keeps the marker
raw = "2026-10-05 | WAYFARER TRAVEL | spent | GBP 958.01 | Alex Sample | | chased 06 Oct · rev:abcd1234"
once = brc._annotated(raw, "awaiting information, possible: Wayfarer Travel GBP 966.30, not certain")
twice = brc._annotated(once, "awaiting information, possible: Wayfarer Travel GBP 966.30, not certain")
check("feed note: idempotent, marker kept", once == twice and once.endswith("chased 06 Oct · rev:abcd1234"), once)

# 14. stage a1: the cardholder named on the record's bill meets the 10-day window
mv = line(14, -30.0, "SQ *CORNER SHOP", "Alex Sample", "2026-10-01")
rec = {"rid": "p1", "kind": "payment", "xero": ACCT, "currency": "GBP", "date": "2026-10-09", "amount": -30.0,
       "reconciled": True, "batch": "", "contact": "Corner Shop Ltd", "number": "SHOP-AS-24SEP2026", "doc_id": "",
       "doc_currency": "GBP", "doc_amount": 30.0, "doc_date": "", "doc_total": None, "reference": "",
       "updated": "", "words": sorted(brc.words("Corner Shop Ltd")), "people": ["Alex Sample"], "ntoks": []}
o = brc.classify(KEY, [mv], [rec], {KEY: []}, {}, brc.Rates([], "2026-01-01"), {}, "2026-09-01", "2026-08-20")
check("a1: who paid stretches the reconciled window to the named gap", o["movements"][0]["state"] == "RECONCILED")

# 15. a certain pick of another supplier is refused by code
moves = [line(15, -61.0, "CORNER BAKERY")]
out = run(moves, [bill(15, 61.0, "Acme Widgets Ltd", "Widgets")])
m = by_id(out)[moves[0]["id"]]
settle(out, fake(lambda q: (0, "certain")))
check("certain pick, other supplier: refused", m["status"] == "NO DOCUMENT" and "neither" in m["awaiting"].get("refused", ""),
      str(m.get("awaiting")))

# 16. a certain pick while another bill of the supplier is nearer the amount
moves = [line(16, -17.63, "HARBOUR CAFE", "Kim Paula Rowan")]
docs = [bill(16, 17.04, "Harbour Cafe", "Brunch, Kim Rowan"),
        bill(17, 17.76, "Harbour Cafe", "Brunch, Kim Rowan", "", "2026-10-04")]
out = run(moves, docs)
m = by_id(out)[moves[0]["id"]]
check("middle name: the cardholder agrees with the bill", all(c["who"] is True for c in m["_t3"]["candidates"]))
settle(out, fake(lambda q: (next(i for i, c in enumerate(q["candidates"]) if c["amount"] == "GBP 17.04"), "certain")))
check("certain pick, a nearer amount beside it: refused", m["status"] == "NO DOCUMENT"
      and "nearer" in m["awaiting"].get("refused", ""), str(m.get("awaiting")))

# 17. less than the bill in the card's own currency, no estimate: refused
mv = line(17, -3561.94, "EXAMPLE WORKSPACE", "Jo Sample")
mv.update(orig_currency="EUR", orig_amount=4183.62)
d = bill(18, 4350.84, "Example Workspace SAS", "Office fees, November")
d.update(currency="EUR", rate=1.18)
out = run([mv], [d])
m = by_id(out)[mv["id"]]
check("card currency: a tier 3 candidate", len(m.get("_t3", {}).get("candidates", [])) == 1)
settle(out, fake(lambda q: (0, "certain")))
check("card currency, 4% under, no estimate: refused", m["status"] == "NO DOCUMENT"
      and "no exchange rate applies" in m["awaiting"].get("refused", ""), str(m.get("awaiting")))

print(f"\n{len(FAILS)} failed" if FAILS else "\nall passed")
sys.exit(1 if FAILS else 0)
