#!/usr/bin/env python
"""The standing register of everything the agent has raised and nobody has closed.

One item per thing a person still owes us: a manual action only the Xero UI can
take, a question put to an admin, a document chased from a user, a run that hit
a wall. The register is written the moment a run sends the message, and the
item is deleted the moment it is answered, so the file is always the live
position rather than a list rebuilt from Slack history after the fact.

    python scripts/outstanding.py add --domain bookkeeping --kind manual \
        --with admin --key INV-1042 \
        --text "reconcile the bill in OpCo EU to intercompany" \
        --refs "Example Bistro · 03 Sep · EUR 35.75 · paid OpCo US · recognised OpCo EU · INV-1042"
    python scripts/outstanding.py bump  --key INV-1042
    python scripts/outstanding.py answer --key INV-1042 --answer "reconciled"
    python scripts/outstanding.py close --key INV-1042 --reason "PAID, settled to intercompany"
    python scripts/outstanding.py accept           # close `decided` items nobody overruled
                                                   # within [auto_resolve] confirm_days
    python scripts/outstanding.py list [--domain bookkeeping] [--kind manual]
    python scripts/outstanding.py list --domain bookkeeping --brief     # one short line per item
    python scripts/outstanding.py list --grep "Example Supplier|INV-1042"  # only the items that mention these
    python scripts/outstanding.py list --key INV-1042                   # one item, every field
    python scripts/outstanding.py list --count                          # how many, per domain and kind
    python scripts/outstanding.py check-queries    # every bookkeeping query investigated, none left that
                                                   # should have gone through the gate
    python scripts/outstanding.py prune-trivial [--dry-run] [--json]
                                                   # delete every bookkeeping line under
                                                   # [outstanding_check] trivial_amount on its wording
    python scripts/outstanding.py slack            # the message, split into chunks,
                                                   # for the channel in config/group.toml
    python scripts/outstanding.py render           # rewrite the .md from the store
    python scripts/outstanding.py publish          # rebuild the Word copy, put it on Drive
    python scripts/outstanding.py set-reconcile --file rows.json   # scripts/reconcile_list.py does this

One store, three copies of it:
  - `docs/bookkept/outstanding.json` is the store, and the only thing edited.
  - `docs/bookkept/OUTSTANDING.md` is rendered from it on every change.
  - `data/reports/Outstanding Items.docx` is the Word copy people read,
    rebuilt and published into the Drive publish folder (config/group.toml,
    [drive]) after every change, overwriting the copy already there. Every
    command that changes an item publishes; `--defer` holds
    it back for a run making many changes at once, which then ends with
    `publish`. A Drive that cannot be reached prints a line and changes
    nothing else: the register update is never lost to it. With no Drive
    folder configured, publishing is a no-op.
  Never edit the markdown or the Word copy by hand: the next change overwrites
  both.

The bank lines to reconcile are the one part of the register no run adds to or
closes: `scripts/reconcile_list.py` rebuilds the whole list on every run and
replaces it here, in `docs/bookkept/reconcile.json`, rendered under Bookkeeping
as its own subsection of the manual items.

A closed item is deleted, not archived. The trail is the file's git history and
the run report that closed it, and a register that keeps its dead lines is one
nobody reads to the bottom of.

`docs/COMMS.md` governs the rendered line: what happened in words first,
references trailing, no em dash, currency code in front of the amount.

A bookkeeping query is investigated before it is registered (skill
`query-investigation`, scripts/investigate_query.py). Its certainty maps to a
grade and scripts/resolve_gate.py decides: act and confirm are posted, not
queried; only a query the gate says to ask, a low grade, a failed investigation
or one deferred by the daily cap reaches the register, with `--investigation
<result.json>`. `check-queries` is the exit check that nothing else is there.

With [outstanding_check] trivial_amount set, a bookkeeping line whose amounts,
summed and converted into the reporting currency, come to under it stays off
the register: `add` refuses it (and deletes the open line it would have
bumped) unless `--keep` says an admin wants it, and `prune-trivial` deletes
every such line already there. 0, the default, is off.

The register runs to hundreds of items, and a run keeps in its context every
line it prints. `list` with no filter is the whole rendered register, for a
person reading it; a run asks for the part its step needs. `--brief` is one
line per item, `--grep` matches a regular expression against the key, text,
refs and note, `--key` (or `--id`) prints one item with every field and
`--count` prints only the numbers. The filters combine with each other and
with `--domain`, `--kind` and `--json`.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import sys
from contextlib import contextmanager
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from accounting_agent import config  # noqa: E402

STORE = ROOT / "docs" / "bookkept" / "outstanding.json"
RENDERED = ROOT / "docs" / "bookkept" / "OUTSTANDING.md"
LOCK = ROOT / "docs" / "bookkept" / ".outstanding.lock"
# The Word copy people read, built beside the other reports the server holds
# as masters and published to the Drive publish folder after every change.
DOCX = ROOT / "data" / "reports" / "Outstanding Items.docx"
# Replaced whole by scripts/reconcile_list.py on every run, never edited line
# by line, so it sits beside the store rather than in it.
RECONCILE = ROOT / "docs" / "bookkept" / "reconcile.json"
RECONCILE_LABEL = "manual items: reconcile on the bank feed"


def _domains() -> list[tuple[str, str]]:
    """One section per domain, in the order config/group.toml lists them,
    plus `platform` for the agent's own plumbing, which belongs to no domain."""
    out = [(d.key, d.title) for d in config.domains()]
    if "platform" not in [k for k, _ in out]:
        out.append(("platform", "Platform"))
    return out


