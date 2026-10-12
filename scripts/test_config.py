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
ap = config.accounts_payable()
check((ap["named_lead_days"], ap["named_lead_min"], ap["named_amount_tolerance"]) == (35, 1000, 0.01),
      "AP named-line defaults")
check((ap["stranger_fx_tolerance"], ap["stranger_days"]) == (0.02, 4), "AP stranger-charge defaults")
check(ap["periodic_billing"] == [] and ap["never_duplicate"] == [] and ap["agent_payees"] == {},
      "no periodic billing, never-duplicate or agent payees unless configured")
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

# -- balance report settings -------------------------------------------------------

check(config.quarter_end_from() is None, "no quarter-end copies unless configured")
check(config.period_reader() == {"model": "haiku", "effort": "low", "max_usd": 2.0,
                                 "max_files": 8, "timeout_seconds": 600},
      "period reader defaults")
check(config.fy_quarter_end_months() == frozenset({3, 6, 9, 12}), "quarters end in 3, 6, 9, 12 on a 12-31 year end")
check(config.fy_quarter_of("2026-09") == (3, 2026) and config.fy_quarter_of("2026-12") == (4, 2026),
      "financial quarter number and year on a 12-31 year end")

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

with_config(BASE.replace('[company]\n', '[company]\nfinancial_year_end = "03-31"\n')
            + '[reports]\nquarter_end_from = "2026-06"\n[reports.period_reader]\nmodel = "sonnet"\nmax_files = 3\n')
config.reload()
check(config.quarter_end_from() == "2026-06", "quarter_end_from reads YYYY-MM")
check(config.fy_quarter_end_months() == frozenset({3, 6, 9, 12}), "a 03-31 year end ends quarters in 3, 6, 9, 12")
check(config.fy_quarter_of("2026-06") == (1, 2027) and config.fy_quarter_of("2026-03") == (4, 2026),
      "a quarter ending June is Q1 of the financial year ending March next")
check(config.period_reader()["model"] == "sonnet" and config.period_reader()["max_files"] == 3
      and config.period_reader()["effort"] == "low", "period reader overrides merge over the defaults")
with_config(BASE.replace('[company]\n', '[company]\nfinancial_year_end = "06-30"\n'))
config.reload()
check(config.fy_quarter_end_months() == frozenset({3, 6, 9, 12}) and config.fy_quarter_of("2026-09") == (1, 2027),
      "a 06-30 year end: September ends Q1")
for bad in ("Sept 2026", "2026-13"):
    with_config(BASE + f'[reports]\nquarter_end_from = "{bad}"\n')
    config.reload()
    try:
        config.quarter_end_from()
        check(False, f"refuses quarter_end_from {bad!r}")
    except config.ConfigError:
        check(True, f"refuses quarter_end_from {bad!r}")
with_config(BASE + '[reports.period_reader]\nmax_usd = 0\n')
config.reload()
try:
    config.period_reader()
    check(False, "refuses a zero cost cap")
except config.ConfigError:
    check(True, "refuses a zero cost cap")

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

with_config(BASE + '[accounts_payable]\nnamed_lead_days = 20\nnever_duplicate = ["Example Takeaway"]\n'
            '[accounts_payable.agent_payees]\nacme = ["hotelsite"]\n'
            '[[accounts_payable.periodic_billing]]\nentity = "A"\npattern = "example rides"\n'
            'from = "2026-01-01"\nreason = "monthly debit"\n')
config.reload()
ap = config.accounts_payable()
check(ap["named_lead_days"] == 20 and ap["named_lead_min"] == 1000, "an AP tunable overrides only itself")
check(ap["agent_payees"] == {"acme": ["hotelsite"]} and ap["never_duplicate"] == ["Example Takeaway"],
      "agent payees and never-duplicate load")
check(ap["periodic_billing"][0]["from"] == "2026-01-01" and ap["periodic_billing"][0]["entity"] == "A",
      "a periodic billing entry loads")

os.environ["AGENT_GROUP_CONFIG"] = str(ROOT / "config" / "group.example.toml")
config.reload()
check(config.auto_resolve()["act_from"] == "medium", "the example acts from medium")

