#!/usr/bin/env python
"""The bookkeeping run's briefing: every mechanical step before the first
judgement, done in plain Python, so the session opens on one short file
instead of spending its first forty turns on refreshes, sweeps and downloads.

    .venv/bin/python scripts/daily_chain.py bookkeep-brief    prints the briefing,
                                                              building it if none is fresh
    .venv/bin/python scripts/daily_chain.py bookkeep-brief --rebuild

The scheduled gate (daily_chain.py bookkeep) builds it itself the moment it
decides a session must run. A session started any other way, from a Slack
thread, runs the command above once.

Each step stands on its own: one that fails is named under "not done here" and
the rest still run.

  1. the bank feed refresh (left out when the gate has just done it)
  2. the bill feed refresh
  3. the ledger prune, seven days
  4. the completeness sweep: every human message to the Slack app (thread
     replies included) and every email in the accounting inbox of the last
     [briefing] window_days, matched to its ledger line by time
  5. the documents of every message still to work, downloaded into docs/, and
     each such email's body saved beside them as text for invoice-extract
  6. with [briefing] read_documents on, every document read by invoice-extract
     and looked up in Xero and rules/SUPPLIERS.md (scripts/bookkeep_extract.py)
  7. bankfeed.py status, the feed lines new since the last session, and the
     xero-check of every line, saved whole beside the briefing
  8. the open bills per entity, the investigations due, and the register's
     bookkeeping lines saved as register.txt for a grep, the ones an admin
     has answered printed

Keeps nothing it does not need: one folder,
data/briefings/bookkeep/, emptied at the start of every build, deleted when the
session that used it finishes (daily_chain.py bookkeep-commit), and by the next
gate once it is a day old. Nothing it gathers is copied into a log. Every read
is GET; the only writes are the two feeds' own refreshes and the prune.
"""

from __future__ import annotations

import base64
import html
import json
import re
import shutil
import subprocess
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import daily_chain as dc

config = dc.config
ROOT = dc.ROOT
DIR = ROOT / "data" / "briefings" / "bookkeep"
DOCS = DIR / "docs"
BRIEF = DIR / "brief.md"
STALE_HOURS = 24
NOT_DOCS = re.compile(r"\.(p7s|p7m|vcf|ics|asc|sig)$", re.I)
SMALL_INLINE = 20 * 1024      # an inline image this small is a logo or a signature


# ---------------------------------------------------------------- plumbing

def quiet(cmd: list[str], timeout: int = 1800) -> tuple[int, str]:
    """Run a command and keep its output out of the logs: the briefing is the
    only copy of what it prints."""
    try:
        p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout} seconds"


def safe(s: str, n: int = 60) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s or "").strip("_")[:n] or "file"


def utc_when(ts: float) -> str:
    """The ledger's own `msg datetime (UTC)`."""
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M")


def one_line(text: str, n: int) -> str:
    t = " ".join((text or "").split())
    return t if len(t) <= n else t[:n - 1].rstrip() + "…"


def kb(size: int) -> str:
    return f"{size / 1024 / 1024:.1f} MB" if size >= 1024 * 1024 else f"{max(1, size // 1024)} KB"


def html_text(raw: str) -> str:
    t = re.sub(r"(?is)<(style|script|head).*?</\1>", "", raw)
    t = re.sub(r"(?i)<br\s*/?>|</(p|div|tr|li|h\d|table)>", "\n", t)
    t = html.unescape(re.sub(r"<[^>]+>", " ", t))
    lines = [re.sub(r"[ \t\xa0]+", " ", l).strip() for l in t.splitlines()]
    return "\n".join(l for l in lines if l)


def unique(path: Path) -> Path:
    n, p = 2, path
    while p.exists():
        p = path.with_name(f"{path.stem}-{n}{path.suffix}")
        n += 1
    return p


# ---------------------------------------------------------------- the ledger

ROW = re.compile(r"^(\d{4}-\d{2}-\d{2}) ([^|]*)\|\s*(slack|email)\s*\|([^|]*)\|\s*(\d+)\s*/\s*(\d+)\s*\|(.*)$", re.I)


