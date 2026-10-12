"""The group configuration: config/group.toml, read once, used everywhere.

Every script that needs to know the shape of the group (entities, bank
accounts, intercompany pairs, Slack tiers, payroll controls, AP and report
settings) asks this module. Nothing structural is hard-coded anywhere else,
so editing config/group.toml re-shapes every feed, report and reconciliation
on its next run.

    from accounting_agent import config

    config.entities()                 # [Entity, ...] in file order
    config.entity("parent")           # by key, alias, short name or Xero name
    config.bank_accounts("HOLDCO")    # [BankAccount, ...]
    config.intercompany_pairs()       # [Pair, ...]
    config.slack().admins             # {member_id: name}

The file path can be overridden with AGENT_GROUP_CONFIG (tests point it at
config/group.example.toml).
"""

from __future__ import annotations

import calendar
import datetime
import os
import re
import tomllib
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PATH = ROOT / "config" / "group.toml"
EXAMPLE_PATH = ROOT / "config" / "group.example.toml"


class ConfigError(Exception):
    """config/group.toml is missing or describes something impossible."""


# [auto_resolve] act_from: the lowest grade the agent acts on without asking.
GRADES_ACTED_FROM = ("off", "high", "medium")


# --------------------------------------------------------------------- shapes

@dataclass(frozen=True)
class BankAccount:
    entity: str                  # entity key
    label: str                   # "Revolut", "Mercury", "Local bank"
    provider: str                # revolut | mercury | csv
    slug: str = ""               # credential selector for the API providers
    statement_csv: str = ""      # file name under data/statements/
    csv_until: str = ""          # last ISO date read from the CSV ("" = API only)
    xero_account_code: str = ""
    currency: str = ""           # optional: restrict to one currency sub-account
    statement_name: str = ""     # optional: the account's name as the statement or API gives it
    xero_account: str = ""       # optional: the Xero bank account's name (else found by code)

    @property
    def has_api(self) -> bool:
        return self.provider in ("revolut", "mercury") and bool(self.slug)

    def account_heading(self, line: dict) -> str:
        """The name a statement line's sub-account goes by in the bank feed
        and the reports: the API's own account name (statement line
        `account_name`), falling back to this account's label."""
        return statement_account_name(self.provider, line.get("currency") or self.currency,
                                      line.get("account_name"), self.label)

    @property
    def api_from(self) -> str:
        """First ISO date the API is the source; "" when there is no CSV cut-over."""
        if not self.csv_until:
            return ""
        import datetime as _dt
        return (_dt.date.fromisoformat(self.csv_until) + _dt.timedelta(days=1)).isoformat()


def statement_account_name(provider: str, currency: str | None, name: str | None,
                           fallback: str = "") -> str:
    """One naming rule for a bank sub-account across the feed, the bills report
    and the fees report. A Revolut business holds one sub-account per
    currency, so its name is prefixed with the currency unless it already
    carries it ("Main" -> "GBP Main"); any other provider's name is used as
    the bank gives it. Masked digits ("••1234") are written "xx1234"."""
    clean = " ".join(str(name or "").replace("\u2022\u2022", "xx").split()) or fallback.strip()
    if provider == "revolut":
        ccy = str(currency or "").upper()
        if not clean:
            return ccy
        return clean if not ccy or ccy in clean.upper().split() else f"{ccy} {clean}"
    return clean


@dataclass(frozen=True)
class PayrollControl:
    code: str
    name: str
    residual: str                # what a residual balance may legitimately be


@dataclass(frozen=True)
class Entity:
    key: str
    xero_name: str
    short: str
    base_currency: str
    country: str = ""
    aliases: tuple[str, ...] = ()
    rules_file: str = ""
    bank_accounts: tuple[BankAccount, ...] = ()
    payroll_controls: tuple[PayrollControl, ...] = ()
    reconcile_check: str = ""    # "full" | "documents" | "off", see bank_check_mode
    required_tracking: tuple[str, ...] = ()   # tracking categories every posted line must carry

    @property
    def bank_check_mode(self) -> str:
        """How the bank reconcile check treats this entity: `full` reads its
        banks and Xero (the default for an entity with a bank account),
        `documents` reads only its open bills and invoices for the
        cross-entity search (the default with no bank account), `off` never
        reads it at all."""
        mode = self.reconcile_check or "full"
        return "documents" if mode == "full" and not self.bank_accounts else mode

    @property
    def slug(self) -> str:
        """Lower-case, hyphenated key for file names: OPCO_US -> opco-us."""
        return self.key.lower().replace("_", "-")

    @property
    def title(self) -> str:
        return f"{self.xero_name} ({self.short})"


@dataclass(frozen=True)
class Pair:
    a: str
    b: str
    flavour: str
    label: str
    accounts: dict[str, str]     # entity key -> account code ("" = missing mirror)
    payments_enabled: bool = False

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.a, self.b, self.flavour)

    @property
    def name(self) -> str:
        return f"{self.a}:{self.b}:{self.flavour}"

    def account(self, entity: str) -> str | None:
        code = self.accounts.get(entity, "")
        return code or None

    def other(self, entity: str) -> str:
        return self.b if entity == self.a else self.a

    @property
    def complete(self) -> bool:
        return bool(self.account(self.a)) and bool(self.account(self.b))


@dataclass(frozen=True)
class NonGroupAccount:
    entity: str
    account: str
    label: str


@dataclass(frozen=True)
class SlackConfig:
    channel_id: str
    channel_name: str
    admins: dict[str, str]
    users: dict[str, str]
    readonly: dict[str, str]
    nicknames: dict[str, str] = field(default_factory=dict)

    @property
    def allowed(self) -> dict[str, str]:
        return {**self.admins, **self.users, **self.readonly}


@dataclass(frozen=True)
class Domain:
    key: str
    title: str
    skill: str
    docs: tuple[str, ...] = ()


@dataclass(frozen=True)
class Group:
    path: Path
    raw: dict[str, Any]
    company: dict[str, Any]
    entities: tuple[Entity, ...]
    pairs: tuple[Pair, ...]
    non_group: tuple[NonGroupAccount, ...]
    slack: SlackConfig
    domains: tuple[Domain, ...]


# --------------------------------------------------------------------- loading

