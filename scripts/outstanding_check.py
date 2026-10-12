#!/usr/bin/env python
"""The daily outstanding-items check: is every line on the register still
outstanding? People close things in Xero without telling the agent, so the
list goes stale; this finds what is done and deletes it. Skill:
outstanding-items.

    .venv/bin/python scripts/outstanding_check.py run              # the scheduled job
    .venv/bin/python scripts/outstanding_check.py run --dry-run    # check, close nothing, post nothing
    .venv/bin/python scripts/outstanding_check.py run --domain bookkeeping
    .venv/bin/python scripts/outstanding_check.py run --no-session # the Python checks only

HOW. Python first, then a session for what is left. The register
(docs/bookkept/outstanding.json) and the reconcile list
(docs/bookkept/reconcile.json) are read, and before any agent starts:

  - the reconcile list is not checked again when it was built today: the
    reconcile list build (scripts/reconcile_list.py) ran from a fresh bank
    reconcile check, which holds every line on it open by construction;
  - a manual line whose whole ask is to settle or reconcile one named bill
    against a loan account (one bill, one entity, one step) is read in Xero,
    one filtered read: the bill PAID, every payment on it from a non-bank
    account, the account the line names where it names one, closes it here;
  - an admin's `done` in the channel's MANUAL ITEMS threads is read and
    handed to the checker with the line (`done_in_thread`). It is a claim: a
    `done` with Xero unchanged keeps the line, so it closes nothing on its
    own;
  - lines the balance reports own (keys `rpt-check:` and `rpt-note:`, which
    the report adds and deletes itself) and the runners' turn-cap lines
    (`turn-cap-`) are left alone;
  - with [outstanding_check] trivial_amount set, every bookkeeping line whose
    own wording puts it under that amount in the reporting currency is
    deleted (`outstanding.py prune-trivial`) before any agent sees it.

The rest are split into batches of at most [outstanding_check] batch_size
lines (25), one domain and kind to a batch, under
data/outstanding_checks/<stamp>/batches/. The batch's file name carries what
it costs to check: `-light-` for chased documents and watches (one lookup a
line), `-full-` for manual steps, queries and blocked runs (reading and
judging). One headless Claude session runs the coordinator agent
`outstanding-verifier` (model [models] default), which hands each light batch
to an `outstanding-checker` on [outstanding_check] light_checker_model and
each full batch to one on checker_model, reviews every line a checker calls
done, not needed or trivial, and returns one verdict per line. When Python
leaves nothing, no session starts. The session is read-only three ways, as a
query is: AGENT_READONLY=1 (XeroClient refuses every non-GET),
.claude/hooks/guard-readonly.py, and Write/Edit disallowed.

WHAT IS CLOSED. Only a line Python read settled in Xero as above, or one the
coordinator marks CLOSED and definite: every part of what it asks for read
done in the record itself today. A bookkeeping line also goes, definite and
named from the record, as NOT_NEEDED (a manual step no person needs to take)
or TRIVIAL (everything at stake under trivial_amount, offered to the agents
only when it is set). Those lines are deleted from the register with
`outstanding.py close` (a reconcile line is dropped from the reconcile list)
and the Word copy is republished once. A PARTLY line stays as it is; the post
says what is left. Lines that share the same reason are said once in the
post, with a count.

THE POST goes to the channel in config/group.toml: a headline at top level and
the report in its thread, queued with scripts/slack_outbox.py. The session's
cost is written as an extra cost file, data/runs/<run id>/extra/<name>.json,
and scripts/run_cost.py puts the bracket on the headline. When AGENT_RUN_ID is
already set the caller owns the run and stamps it; otherwise this script
begins and stamps its own run, and posts directly when it cannot.

The day is skipped when any entity has fewer than [outstanding_check]
min_xero_calls (800) Xero calls left, so the check never eats into the next
scheduled run's budget.

Read-only against Xero. Its writes are the register, its Word copy on Drive
and the channel post. Runs on the server, after the daily chain
(deploy/run-scheduled.sh outstanding-check).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import zoneinfo
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
from accounting_agent import config  # noqa: E402

REGISTER = ROOT / "docs" / "bookkept" / "outstanding.json"
RECONCILE = ROOT / "docs" / "bookkept" / "reconcile.json"
RUNS = ROOT / "data" / "runs"
BASE = ROOT / "data" / "outstanding_checks"
_VENV = ROOT / ".venv" / "bin" / "python"
PY = str(_VENV) if _VENV.exists() else sys.executable

COORDINATOR_AGENT = "outstanding-verifier"
COORDINATOR_EFFORT = os.environ.get("AGENT_OUTSTANDING_CHECK_EFFORT", "medium")
HARD_TIMEOUT = int(os.environ.get("AGENT_OUTSTANDING_CHECK_TIMEOUT", "9000"))

# Lines other code owns: a balance report adds and deletes its own check and
# note lines on every build, and the runners write the turn-cap lines.
OWNED_KEY = re.compile(r"^(?:rpt-check:|rpt-note:|turn-cap-)")

# What a batch costs to check: a chased document or a watch is one lookup and
# goes to a checker on light_checker_model; a manual step, a query or a
# blocked run needs reading and judging and goes to one on checker_model. The
# tier is in the batch's file name and the coordinator picks the model.
LIGHT_KINDS = {"documents", "watch"}
# The model names the Agent tool takes for a subagent, found in a configured
# model's name. The coordinator passes one on every call, so the configured
# model wins over the checker agent file's own.
AGENT_MODELS = ("haiku", "sonnet", "opus")

PROMPT = """Daily outstanding-items check. Follow your agent instructions.
The batches are the JSON files under {batches}: {n} lines in {b} batches.
Send one outstanding-checker per batch file, at most five at a time, {models}
Review every line they call CLOSED, NOT_NEEDED or TRIVIAL, and end with the
JSON verdicts object, one entry per line, ref exactly as given.
{trivial}"""


def shown(path: Path) -> Path:
    return path.relative_to(ROOT) if path.is_relative_to(ROOT) else path


def load_env() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    for raw in env.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def local_stamp() -> str:
    try:
        zone = zoneinfo.ZoneInfo(str(config.company("timezone", "UTC") or "UTC"))
    except zoneinfo.ZoneInfoNotFoundError:
        zone = zoneinfo.ZoneInfo("UTC")
    return f"{datetime.now(timezone.utc).astimezone(zone):%d %b %Y, %H:%M %Z}"


def headline() -> str:
    return f"{config.company_name().upper()} OUTSTANDING CHECK ({local_stamp()})"


def xero_budget() -> tuple[int, str]:
    """-> (smallest daily calls left, which entity). 0 when the probe fails."""
    try:
        out = subprocess.run([PY, "scripts/xero_rate_limit.py", "--json"], cwd=ROOT,
                             capture_output=True, text=True, timeout=180).stdout
        rows = json.loads(out)["rows"]
        low = min(rows, key=lambda r: r.get("day_remaining") or 0)
        return int(low.get("day_remaining") or 0), low["entity"]
    except Exception as e:                                       # noqa: BLE001
        return 0, f"the probe failed ({type(e).__name__})"


# ---------------------------------------------------------------- the lines

def lines(domain: str | None) -> list[dict]:
    """Every register line plus every reconcile line, each with a `ref` the
    coordinator returns it under: the item's key where that is unique, since
    some ids repeat across domains."""
    items = json.loads(REGISTER.read_text())
    keys = Counter(i.get("key") for i in items if i.get("key"))
    out = []
    for i in items:
        if domain and i["domain"] != domain:
            continue
        if OWNED_KEY.match(str(i.get("key", ""))):
            continue
        unique = i.get("key") and keys[i["key"]] == 1
        out.append({
            "ref": i["key"] if unique else f"{i['id']}@{i['domain']}",
            "closable": bool(unique),
            **{k: i.get(k, "") for k in ("id", "domain", "kind", "with", "text", "refs", "raised", "answered")},
        })
    if domain in (None, "bookkeeping") and RECONCILE.exists():
        for n, r in enumerate(json.loads(RECONCILE.read_text()).get("items", []), 1):
            out.append({"ref": f"REC-{n:02d}", "closable": True, "id": f"REC-{n:02d}",
                        "domain": "bookkeeping", "kind": "reconcile", "with": "admin",
                        "text": r["text"], "refs": r.get("refs", ""), "entity": r.get("entity", "")})
    return out


def reconcile_fresh(today: str) -> bool:
    """Was the reconcile list built today? Then the bank reconcile check held
    every line on it open this morning, and checking them again is waste."""
    try:
        return str(json.loads(RECONCILE.read_text()).get("built") or "")[:10] == today
    except (OSError, ValueError):
        return False


# ---------------------------------------------------------------- closed in Python

MORE = re.compile(r"\bthen\b|;|\balso\b|\bbefore\b|\bafter that\b"
                  r"|\band (?:post|void|recode|retype|move|clear|match|delete|create|revalue)\b", re.I)
LOANISH = re.compile(r"intercompany|interco|\bloan\b", re.I)
CODE = re.compile(r"\((\d{3,5}(?:\.\d+)?)\)")
DOC_NO = re.compile(r"[A-Za-z0-9][A-Za-z0-9/._-]{2,}")


def entity_names() -> list[str]:
    """The short names a line can carry, longest first so one never hides inside
    another."""
    return sorted({e.short for e in config.entities()}, key=len, reverse=True)


def bill_rule(row: dict) -> tuple[str, str, str | None] | None:
    """(entity short name, bill number, account code or None) when the line's
    whole ask is to settle or reconcile one named bill against a loan account,
    the shape the bill-payments skill writes. Anything with a second step, a
    second entity or a second account is the coordinator's."""
    if row.get("domain") != "bookkeeping" or row.get("kind") != "manual" or not row.get("closable"):
        return None
    names = "|".join(re.escape(n) for n in entity_names())
    if not names:
        return None
    settle = re.compile(rf"^(?:in (?:{names}), )?(?:manually )?(?:settle|reconcile)\b", re.I)
    text, refs = row.get("text") or "", row.get("refs") or ""
    if not settle.match(text) or MORE.search(text) or not (LOANISH.search(text) or CODE.search(text)):
        return None
    entities = set(re.findall(rf"recognised ({names})\b", refs))
    number = refs.split("·")[-1].strip()
    codes = set(CODE.findall(text))
    if (len(entities) != 1 or len(codes) > 1 or not DOC_NO.fullmatch(number)
            or not re.search(r"\d", number) or '"' in number):
        return None
    return entities.pop(), number, (codes.pop() if codes else None)


