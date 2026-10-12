#!/usr/bin/env python
"""Weekly Claude usage report: what every session on the server cost, by job.

Plain Python, no Claude session, so the report itself costs nothing. Cron runs
it on Mondays (deploy/crontab.example) and posts it to the channel in the shape
every scheduled run uses (docs/COMMS.md): a top-level message that is only
`<COMPANY> CLAUDE USAGE (<date range>)`, the detail as the first reply in its
thread, queued through scripts/slack_outbox.py like every other post.

    .venv/bin/python scripts/usage_report.py              # print, last 7 days
    .venv/bin/python scripts/usage_report.py --days 14
    .venv/bin/python scripts/usage_report.py --post       # print and post
    .venv/bin/python scripts/usage_report.py --root <dir> --end 2026-10-08   # fixtures

The period is the N whole UTC days before --end (default today), compared with
the N days before that. Every dollar figure is the CLI's own total_cost_usd:
the usage priced at API list rates. When the server runs on a subscription
token (CLAUDE_CODE_OAUTH_TOKEN) nothing is billed per token, so the figures
are notional: they show where the usage goes, not an invoice.

Where the figures come from, each Claude session counted once (deduped by
session id and cost):
  data/runs/<run_id>.json            one entry per run or thread turn (job,
                                     start, run_usd, session ids), written by
                                     scripts/run_cost.py; a Slack thread turn
                                     is its share of a resumed session's
                                     cumulative cost
  data/runs/<run_id>.session.json    the CLI's JSON for a run: turns and
                                     per-model tokens
  data/runs/<run_id>/extra/*.json    other Claude work a run paid for, one
                                     row per label (see scripts/run_cost.py)
  data/query/*/session.json          read-only query sessions
  data/consensus/*/session-*.json    the three consensus passes
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UTC = dt.timezone.utc
FLAG_RISE = 0.50             # a job up more than this on the previous period is flagged
FLAG_MIN_USD = 1.00          # ... unless the rise is smaller than this in dollars
TOKEN_KEYS = ("inputTokens", "cacheCreationInputTokens", "cacheReadInputTokens", "outputTokens")


# ---------------------------------------------------------------- reading

def _read(p: Path):
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None


def _result(d):
    """The CLI's result object: a dict, or the last result in a stream list."""
    if isinstance(d, list):
        for item in reversed(d):
            if isinstance(item, dict) and item.get("type") == "result":
                return item
        return None
    return d if isinstance(d, dict) else None


def _tokens(d: dict) -> dict:
    """Per-model tokens and cost. modelUsage covers the subagents too; usage is
    the main model only, so it is the fallback."""
    out = {}
    for model, m in (d.get("modelUsage") or {}).items():
        if isinstance(m, dict):
            out[model] = {k: float(m.get(k) or 0) for k in TOKEN_KEYS}
            out[model]["costUSD"] = float(m.get("costUSD") or 0)
    if not out and isinstance(d.get("usage"), dict):
        u = d["usage"]
        out["unknown"] = {"inputTokens": float(u.get("input_tokens") or 0),
                          "cacheCreationInputTokens": float(u.get("cache_creation_input_tokens") or 0),
                          "cacheReadInputTokens": float(u.get("cache_read_input_tokens") or 0),
                          "outputTokens": float(u.get("output_tokens") or 0),
                          "costUSD": float(d.get("total_cost_usd") or 0)}
    return out


def _job(name: str) -> str:
    if name.startswith("run-"):          # run-<slack user>-<thread key>: one Slack thread turn
        return "slack threads"
    return name or "other"