def config_path() -> Path:
    override = os.environ.get("AGENT_GROUP_CONFIG")
    return Path(override) if override else DEFAULT_PATH


def _entity(block: dict) -> Entity:
    key = str(block["key"]).strip()
    if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
        raise ConfigError(f"entity key {key!r} must be UPPER_SNAKE (e.g. HOLDCO, OPCO_US)")
    banks = tuple(
        BankAccount(
            entity=key,
            label=b.get("label", b.get("provider", "bank")),
            provider=b.get("provider", "csv").lower(),
            slug=b.get("slug", ""),
            statement_csv=b.get("statement_csv", ""),
            csv_until=b.get("csv_until", ""),
            xero_account_code=str(b.get("xero_account_code", "")),
            currency=b.get("currency", ""),
            statement_name=b.get("statement_name", ""),
            xero_account=b.get("xero_account", ""),
        )
        for b in block.get("bank_accounts", [])
    )
    for b in banks:
        if b.provider not in ("revolut", "mercury", "csv"):
            raise ConfigError(f"{key}: bank provider {b.provider!r} must be revolut, mercury or csv")
        if b.provider != "csv" and not b.slug:
            raise ConfigError(f"{key}: the {b.provider} account {b.label!r} needs a slug")
    base = str(block.get("base_currency", "")).strip().upper()
    if not re.fullmatch(r"[A-Z]{3}", base):
        raise ConfigError(
            f"entity {key}: base_currency is required, the Xero organisation's base currency "
            f"as a three-letter ISO code (e.g. GBP, USD, EUR); got {block.get('base_currency')!r}")
    mode = str(block.get("reconcile_check", "") or "").strip().lower()
    if mode not in ("", "full", "documents", "off"):
        raise ConfigError(f"entity {key}: reconcile_check must be full, documents or off; got {mode!r}")
    required = block.get("required_tracking", []) or []
    if not isinstance(required, list) or any(not isinstance(t, str) for t in required):
        raise ConfigError(f"entity {key}: required_tracking must be a list of tracking category names")
    controls = tuple(
        PayrollControl(code=str(code), name=v.get("name", ""), residual=v.get("residual", ""))
        for code, v in (block.get("payroll_controls") or {}).items()
    )
    return Entity(
        key=key,
        xero_name=block["xero_name"],
        short=block.get("short", key),
        base_currency=base,
        country=block.get("country", ""),
        aliases=tuple(a.lower() for a in block.get("aliases", [])),
        rules_file=block.get("rules_file", f"rules/entities/{key}.md"),
        bank_accounts=banks,
        payroll_controls=controls,
        reconcile_check=mode,
        required_tracking=tuple(t.strip() for t in required if t.strip()),
    )


@lru_cache(maxsize=4)
def _load(path_str: str) -> Group:
    path = Path(path_str)
    if not path.exists():
        raise ConfigError(
            f"{path} not found. Copy config/group.example.toml to config/group.toml "
            "and describe your group in it (ADAPTING.md)."
        )
    raw = tomllib.loads(path.read_text())
    entities = tuple(_entity(b) for b in raw.get("entities", []))
    if not entities:
        raise ConfigError(f"{path}: no [[entities]] defined")
    keys = [e.key for e in entities]
    if len(set(keys)) != len(keys):
        raise ConfigError(f"{path}: duplicate entity keys {keys}")

    pairs = []
    for p in raw.get("intercompany", []):
        a, b = p["a"], p["b"]
        for k in (a, b):
            if k not in keys:
                raise ConfigError(f"intercompany pair {a}/{b}: unknown entity {k!r}")
        accounts = {k: str(v) for k, v in (p.get("accounts") or {}).items()}
        for k in accounts:
            if k not in (a, b):
                raise ConfigError(f"intercompany pair {a}/{b}: account given for {k!r}, not in the pair")
        pairs.append(Pair(a=a, b=b, flavour=p.get("flavour", "loan"),
                          label=p.get("label", p.get("flavour", "loan")),
                          accounts={a: accounts.get(a, ""), b: accounts.get(b, "")},
                          payments_enabled=bool(p.get("payments_enabled", False))))
    seen = set()
    for p in pairs:
        if p.key in seen:
            raise ConfigError(f"intercompany pair {p.name} defined twice; give one a different flavour")
        seen.add(p.key)

    non_group = tuple(NonGroupAccount(entity=n["entity"], account=str(n["account"]),
                                      label=n.get("label", ""))
                      for n in raw.get("non_group", []))

    s = raw.get("slack", {})
    slack = SlackConfig(
        channel_id=s.get("channel_id", "") or os.environ.get("SLACK_CHANNEL_ID", ""),
        channel_name=s.get("channel_name", "accounting-agent"),
        admins=dict(s.get("admins", {})),
        users=dict(s.get("users", {})),
        readonly=dict(s.get("readonly", {})),
        nicknames=dict(s.get("nicknames", {})),
    )
    overlap = (set(slack.admins) & set(slack.users)) | (set(slack.admins) & set(slack.readonly)) \
        | (set(slack.users) & set(slack.readonly))
    if overlap:
        raise ConfigError(f"Slack members in more than one tier: {sorted(overlap)}")

    domains = tuple(Domain(key=d["key"], title=d.get("title", d["key"]), skill=d.get("skill", ""),
                           docs=tuple(d.get("docs", [])))
                    for d in raw.get("domains", []))

    company_block = raw.get("company", {}) or {}
    rep = str(company_block.get("reporting_currency", "") or "").strip()
    if not re.fullmatch(r"[A-Za-z]{3}", rep):
        raise ConfigError(
            f"{path}: [company] reporting_currency is required, the currency group-wide "
            "figures are shown in, as a three-letter ISO code (e.g. USD, EUR, GBP); "
            f"got {company_block.get('reporting_currency')!r}")
    fye = str(company_block.get("financial_year_end", "12-31") or "12-31").strip()
    fm = re.fullmatch(r"(\d{1,2})-(\d{1,2})", fye)
    if not (fm and 1 <= int(fm.group(1)) <= 12
            and 1 <= int(fm.group(2)) <= calendar.monthrange(2000, int(fm.group(1)))[1]):
        raise ConfigError(f"{path}: [company] financial_year_end must be MM-DD "
                          f"(e.g. 12-31, 03-31); got {fye!r}")

    ar = raw.get("auto_resolve") or {}
    if str(ar.get("act_from", "off")) not in GRADES_ACTED_FROM:
        raise ConfigError(f"{path}: [auto_resolve] act_from must be one of "
                          f"{', '.join(GRADES_ACTED_FROM)}; got {ar.get('act_from')!r}")
    for k in ("medium_limit", "high_limit", "confirm_days"):
        v = ar.get(k, 0)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
            raise ConfigError(f"{path}: [auto_resolve] {k} must be a number, 0 or more; got {v!r}")

    return Group(path=path, raw=raw, company=company_block, entities=entities,
                 pairs=tuple(pairs), non_group=non_group, slack=slack, domains=domains)


