#!/usr/bin/env python
"""The part of a bookkeeping run's report that needs no judgement, written by
code.

    .venv/bin/python scripts/bookkeep_report.py [RUN]     RUN defaults to data/briefings/bookkeep

Run it once, after the closing bank feed refresh and the two exit checks. It
reads the run's spec (RUN/bills.json) and the poster's results file
(RUN/bills.results.jsonl) and writes RUN/report.md with:

  *bookkept*   one line per document the poster posted (re-runs included,
               each document once), in docs/COMMS.md's invoice shape:
               <supplier> · <date> · <CCY> <amount> · paid <entity> ·
               recognised <entity> · <number>. `paid` comes from the spec's
               `paid` field and is dropped where the spec has none, as
               COMMS.md says of a field you do not have.
  *bank feed*  the lines still open per entity, as the feed now holds them,
               and the open bills per entity.

and prints, for the session to place under the right heading itself, every
document the poster skipped or refused and every FLAG it raised. The session
writes the headline and every section that needs judgement (queries, manual,
blocked, not attempted, chased, from users, wrote back) into the same file
with Edit, then queues it with slack_outbox.py. It never posts anything and
never reads Xero. Should it fail, the session writes the whole report by hand
(xero-bills, the report), so the report never depends on it.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

from accounting_agent import config  # noqa: E402


def norm(s) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


def short(key) -> str:
    """An entity's short name from its key, slug or alias; the raw value when
    config does not know it."""
    try:
        return config.entity(str(key)).short
    except config.ConfigError:
        return str(key)


def rank(key) -> int:
    keys = config.entity_keys()
    try:
        k = config.entity(str(key)).key
    except config.ConfigError:
        return len(keys)
    return keys.index(k)


def day(iso: str) -> str:
    """`04 Sep`, the year only when it is not the current one."""
    try:
        d = date.fromisoformat(iso[:10])
    except ValueError:
        return iso
    return f"{d:%d %b}" if d.year == date.today().year else f"{d:%d %b %Y}"


def invoice_line(r: dict, paid: str | None) -> str:
    who = r["contact"] + (" credit note" if r.get("type") == "credit" else "")
    parts = [who, day(r["date"]), f"{r['currency']} {float(r['total']):,.2f}"]
    if paid:
        parts.append(f"paid {short(paid)}")
    parts += [f"recognised {short(r['entity'])}", r["number"]]
    return "• " + " · ".join(parts)


def latest(results: Path) -> dict[tuple, dict]:
    """Each document's last outcome across every --live of the spec."""
    out: dict[tuple, dict] = {}
    if not results.exists():
        return out
    for raw in results.read_text().splitlines():
        try:
            r = json.loads(raw)
        except ValueError:
            continue
        if r.get("status") == "settled" or "number" not in r:
            continue
        k = (r.get("entity"), r.get("type"), norm(r.get("number")))
        if out.get(k, {}).get("status") == "posted":
            continue                              # a re-run's "posted before" never undoes a post
        out[k] = r
    return out


def spec_key(b: dict) -> tuple:
    try:
        ent = config.entity(str(b.get("entity", ""))).key
    except config.ConfigError:
        ent = str(b.get("entity", ""))
    return ent, str(b.get("type", "bill")).lower(), norm(b.get("number"))


def build(run: Path) -> tuple[str, list[str]]:
    """-> (report.md's text, the lines for the session to place)."""
    spec_path = run / "bills.json"
    spec = json.loads(spec_path.read_text()) if spec_path.exists() else {}
    bills = spec.get("bills", []) if isinstance(spec, dict) else spec
    paid = {spec_key(b): b.get("paid") for b in bills if isinstance(b, dict)}
    rows = latest(run / "bills.results.jsonl")
    posted = [r for r in rows.values() if r.get("status") == "posted"]
    posted.sort(key=lambda r: (rank(r["entity"]), r["date"], r["contact"]))
    out: list[str] = []
    if posted:
        out += ["*bookkept*"] + [invoice_line(r, r.get("paid") or paid.get((r["entity"], r["type"], norm(r["number"]))))
                                 for r in posted]

    import daily_chain as dc
    slugs = {e.slug: e.short for e in config.entities()}
    feed: dict[str, int] = {}
    for l in dc.feed_lines():
        feed[l["entity"]] = feed.get(l["entity"], 0) + 1
    open_bills = dc.bill_feed()
    out += ["", "*bank feed*",
            "• lines still open after the closing refresh: "
            + ", ".join(f"{s} {feed.get(k, 0)}" for k, s in slugs.items()),
            "• open bills: " + ", ".join(f"{s} {len(open_bills.get(k, []))}" for k, s in slugs.items())]

    place: list[str] = []
    for r in rows.values():
        what = (f"{r['contact']} · {day(r['date'])} · {r['currency']} {float(r['total']):,.2f} · "
                f"recognised {short(r['entity'])} · {r['number']}")
        if r.get("status") == "skipped":
            hits = "; ".join(f"{short(h.get('entity'))} {h.get('why')} {h.get('number') or ''}".strip()
                             for h in r.get("hits") or [])
            place.append(f"not posted, the duplicate guard found {hits} · {what}")
        elif r.get("status") in ("error", "unknown"):
            place.append(f"not posted, {'; '.join(r.get('problems') or [])} · {what}")
        for f in r.get("flags") or []:
            place.append(f"posted with a flag, {f} · {what}")
        if r.get("status") == "posted" and r.get("feed") and "NOT removed" in r["feed"]:
            place.append(f"posted, but its bank feed line was not removed: {r['feed']} · {what}")
    return "\n".join(out).strip() + "\n", place


def main(argv=None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    run = Path(args[0]) if args else ROOT / "data" / "briefings" / "bookkeep"
    run = run if run.is_absolute() else ROOT / run
    text, place = build(run)
    (run / "report.md").write_text(text)
    print(f"wrote {run / 'report.md'}: the bookkept and bank feed sections. Add the headline above them and every "
          "other section the run has, then queue the file.")
    if place:
        print("\nfor you to place under the right heading:")
        print("\n".join(f"• {p}" for p in place))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
