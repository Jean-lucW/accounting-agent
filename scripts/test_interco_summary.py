#!/usr/bin/env python
"""Offline check for interco_recon.summary_text: only what is still open is
listed, with counts of what is already explained. No network, no credentials.

    .venv/bin/python scripts/test_interco_summary.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
os.environ["AGENT_GROUP_CONFIG"] = str(ROOT / "config" / "group.example.toml")

import interco_recon  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, what: str) -> None:
    print(("ok    " if cond else "FAIL  ") + what)
    if not cond:
        FAILS.append(what)


def txn(entity, base, amount, doc_id, source="INV:ACCPAY", date="2026-03-05"):
    return {"entity": entity, "entity_base": base, "base": amount, "doc_amount": amount,
            "currency": base, "date": date, "source": source, "party": "Example Supplier Ltd",
            "desc": "March hosting", "doc_id": doc_id, "code": "1620"}


def pair(name, unmatched_a=(), matched=()):
    return {"pair": name, "flavour": "loan", "break": 100.0, "status": "BREAK",
            "a": {"entity": "HOLDCO", "code": "1620", "base": "USD", "balance": 100.0},
            "b": {"entity": "OPCO_US", "code": "2620", "base": "USD", "balance": 0.0},
            "matched": list(matched), "groups": [], "probable": [], "confirmed": [],
            "fx_adjustments": {"a": [], "b": []}, "contras": {"a": [], "b": []},
            "unmatched_a": list(unmatched_a), "unmatched_b": []}


floor = float(interco_recon.settings()["tolerance_abs"])
agreed = (txn("HOLDCO", "USD", 50.0, "d1"), txn("OPCO_US", "USD", -50.0, "d2"), "same amount")
off = (txn("HOLDCO", "USD", 500.0, "d3"), txn("OPCO_US", "USD", -420.0, "d4"), "same supplier")
report = {
    "window": ["2026-03-01", "2026-03-31"], "as_at": "2026-03-31", "reporting_currency": "USD",
    "rates": {"USD": 1.0},
    "pairs": [
        pair("HoldCo / OpCo US", unmatched_a=[txn("HOLDCO", "USD", 100.0, "d9")], matched=[agreed, off]),
        pair("HoldCo / OpCo EU", matched=[agreed]),
        {"pair": "OpCo US / OpCo EU", "status": "no_mirror_account", "missing_in": "OPCO_EU"},
    ],
}
text = interco_recon.summary_text(report)

check("doc_id d9" in text, "the one-sided line is listed")
check("doc_id d3" in text and "doc_id d4" in text, "the pair at different amounts is listed")
check("doc_id d1" not in text, "an agreed matched pair is not listed")
check("== HoldCo / OpCo EU" not in text, "a pair with nothing open has no section")
check("NO MIRROR ACCOUNT in OPCO_EU" in text, "a pair with no mirror account has its line")
check("2 matched" in text, "counts of what is explained are given")
check("break USD" in text and "cross rates to USD" in text, "wording uses the reporting currency")
check(floor > 0, "tolerance comes from config")

if FAILS:
    print(f"\n{len(FAILS)} failed")
    sys.exit(1)
print("\nall passed")