def load() -> Group:
    return _load(str(config_path()))


def reload() -> Group:
    _load.cache_clear()
    return load()


# --------------------------------------------------------------------- queries

def company(key: str | None = None, default: Any = None) -> Any:
    c = load().company
    return c if key is None else c.get(key, default)


def company_name() -> str:
    return company("name", "the group")


def reporting_currency() -> str:
    """[company] reporting_currency: required, like each entity's base_currency."""
    ccy = str(company("reporting_currency", "") or "").strip().upper()
    if not re.fullmatch(r"[A-Z]{3}", ccy):
        raise ConfigError(
            "[company] reporting_currency is required: the currency group-wide figures are "
            f"shown in, as a three-letter ISO code (e.g. USD, EUR, GBP); got "
            f"{company('reporting_currency')!r}")
    return ccy


# --------------------------------------------------------------------- financial year

def financial_year_end() -> tuple[int, int]:
    """[company] financial_year_end as (month, day); "MM-DD", default "12-31"."""
    raw = str(company("financial_year_end", "12-31") or "12-31").strip()
    m = re.fullmatch(r"(\d{1,2})-(\d{1,2})", raw)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        if 1 <= month <= 12 and 1 <= day <= _month_days(2000, month):
            return month, day
    raise ConfigError(f"[company] financial_year_end must be MM-DD (e.g. 12-31, 03-31); got {raw!r}")


def _month_days(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]


def fy_end(year: int) -> datetime.date:
    """Last day of the financial year that ends in calendar year `year`
    (a 02-29 year end falls on 28 Feb in a non-leap year)."""
    month, day = financial_year_end()
    return datetime.date(year, month, min(day, _month_days(year, month)))


def fy_start(year: int) -> datetime.date:
    """First day of the financial year that ends in calendar year `year`.
    With the default 12-31 year end this is 1 January of `year`."""
    return fy_end(year - 1) + datetime.timedelta(days=1)


def fy_year_for(d: datetime.date) -> int:
    """The calendar year in which the financial year containing `d` ends."""
    return d.year + 1 if d > fy_end(d.year) else d.year


def fy_start_for(d: datetime.date) -> datetime.date:
    """First day of the financial year containing `d`."""
    return fy_start(fy_year_for(d))


def fy_quarter_start(d: datetime.date) -> datetime.date:
    """First day of the financial quarter (three months from the financial
    year start) containing `d`. With a 12-31 year end: the calendar quarter."""
    start = fy_start_for(d)
    months = (d.year - start.year) * 12 + d.month - start.month - (1 if d.day < start.day else 0)
    k = months // 3 * 3
    y, m = divmod(start.month - 1 + k, 12)
    y, m = start.year + y, m + 1
    return datetime.date(y, m, min(start.day, _month_days(y, m)))


def latest_quarter_end(d: datetime.date) -> datetime.date:
    """The last financial quarter end on or before `d` (a quarter is three
    months from the financial year start; `d` itself when it is one)."""
    return fy_quarter_start(d + datetime.timedelta(days=1)) - datetime.timedelta(days=1)


def fy_quarter_label(d: datetime.date) -> tuple[int, int]:
    """(quarter number 1-4, the calendar year the financial year ends in) for
    the financial quarter containing `d`."""
    start, q = fy_start_for(d), fy_quarter_start(d)
    months = (q.year - start.year) * 12 + q.month - start.month
    return months // 3 + 1, fy_year_for(d)


def default_history_start(today: datetime.date | None = None) -> datetime.date:
    """Start of the previous financial year: the default look-back when a
    history start (records_from, first_build_from) is not configured."""
    today = today or datetime.date.today()
    return fy_start(fy_year_for(today) - 1)


def records_from() -> str:
    """[company] records_from, else the start of the previous financial year."""
    return str(company("records_from", "") or default_history_start().isoformat())


# Legal-form words in company names, as tokens after splitting on anything
# that is not a letter or digit ("B.V." -> "b", "v"; "Sp. z o.o." -> "sp",
# "z", "o"). They say nothing about which company is which, so name matching
# ignores them. One list for every script; a group extends it with
# [accounts_payable] generic_supplier_words (legal_form_words()).
LEGAL_FORMS = frozenset({
    "ltd", "limited", "plc", "llc", "llp", "lp", "inc", "incorporated", "corp", "corporation",
    "co", "company", "gmbh", "ag", "kg", "sa", "sas", "sarl", "srl", "spa", "bv", "nv",
    "vof", "aps", "as", "asa", "ab", "oy", "oyj", "kk", "pte", "pty", "sl", "lda", "sp",
    "zoo", "sro", "kft", "doo", "private", "pvt",
})


def legal_form_words() -> frozenset[str]:
    """LEGAL_FORMS plus [accounts_payable] generic_supplier_words."""
    try:
        extra = accounts_payable().get("generic_supplier_words") or []
    except ConfigError:
        extra = []
    return LEGAL_FORMS | {str(w).strip().lower() for w in extra if str(w).strip()}


def section(name: str) -> dict[str, Any]:
    """A raw top-level table, e.g. section("accounts_payable")."""
    return load().raw.get(name, {}) or {}


def entities() -> list[Entity]:
    return list(load().entities)


def entity_keys() -> list[str]:
    return [e.key for e in load().entities]


