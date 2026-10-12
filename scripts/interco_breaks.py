#!/usr/bin/env python
"""The open items an intercompany reconciliation leaves, and the record of
lines that are one transaction although the matcher cannot pair them.

    python scripts/interco_breaks.py list --json data/interco/latest.json
    python scripts/interco_breaks.py open [--json data/interco/latest.json] [--key <key> ...] \\
        [--max 12] [--fresh]
    python scripts/interco_breaks.py confirm --key <key> --reason "<evidence>" --by <who> \\
        [--all-lines] [--doc-id <id> ...] [--json <recon json>]
    python scripts/interco_breaks.py unconfirm --key <key>

What counts as an open item, per pair and flavour of interco_recon.py's JSON:

  one-sided   a line in one ledger with no counterpart in the other
  probable    a probable match whose gap is a real difference to explain
  amount      a matched pair booked in the same document currency at
              different amounts, apart in the reporting currency as well,
              payments excluded

Lines under the rounding tolerance, reversals, FX revaluations, timing
differences, group matches and translation-only gaps are left out: the recon
has already explained them (docs/INTERCOMPANY_RECON.md §3). Related lines in
one pair (the same amount, dates within 3 days, or the same counterparty) are
one item, so whoever settles it sees both sides of the story at once. Each
item has a stable key, `interco-<pair>-<flavour>-<first date>-<amount>`.

`open` turns each open item into a query investigation (skill
`query-investigation`, scripts/investigate_query.py), all of one run's items in
one batch. The brief names the pair, the flavour, both loan accounts and every
open line; the lines and the same amount in the other entity go in as --txn
evidence, so the pre-check looks for the mirror before any agent starts, and the
lines themselves are the --evidence. An item whose lines (by doc_id) an earlier
investigation already had keeps that earlier key when the rate moves the
reporting-currency size. An item whose lines are unchanged since a verdict that
is safe to act on twice is reused with no pre-check and no session, unless
--fresh; a verdict that posted a correction is looked at again, and the look
sees the correction. The verdict is a plan: the run takes its grade to the gate,
and its actions are manual journals in the entity missing the entry, or a
`match` action recorded with `confirm --doc-id`.

`confirm` records that an item's lines are each other's counterpart (all of
them with --all-lines, or the --doc-id lines named) in
data/interco/confirmed_matches.json. The usual case is a correction dated a
month end against a line weeks earlier: the matcher's date window cannot
reach it, so the pair stays one-sided and its FX revaluation refuses. From
then on interco_recon.py pairs those lines before any other pass, and
interco_matrix.py files them under Netted, which the pair tab's filter hides.
--doc-id adds a correction already posted, so it nets with the pair it fixes.

A confirmation is a judgement, so it says why (--reason: the documents that
prove it) and who decided (--by: the admin who ruled, or `agent` where the
documents settle it on their own: equal and opposite amounts, or a correction
whose narration names the line it fixes). It changes no ledger. `unconfirm`
removes one recorded in error.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from accounting_agent import config  # noqa: E402
import investigate_query  # noqa: E402
from interco_recon import CONFIRMED, _tokens  # noqa: E402

SAME_DAYS = 3
MIRROR_WINDOW_DAYS = 60


def tol() -> float:
    return float(config.intercompany_settings()["tolerance_abs"])


def doc_ccy(t: dict) -> str:
    return t.get("currency") or t.get("entity_base") or ""


def in_rep(t: dict, rates: dict) -> float:
    return float(t["base"]) * float(rates.get(t.get("entity_base") or "", 1.0) or 1.0)


def tokens(t: dict) -> set[str]:
    return _tokens(f"{t.get('party') or ''} {t.get('desc') or ''}")


def line(t: dict, rates: dict, rep: str) -> str:
    return (f"[{t['entity']}] {t['date']} {t['source']:13} {doc_ccy(t)} {float(t['doc_amount']):,.2f}"
            f" ({rep} {in_rep(t, rates):,.2f}) account {t['code']} | {t.get('party') or ''}"
            f" | {(t.get('desc') or '').strip()[:120]} | doc_id {t.get('doc_id') or '?'}")


def clusters(rows: list[dict], rates: dict) -> list[list[dict]]:
    """Union the related one-sided lines of one pair."""
    parent = list(range(len(rows)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(rows)):
        for j in range(i + 1, len(rows)):
            a, b = rows[i], rows[j]
            ua, ub = abs(in_rep(a, rates)), abs(in_rep(b, rates))
            close_amount = ua and ub and abs(ua - ub) <= 0.02 * max(ua, ub)
            close_date = abs((date.fromisoformat(a["date"]) - date.fromisoformat(b["date"])).days) <= SAME_DAYS
            if close_amount or close_date or (tokens(a) & tokens(b)):
                parent[find(i)] = find(j)
    groups: dict[int, list[dict]] = {}
    for i, r in enumerate(rows):
        groups.setdefault(find(i), []).append(r)
    return list(groups.values())


def open_items(recon: dict) -> list[dict]:
    rates = recon.get("rates") or {}
    floor = tol()
    out = []
    for p in recon["pairs"]:
        if not (p.get("a") and p.get("b")):
            continue                     # no mirror account: a structural gap, not a break
        pair, _, flav = p["pair"].partition(" / ")
        flavour = p.get("flavour") or flav
        a, b = p["a"], p["b"]
        head = {"pair": pair, "flavour": flavour, "break": p.get("break"), "status": p.get("status"),
                "accounts": f"{a['entity']} {a.get('code')} {a.get('balance')} {a.get('base')}"
                            f" / {b['entity']} {b.get('code')} {b.get('balance')} {b.get('base')}"}
        one = [t for t in (p.get("unmatched_a") or []) + (p.get("unmatched_b") or [])
               if abs(in_rep(t, rates)) >= floor and not t.get("reversed")]
        for c in clusters(one, rates):
            out.append({**head, "kind": "one-sided", "lines": c})
        for ga, gb, gap in p.get("probable") or []:
            out.append({**head, "kind": "probable", "lines": list(ga) + list(gb), "gap": gap})
        for m in p.get("matched") or []:
            if len(m) < 2 or not isinstance(m[0], dict):
                continue
            ta, tb = m[0], m[1]
            # A payment line carries its bill's currency label, not the cash's,
            # so its document amount cannot be compared; and both the document
            # amounts and the reporting amounts must differ, or it is translation.
            if doc_ccy(ta) != doc_ccy(tb) or "PAY:" in ta["source"] + tb["source"]:
                continue
            da, db = abs(float(ta["doc_amount"])), abs(float(tb["doc_amount"]))
            ua, ub = abs(in_rep(ta, rates)), abs(in_rep(tb, rates))
            if (abs(da - db) > max(floor, 0.005 * max(da, db))
                    and abs(ua - ub) > max(5 * floor, 0.005 * max(ua, ub))):
                out.append({**head, "kind": "amount", "lines": [ta, tb]})
    # Amount differences on one pair and one day are one story: one item.
    merged, by_day = [], {}
    for it in out:
        if it["kind"] != "amount":
            merged.append(it)
            continue
        k = (it["pair"], it["flavour"], min(t["date"] for t in it["lines"]))
        if k in by_day:
            by_day[k]["lines"] += it["lines"]
        else:
            by_day[k] = it
            merged.append(it)
    for it in merged:
        first = min(t["date"] for t in it["lines"])
        size = max(abs(in_rep(t, rates)) for t in it["lines"])
        slug = re.sub(r"[^a-z0-9]+", "-", f"{it['pair']}-{it['flavour']}".lower()).strip("-")
        it["key"] = f"interco-{slug}-{first}-{int(round(size))}"
        it["size"] = size
    return merged


def load_store() -> list[dict]:
    try:
        return json.loads(CONFIRMED.read_text())
    except (OSError, ValueError):
        return []


def save_store(store: list[dict]) -> None:
    CONFIRMED.parent.mkdir(parents=True, exist_ok=True)
    CONFIRMED.write_text(json.dumps(store, indent=2, ensure_ascii=False) + "\n")


def entity_name(key: str) -> str:
    """An entity as the brief names it: its Xero name, from config."""
    try:
        return config.entity(key).xero_name
    except config.ConfigError:
        return key


def entity_ref(key: str) -> str:
    """The same entity as a --txn token (investigate_query resolves it)."""
    return entity_name(key)


def pair_ends(it: dict) -> tuple[str, str]:
    a, _, b = it["pair"].partition("<>")
    return a, b


def describe(it: dict, rates: dict) -> tuple[str, str]:
    """The question and the context an investigation of one item starts from."""
    rep = config.reporting_currency()
    a, b = pair_ends(it)
    what = {"one-sided": "has lines on one side with no counterpart on the other",
            "probable": "has a probable match whose amounts differ",
            "amount": "has a matched pair booked at different amounts in the same currency"}[it["kind"]]
    question = (f"The {entity_name(a)} and {entity_name(b)} {it['flavour']} intercompany loan "
                f"{what}. What corrects it: post the missing side (and where, how coded, at what "
                f"amount and date), recode or reverse a wrong entry, or nothing because it is a match, "
                f"timing or already dealt with?")
    ctx = [f"From the intercompany reconciliation (scripts/interco_recon.py), pair {it['pair']} / "
           f"{it['flavour']}, status {it['status']}, pair break {rep} {float(it.get('break') or 0):,.2f}.",
           f"Loan accounts: {it['accounts']}.", "The open lines:"]
    ctx += ["  " + line(t, rates, rep) for t in it["lines"]]
    if it.get("gap") is not None:
        ctx.append(f"Probable-match gap: {it['gap']}")
    ctx.append("If the open lines are each other's counterpart (one transaction posted on dates "
               "too far apart for the matcher), add a `match` action listing their doc_ids: the run "
               "records it with interco_breaks.py confirm and the recon pairs them from then on.")
    ctx.append("Method, break classes and the corrections each class takes: "
               "docs/INTERCOMPANY_RECON.md and rules/INTERCOMPANY.md. "
               "A one-sided line usually means the other entity never posted its side; "
               "check before proposing one.")
    return question, "\n".join(ctx)


def txn_args(it: dict) -> list[str]:
    """Each line where it sits, and the same amount in the other entity over a
    wide window, so the pre-check looks for the mirror."""
    a, b = pair_ends(it)
    args = []
    for t in it["lines"][:4]:
        other = b if t["entity"] == a else a
        amt = abs(float(t["doc_amount"]))
        d = date.fromisoformat(t["date"])
        lo, hi = d - timedelta(days=MIRROR_WINDOW_DAYS), d + timedelta(days=MIRROR_WINDOW_DAYS)
        ccy = t.get("currency") or ""
        args += ["--txn", f"{entity_ref(t['entity'])}|{t['date']}|{amt:.2f}|{ccy}",
                 "--txn", f"{entity_ref(other)}|{lo}..{hi}|{amt:.2f}"]
    return args


DOC_ID = re.compile(r"doc_id ([0-9a-fA-F-]{36})")


def doc_ids(it: dict) -> list[str]:
    return sorted({t["doc_id"] for t in it["lines"] if t.get("doc_id")})


def stable_key(it: dict) -> str:
    """The key an earlier investigation of exactly these lines carried, so a
    rate move that shifts the reporting-currency size by a unit does not start
    afresh."""
    ids = set(doc_ids(it))
    if not ids:
        return it["key"]
    prefix = it["key"].rsplit("-", 1)[0] + "-"
    for d in sorted(investigate_query.key_dirs(), key=lambda p: p.name):
        if d.name == it["key"] or not d.name.startswith(prefix):
            continue
        run = investigate_query.latest(d.name)
        b = investigate_query._read(run / "brief.json") if run else {}
        seen = set(b.get("doc_ids") or DOC_ID.findall(b.get("context") or ""))
        if seen == ids and b.get("key"):
            return b["key"]
    return it["key"]


def evidence(it: dict) -> list[str]:
    """The item's lines as they stand, without the reporting-currency translation."""
    return sorted(f"{t['entity']}|{t['date']}|{t['source'].strip()}|{doc_ccy(t)}|"
                  f"{float(t['doc_amount']):.2f}|{t['code']}|{t.get('doc_id') or ''}"
                  for t in it["lines"])


