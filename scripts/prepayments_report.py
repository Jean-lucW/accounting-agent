#!/usr/bin/env python
"""The prepayments report: every prepaid invoice in the group, and what has
actually been released against it, month by month.

One workbook for every entity in config/group.toml, one row per prepaid
invoice, one column per month holding the amount that POSTED journals released
in that month. Not a plan and not an expectation: what is in the ledger. A row
whose releases stop halfway shows as a gap, which is the point of the report.

    data/reports/Prepayments.xlsx           the single copy, overwritten
    data/reports/prepayments_store.json     the store, accumulates

Sheet "Prepayments": the rows, filterable on the entity column, then a totals
block per entity in that entity's base currency: additions, released, balance,
balance in Xero, difference. The difference row is the whole check: the
schedule and the Balance Sheet must agree at every month end.
Sheet "Checks": everything a person has to look at, including any release the
report could not attribute to an invoice.

Every row from [reports] from_year on is on the report, with a Status column
(active while a balance remains, inactive once released in full); the
workbook opens filtered to active rows. An inactive row from before from_year
is left out and carried by "rows not shown", so the balance is still the whole
account.

Accounts are found by name ([reports.prepayments] account_words) or listed
explicitly per entity ([reports.prepayments] accounts).

The engine, the ledger rebuild and the attribution rules are in
scripts/balance_report.py, shared with the accruals and deposits reports.
Read-only against Xero.

Usage (entity = key, slug, alias or short name from config/group.toml):
    .venv/bin/python scripts/prepayments_report.py refresh all
    .venv/bin/python scripts/prepayments_report.py refresh <entity> --full
    .venv/bin/python scripts/prepayments_report.py build          # no Xero
    .venv/bin/python scripts/prepayments_report.py show [<entity>]
    .venv/bin/python scripts/prepayments_report.py checks
    .venv/bin/python scripts/prepayments_report.py summary
    .venv/bin/python scripts/prepayments_report.py rows [<entity>]
    .venv/bin/python scripts/prepayments_report.py set-period <row> <start> <end>
    .venv/bin/python scripts/prepayments_report.py set-field <row> <field> <value>
    .venv/bin/python scripts/prepayments_report.py link <release> <row>
    .venv/bin/python scripts/prepayments_report.py unlink <release>
    .venv/bin/python scripts/prepayments_report.py combine <row> <into row>
    .venv/bin/python scripts/prepayments_report.py uncombine <row>
    .venv/bin/python scripts/prepayments_report.py clear <row> "<why>"
    .venv/bin/python scripts/prepayments_report.py unclear <row>
    .venv/bin/python scripts/prepayments_report.py notes
    .venv/bin/python scripts/prepayments_report.py note <entity> <kind> "<what it says>"
    .venv/bin/python scripts/prepayments_report.py unnote <note>
    .venv/bin/python scripts/prepayments_report.py accept <row> "<check>" "<why>"
    .venv/bin/python scripts/prepayments_report.py unaccept <row> "<check>"

--no-publish skips the Drive copy on refresh and build.
Months for set-period are "Mmm YY" or "YYYY-MM" ("Jan 26", "2026-01").
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from balance_report import PREPAYMENTS, main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(PREPAYMENTS, sys.argv[1:], __doc__))