def make_client(entity: str):
    from accounting_agent.xero import client_for
    return client_for(entity)


def bill_settled(entity: str, number: str, code: str | None, cache: dict, client=None) -> str:
    """Evidence that the bill is settled the way the line asks, or ''. One
    filtered read for the bill, one per payment, the chart once per entity."""
    cx = cache.get(entity) or cache.setdefault(entity, (client or make_client)(entity))
    bills = [b for b in cx.get("Invoices", where=f'Type=="ACCPAY" AND InvoiceNumber=="{number}"').get("Invoices") or []
             if b.get("Status") not in ("DELETED", "VOIDED")]
    if len(bills) != 1:
        return ""
    b = bills[0]
    if (b.get("Status") != "PAID" or abs(float(b.get("AmountDue") or 0)) > 0.005 or not b.get("Payments")
            or b.get("CreditNotes") or b.get("Prepayments") or b.get("Overpayments")):
        return ""
    chart = cache.get(("chart", entity))
    if chart is None:
        chart = cache[("chart", entity)] = {a["AccountID"]: a for a in cx.get("Accounts").get("Accounts") or []}
    used = set()
    for p in b["Payments"]:
        d = (cx.get(f"Payments/{p['PaymentID']}").get("Payments") or [{}])[0]
        acct = chart.get((d.get("Account") or {}).get("AccountID"), {})
        if not acct or acct.get("Type") == "BANK" or (code and acct.get("Code") != code):
            return ""
        used.add(f"{acct.get('Name')} ({acct.get('Code')})")
    return f"PAID in {entity}, settled from {', '.join(sorted(used))} · {number}"