def ledger_rows(text: str) -> list[dict]:
    """Every slack or email line with its UTC times, documents seen and
    bookkept, and outcome. A time is read from the date column, or from the
    sender column where an older line put it there."""
    out = []
    for line in text.splitlines():
        m = ROW.match(line)
        if not m:
            continue
        day = date.fromisoformat(m.group(1))
        found = dc.HHMM.findall(m.group(2)) or dc.HHMM.findall(m.group(4))
        times = [(int(h), int(mi)) for h, mi in found if int(h) < 24 and int(mi) < 60]
        stamps = []
        for n, (h, mi) in enumerate(times):
            base = datetime(day.year, day.month, day.day, h, mi, tzinfo=timezone.utc)
            stamps.append(base.timestamp())
            if n and (h, mi) < times[0]:
                stamps += [(base + timedelta(days=1)).timestamp(), (base - timedelta(days=1)).timestamp()]
        rest = [p.strip() for p in m.group(7).split("|")]
        out.append({"source": m.group(3).lower(), "stamps": stamps, "seen": int(m.group(5)),
                    "bookkept": int(m.group(6)), "outcome": rest[-1] if rest else ""})
    return out


def ledger_state(msg: dict, rows: list[dict]) -> tuple[str, str]:
    """-> (done | new, what the ledger says). A message is done when a line of
    its source sits within the tolerance of its time: the gate's own test.
    The counts are not a test: runs write `seen` as the
    files and `bookkept` as the bills, so an invoice with its receipt is 2/1
    and done, and a signature image or a proof of payment is a file nobody
    bookkeeps. What a message still owes is on the register."""
    hits = [r for r in rows if r["source"] == msg["source"]
            and any(abs(t - s) <= dc.LEDGER_TOLERANCE for t in msg["stamps"] for s in r["stamps"])]
    return ("done", "") if hits else ("new", "not in the ledger")


# ---------------------------------------------------------------- Slack

def slack_sweep(window_start: float, since: float) -> tuple[list[dict], list[dict]]:
    """(every human message to the app in the window, thread replies included,
    and the threads with a human reply since `since`). Metadata and text."""
    me = dc.slack_get("auth.test").get("user_id", "")
    names: dict[str, str] = {}

    def name(u: str) -> str:
        if u not in names:
            try:
                p = dc.slack_get("users.info", user=u)["user"]
                names[u] = p.get("real_name") or p.get("name") or u
            except Exception:                                    # noqa: BLE001 - a name is a nicety
                names[u] = u
        return names[u]

    def human(m: dict) -> bool:
        return bool(m.get("user") and m["user"] != me and not m.get("bot_id")
                    and m.get("subtype") in (None, "file_share", "thread_broadcast"))

    def files(m: dict) -> list[dict]:
        return [{"id": f.get("id"), "name": f.get("name") or f.get("title") or f.get("id"),
                 "size": int(f.get("size") or 0), "url": f.get("url_private_download") or f.get("url_private"),
                 "mime": f.get("mimetype") or ""} for f in m.get("files") or [] if f.get("mode") != "tombstone"]

    msgs, threads = [], []
    oldest = int(min(window_start, since) - config.briefing()["thread_lookback_days"] * 86400)
    for im in dc.slack_pages("conversations.list", "channels", types="im", limit=200):
        if im.get("user") in (me, "USLACKBOT") or im.get("is_user_deleted"):
            continue
        for m in dc.slack_pages("conversations.history", "messages", channel=im["id"], oldest=str(oldest), limit=200):
            ts = float(m["ts"])
            if human(m) and ts >= window_start:
                msgs.append({"source": "slack", "ts": ts, "stamps": [ts], "channel": im["id"], "user": m["user"],
                             "who": name(m["user"]), "text": m.get("text") or "", "files": files(m), "thread": ""})
            if float(m.get("latest_reply") or 0) >= since and any(u != me for u in m.get("reply_users") or []):
                reps = dc.slack_get("conversations.replies", channel=im["id"], ts=m["ts"], limit=200).get("messages") or []
                new = []
                for r in reps[1:]:
                    rts = float(r["ts"])
                    if not human(r):
                        continue
                    if rts >= window_start and r.get("files"):
                        msgs.append({"source": "slack", "ts": rts, "stamps": [rts], "channel": im["id"],
                                     "user": r["user"], "who": name(r["user"]), "text": r.get("text") or "",
                                     "files": files(r), "thread": m["ts"]})
                    if rts >= since:
                        new.append({"ts": rts, "who": name(r["user"]), "text": r.get("text") or "",
                                    "files": len(r.get("files") or [])})
                if new:
                    threads.append({"channel": im["id"], "ts": m["ts"], "parent": m.get("text") or "",
                                    "parent_who": "the bot" if m.get("user") == me or m.get("bot_id")
                                    else name(m.get("user") or ""), "replies": new})
    for m in msgs:
        m["ndocs"] = len(m["files"])
    return msgs, threads


