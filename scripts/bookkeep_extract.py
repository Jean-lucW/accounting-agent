#!/usr/bin/env python
"""Every document of a bookkeeping run read and looked up before the session
opens, so the session only judges.

Called by scripts/bookkeep_brief.py for each document it downloaded, when
[briefing] read_documents is on:

  1. the read: the `invoice-extract` agent, headless, on [briefing]
     reader_model ([models] small by default), one call per document, several
     at once, read-only (AGENT_READONLY=1, so the transport lock and the guard
     hook are both up). A CSV or a spreadsheet is data, read by the session
     with Python, and is not sent.
  2. the second look, decided by code from the rules in xero-bills section 2:
     a photo the read calls rotated or angled, a field marked illegible, a
     receipt dated in an earlier month than the message that carried it, a
     net and tax that do not make the gross, more than one document in one
     answer, an unpaid invoice with neither a due date nor terms. The same
     agent on [briefing] second_look_model ([models] query by default),
     briefed `second look: <fields>, <reason>`; both reads go to the session,
     which applies the skill's rule between them.
  3. Xero, read-only, every entity in scope: any live bill with the same
     number, and the records matching.candidates ranks at strength 3 or more
     for the supplier and gross (the bank-side amount of payments, other
     currencies, the name). A hit is a lead for the session, never a decision.
  4. the supplier: its rules/SUPPLIERS.md entry, and its bills of the last
     [briefing] history_days in each entity with the account (name and code)
     and tax type each was coded to.

Every call's cost is written to data/briefings_cost/, which run_cost.py
counts in the run's bracket and usage_report.py in the week's figures.
Nothing here writes to Xero, Slack or the mail.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from accounting_agent import config  # noqa: E402

AGENT = "invoice-extract"
CLAUDE_BIN = os.environ.get("BRIEF_CLAUDE_BIN", "claude")
COST_DIR = ROOT / "data" / "briefings_cost"
COST_KEEP_DAYS = 35
SUPPLIERS = ROOT / "rules" / "SUPPLIERS.md"
DATA_FILES = re.compile(r"\.(csv|xlsx|xls|xlsm|tsv|json)$", re.I)
DOC_TYPES = {"invoice", "receipt", "credit note", "statement", "summary sheet"}


# ---------------------------------------------------------------- the read

def parse_answer(text: str):
    """The agent's JSON object or array, fenced or not. None when absent."""
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    for start in (i for i, ch in enumerate(t) if ch in "{["):
        try:
            obj, _ = json.JSONDecoder().raw_decode(t[start:])
            if isinstance(obj, (dict, list)):
                return obj
        except ValueError:
            continue
    return None


def record_cost(file: str, model: str, cost, sid: str | None) -> None:
    try:
        day = COST_DIR / date.today().isoformat()
        day.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        (day / f"{stamp}.json").write_text(json.dumps({
            "job": "bookkeep-brief", "total_cost_usd": float(cost or 0), "session_id": sid, "model": model,
            "file": file, "run_id": os.environ.get("AGENT_RUN_ID", ""),
            "at": datetime.now(timezone.utc).timestamp()}))
    except OSError:
        pass


def prune_costs() -> None:
    cut = (date.today() - timedelta(days=COST_KEEP_DAYS)).isoformat()
    for d in COST_DIR.glob("*") if COST_DIR.exists() else []:
        if d.is_dir() and d.name < cut:
            for f in d.glob("*.json"):
                f.unlink(missing_ok=True)
            d.rmdir()


def call(path: Path, brief: str, model: str, budget: float, scratch: Path, timeout: int) -> tuple[object, str]:
    """-> (the parsed answer or None, the error). Read-only by flag and hook."""
    env = dict(os.environ)
    env["PATH"] = f"{Path.home() / '.local' / 'bin'}:{env.get('PATH', '')}"
    env.update({"AGENT_READONLY": "1", "QUERY_DIR": str(scratch), "QUERY_JOB": "bookkeep-extract"})
    prompt = f"{brief}\n{path}" if brief else str(path)
    cmd = [CLAUDE_BIN, "-p", prompt, "--agent", AGENT, "--model", model, "--permission-mode", "bypassPermissions",
           "--output-format", "json", "--max-budget-usd", f"{budget:.2f}", "--no-session-persistence",
           "--disallowedTools", "Write", "Edit", "NotebookEdit"]
    try:
        p = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, f"no answer in {timeout} seconds"
    except OSError as e:
        return None, f"{type(e).__name__}: {e}"
    try:
        out = json.loads(p.stdout or "{}")
    except ValueError:
        return None, f"not JSON from the CLI (exit {p.returncode}): {(p.stdout or p.stderr)[:160]}"
    record_cost(path.name, model, out.get("total_cost_usd"), out.get("session_id"))
    if out.get("is_error"):
        return None, f"the read failed: {str(out.get('result'))[:160]}"
    ans = parse_answer(out.get("result") or "")
    return (ans, "") if ans is not None else (None, "the read returned no JSON")