# -- [models], and the financial quarter helpers the runners use ---------------
for _k in ("AGENT_MODEL", "AGENT_QUERY_MODEL", "AGENT_SMALL_MODEL"):
    os.environ.pop(_k, None)
with_config(BASE)
config.reload()
check(config.models() == {"default": "opus", "query": "claude-sonnet-5-5", "small": "haiku"},
      "no [models] section: the built-in defaults")
with_config(BASE + '[models]\nquery = "claude-test-1"\n')
config.reload()
check(config.model("query") == "claude-test-1" and config.model() == "opus",
      "[models] overrides one key and leaves the rest")
os.environ["AGENT_QUERY_MODEL"] = "from-env"
check(config.model("query") == "from-env", "the environment overrides [models]")
os.environ.pop("AGENT_QUERY_MODEL")
with_config(BASE + '[models]\nfast = "x"\n')
config.reload()
try:
    config.models()
    check(False, "an unknown [models] key is refused")
except config.ConfigError:
    check(True, "an unknown [models] key is refused")

# -- [investigation] ------------------------------------------------------------
with_config(BASE)
config.reload()
inv = config.investigation()
check(inv["enabled"] is True and inv["daily_cap_usd"] == 35.0 and inv["batch_max"] == 8
      and inv["debounce_seconds"] == 300 and inv["reuse_days"] == 14
      and inv["researchers_parallel"] == 2 and inv["max_rounds"] == 3,
      "no [investigation] section: the built-in defaults")
check(inv["coordinator_model"] == "opus" and inv["researcher_model"] == "haiku",
      "the investigation models default to [models] default and small")
with_config(BASE + '[models]\ndefault = "m-big"\nsmall = "m-small"\n[investigation]\ndaily_cap_usd = 10\nbatch_max = 3\n')
config.reload()
inv = config.investigation()
check(inv["daily_cap_usd"] == 10.0 and inv["batch_max"] == 3 and inv["reuse_days"] == 14,
      "[investigation] overrides one key and leaves the rest")
check(inv["coordinator_model"] == "m-big" and inv["researcher_model"] == "m-small",
      "the investigation models follow [models]")
for bad, what in (('[investigation]\nbatch_max = 0\n', "a batch of none"),
                  ('[investigation]\ndaily_cap_usd = "lots"\n', "a cap that is not a number"),
                  ('[investigation]\nenabled = "yes"\n', "enabled that is not a boolean"),
                  ('[investigation]\nturbo = 1\n', "an unknown key")):
    with_config(BASE + bad)
    config.reload()
    try:
        config.investigation()
        check(False, f"[investigation] refuses {what}")
    except config.ConfigError:
        check(True, f"[investigation] refuses {what}")
with_config(BASE)
config.reload()
check(config.default_admin() == "admin", "no admins: the register owner is the word admin")
with_config(BASE + '[slack.admins]\nU1 = "First Admin"\nU2 = "Second Admin"\n')
config.reload()
check(config.default_admin() == "First Admin", "the register owner is the first admin")

D = datetime.date
with_config(BASE)
config.reload()
check(config.latest_quarter_end(D(2026, 10, 7)) == D(2026, 9, 30), "12-31 year end: 7 Oct is after the Q3 end")
check(config.latest_quarter_end(D(2026, 9, 30)) == D(2026, 9, 30), "a quarter end is its own latest quarter end")
check(config.latest_quarter_end(D(2026, 1, 15)) == D(2025, 12, 31), "January looks back to the previous year end")
check(config.fy_quarter_label(D(2026, 9, 30)) == (3, 2026), "30 Sep is Q3 of the year ending 2026")
with_config(BASE.replace('[company]\n', '[company]\nfinancial_year_end = "03-31"\n'))
config.reload()
check(config.latest_quarter_end(D(2026, 10, 7)) == D(2026, 9, 30), "03-31 year end: 30 Sep ends Q2")
check(config.fy_quarter_label(D(2026, 9, 30)) == (2, 2027), "30 Sep 2026 is Q2 of the year ending Mar 2027")
check(config.fy_quarter_label(D(2026, 3, 31)) == (4, 2026), "31 Mar 2026 is Q4 of the year ending Mar 2026")

# -- the bank reconcile check's settings ---------------------------------------