def entity(name: str) -> Entity:
    """Resolve a key, slug, alias, short name or Xero organisation name."""
    needle = name.strip().lower()
    ents = load().entities
    for e in ents:
        if needle in (e.key.lower(), e.slug, e.short.lower(), e.xero_name.lower(), *e.aliases):
            return e
    partial = [e for e in ents if needle and needle in e.xero_name.lower()]
    if len(partial) == 1:
        return partial[0]
    raise ConfigError(f"no entity matches {name!r}; one of: {', '.join(e.key for e in ents)}")


def bank_accounts(entity_key: str | None = None) -> list[BankAccount]:
    out = []
    for e in load().entities:
        if entity_key is None or e.key == entity(entity_key).key:
            out.extend(e.bank_accounts)
    return out


def intercompany_pairs(entity_key: str | None = None) -> list[Pair]:
    pairs = list(load().pairs)
    if entity_key:
        k = entity(entity_key).key
        pairs = [p for p in pairs if k in (p.a, p.b)]
    return pairs


def non_group_accounts() -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for n in load().non_group:
        out.setdefault(n.entity, {})[n.account] = n.label
    return out


def intercompany_settings() -> dict[str, Any]:
    s = {"fx_gain_loss_account": "", "tolerance_abs": 1.0, "tolerance_rel": 0.005,
         "account_name_pattern": "intercompany|interco|inter-company|group cost|recharge"}
    s.update(section("intercompany_settings"))
    return s


def payroll_controls(entity_key: str | None = None) -> dict[str, list[PayrollControl]]:
    return {e.key: list(e.payroll_controls) for e in load().entities
            if e.payroll_controls and (entity_key is None or e.key == entity(entity_key).key)}


def phantom_exempt_accounts() -> set[str]:
    """Account codes whose payments are cleared by journal, never by the bank."""
    codes = {c.code for e in load().entities for c in e.payroll_controls}
    codes.update(str(c) for c in section("safety").get("phantom_check_exempt_accounts", []))
    return codes


def group_name_pattern() -> re.Pattern:
    """Matches any group entity's name in free text (bank payees, contacts)."""
    words = sorted({re.escape(e.xero_name) for e in load().entities} |
                   {re.escape(e.short) for e in load().entities if len(e.short) > 3},
                   key=len, reverse=True)
    return re.compile("|".join(words), re.I)


def slack() -> SlackConfig:
    return load().slack


def domains() -> list[Domain]:
    return list(load().domains)


def domain(key: str) -> Domain:
    for d in load().domains:
        if d.key == key:
            return d
    raise ConfigError(f"no domain {key!r}; one of: {', '.join(d.key for d in load().domains)}")


def accounts_payable() -> dict[str, Any]:
    """[accounts_payable] with every key filled. The classifier tunables:
    named_lead_days / named_lead_min (a named, exact-amount line may precede a
    bill of at least that many reporting-currency units by that many days),
    named_amount_tolerance (a named transfer within this share of the bill),
    stranger_fx_tolerance / stranger_days (a card charge under another
    merchant's name is still the bill). The lists: not_via_bank,
    periodic_billing (entity, pattern, from, reason), never_duplicate
    (supplier patterns) and the agent_payees table (a booking agent's word ->
    the merchant words its bookings are charged under)."""
    s = {"unpaid_notify": "", "reconcile_notify": "", "lead_days": 7, "leg_window_days": 3, "card_lag_days": 3,
         "named_lead_days": 35, "named_lead_min": 1000, "named_amount_tolerance": 0.01,
         "stranger_fx_tolerance": 0.02, "stranger_days": 4,
         "not_via_bank": [], "periodic_billing": [], "never_duplicate": [],
         "agent_payees": {}, "generic_supplier_words": []}
    s.update(section("accounts_payable"))
    return s


def reconcile_notify() -> str:
    """The Slack member ID the reconcile list is DM'd to: [accounts_payable]
    reconcile_notify, else unpaid_notify, else nobody."""
    s = accounts_payable()
    return str(s.get("reconcile_notify") or s.get("unpaid_notify") or "")


BANK_RECONCILE_DEFAULTS: dict[str, Any] = {
    # amounts: one currency always agrees to the cent; across currencies a
    # converted document fits within fx_tolerance
    "fx_tolerance": 0.02,
    "wrong_fx": 0.02,              # a reconciled payment's implied rate further than this from the median is suspect
    "wrong_fx_named": 0.02,        # the same, where the line names the payee
    # dates
    "reconciled_gap_days": 5,      # a reconciled record and its statement line, days apart at most
    "reconciled_gap_named_days": 10,   # ... where the payee's name agrees
    "record_gap_days": 5,          # an unreconciled record and an open line
    "record_gap_named_days": 45,   # ... with the payee's name (a claim dated at the receipt)
    "doc_before_days": 45,         # a document is looked for this far before the line ...
    "doc_after_days": 7,           # ... and this far after
    "feed_lag_days": 2,            # a line settled this close before a paste may not have reached Xero yet
    "docs_back_days": 60,          # documents of every status read this far before the window
    # the window the bank is read over
    "window_cap_days": 30,         # never read further back than this
    "watermark_pad_days": 3,       # the window opens this many days before the oldest open line
    "pad_days": 10,                # movements read before the window as context
    # candidates for the line-match agent
    "narrow_amount": 0.05,         # candidates: the amount within 5% ...
    "narrow_named_days": 7,        # ... or the name or who paid agreeing within 7 days
    "tip_max": 1.25,               # the line at most 25% over the bill (a tip or fee)
    "estimate_min": 0.90,          # the line at least 90% of an estimate
    "fx_band": 0.05,               # across currencies, 5% either side before any tip
    # the line-match agent
    "line_match_cap": 40,          # lines asked per run
    "line_match_candidates": 6,    # candidates per line
    "line_match_budget_tokens": 12000,   # the payload, estimated as chars / 4
    "line_match_model": "haiku",
    "line_match_effort": "medium",
    "line_match_timeout": 300,     # seconds
    "line_match_max_usd": 1.00,    # the agent call's own budget
    # who-paid signals
    "staff_register": "",          # optional JSON file of staff names; absent = skip the signal
    "agent_bill_number_initials": "",  # optional regex, one group: the initials in an agent-made bill number
}