def slack_download(m: dict, f: dict) -> Path | None:
    import os
    if not f.get("url"):
        return None
    when = datetime.fromtimestamp(m["ts"], timezone.utc).astimezone(dc.zone())
    path = unique(DOCS / f"S-{when:%Y-%m-%d-%H%M}-{safe(m['who'].split()[0], 20)}-{safe(f['name'])}")
    req = urllib.request.Request(f["url"], headers={"Authorization": f"Bearer {os.environ['SLACK_BOT_TOKEN']}"})
    with urllib.request.urlopen(req, timeout=120, context=dc._ssl()) as r:
        path.write_bytes(r.read())
    return path


# ---------------------------------------------------------------- the inbox

def _parts(payload: dict) -> list[dict]:
    out, stack = [], [payload]
    while stack:
        p = stack.pop()
        stack += p.get("parts") or []
        out.append(p)
    return out


def _b64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def gmail_sweep(window_start: float, since: float) -> tuple[str, list[dict]]:
    """(the token, every inbox email of the window with an attachment plus
    every email since `since`), full format: the attachment count, the
    headers, the body."""
    import email.utils
    from accounting_agent.gmail_auth import get_access_token
    token = get_access_token()
    skip = "-in:sent -in:drafts -in:chats"
    ids: list[str] = []
    for q in (f"after:{int(window_start)} has:attachment {skip}", f"after:{int(since)} {skip}"):
        page = None
        while True:
            params = {"q": q, "maxResults": 100, **({"pageToken": page} if page else {})}
            r = dc.gmail_get(token, "messages", **params)
            ids += [x["id"] for x in r.get("messages") or [] if x["id"] not in ids]
            page = r.get("nextPageToken")
            if not page or len(ids) > 600:
                break
    out = []
    for mid in ids:
        d = dc.gmail_get(token, f"messages/{mid}", format="full")
        hdr = {h["name"].lower(): h["value"] for h in (d.get("payload") or {}).get("headers") or []}
        stamps = [int(d.get("internalDate") or 0) / 1000.0]
        try:
            stamps.append(email.utils.parsedate_to_datetime(hdr.get("date", "")).timestamp())
        except (TypeError, ValueError):
            pass
        atts, body_plain, body_html = [], "", ""
        for p in _parts(d.get("payload") or {}):
            b = p.get("body") or {}
            fname = p.get("filename") or ""
            disp = next((h["value"] for h in p.get("headers") or [] if h["name"].lower() == "content-disposition"), "")
            if fname and (b.get("attachmentId") or b.get("data")):
                inline = disp.lower().startswith("inline")
                size = int(b.get("size") or 0)
                doc = not NOT_DOCS.search(fname) and not (inline and size < SMALL_INLINE
                                                          and (p.get("mimeType") or "").startswith("image/"))
                atts.append({"name": fname, "att": b.get("attachmentId"), "data": b.get("data"), "size": size,
                             "mime": p.get("mimeType") or "", "doc": doc})
            elif p.get("mimeType") == "text/plain" and b.get("data") and not body_plain:
                body_plain = _b64(b["data"]).decode("utf-8", "replace")
            elif p.get("mimeType") == "text/html" and b.get("data") and not body_html:
                body_html = _b64(b["data"]).decode("utf-8", "replace")
        sender = email.utils.parseaddr(hdr.get("from", ""))
        out.append({"source": "email", "id": mid, "thread": d.get("threadId"), "stamps": stamps, "ts": stamps[0],
                    "who": sender[0] or sender[1], "addr": sender[1], "subject": hdr.get("subject", ""),
                    "labels": d.get("labelIds") or [], "files": atts, "ndocs": sum(a["doc"] for a in atts),
                    "body": body_plain.strip() or html_text(body_html)})
        time.sleep(0.12)
    return token, out


