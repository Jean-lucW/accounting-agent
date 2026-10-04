#!/usr/bin/env python
"""Payroll control-account zero check.

Run at the end of every payroll or bookkeeping run (see rules/PAYROLL.md,
"Control-account zero check"). For each payroll control account in
config/group.toml ([entities.payroll_controls]) it prints the balance as at
month end for the last few months plus today, and flags any balance that
cannot be explained by the current, not-yet-remitted cycle (the `residual`
text configured for that account).

It also flags bills that are cleared from the payroll controls by journal
rather than by the bank ([[accounts_payable.not_via_bank]] patterns, matched
on contact or reference) left with an unpaid balance beyond the current month,
in every entity that has payroll controls: until they are cleared, the control
accounts cannot be.

Usage:
    .venv/bin/python scripts/check_payroll_controls.py [--months N]

Read-only: trial balance and bill reads only.
"""
import argparse
import datetime as dt
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from accounting_agent import config  # noqa: E402


def month_ends(n):
    today = dt.date.today()
    first = today.replace(day=1)
    outs = []
    d = first - dt.timedelta(days=1)
    for _ in range(n):
        outs.append(d)
        d = d.replace(day=1) - dt.timedelta(days=1)
    return list(reversed(outs)) + [today]


def tb_balances(client, date, codes):
    from accounting_agent.xero.reports import report_to_rows, trial_balance

    rows = report_to_rows(trial_balance(client, date=date.isoformat()))
    out = {}
    for r in rows:
        acct = r.get("Account", "")
        for code in codes:
            if f"({code})" in acct or acct.startswith(code + " "):
                credit = float(r.get("YTD Credit") or 0)
                debit = float(r.get("YTD Debit") or 0)
                out[code] = round(debit - credit, 2)  # +ve = debit balance
    return out


def journal_cleared_patterns():
    """The not_via_bank patterns, compiled: bills settled by journal."""
    out = []
    for item in config.accounts_payable().get("not_via_bank") or []:
        pattern = (item or {}).get("pattern", "")
        if pattern:
            out.append((re.compile(pattern, re.I), item.get("reason", "")))
    return out


def main():
    ap = argparse.ArgumentParser(description="Payroll control-account zero check "
                                 "(controls from config/group.toml). Read-only.")
    ap.add_argument("--months", type=int, default=3, help="month-ends to show")
    args = ap.parse_args()

    controls_by_entity = config.payroll_controls()
    if not controls_by_entity:
        print("no payroll control accounts in config/group.toml; nothing to check")
        return 0

    from accounting_agent.xero import XeroClient
    from accounting_agent.xero.purchases import get_bills

    dates = month_ends(args.months)
    flagged = 0
    patterns = journal_cleared_patterns()
    this_month = dt.date.today().strftime("%Y-%m")

    for key, controls in controls_by_entity.items():
        ent = config.entity(key)
        c = XeroClient(ent.xero_name)
        print(f"\n=== {ent.title}")
        codes = [ctl.code for ctl in controls]
        history = {d: tb_balances(c, d, codes) for d in dates}
        for ctl in controls:
            series = "  ".join(
                f"{d.strftime('%d %b')}: {history[d].get(ctl.code, 0.0):>12,.2f}" for d in dates
            )
            bal = history[dates[-1]].get(ctl.code, 0.0)
            print(f"  {ctl.code} {ctl.name:<26} {series}")
            if abs(bal) > 0.05:
                flagged += 1
                print(f"      -> NON-ZERO {bal:,.2f}. Acceptable only if it is: "
                      f"{ctl.residual or 'nothing (no residual configured)'}.")
                print("         Anything else (older months, a debit balance on a "
                      "liability control, FX stubs) = investigate.")

        # Bills cleared from the controls by journal must end fully paid, or
        # the controls never clear.
        if not patterns:
            continue
        stale = []
        for b in get_bills(c, unpaid_only=True):
            date = (b.get("DateString") or "")[:10]
            if date[:7] >= this_month:
                continue
            text = f"{(b.get('Contact') or {}).get('Name', '')} {b.get('Reference') or ''}"
            if any(p.search(text) for p, _ in patterns):
                stale.append((date, (b.get("Contact") or {}).get("Name", ""),
                              b.get("InvoiceNumber"), b.get("AmountDue") or 0,
                              b.get("CurrencyCode")))
        if stale:
            print("  bills cleared by journal still unpaid after their month "
                  "(clear them from the control accounts):")
            for date, who, no, due, cur in stale:
                print(f"    {date}  {who}  {no}  due {due:,.2f} {cur}")
                flagged += 1

    print(f"\n{'ALL CLEAR' if flagged == 0 else f'{flagged} item(s) need attention'}")
    return 0 if flagged == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