def bank_reconcile() -> dict[str, Any]:
    """[bank_reconcile], every key filled (BANK_RECONCILE_DEFAULTS). No section
    means the defaults. Numbers are validated here, once."""
    s = dict(BANK_RECONCILE_DEFAULTS)
    s.update(section("bank_reconcile"))
    for k, default in BANK_RECONCILE_DEFAULTS.items():
        v = s[k]
        if isinstance(default, str):
            if not isinstance(v, str):
                raise ConfigError(f"[bank_reconcile] {k} must be text; got {v!r}")
            continue
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
            raise ConfigError(f"[bank_reconcile] {k} must be a number, 0 or more; got {v!r}")
    if s["agent_bill_number_initials"]:
        try:
            pat = re.compile(s["agent_bill_number_initials"])
        except re.error as exc:
            raise ConfigError(f"[bank_reconcile] agent_bill_number_initials is not a regex: {exc}")
        if pat.groups != 1:
            raise ConfigError("[bank_reconcile] agent_bill_number_initials needs exactly one capture group")
    return s


def posting() -> dict[str, Any]:
    """[posting], every key filled. `restricted_tax_types` maps a tax type to the
    entity keys that alone may use it on a posted line (scripts/post_bills.py);
    no section means no restriction."""
    s: dict[str, Any] = {"restricted_tax_types": {}}
    s.update(section("posting"))
    raw = s["restricted_tax_types"]
    if not isinstance(raw, dict):
        raise ConfigError("[posting] restricted_tax_types must be a table of tax type = [entity keys]")
    keys = set(entity_keys())
    out: dict[str, tuple[str, ...]] = {}
    for tax, allowed in raw.items():
        if not isinstance(allowed, list) or not allowed or any(a not in keys for a in allowed):
            raise ConfigError(f"[posting] restricted_tax_types {tax!r} must list known entity keys")
        out[str(tax)] = tuple(allowed)
    s["restricted_tax_types"] = out
    return s


REVIEW_CHECKS_DEFAULTS: dict[str, Any] = {
    # thresholds, in the reporting currency unless a name says days or a ratio
    "capitalisation_threshold": 500.0,  # per item: at or above is capitalised, below is expensed
    "movement_ratio": 2.5,              # a P&L account moved this many times its prior average ...
    "movement_min": 10000.0,            # ... and by at least this much
    "suspense_threshold": 0.005,        # a suspense or clearing balance above this is a finding
    "drift_min": 100.0,                 # a supplier on a new account, at least this much
    "spend_line_min": 50.0,             # a spend money line smaller than this is card noise
    "spend_money_min": 300.0,           # spend money to one supplier and account, at least this much
    "welfare_small": 250.0,             # welfare-account spend money below this is allowed
    "subscription_max": 1000.0,         # a steady monthly amount below this is a subscription
    "round_balance_min": 1000.0,        # a contact's round-number balance, at least this much
    "far_tolerance": 1.0,               # ledger against asset register, in the entity's currency
    "aged_days": 90,
    "stale_journal_days": 21,
    "stale_bill_draft_days": 14,
    "unreconciled_old_days": 30,
    # word and pattern lists (regexes, case-insensitive); empty = the check's built-in words only
    "not_duplicates": [],               # suppliers whose same-amount bills with distinct numbers are normal
    "twin_reference_pattern": "",       # one capture group: a booking or trip number two documents may share
    "card_charge_number_pattern": "",   # bill numbers the agent gives a card-charge receipt
    "payroll_sources": [],              # payroll providers, besides [[accounts_payable.not_via_bank]]
    "travel_words": [],
    "hardware_words": [],
    "small_spend_words": [],
    "invoiceless_words": [],
    "quarter_exempt_suppliers": [],
    "quarter_exempt_accounts": [],      # account-name patterns whose lines name other quarters by design
    "capitalisable_accounts": ["it costs", "office expenses", "computer expenses", "office supplies"],
    "subscription_accounts": ["it costs", "computer expenses"],
    "welfare_accounts": ["welfare"],
    # tables
    "known_bad_accounts": [], "clearing_accounts": [], "cutovers": [], "tracking": [], "tax": [],
    "revenue": [], "routing": [], "register_depreciated": [],
}

_REVIEW_TABLES = {
    # table -> (required keys, regex keys, date keys)
    "known_bad_accounts": (("entity", "code", "why"), ("name_pattern",), ()),
    "clearing_accounts": (("entity", "code"), (), ()),
    "cutovers": (("entity", "from", "kind", "why"), ("match", "exclude"), ("from",)),
    "tracking": (("entity", "category"), (), ()),
    "tax": (("entity",), ("no_tax_accounts",), ()),
    "revenue": (("entity",), ("allowed_accounts",), ()),
    "routing": (("supplier_pattern", "account_pattern", "must"), ("supplier_pattern", "account_pattern"), ()),
    "register_depreciated": (("entity", "assets", "why"), ("journal_pattern",), ()),
}
CUTOVER_KINDS = ("recognise_in", "only_accounts", "not_accounts", "no_bills")