def docs_of(ans) -> list[dict]:
    if isinstance(ans, list):
        return [a for a in ans if isinstance(a, dict)]
    return [ans] if isinstance(ans, dict) else []


def _num(x):
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) else None


def second_look(x: dict, msg_ts: float | None, several: bool) -> tuple[list[str], list[str]]:
    """-> (fields, reasons) for a second look, by the rules of xero-bills
    section 2. Nothing when the first read holds together."""
    fields, why = [], []

    def add(fs: list[str], reason: str) -> None:
        for f in fs:
            if f not in fields:
                fields.append(f)
        why.append(reason)

    notes = str(x.get("notes") or "").lower()
    kind = str(x.get("document_type") or "").lower()
    if kind == "unreadable":
        add(["supplier", "total", "date", "currency"], "the first read could not read the file")
    # "upright, not rotated" and "no rotation" are the reader saying all is well.
    said = re.sub(r"\b(not|no|nor|never|without|isn't|is not|wasn't)\b[^.;,]{0,25}"
                  r"(rotat\w*|angle\w*|skew\w*|upside[- ]down|sideways|tilt\w*)", " ", notes)
    if re.search(r"\brotated\b|\bat an angle\b|\bangled\b|upside[- ]down|\bsideways\b|\bskewed\b|\btilted\b", said):
        add(["supplier", "total", "date"], "the photo is rotated or at an angle")
    leg = x.get("legibility") or {}
    ill = [{"amount": "total"}.get(k, k) for k in ("supplier", "amount", "currency", "date") if leg.get(k) == "illegible"]
    if ill:
        add(ill, "the first read marked them illegible")
    if kind == "receipt" and msg_ts and x.get("invoice_date"):
        try:
            d = date.fromisoformat(str(x["invoice_date"])[:10])
            m = datetime.fromtimestamp(msg_ts, timezone.utc).date()
            if (d.year, d.month) < (m.year, m.month):
                add(["date"], "the first read put it in an earlier month than the message")
        except ValueError:
            pass
    net, tax, gross = _num(x.get("net")), _num(x.get("tax")), _num(x.get("gross"))
    if None not in (net, tax, gross) and abs(net + tax - gross) > 0.02:
        add(["net", "tax", "total"], "the net and the tax do not make the gross")
    if not several and re.search(r"more than one (invoice|document|receipt)|several (invoices|documents)|two invoices", notes):
        add(["supplier", "total", "date"], "the file may hold more than one document: one object per document")
    if (kind == "invoice" and not x.get("payment_evidence") and x.get("due_date") in (None, "")
            and x.get("payment_terms") in (None, "")):
        add(["due date", "payment terms"], "an unpaid invoice with neither: read the foot of the page and the payment block")
    return fields, why


# ---------------------------------------------------------------- the supplier

def norm_number(s) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


# Words a supplier's name shares with unrelated suppliers. Matching on them
# puts one supplier's entry under another's invoice ("Example Financial
# Services" against "Sample Financial Insurance"), so a match needs a word outside
# this list. matching.name_tokens already drops the group's own names.
GENERIC = {"financial", "finance", "bank", "banking", "capital", "international", "technologies", "technology",
           "tech", "solutions", "software", "systems", "system", "partners", "management", "consulting",
           "company", "corporation", "corp", "holding", "trading", "markets", "market", "data", "digital",
           "online", "store", "shop", "europe", "european", "america", "americas", "north", "south", "east",
           "west", "network", "networks", "media", "business", "receipts", "receipt", "billing", "support",
           "team", "office", "services", "service", "group", "global", "cloud", "lending", "payments"}


def distinctive(*names: str | None) -> set[str]:
    from accounting_agent.xero.matching import name_tokens
    return {t for t in name_tokens(*names) if len(t) >= 3 and t not in GENERIC}