DOMAINS = _domains()
DOMAIN_KEYS = [d for d, _ in DOMAINS]

# The three subsections every domain has, and the three that appear only when
# they hold something. `answered` is a state, not a kind: an item whose admin
# has ruled but whose posting no run has reported yet. `decided` is an answer
# the agent reached itself at medium confidence and has already posted; it
# waits for an admin to confirm or overrule it (scripts/resolve_gate.py).
KINDS = [
    ("manual", "manual items"),
    ("queried", "queried"),
    ("decided", "to confirm"),
    ("blocked", "blocked"),
    ("documents", "awaiting documents"),
    ("watch", "watch"),
]
KIND_KEYS = [k for k, _ in KINDS]



def kind_label(domain: str, kind: str, label: str) -> str:
    """Say what a query on the list is. A bookkeeping query reaches the register
    only when its investigation could not settle it (a low grade, or a gate
    that says to ask) or an admin has answered it; a query from any other
    domain is every question that domain raised, never investigated."""
    if kind != "queried":
        return label
    if domain == "bookkeeping":
        return "queried - could not answer"
    return "queried - not investigated"


SLACK_LIMIT = 3500

# A bookkeeping line whose whole amount at stake is under [outstanding_check]
# trivial_amount, in the reporting currency, is not worth a person's time:
# `add` does not register it and `prune-trivial` deletes one already there.
# 0 (the default) is off. A line is judged here on its own wording only; one
# that names no amount, counts more payments than it names amounts, or names a
# currency with no rate has no size here, and the daily check judges it from
# the record instead. `--keep` registers one an admin wants kept all the same.
AMOUNT = re.compile(r"\b([A-Z]{3}) (-?\d[\d,]*(?:\.\d+)?)\b")
COUNTED = re.compile(r"\b(\d+) (?:\w+ ){0,2}(?:payments|charges|lines|receipts|claims|bills|invoices"
                     r"|transactions)\b", re.I)
_RATES: dict[str, float] | None = None


def trivial_amount() -> float:
    return float(config.outstanding_check()["trivial_amount"])


def rates() -> dict[str, float]:
    """Reporting-currency value of one unit of each currency, read once: the
    rates scripts/review_checks.py uses (the last intercompany run, over
    [intercompany_settings] default_rates). Nothing else: a currency neither
    gives has no rate."""
    global _RATES
    if _RATES is None:
        sys.path.insert(0, str(ROOT / "scripts"))
        import review_checks
        review_checks.load_rates()
        _RATES = dict(review_checks.RATES)
    return _RATES


def size(item: dict) -> float | None:
    """The sum of the amounts the line names, in the reporting currency. None
    when it names none, names a currency with no rate, or counts more payments
    than it names amounts ("12 card payments, none over EUR 90"), since its
    size is then unknown."""
    words = f"{item.get('text', '')} {item.get('refs', '')}"
    found = AMOUNT.findall(words)
    table = rates()
    if not found or any(c not in table for c, _ in found):
        return None
    counted = [int(n) for n in COUNTED.findall(words)]
    if counted and max(counted) > len(found):
        return None
    return sum(abs(float(n.replace(",", ""))) * table[c] for c, n in found)


def trivial(item: dict, threshold: float | None = None) -> bool:
    """A bookkeeping line under the threshold on its own wording. Never with
    the threshold off, never a line an admin asked to keep."""
    threshold = trivial_amount() if threshold is None else threshold
    if threshold <= 0 or item.get("domain") != "bookkeeping" or item.get("keep"):
        return False
    s = size(item)
    return s is not None and s < threshold