def review_checks() -> dict[str, Any]:
    """[review_checks], every key filled (REVIEW_CHECKS_DEFAULTS), for
    scripts/review_checks.py. No section means the defaults and no group
    specific rule: known-bad accounts, dated cutovers, tracking, tax, revenue
    shape and routing are listed here, empty until written. Entity names in a
    table are resolved to keys; numbers, regexes and dates are validated once,
    so a typo fails here and not half-way through a review."""
    s = dict(REVIEW_CHECKS_DEFAULTS)
    s.update(section("review_checks"))
    for k, default in REVIEW_CHECKS_DEFAULTS.items():
        v = s[k]
        if isinstance(default, (int, float)) and not isinstance(default, bool):
            if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
                raise ConfigError(f"[review_checks] {k} must be a number, 0 or more; got {v!r}")
        elif isinstance(default, str):
            if not isinstance(v, str):
                raise ConfigError(f"[review_checks] {k} must be text; got {v!r}")
        elif not isinstance(v, list):
            raise ConfigError(f"[review_checks] {k} must be a list; got {v!r}")
    for k in ("twin_reference_pattern", "card_charge_number_pattern"):
        if s[k]:
            try:
                pat = re.compile(s[k])
            except re.error as exc:
                raise ConfigError(f"[review_checks] {k} is not a regex: {exc}")
            if k == "twin_reference_pattern" and pat.groups != 1:
                raise ConfigError("[review_checks] twin_reference_pattern needs exactly one capture group")
    for k, default in REVIEW_CHECKS_DEFAULTS.items():
        if isinstance(default, list) and k not in _REVIEW_TABLES:
            for pat in s[k]:
                try:
                    re.compile(str(pat))
                except re.error as exc:
                    raise ConfigError(f"[review_checks] {k}: {pat!r} is not a regex: {exc}")
    for table, (required, regexes, dates) in _REVIEW_TABLES.items():
        rows = []
        for row in s[table]:
            if not isinstance(row, dict):
                raise ConfigError(f"[review_checks] {table}: each entry must be a table; got {row!r}")
            missing = [r for r in required if r not in row]
            if missing:
                raise ConfigError(f"[review_checks] {table}: {row!r} lacks {', '.join(missing)}")
            row = dict(row)
            if row.get("entity"):
                row["entity"] = entity(str(row["entity"])).key
            for rk in regexes:
                value = row.get(rk) or []
                for pat in [value] if isinstance(value, str) else value:
                    try:
                        re.compile(pat)
                    except re.error as exc:
                        raise ConfigError(f"[review_checks] {table}: {pat!r} is not a regex: {exc}")
            for dk in dates:
                try:
                    datetime.date.fromisoformat(str(row[dk]))
                except ValueError:
                    raise ConfigError(f"[review_checks] {table}: {dk} must be YYYY-MM-DD; got {row[dk]!r}")
            if table == "cutovers" and row["kind"] not in CUTOVER_KINDS:
                raise ConfigError(f"[review_checks] cutovers: kind must be one of "
                                  f"{', '.join(CUTOVER_KINDS)}; got {row['kind']!r}")
            if table == "routing" and row["must"] not in ("be_on", "not_be_on"):
                raise ConfigError(f"[review_checks] routing: must is be_on or not_be_on; got {row['must']!r}")
            rows.append(row)
        s[table] = rows
    return s


def auto_resolve() -> dict[str, Any]:
    """[auto_resolve], with every key filled. No section means `off`: every
    question goes to a person, as before the section existed."""
    s = {"act_from": "off", "medium_limit": 0, "high_limit": 0, "confirm_days": 0}
    s.update(section("auto_resolve"))
    return s


def resolve_action(grade: str, amount: float) -> tuple[str, str]:
    """What the agent does with its own answer to a question it would have
    asked: ("act" | "confirm" | "query", why). `amount` is the money the
    answer moves, in the reporting currency.

    act      post it, report it under `resolved`
    confirm  post it, report it under `to confirm`, register it for an admin
    query    ask, with the proposed answer in the question
    """
    grade = grade.strip().lower()
    if grade not in ("high", "medium", "low"):
        raise ConfigError(f"grade must be high, medium or low; got {grade!r}")
    s = auto_resolve()
    cur = reporting_currency()
    act_from = str(s["act_from"])
    if act_from == "off":
        return "query", "[auto_resolve] is off"
    if grade == "low":
        return "query", "low confidence"
    if s["high_limit"] and abs(amount) > s["high_limit"]:
        return "query", f"over the high limit, {cur} {s['high_limit']:,.2f}"
    if grade == "high":
        return "act", "high confidence"
    if act_from == "high":
        return "query", "medium confidence, and only high is acted on"
    if s["medium_limit"] and abs(amount) > s["medium_limit"]:
        return "query", f"medium confidence over the medium limit, {cur} {s['medium_limit']:,.2f}"
    return "confirm", "medium confidence"


# [models]: the Claude model each kind of session runs on. An environment
# variable overrides the table, which overrides the default.
MODEL_DEFAULTS = {"default": "opus", "query": "claude-sonnet-5-5", "small": "haiku"}
MODEL_ENV = {"default": "AGENT_MODEL", "query": "AGENT_QUERY_MODEL", "small": "AGENT_SMALL_MODEL"}


def models() -> dict[str, str]:
    """[models] with every key filled: default (runs that post to Xero and
    Slack), query (read-only answers) and small (helper sessions that only
    transcribe or look things up)."""
    m = dict(MODEL_DEFAULTS)
    for k, v in section("models").items():
        if k not in MODEL_DEFAULTS:
            raise ConfigError(f"[models] {k!r} is not a model key; one of: {', '.join(MODEL_DEFAULTS)}")
        if not isinstance(v, str) or not v.strip():
            raise ConfigError(f"[models] {k} must be a model name or alias; got {v!r}")
        m[k] = v.strip()
    return m


def model(key: str = "default") -> str:
    """The model for `key`: AGENT_MODEL, AGENT_QUERY_MODEL or AGENT_SMALL_MODEL
    when set, else [models], else the built-in default."""
    if key not in MODEL_DEFAULTS:
        raise ConfigError(f"no model key {key!r}; one of: {', '.join(MODEL_DEFAULTS)}")
    env = os.environ.get(MODEL_ENV[key], "").strip()
    return env or models()[key]


INVESTIGATION_DEFAULTS: dict[str, Any] = {
    "enabled": True, "daily_cap_usd": 35.0, "batch_max": 8, "debounce_seconds": 300,
    "reuse_days": 14, "researchers_parallel": 2, "max_rounds": 3, "effort": "medium",
    "coordinator_model": "", "researcher_model": "",
}