def brief_for(it: dict, rates: dict, with_: str, run_job: str) -> dict:
    """The investigation brief for one open item."""
    rep = config.reporting_currency()
    question, context = describe(it, rates)
    args = txn_args(it)
    return {"domain": "bookkeeping", "key": it["key"], "with": with_ or config.default_admin(),
            "question": question, "context": context,
            "refs": f"{it['pair']} / {it['flavour']} · {rep} {it['size']:,.2f}",
            "raised": date.today().isoformat(), "raised_by": run_job,
            "txns": [investigate_query.parse_txn_arg(x) for x in args[1::2]],
            "evidence": evidence(it), "doc_ids": doc_ids(it)}


def cmd_open(a) -> int:
    recon = json.loads(Path(a.json).read_text())
    rates = recon.get("rates") or {}
    items = sorted(open_items(recon), key=lambda i: -i["size"])
    if a.key:
        items = [i for i in items if i["key"] in a.key or stable_key(i) in a.key]
    # One batch for the run's breaks: the run id groups them, or this name
    # when the script is run by hand.
    if not (os.environ.get("AGENT_RUN_ID") or os.environ.get("INVESTIGATION_BATCH")):
        os.environ["INVESTIGATION_BATCH"] = "interco-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    group = investigate_query.group_of()
    opened = reused = 0
    for it in items:
        it["key"] = stable_key(it)
        brief = brief_for(it, rates, a.with_, a.run_job)
        if not a.fresh and investigate_query.reusable_prior(brief, None):
            route, run = investigate_query.open_investigation(brief, reuse_on_evidence=True, group=group)
            print(f"{'reuse' if route == 'reused' else route:8} {it['key']}  {investigate_query.rel(run)}")
            reused += route == "reused"
            continue
        if opened >= a.max:
            print(f"held     {it['key']}  over --max {a.max}; the next run opens it")
            continue
        try:
            route, run = investigate_query.open_investigation(brief, fresh=a.fresh, group=group)
        except Exception as e:                                   # noqa: BLE001
            print(f"FAILED   {it['key']}  {type(e).__name__}: {e}")
            continue
        print(f"{route:8} {it['key']}  {investigate_query.rel(run)}")
        opened += route == "opened"
    if opened:
        investigate_query.trigger(group)          # the batch starts once the pre-checks are in
    print(f"{opened} opened, {reused} re-used, {len(items)} open items")
    return 0