@contextmanager
def locked():
    """Serialise the whole read-modify-write; two runs can be live at once."""
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with LOCK.open("a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def ensure_store() -> None:
    """Create the register on first use: an empty store and its rendering.

    Both files are runtime state on the server and gitignored, so a fresh
    checkout has neither. A read-only session (AGENT_READONLY) creates
    nothing; load() then reads an empty register."""
    from accounting_agent import readonly
    if readonly.active() or (STORE.exists() and RENDERED.exists()):
        return
    STORE.parent.mkdir(parents=True, exist_ok=True)
    if not STORE.exists():
        STORE.write_text("[]\n")
    if not RENDERED.exists():
        RENDERED.write_text(render(load()))


def load() -> list[dict]:
    if not STORE.exists():
        return []
    return json.loads(STORE.read_text() or "[]")


def save(items: list[dict]) -> None:
    items.sort(key=lambda i: (
        DOMAIN_KEYS.index(i["domain"]) if i["domain"] in DOMAIN_KEYS else 99,
        KIND_KEYS.index(i["kind"]) if i["kind"] in KIND_KEYS else 99,
        i["raised"],
        i["id"],
    ))
    STORE.write_text(json.dumps(items, indent=2, ensure_ascii=False) + "\n")
    RENDERED.write_text(render(items))


def load_reconcile() -> dict:
    if not RECONCILE.exists():
        return {"built": "", "items": []}
    return json.loads(RECONCILE.read_text() or "{}") or {"built": "", "items": []}


def set_reconcile(rows: list[dict], built: str) -> None:
    """Replace the reconcile section with this run's list and re-render."""
    with locked():
        RECONCILE.write_text(json.dumps({"built": built, "items": rows}, indent=2,
                                        ensure_ascii=False) + "\n")
        save(load())


def reconcile_lines() -> list[str]:
    return [f"• {r['text']} · {r['refs']}" if r.get("refs") else f"• {r['text']}"
            for r in load_reconcile()["items"]]


def next_id(items: list[dict]) -> str:
    used = [int(m.group(1)) for i in items
            if (m := re.fullmatch(r"OI-(\d+)", i["id"]))]
    return f"OI-{max(used, default=0) + 1:04d}"


def norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def find(items: list[dict], *, item_id=None, key=None, text=None, domain=None):
    """The same thing raised by four runs is one item. Key on the reference
    first, and only then on the words, so a rephrased question does not open a
    second line."""
    for i in items:
        if item_id and i["id"].lower() == item_id.lower():
            return i
    for i in items:
        if key and i.get("key") and i["key"].lower() == key.lower():
            if domain is None or i["domain"] == domain:
                return i
    for i in items:
        if text and norm(i["text"]) == norm(text):
            if domain is None or i["domain"] == domain:
                return i
    return None


# ---------------------------------------------------------------- rendering

def short_date(iso: str) -> str:
    try:
        d = date.fromisoformat(iso)
    except ValueError:
        return iso
    return f"{d.day:02d} {d:%b}"


def line(item: dict) -> str:
    """One bullet, COMMS.md shape: words first, references trailing."""
    tail = [f"{item['with']}, raised {short_date(item['raised'])}"]
    if item.get("runs", 1) > 1:
        tail.append(f"{item['runs']} runs")
    out = f"• {item['text']} - {', '.join(tail)}"
    if item.get("refs"):
        out += f" · {item['refs']}"
    if item.get("certainty") is not None:
        out += f" · investigated, certainty {item['certainty']}"
    elif item.get("investigation_failed"):
        out += " · investigation failed"
    elif item.get("investigation_scheduled"):
        out += f" · investigation scheduled {short_date(item['investigation_scheduled'])}"
    if item.get("answered"):
        out += f" · answered: {item['answered']}"
    if item.get("unverified"):
        out += " · unverified"
    return out


def counts(items: list[dict]) -> str:
    live = [i for i in items if i["kind"] != "watch"]
    parts = []
    for kind, label in KINDS:
        if kind == "watch":
            continue
        n = len([i for i in live if i["kind"] == kind])
        if n:
            parts.append(f"{n} {label}")
    answered = len([i for i in live if i.get("answered")])
    if answered:
        parts.append(f"{answered} answered and waiting on us")
    to_reconcile = len(load_reconcile()["items"])
    if to_reconcile:
        parts.append(f"{to_reconcile} bank lines to reconcile")
    return " · ".join(parts) if parts else "nothing open"


def rows_for(items: list[dict], domain: str, kind: str) -> list[dict]:
    """An answered item stays in its own subsection and sorts to the top of it:
    it is the agent's own backlog, not the reader's, and hoisting it into a
    section of its own breaks the one-section-per-domain shape."""
    rows = [i for i in items if i["domain"] == domain and i["kind"] == kind]
    rows.sort(key=lambda i: (0 if i.get("answered") else 1, i["raised"], i["id"]))
    return rows


def render(items: list[dict]) -> str:
    today = date.today()
    head = [
        "# Outstanding items",
        "",
        "Everything the agent has raised and nobody has closed, one section per",
        "domain. Generated by `scripts/outstanding.py` from",
        "`docs/bookkept/outstanding.json`; do not edit this file by hand.",
        "",
        "A run writes its item here the moment it sends the message, and deletes it",
        "the moment the answer arrives. A closed item is deleted rather than",
        "archived, so what is here is what is still open. Protocol:",
        "`.claude/skills/outstanding-items/SKILL.md`.",
        "",
        "A bookkeeping query is here only when the agent investigated it and could",
        "not answer it, or an admin has answered it and the posting is still ours.",
        "A query from any other domain goes on the list without an investigation.",
        "",
        f"Rebuilt {today:%d %b %Y} · {counts(items)}",
        "",
    ]
    body: list[str] = []
    rec = reconcile_lines()
    for domain, title in DOMAINS:
        mine = [i for i in items if i["domain"] == domain]
        if not mine and not (domain == "bookkeeping" and rec):
            continue
        body.append(f"### {title}")
        body.append("")
        if domain == "bookkeeping" and rec:
            body.append(f"**{RECONCILE_LABEL}**")
            body.append("")
            body += rec
            body.append("")
        for kind, label in KINDS:
            rows = rows_for(items, domain, kind)
            if not rows:
                continue
            body.append(f"**{kind_label(domain, kind, label)}**")
            body.append("")
            body += [line(i) for i in rows]
            body.append("")
    if not body:
        body = ["Nothing open.", ""]
    return "\n".join(head + body)


def build_docx(items: list[dict], path: Path = DOCX) -> Path:
    """The Word copy, the same sections in the same order as the markdown.
    Built from the store, never from the rendered file, so the two cannot
    drift."""
    from docx import Document
    from docx.shared import Pt

    doc = Document()
    doc.add_heading("Outstanding Items", level=0)
    # No italics: the house style allows bold and nothing else.
    sub = doc.add_paragraph()
    stamp = sub.add_run(f"Rebuilt {date.today():%d %b %Y} · {counts(items)}")
    stamp.bold = True
    stamp.font.size = Pt(10)
    doc.add_paragraph(
        "Everything the agent has raised and nobody has closed, one section per "
        "domain. A run writes an item here the moment it sends the message and "
        "deletes it the moment the answer arrives, so what is on this page is "
        "what is still open. Do not edit this document: the server rebuilds it and "
        "the next rebuild overwrites it. A bookkeeping query is here only when "
        "the agent investigated it and could not answer it, or an admin has "
        "answered it and the posting is still ours. A query from any other "
        "domain goes on the list without an investigation."
    )
    rec = reconcile_lines()
    for domain, title in DOMAINS:
        if not any(i["domain"] == domain for i in items) and not (domain == "bookkeeping" and rec):
            continue
        doc.add_heading(title, level=1)
        if domain == "bookkeeping" and rec:
            doc.add_heading(RECONCILE_LABEL, level=2)
            for bullet in rec:
                doc.add_paragraph(bullet.lstrip("• "), style="List Bullet")
        for kind, label in KINDS:
            rows = rows_for(items, domain, kind)
            if not rows:
                continue
            doc.add_heading(kind_label(domain, kind, label), level=2)
            for item in rows:
                # line() carries the leading bullet the Slack post needs; the
                # Word list style draws its own.
                doc.add_paragraph(line(item).lstrip("• "), style="List Bullet")
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(path)
    return path


def publish(items: list[dict], quiet: bool = False) -> str:
    """Rebuild the Word copy and put it on Drive, overwriting the copy already
    there. Never raises: a register update must not be lost because Drive was
    unreachable, and a read-only session cannot publish at all."""
    if os.environ.get("OUTSTANDING_NO_PUBLISH"):
        return "not published: OUTSTANDING_NO_PUBLISH is set"
    try:
        built = build_docx(items)
    except Exception as e:                                   # noqa: BLE001
        return f"NOT published: the Word copy would not build ({e})"
    try:
        from accounting_agent.publish import publish_to_drive
        line_out = publish_to_drive(built, kind="outstanding") or \
            "not published: no Drive publish folder configured"
    except Exception as e:                                   # noqa: BLE001
        line_out = f"NOT published: {e}"
    if not quiet:
        print(line_out)
    return line_out


def slack_chunks(items: list[dict], stamp: str) -> list[str]:
    """The channel post: the same sections, split under Slack's limit at a
    section boundary, never mid-section."""
    headline = f"OUTSTANDING ITEMS ({stamp}) · {counts(items)}"
    # A section longer than one Slack message is split inside itself and its
    # heading repeated, so no chunk is ever over the limit and no bullet is
    # orphaned from the heading that says what it is.
    blocks: list[str] = []
    rec = reconcile_lines()
    for domain, title in DOMAINS:
        if not any(i["domain"] == domain for i in items) and not (domain == "bookkeeping" and rec):
            continue
        sections = [("manual", RECONCILE_LABEL, rec)] if domain == "bookkeeping" and rec else []
        sections += [(kind, label, [line(r) for r in rows_for(items, domain, kind)]) for kind, label in KINDS]
        for kind, label, bullets in sections:
            if not bullets:
                continue
            head = f"*{title} - {kind_label(domain, kind, label)}*"
            part, first = [], True
            for bullet in bullets:
                if part and len(head) + sum(len(x) + 1 for x in part) + len(bullet) + 1 > SLACK_LIMIT:
                    blocks.append(head + ("" if first else " (cont)") + "\n" + "\n".join(part))
                    part, first = [], False
                part.append(bullet)
            if part:
                blocks.append(head + ("" if first else " (cont)") + "\n" + "\n".join(part))
    chunks, cur = [], headline
    for b in blocks:
        if len(cur) + len(b) + 2 > SLACK_LIMIT and cur:
            chunks.append(cur)
            cur = ""
        cur = f"{cur}\n\n{b}" if cur else b
    if cur:
        chunks.append(cur)
    if len(chunks) > 1:
        total = len(chunks)
        chunks[0] = chunks[0]
        for n in range(1, total):
            chunks[n] = f"OUTSTANDING ITEMS ({stamp}) ({n + 1}/{total})\n\n{chunks[n]}"
    return chunks


def after_change(a) -> None:
    """One publish per command, after the lock is released, so a Drive round
    trip never holds the register against another run. `--defer` is for a run
    making many changes at once: it finishes with `outstanding.py publish`."""
    if getattr(a, "defer", False):
        return
    publish(load(), quiet=False)


# ---------------------------------------------------------------- commands

VERDICT_FIELDS = ("investigation_failed", "investigation_scheduled", "certainty", "grade",
                  "gate", "unsure")


def investigation(a, existing: dict | None) -> dict | None:
    """A bookkeeping query is investigated before it is registered (skill
    `query-investigation`). Returns the fields the verdict adds to the line, or
    raises SystemExit with the reason. The PreToolUse hook
    .claude/hooks/guard-queries.py makes the same check before the command
    runs; this is the copy no caller can route around. Inert when
    [investigation] enabled is false."""
    if a.domain != "bookkeeping" or a.kind != "queried":
        return None
    if not config.investigation()["enabled"]:
        return None
    if not a.investigation:
        if existing and (existing.get("certainty") is not None or existing.get("answered")
                         or existing.get("investigation_failed")):
            return None                       # re-raise of an investigated or answered line
        raise SystemExit("refused: a bookkeeping query is investigated before it is "
                         "registered. Run scripts/investigate_query.py open, then pass "
                         "--investigation <result.json>. Skill: query-investigation.")
    path = Path(a.investigation)
    path = path if path.is_absolute() else ROOT / path
    try:
        v = json.loads(path.read_text())
    except (OSError, ValueError):
        raise SystemExit(f"refused: {a.investigation} is not a readable result.json")
    rel = str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path)
    if v.get("status") == "failed":
        return {"investigation": rel, "investigation_failed": v.get("reason") or "failed"}
    if v.get("status") == "deferred":         # the daily investigation cap: it runs on its day
        when = str(v.get("scheduled") or "")
        try:
            date.fromisoformat(when)
        except ValueError:
            raise SystemExit(f"refused: {a.investigation} is deferred without a date")
        if when < date.today().isoformat():
            raise SystemExit(f"refused: {a.investigation} was deferred to {when}, which has "
                             "passed: open it again")
        return {"investigation": rel, "investigation_scheduled": when}
    c = v.get("certainty")
    if v.get("status") != "done" or not isinstance(c, int):
        raise SystemExit(f"refused: {a.investigation} is not a finished verdict")
    grade = v.get("grade") or ("high" if c >= 4 else "medium" if c == 3 else "low")
    fields = {"investigation": rel, "certainty": c, "grade": grade, "unsure": v.get("unsure", "")}
    if grade != "low":
        gated = (v.get("gate") or {}).get("action")
        if gated != "query":
            raise SystemExit(f"refused: the investigation is graded {grade} (certainty {c}): run it "
                             "through resolve_gate (scripts/investigate_query.py gate --key <key> "
                             "--amount <reporting currency>) and act, or report it under to confirm, "
                             "as it says. Only a gate that says query registers it.")
        fields["gate"] = gated
    return fields