def investigation() -> dict[str, Any]:
    """[investigation] with every key filled: the read-only query investigation
    (scripts/investigate_query.py) that gathers the evidence a run grades and
    takes to scripts/resolve_gate.py. `coordinator_model` defaults to
    [models] default and `researcher_model` to [models] small; AGENT_MODEL and
    AGENT_SMALL_MODEL still win, as everywhere else."""
    s = dict(INVESTIGATION_DEFAULTS)
    raw = section("investigation")
    unknown = sorted(set(raw) - set(INVESTIGATION_DEFAULTS))
    if unknown:
        raise ConfigError(f"[investigation] {', '.join(unknown)} is not a key; one of: "
                          f"{', '.join(INVESTIGATION_DEFAULTS)}")
    s.update({k: v for k, v in raw.items() if v not in ("", None)})
    if not isinstance(s["enabled"], bool):
        raise ConfigError(f"[investigation] enabled must be true or false; got {s['enabled']!r}")
    for k in ("daily_cap_usd", "batch_max", "debounce_seconds", "reuse_days",
              "researchers_parallel", "max_rounds"):
        v = s[k]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
            raise ConfigError(f"[investigation] {k} must be a number, 0 or more; got {v!r}")
    s["daily_cap_usd"] = float(s["daily_cap_usd"])
    for k in ("batch_max", "debounce_seconds", "reuse_days", "researchers_parallel", "max_rounds"):
        s[k] = int(s[k])
    if s["batch_max"] < 1 or s["researchers_parallel"] < 1 or s["max_rounds"] < 1:
        raise ConfigError("[investigation] batch_max, researchers_parallel and max_rounds "
                          "must be 1 or more")
    s["coordinator_model"] = str(s["coordinator_model"] or model("default"))
    s["researcher_model"] = str(s["researcher_model"] or model("small"))
    s["effort"] = str(s["effort"])
    return s


def default_admin() -> str:
    """The name register lines are put `with` when a run names nobody: the
    first admin in [slack.admins], else the word `admin`."""
    admins = list(load().slack.admins.values())
    return admins[0] if admins else "admin"


OUTSTANDING_CHECK_DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "batch_size": 25,
    "min_xero_calls": 800,
    "checker_model": "claude-sonnet-5-5",
    "light_checker_model": "",
    "trivial_amount": 0,
}


def outstanding_check() -> dict[str, Any]:
    """[outstanding_check], every key filled: whether the daily check of the
    outstanding-items register runs, how many lines go to one checker agent,
    the Xero daily calls any entity must still have for it to start, the
    model of the checkers on manual, queried and blocked lines
    (`checker_model`) and on chased documents and watches
    (`light_checker_model`, default [models] small), and `trivial_amount`: a
    bookkeeping line whose whole amount at stake is under it, in the
    reporting currency, comes off the register (0, the default, is off). The
    coordinator runs on [models] default."""
    s = dict(OUTSTANDING_CHECK_DEFAULTS)
    s.update(section("outstanding_check"))
    for k in s:
        if k not in OUTSTANDING_CHECK_DEFAULTS:
            raise ConfigError(f"[outstanding_check] {k!r} is not a key; one of: "
                              f"{', '.join(OUTSTANDING_CHECK_DEFAULTS)}")
    if not isinstance(s["enabled"], bool):
        raise ConfigError(f"[outstanding_check] enabled must be true or false; got {s['enabled']!r}")
    for k in ("batch_size", "min_xero_calls"):
        v = s[k]
        if isinstance(v, bool) or not isinstance(v, int) or v < (1 if k == "batch_size" else 0):
            raise ConfigError(f"[outstanding_check] {k} must be a whole number"
                              f"{', 1 or more' if k == 'batch_size' else ', 0 or more'}; got {v!r}")
    if not isinstance(s["checker_model"], str) or not s["checker_model"].strip():
        raise ConfigError(f"[outstanding_check] checker_model must be a model name; got {s['checker_model']!r}")
    s["checker_model"] = s["checker_model"].strip()
    light = s["light_checker_model"]
    if light in ("", None):
        light = model("small")
    if not isinstance(light, str) or not light.strip():
        raise ConfigError(f"[outstanding_check] light_checker_model must be a model name; got {light!r}")
    s["light_checker_model"] = light.strip()
    t = s["trivial_amount"]
    if isinstance(t, bool) or not isinstance(t, (int, float)) or t < 0:
        raise ConfigError(f"[outstanding_check] trivial_amount must be a number, 0 or more; got {t!r}")
    s["trivial_amount"] = float(t)
    return s


DAILY_CHAIN_DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "attachment_lookback_days": 6,
}


def daily_chain() -> dict[str, Any]:
    """[daily_chain], every key filled: whether the scheduled `daily` job gates
    each of its links in plain Python first (scripts/daily_chain.py), and how
    many days back the bookkeeping gate checks that every message with an
    attachment already has a ledger line (inside the ledger's own seven)."""
    s = dict(DAILY_CHAIN_DEFAULTS)
    s.update(section("daily_chain"))
    for k in s:
        if k not in DAILY_CHAIN_DEFAULTS:
            raise ConfigError(f"[daily_chain] {k!r} is not a key; one of: {', '.join(DAILY_CHAIN_DEFAULTS)}")
    if not isinstance(s["enabled"], bool):
        raise ConfigError(f"[daily_chain] enabled must be true or false; got {s['enabled']!r}")
    d = s["attachment_lookback_days"]
    if isinstance(d, bool) or not isinstance(d, int) or not 1 <= d <= 7:
        raise ConfigError(f"[daily_chain] attachment_lookback_days must be a whole number from 1 to 7; got {d!r}")
    return s


BRIEFING_DEFAULTS: dict[str, Any] = {
    "window_days": 7, "thread_lookback_days": 28, "fresh_hours": 2, "history_days": 183,
    "read_documents": True, "reader_model": "", "second_look_model": "",
    "reader_max_usd": 0.5, "second_look_max_usd": 1.5, "workers": 6, "timeout_seconds": 420,
    "inline_chars": 28000, "session_cache_ttl": "5m",
}