def collect(root: Path) -> list[dict]:
    """One record per session (one per turn for Slack threads)."""
    data = root / "data"
    runs = data / "runs"
    records: list[dict] = []
    seen: set = set()

    def add(rec: dict) -> None:
        key = (rec.get("sid"), round(rec["cost"], 6)) if rec.get("sid") else None
        if key and key in seen:
            return
        if key:
            seen.add(key)
        records.append(rec)

    for p in sorted(runs.glob("*.json")) if runs.exists() else []:
        if p.name.endswith(".session.json"):
            continue
        e = _read(p)
        if not isinstance(e, dict) or "job" not in e:
            continue
        job = _job(e["job"])
        when = float(e.get("started") or e.get("ended") or p.stat().st_mtime)
        sids = e.get("session_ids") or []
        sess = _result(_read(runs / f"{p.stem}.session.json"))
        run_usd = e.get("run_usd")
        if isinstance(run_usd, (int, float)):
            add({"job": job, "when": when, "cost": float(run_usd), "sid": sids[0] if sids else None,
                 "turns": (sess or {}).get("num_turns"), "models": _tokens(sess or {}), "ref": p.stem})
        elif sess and isinstance(sess.get("total_cost_usd"), (int, float)):
            # A run that died before `stamp`: its session file still has the cost.
            add({"job": job, "when": when, "cost": float(sess["total_cost_usd"]), "sid": sess.get("session_id"),
                 "turns": sess.get("num_turns"), "models": _tokens(sess), "ref": p.stem})
        for label, c in (e.get("extra_usd") or {}).items():
            if isinstance(c, (int, float)):
                add({"job": str(label), "when": when, "cost": float(c), "sid": None,
                     "turns": None, "models": {}, "ref": p.stem})

    # The briefing's document reads that no run claimed (scripts/run_cost.py
    # counts the claimed ones as the run's "document reads" extra).
    for p in sorted(data.glob("briefings_cost/*/*.json")) if data.exists() else []:
        d = _read(p)
        if isinstance(d, dict) and not d.get("run_id") and isinstance(d.get("total_cost_usd"), (int, float)):
            add({"job": "document reads", "when": float(d.get("at") or p.stat().st_mtime),
                 "cost": float(d["total_cost_usd"]), "sid": d.get("session_id"), "turns": None, "models": {},
                 "ref": str(p.relative_to(data))})

    for pattern, job in (("query/*/session.json", "query"), ("consensus/*/session-*.json", "consensus")):
        for p in sorted(data.glob(pattern)) if data.exists() else []:
            d = _result(_read(p))
            if d and isinstance(d.get("total_cost_usd"), (int, float)):
                add({"job": job, "when": p.stat().st_mtime, "cost": float(d["total_cost_usd"]),
                     "sid": d.get("session_id"), "turns": d.get("num_turns"), "models": _tokens(d),
                     "ref": str(p.parent.relative_to(data))})
    return records


# ---------------------------------------------------------------- figures

def summarise(records: list[dict], lo: float, hi: float) -> dict:
    jobs = defaultdict(lambda: {"cost": 0.0, "sessions": 0, "turns": 0, "turned": 0,
                                "cw": 0.0, "cr": 0.0, "out": 0.0})
    models = defaultdict(lambda: {"cost": 0.0, "out": 0.0})
    rows = [r for r in records if lo <= r["when"] < hi]
    for r in rows:
        j = jobs[r["job"]]
        j["cost"] += r["cost"]
        j["sessions"] += 1
        if isinstance(r.get("turns"), (int, float)):
            j["turns"] += r["turns"]
            j["turned"] += 1
        for m, t in r["models"].items():
            j["cw"] += t.get("cacheCreationInputTokens", 0)
            j["cr"] += t.get("cacheReadInputTokens", 0)
            j["out"] += t.get("outputTokens", 0)
            name = re.sub(r"-\d{8}$", "", m)
            models[name]["cost"] += t.get("costUSD", 0)
            models[name]["out"] += t.get("outputTokens", 0)
    return {"jobs": jobs, "models": models, "rows": rows,
            "cost": sum(j["cost"] for j in jobs.values())}


def usd(x: float) -> str:
    return f"${x:,.2f}"


def tok(n: float) -> str:
    if n >= 1e9:
        return f"{n / 1e9:.1f}B"
    if n >= 1e6:
        return f"{n / 1e6:.1f}M"
    if n >= 1e3:
        return f"{n / 1e3:.0f}k"
    return f"{n:.0f}"


def day(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, UTC).strftime("%d %b")


def span(lo: dt.date, hi: dt.date) -> str:
    """lo inclusive, hi exclusive, as `30 Sep - 06 Oct 2026`."""
    last = hi - dt.timedelta(days=1)
    first = f"{lo:%d %b}" if lo.year == last.year else f"{lo:%d %b %Y}"
    return f"{first} - {last:%d %b %Y}"


def change(now: float, before: float) -> tuple[str, bool]:
    if before <= 0.005:            # no base to compare with: shown, never flagged
        return ("new", False) if now > 0.005 else ("", False)
    pct = (now - before) / before
    flag = pct > FLAG_RISE and now - before >= FLAG_MIN_USD
    if abs(pct) < 0.005:
        return "flat", False
    return f"{'up' if pct > 0 else 'down'} {abs(pct):.0%} on {usd(before)}", flag