def cmd_add(a) -> int:
    with locked():
        items = load()
        hit = find(items, key=a.key, text=a.text, domain=a.domain)
        line_now = {"domain": a.domain, "text": a.text, "refs": a.refs or (hit or {}).get("refs", "")}
        if not a.keep and not (hit and hit.get("keep")) and trivial(line_now):
            # Not worth the list; the open line it would have bumped goes too.
            print(f"not registered: a bookkeeping line under {config.reporting_currency()} "
                  f"{trivial_amount():,.2f}" + (f", {hit['id']} deleted" if hit else ""))
            if hit:
                save([i for i in items if i is not hit])
            dropped = hit
        else:
            dropped = False
    if dropped is not False:
        if dropped:
            after_change(a)
        return 0
    with locked():
        items = load()
        hit = find(items, key=a.key, text=a.text, domain=a.domain)
        verdict = investigation(a, hit)
        if hit:
            if verdict:
                for f in VERDICT_FIELDS:
                    hit.pop(f, None)
                hit.update(verdict)
            hit["runs"] = hit.get("runs", 1) + 1
            hit["last"] = a.date or date.today().isoformat()
            if a.refs:
                hit["refs"] = a.refs
            if a.note:
                hit["note"] = a.note
            save(items)
            print(f"{hit['id']} already open, {hit['runs']} runs")
        else:
            item = {
                "id": next_id(items),
                "domain": a.domain,
                "kind": a.kind,
                "with": a.with_,
                "raised": a.date or date.today().isoformat(),
                "last": a.date or date.today().isoformat(),
                "runs": 1,
                "key": a.key or "",
                "text": a.text,
                "refs": a.refs or "",
            }
            if a.note:
                item["note"] = a.note
            if a.unverified:
                item["unverified"] = True
            if a.keep:
                item["keep"] = True
            if verdict:
                item.update(verdict)
            items.append(item)
            save(items)
            print(item["id"])
    after_change(a)
    return 0