def supplier_entries(text: str) -> list[dict]:
    """Every live entry of rules/SUPPLIERS.md: a level-3 heading under
    `## Entries` and its field list. Fenced blocks and the quoted
    illustrations are not entries."""
    out, cur, fenced, in_entries = [], None, False, False
    for raw in text.splitlines():
        if raw.startswith("```"):
            fenced = not fenced
            continue
        if fenced or raw.startswith(">"):
            continue
        if raw.startswith("## "):
            in_entries = raw[3:].strip().lower() == "entries"
            cur = None
            continue
        if not in_entries:
            continue
        if raw.startswith("### "):
            cur = {"name": raw[4:].strip(), "fields": {}}
            out.append(cur)
            continue
        m = re.match(r"^- \*\*(.+?):\*\*\s*(.*)$", raw)
        if cur is not None and m:
            cur["fields"][m.group(1).strip().lower()] = m.group(2).strip()
            cur["last"] = m.group(1).strip().lower()
        elif cur is not None and raw.startswith("  ") and cur.get("last"):
            cur["fields"][cur["last"]] += " " + raw.strip()
    return out


ENTRY_FIELDS = (("recognised in", "recognised"), ("default account", "account"), ("tax treatment", "tax"),
                ("paid from", "paid from"), ("prepays", "prepays"), ("special handling", "special"))


def supplier_rows(name: str, text: str | None = None) -> list[str]:
    """The SUPPLIERS.md entry whose name and aliases share the most
    distinctive words with the supplier, one line each, and a second only
    when it shares as many."""
    if text is None:
        text = SUPPLIERS.read_text(errors="replace") if SUPPLIERS.exists() else ""
    want = distinctive(name)
    if not want:
        return []
    scored = []
    for e in supplier_entries(text):
        n = len(want & distinctive(e["name"], e["fields"].get("aliases / bank payee names", "")))
        if n:
            bits = [f"{label} {e['fields'][k]}" for k, label in ENTRY_FIELDS
                    if e["fields"].get(k) and e["fields"][k] != "-"]
            scored.append((n, " · ".join([e["name"]] + bits)))
    if not scored:
        return []
    best = max(n for n, _ in scored)
    return [l for n, l in scored if n == best][:2]


# ---------------------------------------------------------------- Xero

def xero_lookups(items: list[dict], history_days: int, say=print) -> None:
    """Fill each item's `in_xero` and `history` in place. One window per
    entity for the whole batch; one contact search and one bill list per
    supplier and entity."""
    import post_bills as pb
    from accounting_agent.xero.contacts import find_contacts
    from accounting_agent.xero.matching import candidates, describe, load_window, name_tokens

    if not items:
        return
    dated = []
    for it in items:
        try:
            dated.append(date.fromisoformat(str(it["x"].get("invoice_date"))[:10]))
        except (ValueError, TypeError):
            dated.append(datetime.fromtimestamp(it["msg_ts"], timezone.utc).date())
    lo, hi = min(dated), max(dated)
    clients, windows, charts = {}, {}, {}
    for e in config.entities():
        if e.bank_check_mode == "off":
            continue
        try:
            clients[e.key] = pb.connect(e.key)
            windows[e.key] = load_window(clients[e.key], lo, hi)
            charts[e.key] = {a.get("Code"): a.get("Name") for a in clients[e.key].get("Accounts").get("Accounts", [])
                             if a.get("Code")}
        except Exception as ex:                                  # noqa: BLE001
            say(f"{e.short}: Xero would not load ({type(ex).__name__}: {str(ex)[:120]})")
    for it, d in zip(items, dated):
        x, found = it["x"], []
        num = norm_number(x.get("invoice_number"))
        gross, ccy = _num(x.get("gross")), (x.get("currency") or "").upper()
        names = name_tokens(x.get("supplier"), x.get("supplier_entity"))
        for k, win in windows.items():
            tag = config.entity(k).short
            for rec in win.get("docs", []):
                if (num and len(num) >= 4 and rec.get("Status") not in ("VOIDED", "DELETED")
                        and norm_number(rec.get("InvoiceNumber")) == num):
                    found.append((5, f"{tag} {describe(pb._shape('bill', rec, ['number']))}"))
            if gross and ccy:
                for c in candidates(win, amount=gross, currency=ccy, day=d, names=names, spent=True, own=True):
                    if c["strength"] >= 3:
                        found.append((c["strength"], f"{tag} {describe(c)}"))
        seen, lines = set(), []
        for _, line in sorted(found, key=lambda f: -f[0]):
            if line not in seen:
                seen.add(line)
                lines.append(line)
        it["in_xero"] = lines[:4]
        it["xero_read"] = bool(windows)

    since = date.today() - timedelta(days=history_days)
    cache: dict[str, list[str]] = {}
    for it in items:
        sup = (it["x"].get("supplier") or "").strip()
        toks = distinctive(sup)
        key = " ".join(sorted(toks))
        if not sup or not key:
            it["history"] = []
            continue
        if key not in cache:
            hist = []
            for k, c in clients.items():
                tag = config.entity(k).short
                try:
                    cons = [ct for ct in find_contacts(c, sup) if toks & distinctive(ct.get("Name"))][:2]
                    if not cons:
                        longest = max(toks, key=len)
                        cons = ([ct for ct in find_contacts(c, longest) if longest in distinctive(ct.get("Name"))][:2]
                                if len(longest) >= 4 else [])
                    bills = []
                    for ct in cons:
                        w = (f'Type=="ACCPAY" AND Contact.ContactID==Guid("{ct["ContactID"]}") AND '
                             f"Date>=DateTime({since.year},{since.month},{since.day})")
                        bills += [b for b in c.get("Invoices", where=w, page=1, order="Date DESC").get("Invoices", [])
                                  if b.get("Status") not in ("VOIDED", "DELETED")]
                except Exception as ex:                          # noqa: BLE001
                    hist.append(f"{tag}: not read ({type(ex).__name__})")
                    continue
                if not bills:
                    continue
                coded: dict[str, int] = {}
                taxes: set[str] = set()
                for b in bills:
                    for li in b.get("LineItems") or []:
                        code = li.get("AccountCode") or ""
                        label = f"{charts.get(k, {}).get(code) or '?'} ({code})"
                        coded[label] = coded.get(label, 0) + 1
                        if li.get("TaxType"):
                            taxes.add(li["TaxType"])
                last = bills[0]
                hist.append(f"{tag} · {len(bills)} bills since {since:%-d %b} under "
                            f"{', '.join(sorted({b['Contact']['Name'] for b in bills}))}"
                            " · coded " + ", ".join(f"{n} x{v}" for n, v in
                                                    sorted(coded.items(), key=lambda kv: -kv[1])[:3])
                            + (f" · tax {', '.join(sorted(taxes))}" if taxes else "")
                            + f" · last {pb.iso_of(last.get('DateString') or last.get('Date'))} "
                              f"{last.get('CurrencyCode')} {float(last.get('Total') or 0):,.2f} "
                              f"{last.get('InvoiceNumber') or ''}".rstrip())
            cache[key] = hist
        it["history"] = cache[key]