os.environ["AGENT_GROUP_CONFIG"] = str(ROOT / "config" / "group.example.toml")
config.reload()
br = config.bank_reconcile()
check(br["fx_tolerance"] == 0.02 and br["window_cap_days"] == 30 and br["watermark_pad_days"] == 3
      and br["pad_days"] == 10 and br["record_gap_named_days"] == 45 and br["tip_max"] == 1.25,
      "no [bank_reconcile] section: the defaults")
check(br["line_match_model"] == "haiku" and br["staff_register"] == "" and br["agent_bill_number_initials"] == "",
      "the staff register and the bill-number initials are off by default")
check(config.reconcile_notify() == "", "no reconcile_notify and no unpaid_notify: nobody")
check([e.bank_check_mode for e in config.entities()] == ["full", "full", "full"],
      "an entity with a bank account is checked in full")

with_config(BASE + '[accounts_payable]\nunpaid_notify = "U1"\n')
config.reload()
check(config.reconcile_notify() == "U1", "reconcile_notify falls back to unpaid_notify")
with_config(BASE + '[accounts_payable]\nunpaid_notify = "U1"\nreconcile_notify = "U2"\n')
config.reload()
check(config.reconcile_notify() == "U2", "reconcile_notify wins when set")
check([e.bank_check_mode for e in config.entities()] == ["documents", "documents"],
      "an entity with no bank account is read for documents only")
with_config(BASE.replace('xero_name = "Beta Ltd"', 'xero_name = "Beta Ltd"\nreconcile_check = "off"'))
config.reload()
check(config.entity("B").bank_check_mode == "off", "reconcile_check = off is kept")
refuses(BASE.replace('xero_name = "Beta Ltd"', 'xero_name = "Beta Ltd"\nreconcile_check = "sometimes"'),
        "a reconcile_check that is not full, documents or off")

with_config(BASE + '[bank_reconcile]\nwindow_cap_days = 14\nline_match_model = "small"\n')
config.reload()
check(config.bank_reconcile()["window_cap_days"] == 14 and config.bank_reconcile()["line_match_model"] == "small"
      and config.bank_reconcile()["pad_days"] == 10, "[bank_reconcile] overrides a key and keeps the rest")
for bad, what in (('fx_tolerance = -1', "a negative tolerance"), ('pad_days = "ten"', "a pad that is not a number"),
                  ('line_match_model = 3', "a model that is not text"),
                  ('agent_bill_number_initials = "("', "a bill-number pattern that is not a regex"),
                  ('agent_bill_number_initials = "^A-(.)-(.)"', "a bill-number pattern with two groups")):
    with_config(BASE + '[bank_reconcile]\n' + bad + '\n')
    config.reload()
    try:
        config.bank_reconcile()
    except config.ConfigError:
        check(True, f"refuses {what}")
    else:
        check(False, f"refuses {what}")

with_config(BASE.replace('xero_name = "Alpha Ltd"', 'xero_name = "Alpha Ltd"\n[[entities.bank_accounts]]\n'
                         'provider = "mercury"\nslug = "a"\nstatement_name = "Checking xx1"\nxero_account = "Operating"'))
config.reload()
ba = config.bank_accounts("A")[0]
check(ba.statement_name == "Checking xx1" and ba.xero_account == "Operating",
      "a bank account carries its statement name and Xero account name")

# -- the bill poster's restrictions -----------------------------------------

with_config(BASE)
config.reload()
check(config.posting()["restricted_tax_types"] == {} and all(not e.required_tracking for e in config.entities()),
      "no [posting] and no required_tracking: nothing restricted")
with_config(BASE.replace('xero_name = "Beta Ltd"', 'xero_name = "Beta Ltd"\nrequired_tracking = ["Region"]')
            + '[posting]\nrestricted_tax_types = { "XTAX" = ["B"] }\n')
config.reload()
check(config.entity("B").required_tracking == ("Region",) and config.entity("A").required_tracking == ()
      and config.posting()["restricted_tax_types"] == {"XTAX": ("B",)},
      "required_tracking and restricted_tax_types are read")
refuses(BASE.replace('xero_name = "Beta Ltd"', 'xero_name = "Beta Ltd"\nrequired_tracking = "Region"'),
        "required_tracking that is not a list")
