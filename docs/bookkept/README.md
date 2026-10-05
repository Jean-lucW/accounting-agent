# Bookkept registers

This folder holds three files the agent keeps while it works:

- `LEDGER.md`: the rolling record of every invoice message the bookkeeping runs have handled. `scripts/ledger_prune.py` creates it and keeps it to the last few days.
- `outstanding.json`: the register of items left with a person (queries, decisions to confirm, manual items, blocked lines, chases). `scripts/outstanding.py` is the only thing that edits it.
- `OUTSTANDING.md`: the readable version of that register, rebuilt by `scripts/outstanding.py` on every change.

The scripts create all three on first use. They live on the server only and are gitignored: they hold live ledger detail and are never committed.