def slack_pages(method: str, key: str, **params) -> list[dict]:
    """Every page of a Slack list method, read-only GETs."""
    import slack_query
    out, cursor = [], ""
    while True:
        res = slack_query._call(method, {**params, **({"cursor": cursor} if cursor else {})}, get=True)
        out += res.get(key) or []
        cursor = (res.get("response_metadata") or {}).get("next_cursor") or ""
        if not cursor:
            return out


def done_replies(rows: list[dict], days: int = 30) -> dict[str, str]:
    """{ref: who said done, where} from the channel's MANUAL ITEMS threads.
    `done` alone names every line the thread's detail carries by id or key;
    `done <reference>` names the line it gives."""
    admins = config.slack().admins
    channel = config.slack().channel_id
    title = f"{config.company_name().upper()} MANUAL ITEMS"
    heads = [m for m in slack_pages("conversations.history", "messages", channel=channel,
                                    oldest=str(int(time.time() - days * 86400)), limit=200)
             if (m.get("text") or "").startswith(title) and m.get("reply_count")]
    out: dict[str, str] = {}
    for h in heads:
        replies = slack_pages("conversations.replies", "messages", channel=channel, ts=h["ts"], limit=200)[1:]
        detail = " ".join(r.get("text") or "" for r in replies if r.get("bot_id"))
        when = datetime.fromtimestamp(float(h["ts"]), timezone.utc).strftime("%d %b %Y")
        for r in replies:
            m = re.match(r"^\s*done\b[\s:,.-]*(.*)$", r.get("text") or "", re.I | re.S)
            if r.get("user") not in admins or not m:
                continue
            rest = m.group(1).strip().lower()
            for row in rows:
                names = [x for x in (row.get("id"), row.get("key")) if x]
                last = (row.get("refs") or "").split("·")[-1].strip()
                if rest:
                    hit = any(x.lower() in rest for x in names + ([last] if len(last) >= 4 else []))
                else:
                    hit = any(re.search(rf"(?<![\w-]){re.escape(x)}(?![\w-])", detail) for x in names)
                if hit and row["kind"] != "reconcile":
                    out[row["ref"]] = f"{admins[r['user']]} replied done in the {title} thread of {when}"
    return out


