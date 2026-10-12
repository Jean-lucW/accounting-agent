#!/usr/bin/env python
"""The gate between the agent's own answer to a question and acting on it.

Before a question goes to a person, the run answers it from the evidence and
grades the answer (CLAUDE.md, "Queries: answer them before asking them"). This
script, not the run, decides what the grade allows, from [auto_resolve] in
config/group.toml:

    .venv/bin/python scripts/resolve_gate.py --grade medium --amount 1240
    confirm    medium confidence

`--amount` is the money the answer moves, converted to the reporting currency.
The first word is the action:

    act        post it; report it under `resolved`
    confirm    post it; report it under `to confirm` and register it,
               scripts/outstanding.py add --kind decided
    query      do not post; ask, with the proposed answer in the question

The run follows the first word and never argues with it. Changing what the
agent may decide alone is an edit to config/group.toml, never a ruling.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from accounting_agent import config  # noqa: E402


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--grade", required=True, choices=["high", "medium", "low"])
    p.add_argument("--amount", required=True, type=float,
                   help="the money the answer moves, in the reporting currency")
    a = p.parse_args(argv)
    action, why = config.resolve_action(a.grade, a.amount)
    print(f"{action:<10} {why}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