def gmail_download(token: str, m: dict, a: dict) -> Path:
    when = datetime.fromtimestamp(m["ts"], timezone.utc).astimezone(dc.zone())
    who = safe((m["who"] or m["addr"]).split()[0] if m["who"] else m["addr"].split("@")[-1], 24)
    path = unique(DOCS / f"E-{when:%Y-%m-%d-%H%M}-{who}-{safe(a['name'])}")
    data = a.get("data") or dc.gmail_get(token, f"messages/{m['id']}/attachments/{a['att']}").get("data", "")
    path.write_bytes(_b64(data))
    return path


def save_body(m: dict) -> Path | None:
    if not m["body"].strip():
        return None
    when = datetime.fromtimestamp(m["ts"], timezone.utc).astimezone(dc.zone())
    who = safe(m["who"] or m["addr"], 24)
    path = unique(DOCS / f"E-{when:%Y-%m-%d-%H%M}-{who}-body.txt")
    path.write_text(f"From: {m['who']} <{m['addr']}>\nSubject: {m['subject']}\n\n{m['body']}\n")
    return path


# ---------------------------------------------------------------- the reads, in the briefing

def money(v) -> str:
    return f"{v:,.2f}" if isinstance(v, (int, float)) and not isinstance(v, bool) else "-"


FIELD_KEYS = {"supplier": "supplier", "total": "gross", "date": "invoice_date", "currency": "currency",
              "net": "net", "tax": "tax", "due date": "due_date", "payment terms": "payment_terms"}


def read_line(x: dict) -> list[str]:
    """One document as the session needs it: the fields, every printed note,
    and the reader's own observations."""
    head = (f"{x.get('document_type') or '?'} · {x.get('supplier') or 'supplier unread'} · "
            f"{x.get('invoice_number') or 'no number'} · {x.get('invoice_date') or 'no date'} · "
            f"{x.get('currency') or '?'} {money(x.get('gross'))} (net {money(x.get('net'))}, tax {money(x.get('tax'))}"
            + (f", {x['tax_label']}" if x.get("tax_label") else "") + ")")
    bits = []
    for k, label in (("due_date", "due"), ("payment_terms", "terms"), ("billed_to", "billed to"),
                     ("delivered_to", "for"), ("supplier_entity", "issued by"), ("payment_evidence", "payment")):
        if x.get(k):
            bits.append(f"{label} {one_line(str(x[k]), 90)}")
    pc = x.get("period_covered") or {}
    if isinstance(pc, dict) and (pc.get("from") or pc.get("to")):
        bits.append(f"period {pc.get('from') or '?'} to {pc.get('to') or '?'}")
    out = [head + (" · " + " · ".join(bits) if bits else "")]
    ill = [k for k, v in (x.get("legibility") or {}).items() if v == "illegible"]
    if ill:
        out.append("illegible: " + ", ".join(ill))
    items = [li for li in x.get("line_items") or [] if isinstance(li, dict)]
    if items:
        shown = "; ".join(f"{one_line(str(li.get('description') or ''), 60)} {money(li.get('amount'))}"
                          for li in items[:4])
        out.append(f"lines ({len(items)}): {shown}" + (" …" if len(items) > 4 else ""))
    notes = [one_line(str(n), 140) for n in x.get("printed_notes") or [] if str(n).strip()]
    if notes:
        out.append("printed: " + one_line(" | ".join(notes), 600))
    if x.get("notes"):
        out.append("reader: " + one_line(str(x["notes"]), 160))
    return out


def same(a, b) -> bool:
    """2500 and 2500.0 are one total."""
    try:
        return abs(float(a) - float(b)) < 0.005
    except (TypeError, ValueError):
        return str(a or "").strip().lower() == str(b or "").strip().lower()