with_config(BASE + '[posting]\nrestricted_tax_types = { "XTAX" = ["NOPE"] }\n')
config.reload()
try:
    config.posting()
except config.ConfigError:
    check(True, "refuses a restricted tax type naming an unknown entity")
else:
    check(False, "refuses a restricted tax type naming an unknown entity")

# -- [outstanding_check] ---------------------------------------------------------

with_config(BASE)
config.reload()
check(config.outstanding_check() == {"enabled": True, "batch_size": 25, "min_xero_calls": 800,
                                     "checker_model": "claude-sonnet-5-5",
                                     "light_checker_model": config.model("small"), "trivial_amount": 0.0},
      "no [outstanding_check] section: the defaults, light checkers on [models] small, trivial off")
with_config(BASE + '[outstanding_check]\nbatch_size = 10\nchecker_model = "claude-test-1"\n'
            'light_checker_model = "claude-test-light"\ntrivial_amount = 75\n')
config.reload()
oc_ = config.outstanding_check()
check(oc_["batch_size"] == 10 and oc_["checker_model"] == "claude-test-1" and oc_["min_xero_calls"] == 800
      and oc_["enabled"] is True, "[outstanding_check] overrides some keys and leaves the rest")
check(oc_["light_checker_model"] == "claude-test-light" and oc_["trivial_amount"] == 75.0,
      "[outstanding_check] light_checker_model and trivial_amount are read")
with_config(BASE + '[models]\nsmall = "claude-test-small"\n')
config.reload()
check(config.outstanding_check()["light_checker_model"] == "claude-test-small",
      "light_checker_model follows [models] small when unset")
for _body, _what in (('enabled = "yes"', "an enabled flag that is not a boolean"),
                     ("batch_size = 0", "a batch size of zero"),
                     ("min_xero_calls = -1", "a negative Xero calls floor"),
                     ("trivial_amount = -5", "a negative trivial amount"),
                     ('trivial_amount = "300"', "a trivial amount that is not a number"),
                     ("batchsize = 5", "an unknown [outstanding_check] key")):
    with_config(BASE + "[outstanding_check]\n" + _body + "\n")
    config.reload()
    try:
        config.outstanding_check()
        check(False, f"refuses {_what}")
    except config.ConfigError:
        check(True, f"refuses {_what}")
os.environ["AGENT_GROUP_CONFIG"] = str(ROOT / "config" / "group.example.toml")
config.reload()

# -- the review checks' settings ------------------------------------------------

os.environ["AGENT_GROUP_CONFIG"] = str(ROOT / "config" / "group.example.toml")
config.reload()
rv = config.review_checks()
check(rv["capitalisation_threshold"] == 500.0 and rv["movement_ratio"] == 2.5 and rv["aged_days"] == 90
      and rv["unreconciled_old_days"] == 30 and rv["far_tolerance"] == 1.0,
      "no [review_checks] section: the defaults")
check(all(rv[t] == [] for t in ("known_bad_accounts", "cutovers", "tracking", "tax", "revenue", "routing",
                                 "register_depreciated", "clearing_accounts", "not_duplicates")),
      "no [review_checks] section: every rule table is empty")

RC = BASE + """[review_checks]
capitalisation_threshold = 750
not_duplicates = ["usage"]
[[review_checks.known_bad_accounts]]
entity = "a"
code = "803"
name_pattern = "wages"
why = "system account"
[[review_checks.cutovers]]
kind = "no_bills"
entity = "B"
from = "2026-08-01"
match = ["payroll"]
why = "the control accounts carry it"
[[review_checks.routing]]
supplier_pattern = "health"
account_pattern = "health"
must = "be_on"
"""
with_config(RC)
config.reload()
rv = config.review_checks()
check(rv["capitalisation_threshold"] == 750 and rv["aged_days"] == 90, "[review_checks] overrides a key and keeps the rest")
check(rv["known_bad_accounts"][0]["entity"] == "A" and rv["cutovers"][0]["entity"] == "B",
      "[review_checks] tables resolve entity names to keys")