def cmd_bump(a) -> int:
    with locked():
        items = load()
        hit = find(items, item_id=a.id, key=a.key, text=a.text)
        if not hit:
            print("no such item", file=sys.stderr)
            return 1
        hit["runs"] = hit.get("runs", 1) + 1
        hit["last"] = date.today().isoformat()
        save(items)
        print(f"{hit['id']} {hit['runs']} runs")
    after_change(a)
    return 0


def cmd_answer(a) -> int:
    with locked():
        items = load()
        hit = find(items, item_id=a.id, key=a.key, text=a.text)
        if not hit:
            print("no such item", file=sys.stderr)
            return 1
        hit["answered"] = a.text_answer
        hit["last"] = date.today().isoformat()
        save(items)
        print(f"{hit['id']} answered, awaiting the posting")
    after_change(a)
    return 0


def cmd_update(a) -> int:
    """Amend a line a later run has learnt more about: the wording, the
    references, who it is with, or an answer that turned out not to hold.
    `--investigation` attaches a verdict the register accepts (low grade, a
    gate that says query, failed) to a query already in the register, without
    counting it as another run raising it."""
    with locked():
        items = load()
        hit = find(items, item_id=a.id, key=a.key, text=a.match_text)
        if not hit:
            print("no such item", file=sys.stderr)
            return 1
        if a.investigation:
            a.domain, a.kind = hit["domain"], a.kind or hit["kind"]
            verdict = investigation(a, hit)
            if verdict:
                for f in VERDICT_FIELDS:
                    hit.pop(f, None)
                hit.update(verdict)
        if a.schedule_investigation:
            date.fromisoformat(a.schedule_investigation)
            hit["investigation_scheduled"] = a.schedule_investigation
        for field, value in (("text", a.text), ("refs", a.refs),
                             ("with", a.with_), ("kind", a.kind),
                             ("key", a.newkey)):
            if value:
                hit[field] = value
        if a.clear_answer:
            hit.pop("answered", None)
        if a.unverified:
            hit["unverified"] = True
        if a.verified:
            hit.pop("unverified", None)
        hit["last"] = date.today().isoformat()
        save(items)
        print(f"{hit['id']} updated")
    after_change(a)
    return 0