def report(records: list[dict], end: dt.date, days: int, company: str = "") -> tuple[str, str]:
    def at(d: dt.date) -> float:
        return dt.datetime.combine(d, dt.time(), UTC).timestamp()

    lo, prev_lo = end - dt.timedelta(days=days), end - dt.timedelta(days=2 * days)
    cur = summarise(records, at(lo), at(end))
    prev = summarise(records, at(prev_lo), at(lo))
    rng, prev_rng = span(lo, end), span(prev_lo, lo)
    headline = f"{company.upper() + ' ' if company else ''}CLAUDE USAGE ({rng})"

    total_change, _ = change(cur["cost"], prev["cost"])
    sessions = sum(j["sessions"] for j in cur["jobs"].values())
    out = [f"usage · {rng} · notional {usd(cur['cost'])} at API list rates, nothing is billed per "
           f"token on a subscription · {sessions} sessions"
           + (f" · {total_change} for {prev_rng}" if prev["cost"] > 0.005 else "")]

    out += ["", "*by job*"]
    flagged = []
    for name in sorted(set(cur["jobs"]) | set(prev["jobs"]),
                       key=lambda n: -(cur["jobs"].get(n) or {"cost": 0})["cost"]):
        j, p = cur["jobs"].get(name), prev["jobs"].get(name)
        now, before = (j["cost"] if j else 0.0), (p["cost"] if p else 0.0)
        ch, flag = change(now, before)
        if not j:
            out.append(f"• {name}: nothing this period · {usd(before)} for {prev_rng}")
            continue
        avg = f" · avg {j['turns'] / j['turned']:.0f} turns" if j["turned"] else ""
        unit = "thread turn" if name == "slack threads" else "session"
        toks = (f" · cache writes {tok(j['cw'])}, cache reads {tok(j['cr'])}, output {tok(j['out'])}"
                if j["cw"] or j["cr"] or j["out"] else "")
        out.append(f"• {name}: {usd(now)} · {j['sessions']} {unit}{'s' if j['sessions'] != 1 else ''}"
                   f"{avg}{toks}" + (f" · {ch}" if ch else ""))
        if flag:
            flagged.append(f"• {name}: {usd(now)}, {ch}")

    if flagged:
        out += ["", f"*up more than {FLAG_RISE:.0%} on {prev_rng}*"] + flagged

    top = sorted(cur["rows"], key=lambda r: r["cost"], reverse=True)[:10]
    if top:
        out += ["", "*most expensive sessions*"]
        for r in top:
            turns = f" · {r['turns']} turns" if isinstance(r.get("turns"), (int, float)) else ""
            out.append(f"• {r['job']} · {day(r['when'])}{turns} · {usd(r['cost'])} · {r['ref']}")

    if cur["models"]:
        out += ["", "*by model*"]
        for m, t in sorted(cur["models"].items(), key=lambda kv: kv[1]["cost"], reverse=True):
            if t["cost"] >= 0.005:
                out.append(f"• {m}: {usd(t['cost'])} · output {tok(t['out'])}")

    if not records:
        out = [f"usage · {rng} · no session files found under data/"]
    return headline, "\n".join(out)


# ---------------------------------------------------------------- Slack

def post(headline: str, detail: str) -> None:
    """The headline at top level, the detail in its thread, through the
    outbox: queued inside a run, posted at once from cron."""
    sys.path.insert(0, str(ROOT / "scripts"))
    import slack_outbox                                          # noqa: E402
    print(slack_outbox.queue(headline=headline, thread=detail))


def main(argv=None) -> int:
    sys.path.insert(0, str(ROOT / "src"))
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--days", type=int, default=7, help="length of each period (default 7)")
    ap.add_argument("--end", help="first day NOT in the period, YYYY-MM-DD (default today UTC)")
    ap.add_argument("--root", default=str(ROOT), help="repo root holding data/ (default this repo)")
    ap.add_argument("--post", action="store_true", help="post to the channel in config/group.toml")
    a = ap.parse_args(argv)
    end = dt.date.fromisoformat(a.end) if a.end else dt.datetime.now(UTC).date()
    try:
        from accounting_agent import config
        company = config.company_name()
    except Exception:                                            # noqa: BLE001
        company = ""
    headline, detail = report(collect(Path(a.root)), end, a.days, company)
    print(headline)
    print()
    print(detail)
    if a.post:
        post(headline, detail)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
