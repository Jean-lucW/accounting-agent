#!/usr/bin/env python
"""Intercompany FX revaluation: draft (and, on explicit admin instruction,
post) the one-sided journal that brings a fully matched cross-currency pair
to zero.

Reads the reconciliation's JSON (scripts/interco_recon.py --json) and, for
every configured pair whose two ledgers are in different base currencies:

  * REFUSES while any transaction is still one-sided. A revaluation posted
    over an unmatched item books that item as FX and hides it
    (docs/INTERCOMPANY_RECON.md §9, "never plug to FX"). Clear the item first.
  * Otherwise drafts, in the entity carrying the foreign-currency balance,
    a journal on the pair's account against the FX gain/loss account
    (config [intercompany_settings] fx_gain_loss_account), dated the as-at
    date, narrated per fx_narration (default
    "FX Adjustment for Intercompany Balance with <counterparty>").

Which entity revalues: the loan is denominated in one currency, and the
ledger whose base is that currency carries it at face value; the OTHER
ledger's carrying value floats and is the one revalued. The denomination is
read from the documents on the account (the currency most of the value was
booked in). If that is inconclusive and exactly one side is in the reporting
currency, the other side revalues. Otherwise the draft says so and --entity
names the posting side.

Same-currency pairs never get a revaluation: a break between two ledgers in
one currency is a posting error or a transfer-leg difference (§3E), not
translation.

    .venv/bin/python scripts/interco_fx.py --json data/interco/latest.json
    .venv/bin/python scripts/interco_fx.py --json ... --pair HOLDCO:OPCO_US
    .venv/bin/python scripts/interco_fx.py --json ... --pair HOLDCO:OPCO_EU --flavour loan \\
        --post --confirm "Example Holdings Limited"   # posts ONE journal, after the draft was agreed

Drafts are the default and nothing is sent to Xero without --post. Posting
happens only when an admin asks for the month's revaluations from Slack
(skill `xero-interco-fx`), never from a scheduled run, and each --post call
posts one journal (`xero` safety rule 1): the entity is named in full in
--confirm, the lock date is read live, an idempotency key is passed, and the
journal is re-read after posting. Lines carry the entity's tracking from
[intercompany_settings.fx_tracking], if any.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from accounting_agent import config  # noqa: E402

NARRATION = "FX Adjustment for Intercompany Balance with {counterparty}"
LINE_DESC = "FX Adjustment"


def fx_account() -> str:
    return str(config.intercompany_settings().get("fx_gain_loss_account") or "")


def narration_template() -> str:
    return config.section("intercompany_settings").get("fx_narration", NARRATION)


def tracking_for(entity_key: str) -> list[dict] | None:
    """Tracking on the revaluation lines in one entity, from
    [intercompany_settings.fx_tracking] <KEY> = { <category> = "<option>" }.
    None when the entity has none set (the lines go in untracked)."""
    t = (config.section("intercompany_settings").get("fx_tracking") or {}).get(entity_key) or {}
    return [{"Name": k, "Option": v} for k, v in t.items()] or None


def is_month_end(d: str) -> bool:
    dt = datetime.date.fromisoformat(d)
    return (dt + datetime.timedelta(days=1)).day == 1


def _rate(rates: dict, ccy: str) -> float:
    return float(rates.get(ccy) or 1.0)


def denomination(pair: dict, rates: dict) -> str | None:
    """The currency the loan is denominated in: the document currency that
    carries most of the reporting-currency value of the account's
    transactions (both sides, revaluations excluded). None when nothing was
    booked in the window."""
    weight: dict[str, float] = {}

    def add(t):
        ccy = t.get("currency") or t.get("entity_base")
        weight[ccy] = weight.get(ccy, 0.0) + abs(t["base"] * _rate(rates, t["entity_base"]))

    for x, y, _w in pair.get("matched", []):
        add(x)
        add(y)
    for key in ("groups", "probable", "confirmed"):
        for ga, gb, _w in pair.get(key, []):
            for t in ga + gb:
                add(t)
    for t in pair.get("unmatched_a", []) + pair.get("unmatched_b", []):
        add(t)
    if not weight:
        return None
    return max(weight, key=weight.get)


def posting_side(pair: dict, rates: dict, reporting: str, override: str | None = None) -> tuple[str | None, str]:
    """(entity key that revalues, why). None when it cannot be told."""
    a, b = pair["a"], pair["b"]
    if override:
        ent = config.entity(override).key
        if ent not in (a["entity"], b["entity"]):
            return None, f"--entity {override} is not in this pair"
        return ent, "named with --entity"
    denom = denomination(pair, rates)
    if denom == a["base"]:
        return b["entity"], f"loan denominated in {denom}, {a['entity']}'s base"
    if denom == b["base"]:
        return a["entity"], f"loan denominated in {denom}, {b['entity']}'s base"
    in_rep = [s for s in (a, b) if s["base"] == reporting]
    if len(in_rep) == 1:
        other = b if in_rep[0] is a else a
        return other["entity"], f"denomination unclear; {in_rep[0]['entity']} is in the reporting currency"
    return None, (f"denomination unclear ({denom or 'no documents in the window'}) and neither or both "
                  f"sides are in {reporting}; name the posting side with --entity")


def draft(pair: dict, rates: dict, as_at: str, reporting: str, tol: float, override: str | None = None) -> dict:
    a, b = pair["a"], pair["b"]
    a_ent, b_ent = a["entity"], b["entity"]
    brk = a["balance"] * _rate(rates, a["base"]) + b["balance"] * _rate(rates, b["base"])
    # rounding true-ups under the tolerance (category CT) do not hold up a revaluation
    unmatched = [t for t in pair.get("unmatched_a", []) + pair.get("unmatched_b", [])
                 if abs(t["base"] * _rate(rates, t["entity_base"])) >= tol]
    out = {"pair": pair["pair"], "break": round(brk, 2), "unmatched": len(unmatched),
           "matched_translation": round(pair.get("matched_residual", 0.0), 2)}

    if a["base"] == b["base"]:
        out["status"] = "same currency - no revaluation; a break here is a posting or transfer-leg difference"
        return out
    if unmatched:
        out["status"] = f"NOT READY - {len(unmatched)} one-sided item(s) still open; clear them before revaluing"
        out["open_items"] = [f"{t['entity']} {t['date']} {t['source']} {t['entity_base']} {t['base']:,.2f} | "
                             f"{t['party'][:30]} | {t['desc'][:50]}" for t in unmatched]
        return out
    if abs(brk) < tol:
        out["status"] = "agreed - nothing to revalue"
        return out
    ent, why = posting_side(pair, rates, reporting, override)
    if ent is None:
        out["status"] = f"NO POSTING SIDE - {why}"
        return out
    account = fx_account()
    if not account:
        out["status"] = "NO FX ACCOUNT - set fx_gain_loss_account in config/group.toml [intercompany_settings]"
        return out

    side = a if ent == a_ent else b
    other_ent = b_ent if ent == a_ent else a_ent
    delta = -brk / _rate(rates, side["base"])          # debit-positive movement on the loan account
    key = hashlib.sha1(f"interco-fx|{ent}|{side['code']}|{as_at}".encode()).hexdigest()[:32]
    out.update({
        "status": "DRAFT",
        "entity": config.entity(ent).xero_name, "entity_key": ent, "why": why, "date": as_at,
        "month_end": is_month_end(as_at),
        "rate_used": {c: _rate(rates, c) for c in (a["base"], b["base"])},
        "narration": narration_template().format(counterparty=config.entity(other_ent).xero_name),
        "lines": [
            {"AccountCode": side["code"], "LineAmount": round(delta, 2), "Description": LINE_DESC},
            {"AccountCode": account, "LineAmount": round(-delta, 2), "Description": LINE_DESC},
        ],
        "idempotency_key": key,
        "last_revalued_by": _last_fx_poster(pair),
    })
    return out


def _last_fx_poster(pair: dict) -> str | None:
    fx = [t for side in ("a", "b") for t in pair.get("fx_adjustments", {}).get(side, [])]
    if not fx:
        return None
    t = max(fx, key=lambda t: t["date"])
    return f"{t['entity']} on {t['date']} ({t['entity_base']} {t['base']:,.2f})"


def fmt(d: dict, reporting: str) -> str:
    head = f"{d['pair']:<40} break {reporting} {d['break']:>12,.2f}   {d['status']}"
    if d["status"] != "DRAFT":
        return head + "".join(f"\n      {x}" for x in d.get("open_items", []))
    lines = "\n".join(f"      {ln['AccountCode']:>6}  {ln['LineAmount']:>14,.2f}  {ln['Description']}" for ln in d["lines"])
    warn = "" if d["month_end"] else "\n      (as-at is not a month end: revaluations are dated month ends, re-run with --as-at)"
    prior = f"\n      last revalued by {d['last_revalued_by']}" if d["last_revalued_by"] else ""
    return (f"{head}\n      entity {d['entity']} ({d['why']}), date {d['date']}, rates {d['rate_used']}\n"
            f"      narration: {d['narration']}\n{lines}{prior}{warn}")


def post(d: dict, confirm: str, dry: bool) -> str:
    """Post one drafted journal. `confirm` must equal the entity's full name."""
    if d["status"] != "DRAFT":
        return f"not posted: {d['status']}"
    if confirm != d["entity"]:
        return f"not posted: --confirm must be the entity's full name, {d['entity']!r}"
    from accounting_agent import readonly
    if not dry:
        try:
            readonly.refuse("posting an intercompany FX revaluation journal")
        except readonly.ReadOnlyError as e:
            return f"not posted: {e}"
    from accounting_agent.xero import XeroClient
    from accounting_agent.xero.journals import (
        get_manual_journal, make_journal_line, post_manual_journal, prepare_manual_journal,
    )
    c = XeroClient(d["entity"])
    org = c.get("Organisation")["Organisations"][0]
    for k in ("PeriodLockDate", "EndOfYearLockDate"):
        lock = org.get(k)
        if lock and str(lock)[:10] >= d["date"]:
            return f"not posted: {d['entity']} {k} {lock} is on or after {d['date']}; a journal dated there is silently dropped"
    tracking = tracking_for(d["entity_key"])
    lines = [make_journal_line(ln["AccountCode"], ln["LineAmount"], ln["Description"], tracking=tracking)
             for ln in d["lines"]]
    payload = prepare_manual_journal(d["narration"], lines, date=d["date"], status="POSTED")
    if dry:
        return "dry run: payload validated, nothing sent\n" + json.dumps(payload, indent=1)
    res = post_manual_journal(c, payload, idempotency_key=d["idempotency_key"])
    if res.get("HasErrors") or not res.get("ManualJournalID"):
        return f"POST FAILED: {res.get('ValidationErrors')}"
    back = get_manual_journal(c, res["ManualJournalID"])
    return (f"posted in {d['entity']}: {back.get('Narration')} dated {str(back.get('Date'))[:10]}, "
            f"status {back.get('Status')}, {len(back.get('JournalLines', []))} lines")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", required=True, help="interco_recon.py --json output")
    ap.add_argument("--as-at", default=None, help="journal date; must equal the recon's as-at (a month end)")
    ap.add_argument("--pair", default=None, help="one entity pair, e.g. HOLDCO:OPCO_EU (keys from config)")
    ap.add_argument("--flavour", default=None, help="one flavour of that pair, as named in config/group.toml")
    ap.add_argument("--entity", default=None, help="override the inferred posting side (an entity key)")
    ap.add_argument("--post", action="store_true",
                    help="post the ONE drafted journal selected by --pair and --flavour (admin instruction only)")
    ap.add_argument("--confirm", default=None, help="the posting entity's full Xero name, required with --post")
    ap.add_argument("--dry-run", action="store_true", help="with --post: validate the payload, send nothing")
    args = ap.parse_args()

    rep = json.load(open(args.json))
    as_at = args.as_at or rep["as_at"]
    if as_at != rep["as_at"]:
        # the break is computed from the balances in the JSON, which are the
        # recon's as-at; a journal dated any other day would be sized wrong
        print(f"the reconciliation was run as at {rep['as_at']}; re-run interco_recon.py --as-at {as_at} "
              f"and point --json at that output", file=sys.stderr)
        return 2
    rates = rep["rates"]
    reporting = rep.get("reporting_currency") or config.reporting_currency()
    tol = float(config.intercompany_settings()["tolerance_abs"])
    wanted = None
    if args.pair:
        parts = args.pair.split(":")
        wanted = {config.entity(parts[0]).key, config.entity(parts[1]).key}
        if len(parts) == 3 and not args.flavour:
            args.flavour = parts[2]

    drafts = []
    for p in rep["pairs"]:
        if p.get("status") == "no_mirror_account" or "a" not in p:
            continue
        ents = {p["a"]["entity"], p["b"]["entity"]}
        flavour = p.get("flavour") or p["pair"].split(" / ")[1]
        if wanted and ents != wanted:
            continue
        if args.flavour and flavour != args.flavour:
            continue
        if p["status"] == "nil both sides":
            continue
        drafts.append(draft(p, rates, as_at, reporting, tol, args.entity))

    print(f"INTERCOMPANY FX REVALUATION DRAFTS   as at {as_at}   (from {args.json})")
    print(f"cross rates to {reporting}: " + ", ".join(f"{k}={v:.6g}" for k, v in sorted(rates.items())))
    print("=" * 110)
    for d in drafts:
        print(fmt(d, reporting))
    ready = [d for d in drafts if d["status"] == "DRAFT"]
    print("-" * 110)
    print(f"{len(ready)} draft(s) ready to post, {len(drafts) - len(ready)} pair(s) not ready or not applicable. Nothing posted.")

    if args.post:
        if not (args.pair and args.flavour):
            print("--post needs --pair and --flavour: one journal per instruction", file=sys.stderr)
            return 2
        if len(ready) != 1:
            print(f"--post expects exactly one ready draft, found {len(ready)}", file=sys.stderr)
            return 2
        print(post(ready[0], args.confirm or "", args.dry_run))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