def python_first(rows: list[dict], client=None) -> list[dict]:
    """The verdicts Python can give on the record itself. Read-only: the Xero
    client refuses everything but GET while this runs."""
    out, cache = [], {}
    prior = os.environ.get("AGENT_READONLY")
    os.environ["AGENT_READONLY"] = "1"
    try:
        for r in rows:
            rule = bill_rule(r)
            if not rule:
                continue
            try:
                ev = bill_settled(*rule, cache, client)
            except Exception as e:                               # noqa: BLE001 - the coordinator gets it
                print(f"python check of {r['ref']} failed ({type(e).__name__}); left to the coordinator")
                continue
            if ev:
                out.append({"ref": r["ref"], "verdict": "CLOSED", "definite": True, "evidence": ev})
    finally:
        if prior is None:
            os.environ.pop("AGENT_READONLY", None)
        else:
            os.environ["AGENT_READONLY"] = prior
    return out


def tier(row: dict) -> str:
    return "light" if row["kind"] in LIGHT_KINDS else "full"


def write_batches(rows: list[dict], folder: Path, size: int | None = None) -> int:
    size = size or config.outstanding_check()["batch_size"]
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(f"{tier(r)}-{r['domain']}-{r['kind']}", []).append(r)
    folder.mkdir(parents=True, exist_ok=True)
    n = 0
    for name, members in sorted(groups.items()):
        for start in range(0, len(members), size):
            n += 1
            part = [{k: v for k, v in m.items() if k != "closable"} for m in members[start:start + size]]
            (folder / f"{n:02d}-{name}.json").write_text(json.dumps(part, indent=1, ensure_ascii=False) + "\n")
    return n


# ---------------------------------------------------------------- the session

def agent_model(name: str) -> str | None:
    """The Agent tool's name for a configured model, or None when the name
    carries none of them."""
    low = (name or "").lower()
    return next((m for m in AGENT_MODELS if m in low), None)


def models_line(settings: dict) -> str:
    """Which model each tier of batch goes with."""
    said = []
    for t, key in (("light", "light_checker_model"), ("full", "checker_model")):
        m = agent_model(settings[key])
        said.append(f'with model "{m}" for a file whose name has -{t}-' if m
                    else f"with no model override for a file whose name has -{t}-")
    return "; ".join(said) + "."


