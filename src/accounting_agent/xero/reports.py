"""Xero Reports: read-only, row/cell-shaped JSON under Reports/{Name}.

Constraints (docs/XERO_API_REFERENCE.md section 5.2):
- All reports are GET-only; a Standard-role user without the "reports"
  permission gets 403 on Reports, Journals and ManualJournals.
- Responses are Rows of Header/Section/Row/SummaryRow; cell N of a data row
  lines up with cell N of the Header row. Values are strings; blank cells
  may omit "Value". Machine-readable joins live in cell Attributes
  (e.g. {"Id": "account", "Value": <AccountID>}), join on those, not text.
- Aged Receivables/Payables By Contact REQUIRE contactID.
- P&L periods 1-11; Budget Summary timeframe is numeric (1/3/12) unlike
  P&L/Balance Sheet (MONTH/QUARTER/YEAR). Tracking columns capped at 1000.

Dates are "YYYY-MM-DD".
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from .client import XeroClient


def run_report(client: XeroClient, name: str, **params: Any) -> dict[str, Any]:
    """GET Reports/{name} with query params; returns the raw report JSON.

    None-valued params are dropped by the client.
    """
    return client.get(f"Reports/{name}", **params)


def report_to_rows(report_json: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten Xero's Rows/Cells report structure into a list of dicts.

    Each dict carries "Section" (nearest enclosing section title),
    "RowType" (Row/SummaryRow), one key per column (named from the Header
    row, or "Column{i}" where the header cell is blank), and "AccountID"
    when the row's first cell carries an account attribute.
    """
    report = report_json["Reports"][0]
    headers: list[str] = []
    out: list[dict[str, Any]] = []

    def walk(rows: list[dict[str, Any]], section: str) -> None:
        for row in rows:
            row_type = row.get("RowType")
            if row_type == "Header":
                headers[:] = [c.get("Value", "") for c in row.get("Cells", [])]
            elif row_type == "Section":
                walk(row.get("Rows", []), row.get("Title") or section)
            else:  # Row / SummaryRow
                cells = row.get("Cells", [])
                flat: dict[str, Any] = {"Section": section, "RowType": row_type}
                for i, cell in enumerate(cells):
                    key = headers[i] if i < len(headers) and headers[i] else f"Column{i}"
                    flat[key] = cell.get("Value")
                if cells:
                    attrs = {a["Id"]: a["Value"] for a in cells[0].get("Attributes", [])}
                    if "account" in attrs:
                        flat["AccountID"] = attrs["account"]
                out.append(flat)

    walk(report.get("Rows", []), "")
    return out


def save_report_csv(report_json: dict[str, Any], path: str | Path) -> Path:
    """Write the flattened report rows to a CSV file; returns the path."""
    rows = report_to_rows(report_json)
    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    path = Path(path)
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return path


# -- convenience wrappers ----------------------------------------------------


def trial_balance(client: XeroClient, date: str | None = None) -> dict[str, Any]:
    """Trial Balance as at `date` (current month by default; YTD included)."""
    return run_report(client, "TrialBalance", date=date)


def profit_and_loss(
    client: XeroClient,
    from_date: str | None = None,
    to_date: str | None = None,
    periods: int | None = None,
    timeframe: str | None = None,
    tracking: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Profit and Loss. periods 1-11; timeframe MONTH/QUARTER/YEAR.

    With periods + a date range, the same day-range applies to each prior
    period. tracking: extra query params passed through verbatim, e.g.
    {"trackingCategoryID": ..., "trackingOptionID": ...}.
    """
    return run_report(
        client, "ProfitAndLoss",
        fromDate=from_date, toDate=to_date, periods=periods, timeframe=timeframe,
        **(tracking or {}),
    )


def balance_sheet(
    client: XeroClient,
    date: str | None = None,
    periods: int | None = None,
    timeframe: str | None = None,
) -> dict[str, Any]:
    """Balance Sheet as at end of `date`'s month (plus same month prior year)."""
    return run_report(client, "BalanceSheet", date=date, periods=periods,
                      timeframe=timeframe)


def aged_payables(
    client: XeroClient, contact_id: str | None = None, date: str | None = None
) -> dict[str, Any]:
    """Aged Payables By Contact. Xero REQUIRES contactID (400 without it)."""
    return run_report(client, "AgedPayablesByContact", contactID=contact_id, date=date)


def aged_receivables(
    client: XeroClient, contact_id: str | None = None, date: str | None = None
) -> dict[str, Any]:
    """Aged Receivables By Contact. Xero REQUIRES contactID (400 without it)."""
    return run_report(client, "AgedReceivablesByContact", contactID=contact_id, date=date)


def bank_summary(
    client: XeroClient, from_date: str | None = None, to_date: str | None = None
) -> dict[str, Any]:
    """Bank Summary: balances and cash movements per bank account."""
    return run_report(client, "BankSummary", fromDate=from_date, toDate=to_date)


def budget_summary(client: XeroClient) -> dict[str, Any]:
    """Budget Summary (requires accounting.budgets.read for /Budgets detail;
    the report itself sits under the reports scopes)."""
    return run_report(client, "BudgetSummary")


def executive_summary(client: XeroClient) -> dict[str, Any]:
    """Executive Summary: monthly totals and common business ratios."""
    return run_report(client, "ExecutiveSummary")
