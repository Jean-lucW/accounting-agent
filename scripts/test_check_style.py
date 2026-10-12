#!/usr/bin/env python
"""Offline self-check for scripts/check_style.py.

    .venv/bin/python scripts/test_check_style.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import check_style as cs  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, what: str) -> None:
    print(("ok    " if cond else "FAIL  ") + what)
    if not cond:
        FAILS.append(what)


def labels(text: str, **kw) -> set[str]:
    findings, _ = cs.check(text, company_name=kw.pop("company_name", ""), **kw)
    return {f[2] for f in findings}


OPENER = "This note says what was posted.\n\n"

check(not labels(OPENER + "Posted to Travel (5100).\n"), "clean text has no findings")
check("em dash or en dash" in labels(OPENER + "Posted — done.\n"), "em dash flagged")
check("currency symbol before number" in labels(OPENER + "Paid £45 today.\n"), "currency symbol flagged")
check("bullet ends with a full stop" in labels(OPENER + "- a bullet.\n"), "bullet full stop flagged")
check("consultant vocabulary" in labels(OPENER + "A robust process.\n"), "banned word flagged")
check("tacked-on corrective" in labels(OPENER + "Agree it before you sign, not after.\n"),
      "tacked-on corrective flagged")
check("em dash or en dash" not in labels(OPENER + "```\nposted — done\n```\n"), "code fence skipped")

check("americanism" not in labels(OPENER + "We recognize it.\n"), "US spelling ignored by default")
check("americanism" in labels(OPENER + "We recognize it.\n", british=True), "US spelling flagged with british")
check("americanism" not in labels(OPENER + "We recognise it.\n", british=True), "British spelling passes")

check("no Review section" not in labels(OPENER + "Text.\n"), "Review section optional by default")
check("no Review section" in labels(OPENER + "Text.\n", policy=True), "Review section required with policy")
check("no Review section" not in labels(OPENER + "### Review\n\nEvery year.\n", policy=True),
      "Review section accepted with policy")

check("group name abbreviated" in labels(OPENER + "The EG books.\n", company_name="Example Group"),
      "company initials flagged from the given name")
check("group name abbreviated" not in labels(OPENER + "The EG books.\n", company_name=""),
      "no company name, no name check")
check("wrong-organisation language" in labels(OPENER + "Our clients pay.\n", avoid=["clients"]),
      "--avoid words flagged")

if FAILS:
    print(f"\n{len(FAILS)} failed")
    sys.exit(1)
print("\nall passed")
