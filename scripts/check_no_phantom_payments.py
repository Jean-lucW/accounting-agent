#!/usr/bin/env python
"""Detector: unreconciled payments sitting on BANK accounts.

The agent must never settle a bill: it creates the AUTHORISED bill and the
user matches the imported statement line by hand (xero skill safety rule 7).
A payment on a bank account that is not reconciled is a phantom entry
competing with the real feed line, i.e. exactly the failure this check exists
to catch.

Run at the END of every invoice-bookkeeping run:

    .venv/bin/python scripts/check_no_phantom_payments.py [days]

Exit 0 = clean. Exit 1 = phantom payments found (investigate before reporting
the run as done). Payments on the accounts config.phantom_exempt_accounts()
returns (every entity's payroll control accounts plus
[safety].phantom_check_exempt_accounts in config/group.toml) are listed
separately as ALLOWED-BY-EXCEPTION: a payroll bill cleared from a control
account by the user (rules/PAYROLL.md) is not a bank payment.
Read-only: this script never writes to Xero.
"""

import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from accounting_agent import config  # noqa: E402


def main(days: int = 21) -> int:
    from accounting_agent.xero import XeroClient
    from accounting_agent.xero.banking import get_payments, list_bank_accounts

    control_codes = config.phantom_exempt_accounts()
    since = (date.today() - timedelta(days=days)).isoformat()
    until = (date.today() + timedelta(days=1)).isoformat()
    phantom, exception_rows = [], []
    ents = config.entities()

    for ent in ents:
        name = ent.xero_name
        c = XeroClient(name)
        bank_ids = {a["AccountID"] for a in list_bank_accounts(c)}
        control_ids = {
            a["AccountID"]
            for a in c.get("Accounts")["Accounts"]
            if a.get("Code") in control_codes
        }
        for p in get_payments(c, from_date=since, to_date=until):
            if p.get("Status") == "DELETED" or p.get("IsReconciled"):
                continue
            full = c.get(f"Payments/{p['PaymentID']}")["Payments"][0]
            acct = full.get("Account", {}) or {}
            aid = acct.get("AccountID")
            doc = full.get("Invoice") or full.get("CreditNote") or {}
            ref = doc.get("InvoiceNumber") or doc.get("CreditNoteNumber") or "?"
            row = (name, full["Amount"], acct.get("Name", "?"), str(ref)[:30],
                   full["PaymentID"])
            if aid in bank_ids:
                phantom.append(row)
            elif aid in control_ids:
                exception_rows.append(row)

    if exception_rows:
        print("ALLOWED-BY-EXCEPTION: payroll control-account clearing "
              "(rules/PAYROLL.md), not bank payments:")
        for e, amt, acct, ref, pid in exception_rows:
            print(f"  {e[:32]:32} {amt:>12,.2f}  {acct[:22]:22} {ref:30} {pid[:8]}")
        print()

    if not phantom:
        print(f"PASS: no unreconciled payments on any bank account "
              f"(last {days} days, {len(ents)} entities).")
        return 0

    print(f"FAIL: {len(phantom)} unreconciled payment(s) on BANK accounts. "
          f"These duplicate the real statement lines:")
    for e, amt, acct, ref, pid in phantom:
        print(f"  {e[:32]:32} {amt:>12,.2f}  {acct[:22]:22} {ref:30} {pid[:8]}")
    print("\nIf the agent created these, remove them. NEVER remove a payment that "
          "is already reconciled: that is the user's own matching work.")
    return 1


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        sys.exit(0)
    sys.exit(main(int(sys.argv[1]) if len(sys.argv) > 1 else 21))
