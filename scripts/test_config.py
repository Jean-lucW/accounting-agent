#!/usr/bin/env python
"""Offline checks for the group configuration loader.

Runs against config/group.example.toml and a few deliberately broken
configurations, so a change to config.py cannot silently re-shape every
feed, report and reconciliation. No network, no credentials.

    .venv/bin/python scripts/test_config.py
"""

from __future__ import annotations

import datetime
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.environ["AGENT_GROUP_CONFIG"] = str(ROOT / "config" / "group.example.toml")

from accounting_agent import config  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, what: str) -> None:
    print(("ok    " if cond else "FAIL  ") + what)
    if not cond:
        FAILS.append(what)


def with_config(text: str):
    tmp = tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False)
    tmp.write(text)
    tmp.close()
    os.environ["AGENT_GROUP_CONFIG"] = tmp.name
    return config.reload


def refuses(text: str, what: str) -> None:
    with_config(text)
    try:
        config.reload()
    except config.ConfigError:
        check(True, f"refuses {what}")
    else:
        check(False, f"refuses {what}")


# -- the example file --------------------------------------------------------

config.reload()
keys = config.entity_keys()
check(keys == ["HOLDCO", "OPCO_US", "OPCO_EU"], "example entities load in file order")
check(config.entity("us").key == "OPCO_US", "alias resolves")
check(config.entity("opco-eu").key == "OPCO_EU", "slug resolves")
check(config.entity("Example Holdings Limited").key == "HOLDCO", "exact Xero name resolves")
check(config.entity("HoldCo").slug == "holdco", "short name resolves; slug is lower-hyphen")

hb = config.bank_accounts("HOLDCO")[0]
check(hb.has_api and hb.api_from == "2026-01-01", "CSV cut-over gives the API start date")
eu = config.bank_accounts("OPCO_EU")[0]
check(not eu.has_api and eu.provider == "csv", "csv-only bank account has no API")

pairs = config.intercompany_pairs()
check(len(pairs) == 4, "every intercompany block becomes a pair")
gap = [p for p in pairs if not p.complete]
check(len(gap) == 1 and gap[0].account("OPCO_EU") is None, "missing mirror account is a structural gap")
check(len(config.intercompany_pairs("OPCO_EU")) == 2, "pairs filter by entity")
check({"814", "825", "2100"} <= config.phantom_exempt_accounts(), "payroll controls are phantom-check exempt")
check(config.group_name_pattern().search("payment from EXAMPLE OPERATIONS INC.") is not None,
      "group name pattern matches entity names in free text")
check([d.key for d in config.domains()] == ["bookkeeping", "reports"], "domains load")
check(config.accounts_payable()["lead_days"] == 7, "AP settings load")
check(config.accounts_payable()["generic_supplier_words"] == [], "generic supplier words default to none")
check(config.bank_fee_tariffs() == {}, "no bank fee tariff unless configured")
check(config.reporting_currency() == "USD", "reporting currency loads")
check(config.company("timezone") == "UTC", "example timezone is UTC")
check([e.base_currency for e in config.entities()] == ["GBP", "USD", "EUR"],
      "example entities are GBP, USD and EUR")

# -- financial year: default 12-31 is the calendar year ------------------------

D = datetime.date
check(config.financial_year_end() == (12, 31), "financial year end defaults to 12-31")
check(config.fy_start(2026) == D(2026, 1, 1) and config.fy_end(2026) == D(2026, 12, 31),
      "default financial year is the calendar year")
check(config.fy_start_for(D(2026, 8, 15)) == D(2026, 1, 1), "default fy_start_for is 1 January")
check(config.fy_quarter_start(D(2026, 8, 15)) == D(2026, 7, 1), "default quarter is the calendar quarter")
check(config.default_history_start(D(2026, 8, 15)) == D(2025, 1, 1),
      "default history start is the previous financial year")

# -- the legal-form list is shared ----------------------------------------------

check({"ltd", "gmbh", "sarl", "bv", "vof", "aps", "private", "pty"} <= config.LEGAL_FORMS,
      "one international legal-form list")
check(config.statement_account_name("revolut", "GBP", "Main") == "GBP Main"
      and config.statement_account_name("revolut", "EUR", "EUR Savings") == "EUR Savings"
      and config.statement_account_name("revolut", "USD", "", "Revolut") == "USD Revolut"
      and config.statement_account_name("mercury", "USD", "Checking \u2022\u20221234") == "Checking xx1234",
      "statement account names follow the API name, not a fixed suffix")

# -- impossible structures are refused ----------------------------------------