def trivial_line(settings: dict) -> str:
    """What the agents are told about TRIVIAL: off, or the threshold and the
    only rates a line may be converted at."""
    t = float(settings.get("trivial_amount") or 0)
    if t <= 0:
        return "TRIVIAL is off: never return it."
    import outstanding as register
    cur = config.reporting_currency()
    rated = ", ".join(f"{c} {v:.6g}" for c, v in sorted(register.rates().items()) if c != cur)
    return (f"TRIVIAL is on: a bookkeeping line whose whole amount at stake, read from the record, comes to "
            f"under {cur} {t:,.2f} in all. Amounts in {cur} count as they are; convert any other "
            f"currency only at these rates, {cur} per unit: {rated or 'none given'}. A line with an "
            "amount in a currency not listed is never TRIVIAL. Pass this paragraph to every checker "
            "with its file.")


def prompt(batches: Path, n_lines: int, n_batches: int, settings: dict) -> str:
    return PROMPT.format(batches=batches, n=n_lines, b=n_batches, models=models_line(settings),
                         trivial=trivial_line(settings))


def parse(text: str) -> dict | None:
    if not text:
        return None
    fenced = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    for c in fenced[::-1] + [text[text.find("{"): text.rfind("}") + 1]]:
        try:
            obj = json.loads(c)
        except ValueError:
            continue
        if isinstance(obj, dict) and isinstance(obj.get("verdicts"), list):
            return obj
    return None


def run_session(run: Path, n_lines: int, n_batches: int, run_id: str) -> tuple[dict | None, str, float]:
    """-> (verdicts or None, why not, the session's cost in USD)."""
    settings = config.outstanding_check()
    checker = settings["checker_model"]
    env = dict(os.environ)
    env.update({
        "AGENT_READONLY": "1",
        # The bookkeeping prompt hook stands down for an investigation-shaped
        # session, and guard-readonly allows scratch files under QUERY_DIR.
        "AGENT_INVESTIGATION": "1",
        "QUERY_JOB": "outstanding-check",
        "QUERY_DIR": str(run),
        "AGENT_RUN_ID": run_id,
        # The model on each call (the prompt) decides. A checker sent with
        # none, when a configured model's name carries no name the Agent tool
        # takes, runs on its agent file's model; this is the last fallback.
        "CLAUDE_CODE_SUBAGENT_MODEL": checker,
        "PATH": f"{Path.home() / '.local' / 'bin'}:{env.get('PATH', '')}",
    })
    rel = shown(run)
    cmd = ["claude", "-p", prompt(rel / "batches", n_lines, n_batches, settings),
           "--agent", COORDINATOR_AGENT, "--model", config.model("default"), "--effort", COORDINATOR_EFFORT,
           "--permission-mode", "bypassPermissions", "--output-format", "json",
           "--disallowedTools", "Write", "Edit", "NotebookEdit"]
    try:
        with (run / "run.log").open("a") as err:
            proc = subprocess.run(cmd, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=err,
                                  text=True, timeout=HARD_TIMEOUT)
    except subprocess.TimeoutExpired:
        return None, f"the session timed out after {HARD_TIMEOUT} seconds", 0.0
    (run / "session.json").write_text(proc.stdout or "")
    try:
        out = json.loads(proc.stdout or "{}")
    except ValueError:
        out = {"result": proc.stdout}
    cost = out.get("total_cost_usd") if isinstance(out, dict) else None
    cost = float(cost) if isinstance(cost, (int, float)) else 0.0
    verdicts = parse(out.get("result") or "")
    if proc.returncode != 0 or verdicts is None:
        return None, (f"the session exited {proc.returncode}" if proc.returncode
                      else "the session's last message was not the verdicts object"), cost
    return verdicts, "", cost


# ---------------------------------------------------------------- applying

def outstanding(*args: str) -> str:
    r = subprocess.run([PY, "scripts/outstanding.py", *args], cwd=ROOT, capture_output=True, text=True)
    return (r.stdout + r.stderr).strip()


CLEARED = {"NOT_NEEDED": "not_needed", "TRIVIAL": "trivial"}


def clears(row: dict, verdict: str, trivial_on: bool) -> bool:
    """May this verdict take the line off without it being done? NOT_NEEDED
    only for a bookkeeping manual step, TRIVIAL for any bookkeeping line and
    only with a threshold set; never a reconcile line."""
    if row["domain"] != "bookkeeping" or row["kind"] == "reconcile":
        return False
    if verdict == "NOT_NEEDED":
        return row["kind"] == "manual"
    return verdict == "TRIVIAL" and trivial_on