def cmd_close(a) -> int:
    with locked():
        items = load()
        hit = find(items, item_id=a.id, key=a.key, text=a.text)
        if not hit:
            print("no such item", file=sys.stderr)
            return 1
        # The item itself, not its id: an id can repeat across domains.
        items = [i for i in items if i is not hit]
        save(items)
        print(f"{hit['id']} closed: {a.reason or 'no reason given'}")
    after_change(a)
    return 0


def cmd_prune_trivial(a) -> int:
    """Delete every bookkeeping line under [outstanding_check] trivial_amount
    on its own wording. --json prints what went, for
    scripts/outstanding_check.py; --dry-run deletes nothing."""
    threshold = trivial_amount()
    cur = config.reporting_currency()
    with locked():
        items = load()
        gone = [i for i in items if trivial(i, threshold)]
        if gone and not a.dry_run:
            save([i for i in items if not any(i is g for g in gone)])
    out = [{"id": i["id"], "key": i.get("key", ""), "kind": i["kind"], "text": i["text"],
            "refs": i.get("refs", ""), "currency": cur, "amount": round(size(i) or 0, 2)} for i in gone]
    if a.json:
        print(json.dumps(out, ensure_ascii=False))
    else:
        if threshold <= 0:
            print("[outstanding_check] trivial_amount is 0: no line is trivial")
        for g in out:
            print(f"{'would delete' if a.dry_run else 'deleted'} {g['id']} {g['kind']}, "
                  f"{cur} {g['amount']:,.2f}: {g['text'][:90]}")
        print(f"{len(out)} lines under {cur} {threshold:,.2f}")
    if gone and not a.dry_run:
        after_change(a)
    return 0


def cmd_accept(a) -> int:
    """Close every `decided` item nobody overruled within [auto_resolve]
    confirm_days. Run it only after the replies have been read: an overrule
    found in Slack is applied and closed first, so what is left here is
    silence, and silence past the window is acceptance."""
    days = int(config.auto_resolve()["confirm_days"])
    if not days:
        print("confirm_days is 0: decided items stay open until an admin answers")
        return 0
    today = date.fromisoformat(a.date) if a.date else date.today()
    with locked():
        items = load()
        stale = [i for i in items if i["kind"] == "decided"
                 and (today - date.fromisoformat(i["raised"])).days >= days]
        for i in stale:
            print(f"{i['id']} accepted, unchallenged for {days} days: {i['text']}")
        if stale:
            items = [i for i in items if i not in stale]
            save(items)
    if stale:
        after_change(a)
    else:
        print("nothing past the window")
    return 0


def brief(item: dict, width: int = 100) -> str:
    """One short line: enough to decide what to close or update."""
    text = " ".join(item["text"].split())
    if len(text) > width:
        text = text[:width - 3].rstrip() + "..."
    flags = []
    if item.get("answered"):
        flags.append("answered")
    if item.get("certainty") is not None:
        flags.append(f"certainty {item['certainty']}")
    tail = f" [{', '.join(flags)}]" if flags else ""
    return f"{item['id']} {item['kind']} {item['with']} | {item.get('key') or '-'} | {text}{tail}"


