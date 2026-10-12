# Bookkept registers

This folder holds three files the agent keeps while it works:

- `LEDGER.md`: the rolling record of every invoice message the bookkeeping runs have handled. `scripts/ledger_prune.py` creates it and keeps it to the last few days.
- `outstanding.json`: the register of items left with a person (queries, decisions to confirm, manual items, blocked lines, chases). `scripts/outstanding.py` is the only thing that edits it.
- `OUTSTANDING.md`: the readable version of that register, rebuilt by `scripts/outstanding.py` on every change.
- `reconcile.json`: which statement line goes to which posted bill, the register `scripts/reconcile_list.py` and `scripts/outstanding.py` keep.
- `.outstanding.lock`: the register's lock file.

The scripts create these on first use. They live on the server only; the whole folder apart from this README is gitignored: they hold live ledger detail and are never committed.
