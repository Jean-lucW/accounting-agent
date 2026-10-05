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
    s = {"unpaid_notify": "", "lead_days": 7, "leg_window_days": 3, "card_lag_days": 3,
         "not_via_bank": [], "generic_supplier_words": []}
    s.update(section("accounts_payable"))
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


def report_settings(kind: str | None = None) -> dict[str, Any]:
    r = section("reports")
    return r if kind is None else (r.get(kind) or {})


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
    return "\n".join(lines)


if __name__ == "__main__":
    print(describe())