# ---------------------------------------------------------------- the whole pass

def read_all(jobs: list[dict], scratch: Path, say=print) -> None:
    """jobs: [{"path": Path, "msg_ts": float}]. Fills each job's `docs` (the
    reads), `second` (the second look's reads and why) and `error` in place,
    then the Xero and supplier lookups per document."""
    s = config.briefing()
    prune_costs()
    todo = [j for j in jobs if not DATA_FILES.search(j["path"].name)]
    for j in jobs:
        j.setdefault("docs", [])
        j.setdefault("error", "")
        if DATA_FILES.search(j["path"].name):
            j["error"] = "data, not a picture of a document: read it with Python"

    def first(j):
        ans, err = call(j["path"].resolve(), "", s["reader_model"], s["reader_max_usd"], scratch,
                        s["timeout_seconds"])
        j["docs"], j["error"] = docs_of(ans), err
        j["several"] = isinstance(ans, list)

    with ThreadPoolExecutor(max_workers=s["workers"]) as ex:
        list(ex.map(first, todo))

    again = []
    for j in todo:
        fields, why = [], []
        for x in j["docs"]:
            f, w = second_look(x, j["msg_ts"], j.get("several", False))
            fields += [a for a in f if a not in fields]
            why += [b for b in w if b not in why]
        if j["error"] and not j["docs"]:
            fields, why = ["supplier", "total", "date", "currency"], ["the first read failed"]
        if fields:
            j["second_why"] = f"second look: {', '.join(fields)}, {'; '.join(why)}"
            again.append(j)

    def second(j):
        ans, err = call(j["path"].resolve(), j["second_why"], s["second_look_model"], s["second_look_max_usd"],
                        scratch, s["timeout_seconds"])
        j["second"], j["second_error"] = docs_of(ans), err

    with ThreadPoolExecutor(max_workers=s["workers"]) as ex:
        list(ex.map(second, again))

    items = []
    for j in todo:
        for x in (j.get("second") or j["docs"]):
            items.append({"job": j, "x": x, "msg_ts": j["msg_ts"]})
    try:
        xero_lookups([i for i in items if i["x"].get("document_type") in DOC_TYPES], s["history_days"], say=say)
    except Exception as e:                                       # noqa: BLE001 - the session looks up instead
        say(f"the Xero lookups failed ({type(e).__name__}: {str(e)[:160]})")
    text = SUPPLIERS.read_text(errors="replace") if SUPPLIERS.exists() else ""
    for i in items:
        i["suppliers_md"] = supplier_rows(i["x"].get("supplier") or "", text)
        i["job"].setdefault("items", []).append(i)
