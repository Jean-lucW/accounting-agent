"""Offline checks for the compact output modes of bankfeed.py and billfeed.py,
the four-way Xero check, the exchange-leg refresh and the cross-currency exact
match. Fixtures in the files' own shapes, in a temporary folder, for the
fictional group in config/group.example.toml; no network, no write to data/.

    .venv/bin/python scripts/test_feed_outputs.py
"""
from __future__ import annotations

import contextlib
import io
import os
import pathlib
import sys
import tempfile
import types

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "src"))
os.environ["AGENT_GROUP_CONFIG"] = str(REPO / "config" / "group.example.toml")
import bankfeed  # noqa: E402
import billfeed  # noqa: E402
from accounting_agent import config  # noqa: E402

TMP = pathlib.Path(tempfile.mkdtemp(prefix="test_feed_outputs_"))
FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("ok   " if cond else "FAIL ") + name + (f" - {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def out(fn, *a, **k) -> str:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn(*a, **k)
    return buf.getvalue()


(TMP / "bank").mkdir()
(TMP / "bank" / "holdco.md").write_text("""# Bank feed: Example Holdings Limited (HoldCo)

## GBP Main · last checked 2026-10-07T10:18Z
2026-10-01 | Corner Cafe | spent | GBP 7.60 | A. Holder | | no receipt, chased · rev:1a2b3c01
2026-10-07 | Station Deli | spent | GBP 11.45 | B. Holder | | rev:1a2b3c02
2026-10-07 | Exchanged from USD Main | received | GBP 15000.00 |  | | rev:1a2b3c03

## USD Main · last checked 2026-10-07T10:18Z
2026-10-06 | To Jane Doe | spent | USD 1840.10 |  | | rev:1a2b3c04
""")
(TMP / "bank" / "opco-us.md").write_text("""# Bank feed: Example Operations Inc. (OpCo US)

## Mercury Checking xx1234 · last checked 2026-10-07T10:18Z
2026-10-03 | Station Deli | spent | USD 11.45 | C. Holder | | mer:2b3c4d01
2026-10-06 | Acme Hosting | spent | USD 25.00 | D. Holder | | mer:2b3c4d02
""")
bankfeed.FEED = TMP / "bank"

o = out(bankfeed.main, ["find", "11.45"])
check("find an amount searches every entity", "holdco · GBP Main" in o and "opco-us · Mercury" in o and "Corner Cafe" not in o)
o = out(bankfeed.main, ["find", "11.45", "--near", "2026-10-07", "--days", "1"])
check("find --near keeps lines within the days", "holdco" in o and "opco-us" not in o)
o = out(bankfeed.main, ["find", "station deli", "--ccy", "EUR"])
check("find --ccy filters the currency", o.startswith("no feed line matches"))
o = out(bankfeed.main, ["list", "all", "--grep", "acme"])
check("list all --grep prints only the matching line and its account",
      o.count("\n") == 2 and "opco-us · Mercury Checking xx1234" in o and "Acme Hosting" in o)
o = out(bankfeed.main, ["list", "holdco"])
check("list <entity> still prints every account and line", o.count("## ") == 2 and o.count("  20") == 4)
o = out(bankfeed.main, ["list", "holdco", "--since", "2026-10-06"])
check("list --since drops older lines", "Corner Cafe" not in o and "Jane" in o)

# ---- xero-check: a saved check read back ------------------------------------
saved = TMP / "xc.txt"
saved.write_text("""[PAID IN XERO] opco-us · Mercury Checking xx1234 · 2026-10-03 | Station Deli | spent | USD 11.45 | C. Holder | | mer:2b3c4d01
    opco-us   payment 2026-10-03 · Station Deli · USD 11.45, bank 11.45 · Mercury Checking · reconciled · AUTHORISED · PL-1  [bank amount, name]
[CANDIDATES] opco-us · Mercury Checking xx1234 · 2026-10-06 | Acme Hosting | spent | USD 25.00 | D. Holder | | mer:2b3c4d02
    opco-us   bill 2026-10-01 · Acme Hosting · USD 25.00 · AUTHORISED · AH-01  [amount, name]
    holdco    bill 2026-10-02 · Acme Hosting · USD 25.00 · AUTHORISED · X  [amount]
[NOTHING FOUND] holdco · GBP Main · 2026-10-01 | Corner Cafe | spent | GBP 7.60 | A. Holder | | no receipt · rev:1a2b3c01
[TRANSFER] holdco · GBP Main · 2026-10-07 | Exchanged from USD Main | received | GBP 15000.00 |  | | rev:1a2b3c03
""")
o = out(bankfeed.main, ["xero-check", "--from", str(saved), "--summary"])
check("xero-check --summary prints counts only",
      "4 lines" in o and "PAID IN XERO 1" in o and "Station" not in o and o.count("\n") == 3, o)
o = out(bankfeed.main, ["xero-check", "--from", str(saved), "--only", "CANDIDATES"])
check("--only keeps whole lines with every candidate", o.count("[CANDIDATES]") == 1
      and o.count("\n    ") == 2 and "PAID" not in o, o)
o = out(bankfeed.main, ["xero-check", "--from", str(saved), "--only", "nothing found,TRANSFER", "holdco"])
check("--only takes several verdicts and an entity", "[NOTHING FOUND]" in o and "[TRANSFER]" in o and "opco-us" not in o)
o = out(bankfeed.main, ["xero-check", "--from", str(saved)])
check("--from with no filter reprints the saved check unchanged", o == saved.read_text())
copy = TMP / "xc2.txt"
out(bankfeed.main, ["xero-check", "--from", str(saved), "--out", str(copy), "--summary"])
check("--out writes the whole check", copy.read_text() == saved.read_text())
try:
    out(bankfeed.main, ["xero-check", "--from", str(saved), "--only", "MAYBE"])
    check("an unknown verdict is refused", False)
except SystemExit as e:
    check("an unknown verdict is refused", "unknown verdict" in str(e))


# ---- xero-check against a fake Xero window ---------------------------------
def pay(ptype, amount, bank_amount, day, contact, number, acct="A1", ccy="USD"):
    return {"PaymentType": ptype, "Amount": amount, "BankAmount": bank_amount, "DateString": day + "T00:00:00",
            "Account": {"AccountID": acct}, "IsReconciled": True, "Status": "AUTHORISED", "PaymentID": number,
            "Invoice": {"CurrencyCode": ccy, "InvoiceNumber": number, "Contact": {"Name": contact}}}


def bill(total, day, contact, number, ccy="USD", rate=None):
    return {"Type": "ACCPAY", "Total": total, "DateString": day + "T00:00:00", "CurrencyCode": ccy,
            "CurrencyRate": rate, "Contact": {"Name": contact}, "InvoiceNumber": number, "Status": "AUTHORISED",
            "InvoiceID": number, "LineItems": []}


def window(payments, docs, base="USD"):
    return {"base": base, "accounts": {"A1": ("Mercury Checking", "USD")}, "payments": payments, "txns": [],
            "docs": docs, "rates": {"GBP": 0.8}}


fake = {"opco-us": window([pay("ACCPAYPAYMENT", 11.45, 11.45, "2026-10-03", "Station Deli", "PL-1")],
                          [bill(25.00, "2026-10-01", "Other Supplier", "OS-1"),
                           bill(40.00, "2026-10-04", "Corner Cafe", "CC-9", ccy="GBP", rate=0.8)]),
        "holdco": window([], []), "opco-eu": window([], [])}
import accounting_agent.xero as xero_pkg  # noqa: E402
_names = {e.xero_name: e.slug for e in config.entities()}
xero_pkg.XeroClient = lambda name: types.SimpleNamespace(slug=_names[name])
import accounting_agent.xero.matching as matching  # noqa: E402
matching.load_window = lambda client, lo, hi: fake[client.slug]
o = out(bankfeed.main, ["xero-check", "all"])
check("a payment of the exact bank amount to the named payee is PAID IN XERO",
      "[PAID IN XERO] opco-us" in o and "[bank amount, name]" in o, o)
check("an exact amount to another supplier is only a candidate",
      "[CANDIDATES] opco-us" in o and "Other Supplier" in o, o)
check("a line with nothing behind it is NOTHING FOUND", "[NOTHING FOUND] holdco" in o and "Jane Doe" in o, o)
check("an exchange between the entity's own accounts is a TRANSFER", "[TRANSFER] holdco" in o, o)
check("a name alone, in another entity's documents, is a candidate and no more",
      "[CANDIDATES] holdco · GBP Main · 2026-10-01" in o and "[NOTHING FOUND] holdco · GBP Main · 2026-10-07" in o, o)

# ---- refresh keeps both legs of an exchange --------------------------------
two = TMP / "two.toml"
two.write_text('''
[company]
reporting_currency = "USD"
[[entities]]
key = "A"
base_currency = "GBP"
xero_name = "Alpha Ltd"
  [[entities.bank_accounts]]
  provider = "revolut"
  slug = "a"
  currency = "GBP"
  [[entities.bank_accounts]]
  provider = "revolut"
  slug = "a"
  currency = "USD"
''')
os.environ["AGENT_GROUP_CONFIG"] = str(two)
config.reload()
leg = {"id": "abcd1234ef", "state": "completed", "date": "2026-10-07", "account_name": "Main",
       "description": "Exchanged", "completed_at": "2026-10-07T10:00:00Z"}
bankfeed._client = lambda source, slug: types.SimpleNamespace(statement_lines=lambda from_date: [
    {**leg, "amount": -100.0, "currency": "GBP"}, {**leg, "amount": 130.0, "currency": "USD"}])
bankfeed.FEED = TMP / "bank2"
o = out(bankfeed.cmd_refresh, "A", False, "2026-10-01")
body = (TMP / "bank2" / "a.md").read_text()
check("both legs of one exchange id are kept", body.count("rev:abcd1234") == 2
      and "GBP 100.00" in body and "USD 130.00" in body, body)
o = out(bankfeed.cmd_refresh, "A", False, "2026-10-01", True)
check("a second refresh adds nothing and --brief lists nothing", "0 new" in o and " + [" not in o
      and (TMP / "bank2" / "a.md").read_text().count("rev:abcd1234") == 2, o)

os.environ["AGENT_GROUP_CONFIG"] = str(REPO / "config" / "group.example.toml")
config.reload()

# ---- the bill feed ----------------------------------------------------------
(TMP / "bills").mkdir()
(TMP / "bills" / "holdco.md").write_text("""# Bill feed: Example Holdings Limited (HoldCo) · pulled 2026-10-07T09:35Z

2026-08-31 | Example Analytics Corp | USD 4100.00 | Software | ED005430826 |  | 2026-09-30 | xid:aa000001
2026-09-25 | Acme Hosting | EUR 152.40 | Prepayments | INV-77201 |  | 2026-10-25 | xid:aa000002
2026-10-02 | Station Deli Ltd | USD 11.45 | Staff Welfare | PL-2 |  | 2026-10-02 | xid:11111111
""")
(TMP / "bills" / "opco-us.md").write_text("""# Bill feed: Example Operations Inc. (OpCo US) · pulled 2026-10-07T09:35Z

2026-10-05 | Acme Hosting | USD 25.00 | Hosting | AH-2 |  | 2026-10-05 | xid:22222222
""")
billfeed.FEED, billfeed.BANKFEED = TMP / "bills", TMP / "bank"
o = out(billfeed.main, ["list", "all", "--grep", "example analytics"])
check("billfeed list all --grep prints one bill", o.count("Example Analytics Corp") == 1 and "Hosting" not in o and "HoldCo" in o, o)
o = out(billfeed.main, ["list", "holdco"])
check("billfeed list <entity> still prints every bill", "Example Analytics Corp" in o and "INV-77201" in o)
o = out(billfeed.main, ["status"])
check("billfeed status counts per entity", "holdco" in o and "3 open" in o and "USD 4,111.45" in o, o)
full = out(billfeed.main, ["match", "--offline"])
o = out(billfeed.main, ["match", "--offline", "--cross"])
check("match --cross prints only the cross-entity pairs",
      "SAME ENTITY" not in o and "CROSS ENTITY" in o and "report the bill): 1" in o and "Station Deli Ltd" in o, o)
o = out(billfeed.main, ["match", "--offline", "--summary"])
check("match --summary prints the two counts", o.count("\n") == 2 and "SAME ENTITY" in full, o)

# the cross-currency exact rule: HoldCo's GBP card payment is the OpCo US bill in EUR
legs = {"holdco": {"rev:1a2b3c01": [{"currency": "GBP", "amount": -7.60, "bill_currency": "EUR",
                                      "bill_amount": -8.90}]}}
rows = [("holdco", "GBP Main", "2026-10-01", "Corner Cafe", "spent", 7.60, "GBP", "rev:1a2b3c01")]
(TMP / "bills" / "opco-eu.md").write_text("""# Bill feed: Example Operations B.V. (OpCo EU) · pulled 2026-10-07T09:35Z

2026-09-28 | Corner Cafe BV | EUR 8.90 | Meals | CC-1 |  | 2026-10-01 | xid:33333333
2026-09-28 | Corner Cafe BV | EUR 8.95 | Meals | CC-2 |  | 2026-10-01 | xid:44444444
""")
same, cross = billfeed.pairs(0, legs, rows)
check("a leg whose own bill amount is the bill's AmountDue is an exact cross-currency match",
      len(cross) == 1 and cross[0][1].split("|")[4].strip() == "CC-1" and "rate 0.853933 GBP per EUR" in cross[0][7]
      and "payer's bank GBP 7.60" in cross[0][7], str(cross))
same, cross = billfeed.pairs(0, {"holdco": {"rev:1a2b3c01": [{"currency": "GBP", "amount": -7.60,
                                                                "bill_currency": "EUR", "bill_amount": -8.91}]}}, rows)
check("a bill amount a cent out is no match", not cross, str(cross))
same, cross = billfeed.pairs(0, None, rows)
check("without the legs a cross-currency amount is not matched at all", not cross)
check("exact_cross_currency never fires in one currency",
      billfeed.exact_cross_currency("GBP", 7.60, "GBP", 7.60, [{"currency": "GBP", "amount": -7.60,
                                                               "bill_currency": "GBP", "bill_amount": -7.60}]) is None)

print(f"\n{'FAIL' if FAILS else 'PASS'}: {len(FAILS)} failed")
sys.exit(1 if FAILS else 0)
