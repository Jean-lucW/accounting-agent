#!/usr/bin/env python
"""The accruals report: every accrual still open in the group, and the month
it was posted.

One workbook for every entity in config/group.toml, one row per accrual, one
column per month. The month cell holds what POSTED journals did to that
accrual: the accrual itself in the month it went on, any partial reversal as a
negative in its own month. The row therefore sums to what is still open.

    data/reports/Accruals.xlsx              the single copy, overwritten
    data/reports/accruals_store.json        the store, accumulates

EVERY row from [reports] from_year on is on the report, active or not: each
row carries a Status column (active while a balance remains, inactive once the
accrual is reversed or settled in full) and the workbook opens filtered to
active rows. An inactive row from before from_year is not on the report.
The totals block carries a "rows not shown" line holding the net movement of
what is left out, so the balance is still the whole account and agrees with
Xero at every month end.

Sheet "Accruals": the rows, filterable on the entity column, then a totals
block per entity in that entity's base currency: movement, balance, balance in
Xero, difference.
Sheet "Checks": everything a person has to look at: an accrual open more than
four months, a reversal larger than the accrual, anything the report could not
attribute, and any month the balance does not agree with Xero.

Accounts are found by name ([reports.accruals] account_words, less
account_not_words) or listed explicitly per entity ([reports.accruals]
accounts).

The engine, the ledger rebuild and the attribution rules are in
scripts/balance_report.py, shared with the prepayments and deposits reports.
Read-only against Xero.

Usage (entity = key, slug, alias or short name from config/group.toml):
    .venv/bin/python scripts/accruals_report.py refresh all
    .venv/bin/python scripts/accruals_report.py refresh <entity> --full
    .venv/bin/python scripts/accruals_report.py build            # no Xero
    .venv/bin/python scripts/accruals_report.py show [<entity>]
    .venv/bin/python scripts/accruals_report.py checks
    .venv/bin/python scripts/accruals_report.py summary
    .venv/bin/python scripts/accruals_report.py rows [<entity>]
    .venv/bin/python scripts/accruals_report.py set-field <row> <field> <value>
    .venv/bin/python scripts/accruals_report.py link <reversal> <row>
    .venv/bin/python scripts/accruals_report.py unlink <reversal>
    .venv/bin/python scripts/accruals_report.py combine <row> <into row>
    .venv/bin/python scripts/accruals_report.py uncombine <row>
    .venv/bin/python scripts/accruals_report.py clear <row> "<why>"
    .venv/bin/python scripts/accruals_report.py unclear <row>
    .venv/bin/python scripts/accruals_report.py notes
    .venv/bin/python scripts/accruals_report.py note <entity> <kind> "<what it says>"
    .venv/bin/python scripts/accruals_report.py unnote <note>
    .venv/bin/python scripts/accruals_report.py accept <row> "<check>" "<why>"
    .venv/bin/python scripts/accruals_report.py unaccept <row> "<check>"

--no-publish skips the Drive copy on refresh and build.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from balance_report import ACCRUALS, main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(ACCRUALS, sys.argv[1:], __doc__))