def cmd_list(a) -> int:
    items = load()
    if a.domain:
        items = [i for i in items if i["domain"] == a.domain]
    if a.kind:
        items = [i for i in items if i["kind"] == a.kind]
    if a.key or a.id:
        hit = find(items, item_id=a.id, key=a.key)
        items = [hit] if hit else []
        if not items:
            print("no such item", file=sys.stderr)
            return 1
    if a.grep:
        rx = re.compile(a.grep, re.I)
        items = [i for i in items
                 if rx.search(" ".join(str(i.get(f) or "") for f in ("key", "text", "refs", "note", "id")))]
    if a.count:
        by: dict[tuple, int] = {}
        for i in items:
            by[(i["domain"], i["kind"])] = by.get((i["domain"], i["kind"]), 0) + 1
        print(f"{len(items)} items")
        for (d, k), n in sorted(by.items(), key=lambda x: (DOMAIN_KEYS.index(x[0][0])
                                                            if x[0][0] in DOMAIN_KEYS else 99, x[0][1])):
            print(f"  {d:18} {k:10} {n}")
        return 0
    if a.json:
        print(json.dumps(items, indent=2, ensure_ascii=False))
    elif a.key or a.id:
        print(line(items[0]))
        for f in ("domain", "kind", "with", "key", "raised", "last", "runs", "note", "investigation",
                  "certainty", "grade", "gate", "unsure", "answered"):
            if items[0].get(f) not in (None, ""):
                print(f"  {f}: {items[0][f]}")
    elif a.brief or a.grep:
        if not items:
            print("no item matches")
        for i in items:
            print(brief(i))
    else:
        print(render(items))
    return 0


def unsolved_breaches(items: list[dict]) -> list[tuple[dict, str]]:
    """The bookkeeping queries that should not be in the register. Every one
    left there is a question a run could not settle itself, so it carries an
    investigation at a low grade, a high or medium one that the gate said to
    ask, a failed investigation, or an admin's answer. A line with no verdict
    was never investigated; a line graded high or medium that the gate never
    saw should have gone through it and been acted on or reported to confirm.
    A line whose investigation is scheduled for a later run (the daily cap, or
    `update --schedule-investigation`) passes until that date and fails from
    the day after."""
    today = date.today().isoformat()
    out = []
    for i in items:
        if i["domain"] != "bookkeeping" or i["kind"] != "queried" or i.get("answered"):
            continue
        c = i.get("certainty")
        if c is None and not i.get("investigation_failed"):
            when = i.get("investigation_scheduled") or ""
            if when and when >= today:
                continue
            out.append((i, f"investigation was scheduled for {when}, still none" if when
                        else "never investigated"))
        elif c is not None and i.get("grade", "low") != "low" and i.get("gate") != "query":
            out.append((i, f"investigated at certainty {c}, graded {i.get('grade')}: run it through "
                           "resolve_gate and act on it, or report it under to confirm, then close the line"))
    return out


def cmd_check_queries(a) -> int:
    if not config.investigation()["enabled"]:
        print("PASS: [investigation] is switched off, nothing to check")
        return 0
    items = load()
    bad = unsolved_breaches(items)
    held = [i for i in items if i["domain"] == "bookkeeping" and i["kind"] == "queried"
            and i.get("certainty") is None and not i.get("investigation_failed")
            and not i.get("answered") and i.get("investigation_scheduled")
            and i["investigation_scheduled"] >= date.today().isoformat()]
    if not bad:
        note = (f" ({len(held)} scheduled for investigation by a later run, leave them to it)"
                if held else "")
        print(f"PASS: every bookkeeping query in the register is investigated and unsolved{note}")
        return 0
    print(f"FAIL: {len(bad)} bookkeeping queries in the register should not be there")
    for i, why in bad:
        print(f"  {i['id']} {i.get('key') or ''} - {why}")
    return 1


def cmd_slack(a) -> int:
    """Prints the post; it does not send it. The channel it belongs in comes
    from config/group.toml and is named on stderr so stdout stays the message."""
    stamp = a.stamp or f"{date.today():%d %b %Y}"
    s = config.slack()
    print(f"post to #{s.channel_name} ({s.channel_id or 'channel_id not set'}) at top level",
          file=sys.stderr)
    for chunk in slack_chunks(load(), stamp):
        print(chunk)
        print("\n---8<---\n")
    return 0


def cmd_render(a) -> int:
    with locked():
        items = load()
        save(items)
        print(f"{RENDERED.relative_to(ROOT)} rebuilt, {len(items)} open")
    after_change(a)
    return 0


def cmd_publish(a) -> int:
    publish(load())
    return 0


