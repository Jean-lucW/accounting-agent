#!/usr/bin/env python
"""Read-only smoke test across every group entity in config/group.toml.

STRICTLY READ-ONLY: uses GET endpoints only (accounts, bank transactions,
one page of bills/invoices, trial balance). Never writes to any ledger.

Each entity is resolved by its EXACT xero_name (xero.client_for), so a name
in config/group.toml that does not match the connected organisation shows up
here as an error row naming the organisations that are connected.

Run from the repo root:  .venv/bin/python scripts/smoke_test.py [ENTITY ...]
"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from accounting_agent import config  # noqa: E402
from accounting_agent.xero import banking, client_for, reports  # noqa: E402


def _num(value) -> float:
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


def check_entity(key: str) -> dict:
    client = client_for(key)
    row: dict = {"entity": key, "xero_name": client.entity_name}

    accounts = banking.list_bank_accounts(client)
    row["bank_accts"] = len(accounts)

    row["unreconciled"] = len(banking.find_unreconciled(client))

    # One page only: keep the run light on rate limits.
    bills_page = client.get("Invoices", page=1, where='Type=="ACCPAY"')
    row["bills_p1"] = len(bills_page.get("Invoices", []))
    invoices_page = client.get("Invoices", page=1, where='Type=="ACCREC"')
    row["invoices_p1"] = len(invoices_page.get("Invoices", []))

    tb = reports.trial_balance(client)
    rows = reports.report_to_rows(tb)
    summaries = [r for r in rows if r.get("RowType") == "SummaryRow"]
    if summaries:
        s = summaries[-1]
        debit, credit = _num(s.get("Debit")), _num(s.get("Credit"))
        row["tb_debit"] = debit
        row["tb_credit"] = credit
        delta = abs(debit - credit)
        if delta < 0.005:
            row["tb_balanced"] = "YES"
        elif delta <= 0.02:
            # Xero TB totals sum per-account 2dp-rounded strings; cent-level
            # deltas are report presentation rounding, not ledger imbalance.
            row["tb_balanced"] = f"~ (rounding {delta:.2f})"
        else:
            row["tb_balanced"] = f"NO (off {delta:.2f})"
    else:
        row["tb_debit"] = row["tb_credit"] = 0.0
        row["tb_balanced"] = "n/a (no summary row)"
    return row


def main(argv: list[str]) -> int:
    if any(a in ("-h", "--help") for a in argv):
        print(__doc__)
        return 0
    keys = [config.entity(a).key for a in argv] or [e.key for e in config.entities()]
    failures = 0
    results: list[dict] = []
    for key in keys:
        try:
            results.append(check_entity(key))
        except Exception as exc:  # noqa: BLE001 - report and continue
            failures += 1
            traceback.print_exc()
            results.append({"entity": key, "error": f"{type(exc).__name__}: {exc}"})

    fmt = "{:<12} {:<34} {:>10} {:>12} {:>9} {:>12} {:>15} {:>15} {:>18}"
    print()
    print(fmt.format("Entity", "Xero organisation", "BankAccts", "Unreconciled", "Bills p1",
                     "Invoices p1", "TB Debit", "TB Credit", "Balanced"))
    print("-" * 141)
    for r in results:
        if "error" in r:
            print(f"{r['entity']:<12} ERROR: {r['error']}")
            continue
        print(fmt.format(
            r["entity"][:12], r["xero_name"][:34], r["bank_accts"], r["unreconciled"],
            r["bills_p1"], r["invoices_p1"], f"{r['tb_debit']:,.2f}", f"{r['tb_credit']:,.2f}",
            r["tb_balanced"],
        ))
    print()
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