BASE = '''
[company]
reporting_currency = "USD"
[[entities]]
key = "A"
base_currency = "GBP"
xero_name = "Alpha Ltd"
[[entities]]
key = "B"
base_currency = "EUR"
xero_name = "Beta Ltd"
'''
refuses('[company]\nname="x"\n', "a file with no entities")
refuses(BASE + '[[entities]]\nkey = "A"\nbase_currency = "GBP"\nxero_name = "Again"\n', "duplicate entity keys")
refuses(BASE.replace('base_currency = "EUR"\n', ''), "an entity with no base_currency")
refuses(BASE.replace('reporting_currency = "USD"\n', ''), "a group with no reporting_currency")
refuses(BASE.replace('reporting_currency = "USD"', 'reporting_currency = "US Dollar"'),
        "a reporting_currency that is not an ISO code")
refuses(BASE.replace('[company]\n', '[company]\nfinancial_year_end = "31-03"\n'),
        "a financial_year_end that is not MM-DD")
refuses(BASE.replace('key = "A"', 'key = "alpha"'), "a lower-case entity key")
refuses(BASE + '[[intercompany]]\na = "A"\nb = "Z"\naccounts = { A = "1" }\n', "a pair naming an unknown entity")
refuses(BASE + '[[intercompany]]\na = "A"\nb = "B"\naccounts = { A = "1", B = "2" }\n' * 2,
        "the same pair and flavour twice")
refuses(BASE + '[slack.admins]\nU1 = "x"\n[slack.users]\nU1 = "x"\n', "a member in two Slack tiers")
refuses(BASE.replace('xero_name = "Alpha Ltd"', 'xero_name = "Alpha Ltd"\n[[entities.bank_accounts]]\nprovider = "revolut"'),
        "an API bank account without a slug")

with_config(BASE)
config.reload()
check(config.intercompany_pairs() == [], "a group with no intercompany pairs is valid")
check(config.financial_year_end() == (12, 31), "no financial_year_end key means 12-31")

with_config(BASE.replace('[company]\n', '[company]\nfinancial_year_end = "03-31"\n'))
config.reload()
check(config.fy_start(2026) == D(2025, 4, 1) and config.fy_end(2026) == D(2026, 3, 31),
      "03-31 year end: fy_start(2026) is 1 April 2025")
check(config.fy_start_for(D(2026, 3, 31)) == D(2025, 4, 1)
      and config.fy_start_for(D(2026, 4, 1)) == D(2026, 4, 1), "fy_start_for crosses at the year end")
check(config.fy_quarter_start(D(2026, 8, 15)) == D(2026, 7, 1)
      and config.fy_quarter_start(D(2026, 2, 10)) == D(2026, 1, 1)
      and config.fy_quarter_start(D(2026, 5, 31)) == D(2026, 4, 1), "financial quarters from 1 April")
check(config.default_history_start(D(2026, 8, 15)) == D(2025, 4, 1),
      "history start is the previous financial year's first day")

with_config(BASE.replace('[company]\n', '[company]\nfinancial_year_end = "06-30"\n'))
config.reload()
check(config.fy_year_for(D(2026, 7, 1)) == 2027 and config.fy_start_for(D(2026, 7, 1)) == D(2026, 7, 1),
      "06-30 year end: 1 July opens the financial year ending the next June")

# -- [auto_resolve]: what the agent may decide without asking -------------------

refuses(BASE + '[auto_resolve]\nact_from = "low"\n', "acting on low-confidence answers")
refuses(BASE + '[auto_resolve]\nact_from = "medium"\nmedium_limit = -1\n', "a negative limit")
refuses(BASE + '[auto_resolve]\nact_from = "medium"\nhigh_limit = "lots"\n', "a limit that is not a number")

with_config(BASE)
config.reload()
check(config.resolve_action("high", 10)[0] == "query", "no [auto_resolve] section: every answer is queried")

with_config(BASE + '[auto_resolve]\nact_from = "medium"\nmedium_limit = 2500\nhigh_limit = 25000\n')
config.reload()
check(config.resolve_action("high", 24000)[0] == "act", "high under the high limit: act")
check(config.resolve_action("high", 26000)[0] == "query", "high over the high limit: query")
check(config.resolve_action("medium", 2000)[0] == "confirm", "medium under the medium limit: act and confirm")
check(config.resolve_action("medium", 3000)[0] == "query", "medium over the medium limit: query")
check(config.resolve_action("medium", -2000)[0] == "confirm"
      and config.resolve_action("medium", -3000)[0] == "query", "a credit is limited by its size")
check(config.resolve_action("low", 1)[0] == "query", "low: always query")
check("USD 2,500.00" in config.resolve_action("medium", 3000)[1], "the reason names the limit and currency")

with_config(BASE + '[auto_resolve]\nact_from = "high"\n')
config.reload()
check(config.resolve_action("high", 10**9)[0] == "act", "no limits set: high acts at any size")
check(config.resolve_action("medium", 1)[0] == "query", "act_from high: medium is queried")

os.environ["AGENT_GROUP_CONFIG"] = str(ROOT / "config" / "group.example.toml")
config.reload()
check(config.auto_resolve()["act_from"] == "medium", "the example acts from medium")

print()
print("PASS" if not FAILS else f"FAIL ({len(FAILS)})")
sys.exit(1 if FAILS else 0)