def cmd_list(a) -> int:
    recon = json.loads(Path(a.json).read_text())
    rates = recon.get("rates") or {}
    rep = recon.get("reporting_currency") or config.reporting_currency()
    items = open_items(recon)
    for it in sorted(items, key=lambda i: -i["size"]):
        it["key"] = stable_key(it)
        seen = investigate_query.latest(it["key"])
        tag = "verdict on disk" if seen and (seen / "result.json").exists() else "new"
        print(f"{it['kind']:9} {it['pair']} / {it['flavour']}  {rep} {it['size']:,.2f}  [{tag}]  {it['key']}")
        for t in it["lines"]:
            print("    " + line(t, rates, rep))
    print(f"{len(items)} open items")
    return 0


def cmd_confirm(a) -> int:
    if not a.reason.strip() or not a.by.strip():
        raise SystemExit("--reason and --by are required: a confirmation says why and who decided")
    recon = json.loads(Path(a.json).read_text())
    item = next((i for i in open_items(recon) if i["key"] == a.key), None)
    if item is None:
        raise SystemExit(f"{a.key} is not an open item in {a.json}")
    open_ids = [t["doc_id"] for t in item["lines"] if t.get("doc_id")]
    if a.all_lines:
        ids = open_ids
    elif a.doc_id:
        ids = []
    else:
        raise SystemExit("name the lines: --all-lines for every line of the item, or --doc-id for each")
    ids = list(dict.fromkeys(ids + list(a.doc_id or [])))
    # Every line must sit on this pair's two accounts, or the recon could
    # never apply the entry.
    pair = next(p for p in recon["pairs"]
                if p["pair"].partition(" / ")[0] == item["pair"]
                and (p.get("flavour") or p["pair"].partition(" / ")[2]) == item["flavour"])
    on_pair = set()
    for k in ("unmatched_a", "unmatched_b"):
        on_pair |= {t.get("doc_id") for t in pair.get(k) or []}
    for k in ("matched",):
        on_pair |= {t.get("doc_id") for m in pair.get(k) or [] for t in m[:2] if isinstance(t, dict)}
    for k in ("groups", "probable", "confirmed"):
        on_pair |= {t.get("doc_id") for ga, gb, _ in pair.get(k) or [] for t in list(ga) + list(gb)}
    stray = [i for i in ids if i not in on_pair]
    if stray:
        raise SystemExit(f"not on {item['pair']} / {item['flavour']} in {a.json}: {', '.join(stray)}")
    if len(ids) < 2:
        raise SystemExit("a match needs at least two lines")
    store = load_store()
    if any(set(e["doc_ids"]) == set(ids) for e in store):
        print(f"{a.key} already confirmed")
        return 0
    store.append({"key": a.key, "pair": item["pair"], "flavour": item["flavour"], "doc_ids": ids,
                  "reason": a.reason.strip()[:300], "by": a.by.strip(), "added": date.today().isoformat()})
    save_store(store)
    print(f"{a.key} confirmed: {len(ids)} lines pair from the next recon")
    return 0


