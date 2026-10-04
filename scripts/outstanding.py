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
    python scripts/outstanding.py list [--domain bookkeeping] [--kind manual]
    python scripts/outstanding.py slack            # the message, split into chunks,
                                                   # for the channel in config/group.toml
    python scripts/outstanding.py render           # rewrite the .md from the store
    python scripts/outstanding.py publish          # rebuild the Word copy, put it on Drive

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

A closed item is deleted, not archived. The trail is the file's git history and
the run report that closed it, and a register that keeps its dead lines is one
nobody reads to the bottom of.

`docs/COMMS.md` governs the rendered line: what happened in words first,
references trailing, no em dash, currency code in front of the amount.
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


def _domains() -> list[tuple[str, str]]:
    """One section per domain, in the order config/group.toml lists them,
    plus `platform` for the agent's own plumbing, which belongs to no domain."""
    out = [(d.key, d.title) for d in config.domains()]
    if "platform" not in [k for k, _ in out]:
        out.append(("platform", "Platform"))
    return out


DOMAINS = _domains()
DOMAIN_KEYS = [d for d, _ in DOMAINS]

# The three subsections every domain has, and the two that appear only when
# they hold something. `answered` is a state, not a kind: an item whose admin
# has ruled but whose posting no run has reported yet.
KINDS = [
    ("manual", "manual items"),
    ("queried", "queried"),
    ("blocked", "blocked"),
    ("documents", "awaiting documents"),
    ("watch", "watch"),
]
KIND_KEYS = [k for k, _ in KINDS]

SLACK_LIMIT = 3500


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
        f"Rebuilt {today:%d %b %Y} · {counts(items)}",
        "",
    ]
    body: list[str] = []
    for domain, title in DOMAINS:
        mine = [i for i in items if i["domain"] == domain]
        if not mine:
            continue
        body.append(f"### {title}")
        body.append("")
        for kind, label in KINDS:
            rows = rows_for(items, domain, kind)
            if not rows:
                continue
            body.append(f"**{label}**")
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
        "the next rebuild overwrites it."
    )
    for domain, title in DOMAINS:
        if not any(i["domain"] == domain for i in items):
            continue
        doc.add_heading(title, level=1)
        for kind, label in KINDS:
            rows = rows_for(items, domain, kind)
            if not rows:
                continue
            doc.add_heading(label, level=2)
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
    for domain, title in DOMAINS:
        if not any(i["domain"] == domain for i in items):
            continue
        for kind, label in KINDS:
            rows = rows_for(items, domain, kind)
            if not rows:
                continue
            head = f"*{title} - {label}*"
            part, first = [], True
            for row in rows:
                bullet = line(row)
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

def cmd_add(a) -> int:
    with locked():
        items = load()
        hit = find(items, key=a.key, text=a.text, domain=a.domain)
        if hit:
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
    references, who it is with, or an answer that turned out not to hold."""
    with locked():
        items = load()
        hit = find(items, item_id=a.id, key=a.key, text=a.match_text)
        if not hit:
            print("no such item", file=sys.stderr)
            return 1
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
        items = [i for i in items if i["id"] != hit["id"]]
        save(items)
        print(f"{hit['id']} closed: {a.reason or 'no reason given'}")
    after_change(a)
    return 0


def cmd_list(a) -> int:
    items = load()
    if a.domain:
        items = [i for i in items if i["domain"] == a.domain]
    if a.kind:
        items = [i for i in items if i["kind"] == a.kind]
    if a.json:
        print(json.dumps(items, indent=2, ensure_ascii=False))
    else:
        print(render(items))
    return 0


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
    add.add_argument("--defer", action="store_true",
                     help="do not publish to Drive yet; finish with `publish`")
    add.set_defaults(func=cmd_add)

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
    upd.add_argument("--defer", action="store_true",
                     help="do not publish to Drive yet; finish with `publish`")
    upd.set_defaults(func=cmd_update)

    close = sub.add_parser("close", help="it is done; delete the line")
    close.add_argument("--id"); close.add_argument("--key"); close.add_argument("--text")
    close.add_argument("--reason", default="")
    close.add_argument("--defer", action="store_true",
                     help="do not publish to Drive yet; finish with `publish`")
    close.set_defaults(func=cmd_close)

    lst = sub.add_parser("list", help="the register, whole or filtered")
    lst.add_argument("--domain", choices=DOMAIN_KEYS)
    lst.add_argument("--kind", choices=KIND_KEYS)
    lst.add_argument("--json", action="store_true")
    lst.set_defaults(func=cmd_list)

    slk = sub.add_parser("slack", help="the channel post, split into chunks")
    slk.add_argument("--stamp", default="")
    slk.set_defaults(func=cmd_slack)

    ren = sub.add_parser("render", help="rewrite OUTSTANDING.md from the store")
    ren.add_argument("--defer", action="store_true",
                     help="do not publish to Drive yet; finish with `publish`")
    ren.set_defaults(func=cmd_render)

    pub = sub.add_parser("publish", help="rebuild the Word copy and put it on Drive")
    pub.set_defaults(func=cmd_publish)

    a = p.parse_args(argv)
    ensure_store()
    return a.func(a)


if __name__ == "__main__":
    raise SystemExit(main())