def apply(rows: list[dict], verdicts: list[dict], today: str, dry_run: bool,
          trivial_on: bool | None = None) -> dict:
    if trivial_on is None:
        trivial_on = float(config.outstanding_check()["trivial_amount"]) > 0
    by_ref = {r["ref"]: r for r in rows}
    done = {"closed": [], "kept": [], "partly": [], "unverified": [], "open": 0, "missing": [],
            "not_needed": [], "trivial": []}
    seen = set()
    drop_rec = set()
    for v in verdicts:
        row = by_ref.get(v.get("ref", ""))
        if not row:
            continue
        seen.add(row["ref"])
        verdict = (v.get("verdict") or "").upper()
        ev = (v.get("evidence") or "").strip()
        if verdict in CLEARED:
            # Off the list on the same footing as a done line: definite, and
            # the line closable by its key. Anything short of that stays.
            if clears(row, verdict, trivial_on) and v.get("definite") is True and row["closable"]:
                cleared = CLEARED[verdict]
                if not dry_run:
                    outstanding("close", "--key", row["ref"], "--reason",
                                f"checked {today}: {cleared.replace('_', ' ')}, {ev}", "--defer")
                done[cleared].append((row, ev))
            else:
                done["open"] += 1
            continue
        if verdict == "CLOSED" and v.get("definite") is True and row["closable"]:
            if row["kind"] == "reconcile":
                drop_rec.add(row["ref"])
                done["closed"].append((row, ev))
                continue
            if not dry_run:
                outstanding("close", "--key", row["ref"], "--reason", f"checked {today}: {ev}", "--defer")
            done["closed"].append((row, ev))
        elif verdict == "CLOSED":
            done["kept"].append((row, ev))
        elif verdict == "PARTLY":
            done["partly"].append((row, (v.get("left") or ev).strip()))
        elif verdict == "UNVERIFIED":
            done["unverified"].append((row, ev))
        else:
            done["open"] += 1
    done["missing"] = [r for r in rows if r["ref"] not in seen]
    if drop_rec and not dry_run:
        rec = json.loads(RECONCILE.read_text())
        keep = [r for n, r in enumerate(rec.get("items", []), 1) if f"REC-{n:02d}" not in drop_rec]
        BASE.mkdir(parents=True, exist_ok=True)
        tmp = BASE / "reconcile.next.json"
        tmp.write_text(json.dumps({**rec, "items": keep}, indent=1, ensure_ascii=False) + "\n")
        outstanding("set-reconcile", "--file", str(tmp), "--defer")
    if (done["closed"] or done["not_needed"] or done["trivial"]) and not dry_run:
        outstanding("publish")
    return done


def short(text: str, width: int = 80) -> str:
    """The line's first clause, cut at a word."""
    text = re.split(r"[;:.]\s", text, maxsplit=1)[0]
    return text if len(text) <= width else text[:width].rsplit(" ", 1)[0] + "..."


def prune_trivial(dry_run: bool) -> list[tuple[dict, str]]:
    """Delete (or with dry_run, only name) every bookkeeping line under the
    threshold on its own wording, before any agent sees it. -> (row, reason)
    pairs for the post."""
    out = outstanding("prune-trivial", "--json", "--defer", *(["--dry-run"] if dry_run else []))
    pruned = None
    for raw in reversed(out.splitlines()):
        if raw.startswith("["):
            try:
                pruned = json.loads(raw)
                break
            except ValueError:
                pass
    if pruned is None:
        print("the trivial prune failed; the agents judge those lines instead")
        return []
    return [({"id": p["id"], "domain": "bookkeeping", "kind": p["kind"]},
             f"{short(p['text'])}, {p['currency']} {p['amount']:,.2f} in all") for p in pruned]


def with_pruned(done: dict, pruned: list, dry_run: bool) -> dict:
    """Add the lines the trivial prune deleted before any agent ran, and
    publish the Word copy once for them if nothing else did."""
    if pruned and not dry_run and not (done["closed"] or done["not_needed"] or done["trivial"]):
        outstanding("publish")
    done["trivial"] = pruned + done["trivial"]
    return done


