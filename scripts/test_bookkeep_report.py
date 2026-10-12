"""Offline checks for scripts/bookkeep_report.py. No network, no Xero.

The report's bookkept lines must be exactly docs/COMMS.md's invoice shape,
each posted document once across re-runs, `paid` dropped where unknown; every
document not posted, and every flag, handed to the session to place; the bank
feed counts read from the feeds as they stand, per entity in config order.

    .venv/bin/python scripts/test_bookkeep_report.py
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
from datetime import date

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "src"))
os.environ["AGENT_GROUP_CONFIG"] = str(REPO / "config" / "group.example.toml")
import bookkeep_report as br  # noqa: E402
import daily_chain as dc  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("ok   " if cond else "FAIL ") + name + (f" - {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


Y = date.today().year
RUN = pathlib.Path(tempfile.mkdtemp(prefix="test_bookkeep_report_"))
(RUN / "bills.json").write_text(json.dumps({"bills": [
    {"entity": "opco eu", "type": "bill", "number": "INV-1002", "paid": "OPCO_EU"},
    {"entity": "HOLDCO", "type": "bill", "number": "H-17"},
    {"entity": "HOLDCO", "type": "credit", "number": "CN-31", "paid": "holdco"}]}))


def row(status, entity, number, contact, total, ccy="EUR", d=f"{Y}-09-04", typ="bill", **kw):
    return json.dumps({"status": status, "entity": entity, "type": typ, "contact": contact, "number": number,
                       "date": d, "currency": ccy, "total": total, **kw})


(RUN / "bills.results.jsonl").write_text("\n".join([
    row("skipped", "HOLDCO", "H-17", "Fabrikam Travel", 318.45, "GBP",
        hits=[{"entity": "HOLDCO", "why": "same number", "number": "H-17"}]),
    row("posted", "OPCO_EU", "INV-1002", "Contoso Cloud", 1240.0, id="abcd1234", flags=[]),
    row("posted", "HOLDCO", "H-17", "Fabrikam Travel", 318.45, "GBP", id="ffff0000",
        flags=["ATTACH FAILED x.pdf: 500"]),
    row("posted", "HOLDCO", "CN-31", "Northwind Store", 27.9, "GBP", typ="credit", d=f"{Y - 1}-12-30", id="c0c0c0c0"),
    row("error", "OPCO_US", "X-1", "Litware Software", 64.0, "USD", problems=["no account 999 in OpCo US"]),
    json.dumps({"status": "settled", "action": "a1", "kind": "ledger", "ok": True}),
]) + "\n")
dc.feed_lines = lambda folder=None: [{"entity": "opco-eu"}, {"entity": "opco-eu"}, {"entity": "opco-us"}]
dc.bill_feed = lambda folder=None: {"holdco": [1, 2], "opco-us": [], "opco-eu": [1]}

text, place = br.build(RUN)
lines = text.splitlines()
check("the bookkept line is COMMS.md's invoice shape, exactly",
      "• Contoso Cloud · 04 Sep · EUR 1,240.00 · paid OpCo EU · recognised OpCo EU · INV-1002" in lines, text)
check("paid is dropped where the spec has none, never written as unknown",
      "• Fabrikam Travel · 04 Sep · GBP 318.45 · recognised HoldCo · H-17" in lines, text)
check("a credit note says so, and an earlier year shows its year",
      f"• Northwind Store credit note · 30 Dec {Y - 1} · GBP 27.90 · paid HoldCo · recognised HoldCo · CN-31" in lines,
      text)
check("the bookkept lines run in config order", lines.index(next(l for l in lines if "CN-31" in l))
      < lines.index(next(l for l in lines if "INV-1002" in l)), text)
check("each posted document once, and a document skipped then posted counts as posted",
      text.count("H-17") == 1 and not any("duplicate guard" in p for p in place), str(place))
check("the bank feed counts are the feeds as they stand, every entity by short name",
      "• lines still open after the closing refresh: HoldCo 0, OpCo US 1, OpCo EU 2" in lines
      and "• open bills: HoldCo 2, OpCo US 0, OpCo EU 1" in lines, text)
check("a refused document and a flag go to the session to place, not into the report",
      any("not posted, no account 999" in p and "X-1" in p for p in place)
      and any("posted with a flag, ATTACH FAILED" in p for p in place) and "X-1" not in text, str(place))
check("no heading the session must write is invented", "*queries" not in text and "*manual*" not in text
      and not text.startswith("bookkeeping ·"))
rc = br.main([str(RUN)])
check("main writes report.md", rc == 0 and (RUN / "report.md").read_text() == text)

empty = pathlib.Path(tempfile.mkdtemp(prefix="test_bookkeep_report_empty_"))
text, place = br.build(empty)
check("a run that posted nothing still gets its bank feed section and no bookkept heading",
      "*bookkept*" not in text and "*bank feed*" in text and place == [])

print(f"\n{'FAIL' if FAILS else 'PASS'}: {len(FAILS)} failed")
sys.exit(1 if FAILS else 0)