check("entity" not in rv["routing"][0], "a routing rule may name no entity")
for bad, what in (('capitalisation_threshold = -1', "a negative threshold"),
                  ('aged_days = "ninety"', "a threshold that is not a number"),
                  ('not_duplicates = ["("]', "a pattern list with a bad regex"),
                  ('twin_reference_pattern = "(a)(b)"', "a twin reference pattern with two groups"),
                  ('tracking = 3', "a table that is not a list"),
                  ('[[review_checks.cutovers]]\nkind = "sideways"\nentity = "A"\nfrom = "2026-01-01"\nwhy = "x"',
                   "an unknown cutover kind"),
                  ('[[review_checks.cutovers]]\nkind = "no_bills"\nentity = "A"\nfrom = "soon"\nwhy = "x"',
                   "a cutover date that is not a date"),
                  ('[[review_checks.cutovers]]\nkind = "no_bills"\nentity = "Z"\nfrom = "2026-01-01"\nwhy = "x"',
                   "a cutover for an unknown entity"),
                  ('[[review_checks.known_bad_accounts]]\nentity = "A"\ncode = "1"',
                   "a known-bad account with no reason"),
                  ('[[review_checks.routing]]\nsupplier_pattern = "x"\naccount_pattern = "y"\nmust = "maybe"',
                   "a routing rule that is neither be_on nor not_be_on")):
    with_config(BASE + "[review_checks]\n" + bad + "\n")
    config.reload()
    try:
        config.review_checks()
    except config.ConfigError:
        check(True, f"refuses {what}")
    else:
        check(False, f"refuses {what}")

# -- [daily_chain] ---------------------------------------------------------------

with_config(BASE)
config.reload()
check(config.daily_chain() == {"enabled": True, "attachment_lookback_days": 6},
      "no [daily_chain] section: the defaults")
with_config(BASE + "[daily_chain]\nattachment_lookback_days = 3\n")
config.reload()
check(config.daily_chain() == {"enabled": True, "attachment_lookback_days": 3},
      "[daily_chain] overrides one key and leaves the other")
for _body, _what in (('enabled = "yes"', "an enabled flag that is not a boolean"),
                     ("attachment_lookback_days = 0", "a look-back of zero days"),
                     ("attachment_lookback_days = 8", "a look-back past the ledger's seven days"),
                     ("lookback = 5", "an unknown [daily_chain] key")):
    with_config(BASE + "[daily_chain]\n" + _body + "\n")
    config.reload()
    try:
        config.daily_chain()
        check(False, f"refuses {_what}")
    except config.ConfigError:
        check(True, f"refuses {_what}")
os.environ["AGENT_GROUP_CONFIG"] = str(ROOT / "config" / "group.example.toml")
config.reload()

# -- [briefing] -----------------------------------------------------------------

with_config(BASE)
config.reload()
_b = config.briefing()
check(_b["window_days"] == 7 and _b["read_documents"] is True and _b["reader_model"] == config.model("small")
      and _b["second_look_model"] == config.model("query") and _b["reader_max_usd"] == 0.5,
      "no [briefing] section: the defaults, the models from [models]")
with_config(BASE + '[briefing]\nwindow_days = 5\nread_documents = false\nreader_model = "opus"\n')
config.reload()
_b = config.briefing()
check(_b["window_days"] == 5 and _b["read_documents"] is False and _b["reader_model"] == "opus"
      and _b["fresh_hours"] == 2, "[briefing] overrides its keys and leaves the rest")
for _body, _what in (("window_days = 8", "a window past the ledger's seven days"),
                     ('read_documents = "yes"', "a read_documents flag that is not a boolean"),
                     ("reader_max_usd = 0", "a zero budget per read"),
                     ("workers = 0", "no workers"),
                     ("window = 5", "an unknown [briefing] key")):
    with_config(BASE + "[briefing]\n" + _body + "\n")
    config.reload()
    try:
        config.briefing()
        check(False, f"refuses {_what}")
    except config.ConfigError:
        check(True, f"refuses {_what}")

os.environ["AGENT_GROUP_CONFIG"] = str(ROOT / "config" / "group.example.toml")
config.reload()

print()
print("PASS" if not FAILS else f"FAIL ({len(FAILS)})")
sys.exit(1 if FAILS else 0)