def report(done: dict, total: int, notes: list[str] | None = None, threshold: str = "") -> str:
    """The thread under the headline, docs/COMMS.md: words first, the
    reference trailing, one section per category. `threshold` is the trivial
    amount as written, `GBP 50.00`."""
    def ref(row: dict) -> str:
        return row["id"] if row["kind"] != "reconcile" else f"reconcile list · {row['refs']}"

    def said(pairs: list) -> list[str]:
        # One line per distinct reason: a reason shared by three lines or
        # more (a source the checker cannot read) is said once with a count.
        groups: dict[str, list[dict]] = {}
        for row, text in pairs:
            groups.setdefault(text.strip().rstrip("."), []).append(row)
        out = []
        for text, rows in groups.items():
            if len(rows) < 3:
                out += [f"- {text} · {ref(r)}" for r in rows]
                continue
            domains = ", ".join(sorted({r["domain"] for r in rows}))
            out.append(f"- {text} · {len(rows)} {domains} lines, {rows[0]['id']} to {rows[-1]['id']}")
        return out

    under = f"under {threshold}" if threshold else "trivial"
    gone = len(done.get("not_needed", [])) + len(done.get("trivial", []))
    out = [f"checked {total} lines: {len(done['closed'])} done and deleted, "
           + (f"{gone} not needed or {under} and deleted, " if gone else "")
           + f"{len(done['partly'])} partly done, "
           f"{len(done['unverified'])} could not be checked, {done['open']} still open"]
    out += [f"- {n}" for n in notes or []]
    for title, key in (("closed", "closed"), ("not needed, deleted", "not_needed"),
                       (f"{under}, deleted", "trivial"), ("done, not closed: not certain", "kept"),
                       ("partly done", "partly"), ("could not check", "unverified")):
        if done.get(key):
            out += ["", f"*{title}*"] + said(done[key])
    if done["missing"]:
        out += ["", "*not checked*", f"- the session returned no verdict for {len(done['missing'])} lines · "
                + ", ".join(r["id"] for r in done["missing"][:40])]
    return "\n".join(out)


def write_extra_cost(run_id: str, cost: float, running: bool = False) -> Path:
    """The session's cost as an extra cost file, which run_cost.py adds to the
    bracket: data/runs/<run id>/extra/<name>.json."""
    folder = RUNS / run_id / "extra"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "outstanding-check.json"
    path.write_text(json.dumps({"label": "check", "cost_usd": round(cost, 4), "running": running}) + "\n")
    return path


def post(head: str, body: str, run_id: str, cost: float, dry_run: bool, own_run: bool = True) -> None:
    """Queue the post for the run (posted with the cost bracket when the run is
    stamped), or post it at once when there is no run to stamp."""
    if dry_run:
        print(f"DRY RUN, would post:\n{head}\n{body}")
        return
    if run_id:
        os.environ["AGENT_RUN_ID"] = run_id
    else:
        os.environ.pop("AGENT_RUN_ID", None)
    import slack_outbox
    print(slack_outbox.queue(headline=head, thread=body))
    if not run_id:
        return
    write_extra_cost(run_id, cost)
    if not own_run:
        return                                   # the caller stamps its own run
    # The run's own session is none: the Claude work is the extra cost file.
    stub = RUNS / f"{run_id}.session.json"
    stub.write_text(json.dumps({"total_cost_usd": 0.0, "session_id": None}) + "\n")
    r = subprocess.run([PY, "scripts/run_cost.py", "stamp", "--run-id", run_id, "--session-json", str(stub)],
                       cwd=ROOT, capture_output=True, text=True)
    print((r.stdout + r.stderr).strip())


def begin_run() -> tuple[str, bool]:
    """-> (run id, whether this script owns the run). An id from the caller's
    environment is the caller's; otherwise one is begun here, and '' when that
    fails."""
    given = os.environ.get("AGENT_RUN_ID", "").strip()
    if given:
        return given, False
    r = subprocess.run([PY, "scripts/run_cost.py", "begin", "--job", "outstanding-check"],
                       cwd=ROOT, capture_output=True, text=True)
    return (r.stdout.strip() if r.returncode == 0 else ""), True