def doc_lines(j: dict) -> list[str]:
    """A document's read and lookups, under the message that carried it."""
    name = j["path"].name
    if j.get("error") and not j.get("docs") and not j.get("second"):
        return [f"    read {name}: not read here ({j['error']}) · " + (
            "read it with Python" if "Python" in j["error"] else "send it to invoice-extract yourself")]
    reads = j.get("second") or j.get("docs") or []
    out = []
    for n, x in enumerate(reads, 1):
        lines = read_line(x)
        out.append(f"    read {name}" + (f" #{n}" if len(reads) > 1 else "") + f": {lines[0]}")
        out += [f"      {l}" for l in lines[1:]]
        item = next((i for i in j.get("items", []) if i["x"] is x), None)
        if item is None:
            continue
        if "in_xero" in item:
            out += ([f"      in Xero? {l}" for l in item["in_xero"]] if item["in_xero"]
                    else ["      in Xero? nothing with the number, or the supplier and gross, in any entity"])
        rows = item.get("suppliers_md") or []
        out += [f"      SUPPLIERS.md: {one_line(r, 320)}" for r in rows]
        if not rows and x.get("document_type") in ("invoice", "receipt", "credit note"):
            out.append("      SUPPLIERS.md: no entry")
        hist = item.get("history") or []
        out += [f"      history: {one_line(h, 220)}" for h in hist[:3]]
        if len(hist) > 3:
            out.append(f"      history: {len(hist) - 3} more entities, ask query-researcher if they matter")
    if j.get("second"):
        first = (j.get("docs") or [{}])[0]
        fields = [f.strip() for f in j["second_why"].split(":", 1)[1].split(",")]
        diffs = [f"{f} {first.get(FIELD_KEYS[f])!s}" for f in fields
                 if f in FIELD_KEYS and j["docs"] and not same(first.get(FIELD_KEYS[f]), j["second"][0].get(FIELD_KEYS[f]))]
        recheck = j["second"][0].get("recheck")
        out.append(f"      {j['second_why']} · read again on {config.briefing()['second_look_model']}, the read above"
                   + (f"; the first read had {', '.join(diffs)}" if diffs else "; the first read agreed")
                   + (f"; printed: {one_line(json.dumps(recheck, ensure_ascii=False), 160)}" if recheck and diffs else ""))
    elif j.get("second_why"):
        out.append(f"      {j['second_why']} · the second look failed ({j.get('second_error')}): "
                   "send it for one yourself before relying on those fields")
    return out


FONTS = ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
         "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
         "/System/Library/Fonts/Supplemental/Arial Unicode.ttf", "/Library/Fonts/Arial Unicode.ttf"]


def render_pdf(txt: Path) -> Path:
    """An email whose body is the receipt, as a PDF to attach to the bill:
    A4 pages at 150 dpi in a Unicode font, so a £, a € or an accent prints
    (the copies earlier runs made by hand turned them into ?)."""
    from PIL import Image, ImageDraw, ImageFont
    W, H, M, SIZE, LEAD = 1240, 1754, 90, 22, 30
    font = None
    for f in FONTS:
        try:
            font = ImageFont.truetype(f, SIZE)
            break
        except OSError:
            continue
    if font is None:
        try:
            font = ImageFont.load_default(size=SIZE)
        except TypeError:
            font = ImageFont.load_default()
    width = W - 2 * M
    lines: list[str] = []
    for para in txt.read_text(errors="replace").splitlines():
        if not para.strip():
            lines.append("")
            continue
        cur = ""
        for w in para.split(" "):
            while font.getlength(w) > width:
                k = len(w) - 1
                while k > 1 and font.getlength(w[:k]) > width:
                    k -= 1
                if cur:
                    lines.append(cur)
                    cur = ""
                lines.append(w[:k])
                w = w[k:]
            cand = f"{cur} {w}" if cur else w
            if font.getlength(cand) <= width:
                cur = cand
            else:
                lines.append(cur)
                cur = w
        lines.append(cur)
    per = (H - 2 * M) // LEAD
    pages = []
    for i in range(0, max(len(lines), 1), per):
        img = Image.new("RGB", (W, H), "white")
        d = ImageDraw.Draw(img)
        y = M
        for ln in lines[i:i + per]:
            d.text((M, y), ln, fill="black", font=font)
            y += LEAD
        pages.append(img)
    out = unique(txt.with_name(txt.name[: -len("-body.txt")] + "-receipt.pdf"))
    pages[0].save(out, "PDF", resolution=150, save_all=True, append_images=pages[1:])
    return out


# ---------------------------------------------------------------- the build

def last_session() -> float:
    state = dc.read_json(dc.GATES / "bookkeep.json", {}) or {}
    return float(state.get("session_started") or 0)


