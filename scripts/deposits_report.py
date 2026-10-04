#!/usr/bin/env python
"""The deposits report: every deposit the group still has out, across every
deposit account, and the month it was paid.

One workbook for every entity in config/group.toml, one row per deposit, one
column per month. The month cell holds what POSTED documents did to that
deposit: the deposit itself in the month it was paid, any partial refund or
application as a negative in its own month. The row therefore sums to what is
still held.

    data/reports/Deposits.xlsx              the single copy, overwritten
    data/reports/deposits_store.json        the store, accumulates

EVERY row from [reports] from_year on is on the report, active or not: each
row carries a Status column (active while a balance remains, inactive once the
deposit is refunded, returned or applied in full) and the workbook opens
filtered to active rows. An inactive row from before from_year is not on the
report. The totals block carries a "rows not shown" line holding the net
movement of what is left out, so the balance is still the whole account and
agrees with Xero at every month end.

Sheet "Deposits": the rows, filterable on the entity column, with the deposit
account each one sits in, then a totals block per entity in that entity's base
currency: movement, balance, balance in Xero, difference.
Sheet "Checks": everything a person has to look at: a refund larger than the
deposit, anything the report could not attribute, and any month the balance
does not agree with Xero.

Every asset account whose name carries one of [reports.deposits]
account_words is in scope, discovered from each entity's chart of accounts;
bank accounts are not. An account the discovery misses is listed under
[reports.deposits] accounts in config/group.toml, or added by hand under
accounts.<entity>.codes in the store, and is kept from then on.

The engine, the ledger rebuild and the attribution rules are in
scripts/balance_report.py, shared with the prepayments and accruals reports.
Read-only against Xero.

Usage (entity = key, slug, alias or short name from config/group.toml):
    .venv/bin/python scripts/deposits_report.py refresh all
    .venv/bin/python scripts/deposits_report.py refresh <entity> --full
    .venv/bin/python scripts/deposits_report.py build            # no Xero
    .venv/bin/python scripts/deposits_report.py show [<entity>]
    .venv/bin/python scripts/deposits_report.py checks
    .venv/bin/python scripts/deposits_report.py summary
    .venv/bin/python scripts/deposits_report.py rows [<entity>]
    .venv/bin/python scripts/deposits_report.py set-field <row> <field> <value>
    .venv/bin/python scripts/deposits_report.py link <refund> <row>
    .venv/bin/python scripts/deposits_report.py unlink <refund>
    .venv/bin/python scripts/deposits_report.py combine <row> <into row>
    .venv/bin/python scripts/deposits_report.py uncombine <row>
    .venv/bin/python scripts/deposits_report.py clear <row> "<why>"
    .venv/bin/python scripts/deposits_report.py unclear <row>
    .venv/bin/python scripts/deposits_report.py notes
    .venv/bin/python scripts/deposits_report.py note <entity> <kind> "<what it says>"
    .venv/bin/python scripts/deposits_report.py unnote <note>
    .venv/bin/python scripts/deposits_report.py accept <row> "<check>" "<why>"
    .venv/bin/python scripts/deposits_report.py unaccept <row> "<check>"

--no-publish skips the Drive copy on refresh and build.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from balance_report import DEPOSITS, main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(DEPOSITS, sys.argv[1:], __doc__))