def briefing() -> dict[str, Any]:
    """[briefing], every key filled: the bookkeeping run's briefing
    (scripts/bookkeep_brief.py), every mechanical first step done in plain
    Python before the session opens. `reader_model` defaults to [models]
    small and `second_look_model` to [models] query; AGENT_SMALL_MODEL and
    AGENT_QUERY_MODEL still win, as everywhere else."""
    s = dict(BRIEFING_DEFAULTS)
    raw = section("briefing")
    unknown = sorted(set(raw) - set(BRIEFING_DEFAULTS))
    if unknown:
        raise ConfigError(f"[briefing] {', '.join(unknown)} is not a key; one of: {', '.join(BRIEFING_DEFAULTS)}")
    s.update({k: v for k, v in raw.items() if v not in ("", None)})
    if not isinstance(s["read_documents"], bool):
        raise ConfigError(f"[briefing] read_documents must be true or false; got {s['read_documents']!r}")
    for k in ("window_days", "thread_lookback_days", "fresh_hours", "history_days", "workers",
              "timeout_seconds", "inline_chars"):
        v = s[k]
        if isinstance(v, bool) or not isinstance(v, int) or v < 1:
            raise ConfigError(f"[briefing] {k} must be a whole number, 1 or more; got {v!r}")
    if s["window_days"] > 7:
        raise ConfigError("[briefing] window_days must be 7 or less: the ledger keeps seven days")
    for k in ("reader_max_usd", "second_look_max_usd"):
        v = s[k]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
            raise ConfigError(f"[briefing] {k} must be a number above zero; got {v!r}")
        s[k] = float(v)
    if s["session_cache_ttl"] not in ("5m", "1h"):
        raise ConfigError(f"[briefing] session_cache_ttl must be \"5m\" or \"1h\"; got {s['session_cache_ttl']!r}")
    s["reader_model"] = str(s["reader_model"] or model("small"))
    s["second_look_model"] = str(s["second_look_model"] or model("query"))
    return s


def report_settings(kind: str | None = None) -> dict[str, Any]:
    r = section("reports")
    return r if kind is None else (r.get(kind) or {})


PERIOD_READER_DEFAULTS: dict[str, Any] = {
    "model": "haiku", "effort": "low", "max_usd": 2.0, "max_files": 8, "timeout_seconds": 600,
}


def period_reader() -> dict[str, Any]:
    """[reports.period_reader]: the small model that reads a prepayment's period
    off its invoice. Every key optional; the defaults are the ones shown."""
    s = dict(PERIOD_READER_DEFAULTS)
    s.update({k: v for k, v in report_settings("period_reader").items() if v not in ("", None)})
    try:
        s["max_usd"] = float(s["max_usd"])
        s["max_files"] = int(s["max_files"])
        s["timeout_seconds"] = int(s["timeout_seconds"])
    except (TypeError, ValueError):
        raise ConfigError("[reports.period_reader] max_usd, max_files and timeout_seconds "
                          "must be numbers") from None
    if s["max_usd"] <= 0 or s["max_files"] < 1 or s["timeout_seconds"] < 1:
        raise ConfigError("[reports.period_reader] max_usd, max_files and timeout_seconds "
                          "must be above zero")
    s["model"], s["effort"] = str(s["model"]), str(s["effort"])
    return s


def quarter_end_from() -> str | None:
    """[reports] quarter_end_from, "YYYY-MM": the first month a quarter-end copy of each
    balance report is kept from. Blank or absent: no quarter-end copies (None)."""
    raw = str(report_settings().get("quarter_end_from") or "").strip()
    if not raw:
        return None
    m = re.fullmatch(r"(\d{4})-(\d{2})", raw)
    if not m or not 1 <= int(m.group(2)) <= 12:
        raise ConfigError(f"[reports] quarter_end_from must be YYYY-MM; got {raw!r}")
    return raw


def fy_quarter_end_months() -> frozenset[int]:
    """The calendar months (1-12) in which a financial quarter ends: the month of
    [company] financial_year_end and every third month before it."""
    month, _ = financial_year_end()
    return frozenset((month - 1 - 3 * k) % 12 + 1 for k in range(4))


def fy_quarter_of(month: str) -> tuple[int, int]:
    """(quarter number 1-4, the calendar year the financial year ends in) for the
    quarter ending in month "YYYY-MM"; Q4 is the one ending with the year end."""
    y, m = (int(x) for x in month.split("-"))
    fy = fy_year_for(datetime.date(y, m, 1))
    end_month = financial_year_end()[0]
    return 4 - ((end_month - m) % 12) // 3, fy


def bank_fee_tariffs() -> dict[str, dict[str, Any]]:
    """[bank_fees.<provider>] price lists, {} when none is configured."""
    return {k: v for k, v in section("bank_fees").items() if isinstance(v, dict)}


def drive_settings() -> dict[str, Any]:
    d = {"publish_folder_id": "", "subfolders": {}}
    d.update(section("drive"))
    return d


def describe() -> str:
    """Human summary, used by `python -m accounting_agent.config`."""
    g = load()
    lines = [f"{company_name()} ({g.path})", "", "entities:"]
    for e in g.entities:
        banks = ", ".join(f"{b.label} [{b.provider}]" for b in e.bank_accounts) or "no bank"
        lines.append(f"  {e.key:<10} {e.xero_name} · {e.base_currency} · {banks}")
    lines += ["", "intercompany pairs:"]
    for p in g.pairs:
        flag = "" if p.complete else "  (mirror account missing)"
        lines.append(f"  {p.name:<28} {p.accounts}{flag}")
    s = g.slack
    lines += ["", f"slack: {len(s.admins)} admins, {len(s.users)} users, {len(s.readonly)} read-only,"
                  f" channel {s.channel_id or '(unset)'}",
              "domains: " + ", ".join(d.key for d in g.domains)]
    ar = auto_resolve()
    lines.append("auto_resolve: off, every question goes to a person" if ar["act_from"] == "off" else
                 f"auto_resolve: acts from {ar['act_from']}, medium limit {ar['medium_limit']:,},"
                 f" high limit {ar['high_limit']:,} {reporting_currency()},"
                 f" confirm within {ar['confirm_days']} days")
    lines.append("models: " + ", ".join(f"{k} {v}" for k, v in models().items()))
    return "\n".join(lines)


if __name__ == "__main__":
    # `python -m accounting_agent.config model [key]` prints one model name, for
    # the deploy/ runners; `latest-quarter-end <date>` prints the quarter end
    # and its label; with no argument, the whole group.
    import sys as _sys
    _a = _sys.argv[1:]
    if _a[:1] == ["model"]:
        print(model(_a[1] if len(_a) > 1 else "default"))
    elif _a[:1] == ["latest-quarter-end"]:
        _d = datetime.date.fromisoformat(_a[1]) if len(_a) > 1 else datetime.date.today()
        _qe = latest_quarter_end(_d)
        _n, _y = fy_quarter_label(_qe)
        print(_qe.isoformat(), f"Q{_n}", _y)
    else:
        print(describe())