def cmd_run(a) -> int:
    settings = config.outstanding_check()
    if not settings["enabled"]:
        print("[outstanding_check] enabled is false: nothing to do")
        return 0
    quiet = a.dry_run or a.no_session
    if not quiet:
        from accounting_agent import readonly
        readonly.refuse("closing register lines and posting the outstanding check")
    load_env()
    today = datetime.now(timezone.utc).date().isoformat()
    head = headline()
    run_id, own = ("", True) if quiet else begin_run()
    left, where = xero_budget()
    if left < settings["min_xero_calls"]:
        post(head, f"not run: {left} Xero calls left today in {where}, under the {settings['min_xero_calls']} "
                   "the check needs; tomorrow's run picks it up", run_id, 0.0, quiet, own)
        return 0
    if not REGISTER.exists():
        print("no register yet")
        return 0
    # Under the trivial amount on its own wording: deleted here, before any
    # agent sees it. --no-session and --dry-run only say what would go.
    threshold = float(settings["trivial_amount"])
    pruned = []
    if threshold > 0 and a.domain in (None, "bookkeeping"):
        pruned = prune_trivial(quiet)
    said = f"{config.reporting_currency()} {threshold:,.2f}" if threshold > 0 else ""
    rows = lines(a.domain)
    if quiet:
        gone = {p["id"] for p, _ in pruned}
        rows = [r for r in rows if r["domain"] != "bookkeeping" or r["kind"] == "reconcile"
                or r["id"] not in gone]
    if not rows and not pruned:
        print("nothing on the register")
        return 0
    notes = []
    held = [r for r in rows if r["kind"] == "reconcile" and reconcile_fresh(today)]
    if held:
        notes.append(f"the {len(held)} reconcile list lines were not checked again: the list was rebuilt this "
                     "morning from the bank reconcile check, which holds each one open")
    rows = [r for r in rows if r not in held]
    try:
        for ref, said in done_replies(rows).items():
            next(r for r in rows if r["ref"] == ref)["done_in_thread"] = said
    except (Exception, SystemExit) as e:                         # noqa: BLE001 - evidence, not a gate
        print(f"the channel's manual items threads could not be read ({type(e).__name__}: {e})")
    ours = python_first(rows)
    closed_here = {v["ref"] for v in ours}
    rest = [r for r in rows if r["ref"] not in closed_here]
    if ours:
        notes.append(f"{len(ours)} closed here on the bill in Xero, {len(rest)} sent to the coordinator")
    if a.no_session:
        print("\n".join(notes) or "nothing closed in Python")
        for v in ours:
            print(f"  CLOSED {v['ref']}: {v['evidence']}")
        for r in rest:
            if r.get("done_in_thread"):
                print(f"  done in thread {r['ref']}: {r['done_in_thread']}")
        for row, ev in pruned:
            print(f"  UNDER {said} {row['id']}: {ev}")
        print(f"{len(rest)} lines would go to the coordinator; nothing closed, nothing posted")
        return 0
    run = BASE / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    verdicts, cost = {"verdicts": []}, 0.0
    if rest:
        n_batches = write_batches(rest, run / "batches", settings["batch_size"])
        print(f"{len(rest)} lines in {n_batches} batches under {shown(run)}")
        verdicts, why, cost = run_session(run, len(rest), n_batches, run_id)
        if verdicts is None:
            done = with_pruned(apply([r for r in rows if r["ref"] in closed_here], ours, today, a.dry_run,
                                     threshold > 0), pruned, a.dry_run)
            post(head, f"failed: {why}; nothing the session checked was closed · {shown(run)}\n"
                 + report(done, len(closed_here) + len(pruned), notes, said), run_id, cost, a.dry_run, own)
            return 1
    run.mkdir(parents=True, exist_ok=True)
    (run / "result.json").write_text(json.dumps({"verdicts": ours + verdicts["verdicts"]},
                                                indent=1, ensure_ascii=False) + "\n")
    done = with_pruned(apply(rows, ours + verdicts["verdicts"], today, a.dry_run, threshold > 0),
                       pruned, a.dry_run)
    done["open"] += len(held)
    post(head, report(done, len(rows) + len(held) + len(pruned), notes, said), run_id, cost, a.dry_run, own)
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="check every line, close what is definitely done, post the run")
    r.add_argument("--dry-run", action="store_true", help="close nothing, post nothing, print the report")
    r.add_argument("--domain", help="one domain only")
    r.add_argument("--no-session", action="store_true",
                   help="the Python checks only: print what they find, start no session, close and post nothing")
    a = p.parse_args(argv)
    return cmd_run(a)


if __name__ == "__main__":
    sys.exit(main())