def cmd_unconfirm(a) -> int:
    store = load_store()
    keep = [e for e in store if e.get("key") != a.key]
    if len(keep) == len(store):
        raise SystemExit(f"{a.key} is not confirmed")
    save_store(keep)
    print(f"{a.key} removed; the next recon matches those lines on its own again")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    latest = str(ROOT / "data" / "interco" / "latest.json")
    ls = sub.add_parser("list", help="the open items, largest first")
    ls.add_argument("--json", default=latest)
    ls.set_defaults(func=cmd_list)
    op = sub.add_parser("open", help="open an investigation for every open item")
    op.add_argument("--json", default=latest)
    op.add_argument("--max", type=int, default=12, help="new investigations this run, largest first")
    op.add_argument("--fresh", action="store_true", help="investigate again even where a verdict exists")
    op.add_argument("--key", action="append", help="only this item; repeat for several")
    op.add_argument("--with", dest="with_", default="", help="who it goes to if it stays open (default: first admin)")
    op.add_argument("--run-job", default="interco")
    op.set_defaults(func=cmd_open)
    cf = sub.add_parser("confirm", help="record that an item's lines are one transaction")
    cf.add_argument("--key", required=True)
    cf.add_argument("--reason", required=True, help="the evidence: which documents prove it")
    cf.add_argument("--by", required=True, help="the admin who ruled, or `agent` where the documents settle it")
    cf.add_argument("--all-lines", action="store_true", help="every line of the item is one transaction")
    cf.add_argument("--doc-id", action="append",
                    help="a line to include (the item's own, or a correction already posted); repeat")
    cf.add_argument("--json", default=latest)
    cf.set_defaults(func=cmd_confirm)
    un = sub.add_parser("unconfirm", help="remove a confirmation recorded in error")
    un.add_argument("--key", required=True)
    un.set_defaults(func=cmd_unconfirm)
    a = p.parse_args(argv)
    return a.func(a)


if __name__ == "__main__":
    raise SystemExit(main())