def wipe() -> None:
    shutil.rmtree(DIR, ignore_errors=True)


def is_fresh() -> bool:
    return BRIEF.exists() and time.time() - BRIEF.stat().st_mtime < config.briefing()["fresh_hours"] * 3600


def prune_stale() -> None:
    if DIR.exists() and time.time() - DIR.stat().st_mtime > STALE_HOURS * 3600:
        wipe()


def build(*, refresh_bank: bool = True, extract: bool | None = None) -> Path:
    started = time.time()
    wipe()
    DOCS.mkdir(parents=True, exist_ok=True)
    dc.load_env()
    settings = config.briefing()
    window_days = settings["window_days"]
    if extract is None:
        extract = settings["read_documents"]
    failed: list[str] = []
    since = last_session()
    window_start = started - window_days * 86400
    if not since:
        since = window_start

    did: list[str] = []

    def step(label: str, cmd: list[str], timeout: int = 1800) -> str:
        rc, out = quiet(cmd, timeout)
        if rc:
            failed.append(f"{label} failed (exit {rc}): {one_line(out.strip().splitlines()[-1] if out.strip() else '', 160)}"
                          f" · run `.venv/bin/python {' '.join(cmd[1:])}` yourself")
        else:
            did.append(label)
        return out

    if refresh_bank:
        step("the bank feed refresh", [dc.PY, "scripts/bankfeed.py", "refresh", "all", "--brief"], 900)
    else:
        did.append("the bank feed refresh")                      # the gate's, minutes ago
    step("the bill feed refresh", [dc.PY, "scripts/billfeed.py", "refresh", "all"])
    step("the ledger prune", [dc.PY, "scripts/ledger_prune.py"], 120)

    # The sweep.
    rows = ledger_rows(dc.LEDGER.read_text(errors="replace")) if dc.LEDGER.exists() else []
    slack, threads, mail, token = [], [], [], ""
    try:
        slack, threads = slack_sweep(window_start, since)
        did.append(f"the {window_days}-day sweep of the Slack app against the ledger")
    except Exception as e:                                       # noqa: BLE001 - name it, carry on
        failed.append(f"the Slack sweep failed ({type(e).__name__}: {one_line(str(e), 120)}) · "
                      "sweep the app's DMs yourself, xero-bills section 1")
    inbox = config.company("accounting_inbox", "") or ""
    if inbox:
        try:
            token, mail = gmail_sweep(window_start, since)
            did.append(f"the {window_days}-day sweep of the inbox against the ledger")
        except Exception as e:                                   # noqa: BLE001
            failed.append(f"the inbox sweep failed ({type(e).__name__}: {one_line(str(e), 120)}) · "
                          f"sweep {inbox} yourself, xero-bills Email intake")
    msgs = sorted(slack + mail, key=lambda m: m["ts"])
    for m in msgs:
        m["state"], m["ledger"] = ledger_state(m, rows) if m["ndocs"] else ("done", "")
        if not m["ndocs"] and m["ts"] >= since:
            m["state"] = "talk"                                  # a message with no document, new since the last session

    # The documents of everything still to work.
    for m in msgs:
        m["saved"] = []
        if m["state"] != "new":
            continue
        for f in m["files"]:
            if m["source"] == "email" and not f["doc"]:
                continue
            try:
                p = slack_download(m, f) if m["source"] == "slack" else gmail_download(token, m, f)
                if p:
                    m["saved"].append(p)
            except Exception as e:                               # noqa: BLE001
                failed.append(f"{f['name']} did not download ({type(e).__name__}) · "
                              f"{'slack ' + m['channel'] + ' ' + str(m['ts']) if m['source'] == 'slack' else 'email ' + m['id']}")
        if m["source"] == "email":
            body = save_body(m)
            if body:
                m["saved"].append(body)

    # An email with no attachment may be the receipt itself: keep its body.
    for m in msgs:
        if m["source"] == "email" and m["state"] == "talk" and m["body"].strip():
            body = save_body(m)
            if body:
                m["saved"].append(body)

    # Every document read and looked up before the session opens.
    jobs = []
    for m in msgs:
        if m["state"] not in ("new", "talk"):
            continue
        body_is_doc = m["source"] == "email" and not [p for p in m["saved"] if not p.name.endswith("-body.txt")]
        for p in m["saved"]:
            if p.name.endswith("-body.txt") and not body_is_doc:
                continue
            jobs.append({"path": p, "msg_ts": m["ts"], "msg": m})
    if extract and jobs:
        try:
            import bookkeep_extract as bx
            bx.read_all(jobs, DIR, say=failed.append)
            (DIR / "extract").mkdir(exist_ok=True)
            for j in jobs:
                (DIR / "extract" / f"{j['path'].name}.json").write_text(json.dumps(
                    {"first": j.get("docs"), "second": j.get("second"), "second_why": j.get("second_why"),
                     "error": j.get("error")}, indent=1, ensure_ascii=False))
            did.append("every document read by invoice-extract (the second looks the skill's rules call for "
                       "included) and looked up in Xero and SUPPLIERS.md")
            for j in jobs:
                m = j["msg"]
                if (m["state"] == "talk" and ledger_state(m, rows)[0] == "new"
                        and any(str(x.get("document_type")) in bx.DOC_TYPES for x in (j.get("second") or j["docs"]))):
                    m["state"], m["ledger"] = "new", "not in the ledger, the receipt is the email itself"
        except Exception as e:                                   # noqa: BLE001 - the session reads them instead
            failed.append(f"the document reads failed ({type(e).__name__}: {one_line(str(e), 160)}) · "
                          "read each document with invoice-extract yourself")
    for m in msgs:
        if m["state"] == "new" and m["source"] == "email" and not [
                p for p in m["saved"] if not p.name.endswith("-body.txt")]:
            body = next((p for p in m["saved"] if p.name.endswith("-body.txt")), None)
            if body:
                try:
                    m["saved"].append(render_pdf(body))
                except Exception as e:                           # noqa: BLE001
                    failed.append(f"{body.name} was not rendered to PDF ({type(e).__name__}) · render it yourself")
    by_path = {j["path"]: j for j in jobs}

    # The feeds and the register.
    status = step("bankfeed.py status", [dc.PY, "scripts/bankfeed.py", "status"], 300).strip()
    xc = DIR / "xero-check.txt"
    xsum = step("the xero-check", [dc.PY, "scripts/bankfeed.py", "xero-check", "all", "--out", str(xc), "--summary"],
                1800).strip()
    state = dc.read_json(dc.GATES / "bookkeep.json", {}) or {}
    seen = set(state.get("feed") or [])
    new_lines = [l for l in dc.feed_lines() if dc.feed_key(l) not in seen] if seen else []
    bills = dc.bill_feed()
    rc, reg = quiet([dc.PY, "scripts/outstanding.py", "list", "--domain", "bookkeeping", "--brief"], 300)
    if rc:
        failed.append(f"the register list failed (exit {rc}) · run `outstanding.py list --domain bookkeeping --brief`")
    rc, due = quiet([dc.PY, "scripts/investigate_query.py", "due"], 300)

    # The briefing.
    def rel(p: Path) -> str:
        return str(p.relative_to(ROOT)) if p.is_relative_to(ROOT) else str(p)

    out = [f"# Bookkeeping briefing · built {dc.local(started)}",
           "",
           f"RUN={rel(DIR)} · documents in {rel(DOCS)} · window from {dc.local(window_start)} · "
           f"last finished session {dc.local(since) if last_session() else 'none on record'}",
           "Done already, do not repeat: " + ", ".join(did + ["every document below downloaded"]) + ". "
           "Times in brackets are UTC, the ledger's own.", ""]
    work = [m for m in msgs if m["state"] == "new"]
    out.append(f"## intake to work · {len(work)} messages, "
               f"{sum(len([p for p in m['saved'] if not p.name.endswith('-body.txt')]) for m in work)} documents")
    sn = en = 0
    for m in work:
        if m["source"] == "slack":
            sn += 1
            tag = f"S{sn}"
        else:
            en += 1
            tag = f"E{en}"
        m["tag"] = tag
        local = datetime.fromtimestamp(m["ts"], timezone.utc).astimezone(dc.zone())
        head = f"{tag} · {m['source']} · {local:%a %-d %b %H:%M} ({utc_when(m['ts'])[11:]}) · {m['who']}"
        if m["source"] == "email":
            head += f" <{m['addr']}> · \"{one_line(m['subject'], 90)}\""
            head += " · in the inbox" if "INBOX" in m["labels"] else " · out of the inbox"
        out.append(f"{head} · {m['ledger']}")
        out.append(f"    ledger when {utc_when(m['ts'])} · " + (
            f"slack {m['channel']} ts {m['ts']:.6f}" + (f" in thread {m['thread']}" if m["thread"] else "")
            if m["source"] == "slack" else f"email thread {m['thread']}"))
        words = m["text"] if m["source"] == "slack" else m["body"]
        if words.strip():
            out.append(f"    says: {one_line(words, 160)}")
        if m["saved"]:
            out.append("    files: " + ", ".join(f"{p.name} ({kb(p.stat().st_size)})" for p in m["saved"]))
        for p in m["saved"]:
            if p in by_path:
                out += doc_lines(by_path[p])
        skipped = [f["name"] for f in m["files"] if m["source"] == "email" and not f["doc"]]
        if skipped:
            out.append("    not downloaded (signature, logo or certificate): " + ", ".join(skipped))
    done = [m for m in msgs if m["state"] == "done" and m["ndocs"]]
    out.append(f"• already in the ledger, nothing to do: {sum(m['source'] == 'slack' for m in done)} Slack messages, "
               f"{sum(m['source'] == 'email' for m in done)} emails")
    talk = [m for m in msgs if m["state"] == "talk"]
    if talk:
        out += ["", f"## new since the last session, no document · {len(talk)}"]
        for m in talk:
            local = datetime.fromtimestamp(m["ts"], timezone.utc).astimezone(dc.zone())
            who = m["who"] + (f" <{m['addr']}> \"{one_line(m['subject'], 70)}\"" if m["source"] == "email" else "")
            ref = f"slack {m['channel']} ts {m['ts']:.6f}" if m["source"] == "slack" else f"email thread {m['thread']}"
            words = m["text"] if m["source"] == "slack" else m["body"]
            out.append(f"• {m['source']} · {local:%-d %b %H:%M} · {who}: {one_line(words, 220)} · {ref}")
    if threads:
        out += ["", f"## thread replies since the last session · {len(threads)} threads"]
        for t in threads:
            out.append(f"• slack {t['channel']} thread {t['ts']} · {t['parent_who']}: {one_line(t['parent'], 140)}")
            for r in t["replies"]:
                local = datetime.fromtimestamp(r["ts"], timezone.utc).astimezone(dc.zone())
                files = f" [{r['files']} file(s), listed above]" if r["files"] else ""
                out.append(f"    {local:%-d %b %H:%M} {r['who']}: {one_line(r['text'], 300)}{files}")
    out += ["", "## bank feed", status or "(status not read)"]
    if new_lines:
        out.append(f"new since the last session ({len(new_lines)}):")
        out += [f"    {l['raw']}" for l in new_lines]
    elif seen:
        out.append("no line new since the last session")
    out += ["", "## xero-check of every feed line", xsum or "(not run)",
            f'read one verdict group, whole: .venv/bin/python scripts/bankfeed.py xero-check --from {rel(xc)} '
            '--only "PAID IN XERO" (then LIKELY,CANDIDATES, then "NOTHING FOUND,TRANSFER")']
    out += ["", "## bill feed, open bills",
            " · ".join(f"{dc.entity_short(k)} {len(v)}" for k, v in bills.items())]
    reg_lines = [l for l in reg.strip().splitlines() if l.strip()]
    (DIR / "register.txt").write_text("\n".join(reg_lines) + "\n")
    acting = [l for l in reg_lines if "[answered" in l or ", answered" in l]
    out += ["", f"## register, bookkeeping lines · {len(reg_lines)}",
            f"the whole list, one line each, is {rel(DIR / 'register.txt')}: find the lines a posting answers with "
            f"grep -iE \"<supplier|number>\" {rel(DIR / 'register.txt')}, never by listing the register again"]
    if acting:
        out.append(f"answered by an admin, the posting is ours ({len(acting)}):")
        out += [f"    {l}" for l in acting]
    out += ["", "## investigations due", (due.strip() or "nothing due") if rc == 0 else "(not read)"]
    if failed:
        out += ["", "## not done here, do it yourself"] + [f"• {f}" for f in failed]
    BRIEF.write_text("\n".join(out) + "\n")
    return BRIEF