def cmd_set_reconcile(a) -> int:
    from accounting_agent import readonly
    if readonly.active():
        readonly.refuse("replacing the reconcile section of the register")
    rows = json.loads(Path(a.file).read_text())
    set_reconcile(rows["items"] if isinstance(rows, dict) else rows,
                  (rows.get("built") if isinstance(rows, dict) else "") or date.today().isoformat())
    print(f"reconcile section replaced, {len(load_reconcile()['items'])} lines")
    after_change(a)
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    add = sub.add_parser("add", help="raise an item, or bump the one already open")
    add.add_argument("--domain", required=True, choices=DOMAIN_KEYS)
    add.add_argument("--kind", required=True, choices=KIND_KEYS)
    add.add_argument("--with", dest="with_", required=True,
                     help="who closes it: admin, or the person's name as in config [slack]")
    add.add_argument("--text", required=True, help="what has to happen, in words")
    add.add_argument("--refs", default="", help="the reference tail, · separated")
    add.add_argument("--key", default="", help="dedup key: the invoice number, journal or account")
    add.add_argument("--date", default="", help="the date it was raised (default today)")
    add.add_argument("--note", default="")
    add.add_argument("--unverified", action="store_true")
    add.add_argument("--investigation", default="",
                     help="the result.json of the query investigation; required for a "
                          "bookkeeping query, at a low grade, a gate that says query, or failed")
    add.add_argument("--keep", action="store_true",
                     help="register a bookkeeping line under [outstanding_check] trivial_amount "
                          "all the same (an admin asked)")
    add.add_argument("--defer", action="store_true",
                     help="do not publish to Drive yet; finish with `publish`")
    add.set_defaults(func=cmd_add)

    prn = sub.add_parser("prune-trivial",
                         help="delete every bookkeeping line whose wording puts it under "
                              "[outstanding_check] trivial_amount")
    prn.add_argument("--dry-run", action="store_true", help="say what would go, delete nothing")
    prn.add_argument("--json", action="store_true")
    prn.add_argument("--defer", action="store_true",
                     help="do not publish to Drive yet; finish with `publish`")
    prn.set_defaults(func=cmd_prune_trivial)

    bump = sub.add_parser("bump", help="another run raised the same thing")
    bump.add_argument("--id"); bump.add_argument("--key"); bump.add_argument("--text")
    bump.add_argument("--defer", action="store_true",
                     help="do not publish to Drive yet; finish with `publish`")
    bump.set_defaults(func=cmd_bump)

    ans = sub.add_parser("answer", help="an admin ruled; the posting has not happened yet")
    ans.add_argument("--id"); ans.add_argument("--key"); ans.add_argument("--text")
    ans.add_argument("--answer", dest="text_answer", required=True)
    ans.add_argument("--defer", action="store_true",
                     help="do not publish to Drive yet; finish with `publish`")
    ans.set_defaults(func=cmd_answer)

    upd = sub.add_parser("update", help="amend a line a run has learnt more about")
    upd.add_argument("--id"); upd.add_argument("--key")
    upd.add_argument("--match-text", dest="match_text")
    upd.add_argument("--text", default="", help="new wording")
    upd.add_argument("--refs", default="", help="new reference tail")
    upd.add_argument("--with", dest="with_", default="", help="new owner")
    upd.add_argument("--kind", default="", choices=[""] + KIND_KEYS)
    upd.add_argument("--newkey", default="", help="new dedup key")
    upd.add_argument("--clear-answer", action="store_true",
                     help="the answer did not hold; put it back to the person")
    upd.add_argument("--unverified", action="store_true")
    upd.add_argument("--verified", action="store_true")
    upd.add_argument("--schedule-investigation", default="", metavar="YYYY-MM-DD",
                     help="a query left for a later run to investigate (the daily cap); "
                          "check-queries passes it until that date")
    upd.add_argument("--investigation", default="",
                     help="attach a verdict the register accepts to a query already in it")
    upd.add_argument("--defer", action="store_true",
                     help="do not publish to Drive yet; finish with `publish`")
    upd.set_defaults(func=cmd_update)

    close = sub.add_parser("close", help="it is done; delete the line")
    close.add_argument("--id"); close.add_argument("--key"); close.add_argument("--text")
    close.add_argument("--reason", default="")
    close.add_argument("--defer", action="store_true",
                     help="do not publish to Drive yet; finish with `publish`")
    close.set_defaults(func=cmd_close)

    acc = sub.add_parser("accept", help="close `decided` items unchallenged past confirm_days")
    acc.add_argument("--date", default="", help="treat this ISO date as today (default today)")
    acc.add_argument("--defer", action="store_true",
                     help="do not publish to Drive yet; finish with `publish`")
    acc.set_defaults(func=cmd_accept)

    lst = sub.add_parser("list", help="the register, whole or filtered")
    lst.add_argument("--domain", choices=DOMAIN_KEYS)
    lst.add_argument("--kind", choices=KIND_KEYS)
    lst.add_argument("--json", action="store_true")
    lst.add_argument("--brief", action="store_true", help="one short line per item")
    lst.add_argument("--grep", default="", help="regex over key, text, refs and note; brief lines")
    lst.add_argument("--key", default="", help="one item by its key, every field")
    lst.add_argument("--id", default="", help="one item by its OI id, every field")
    lst.add_argument("--count", action="store_true", help="counts per domain and kind only")
    lst.set_defaults(func=cmd_list)

    chk = sub.add_parser("check-queries",
                         help="exit check: every bookkeeping query is investigated and unsolved")
    chk.set_defaults(func=cmd_check_queries)

    slk = sub.add_parser("slack", help="the channel post, split into chunks")
    slk.add_argument("--stamp", default="")
    slk.set_defaults(func=cmd_slack)

    ren = sub.add_parser("render", help="rewrite OUTSTANDING.md from the store")
    ren.add_argument("--defer", action="store_true",
                     help="do not publish to Drive yet; finish with `publish`")
    ren.set_defaults(func=cmd_render)

    pub = sub.add_parser("publish", help="rebuild the Word copy and put it on Drive")
    pub.set_defaults(func=cmd_publish)

    rec = sub.add_parser("set-reconcile",
                         help="replace the bank lines to reconcile (scripts/reconcile_list.py)")
    rec.add_argument("--file", required=True, help="json: {built, items: [{entity, text, refs}]}")
    rec.add_argument("--defer", action="store_true",
                     help="do not publish to Drive yet; finish with `publish`")
    rec.set_defaults(func=cmd_set_reconcile)

    a = p.parse_args(argv)
    ensure_store()
    return a.func(a)


if __name__ == "__main__":
    raise SystemExit(main())
