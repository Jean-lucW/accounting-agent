#!/usr/bin/env python3
"""PreToolUse guard for read-only sessions.

Active only while AGENT_READONLY is set (deploy/run-query.sh sets it for
every `query` session started from Slack). Otherwise it exits 0 at once and
changes nothing, so admin runs and cron are untouched.

What it denies while active:

  Write / Edit / MultiEdit / NotebookEdit  anywhere but the session's own
                                           scratch folder ($QUERY_DIR,
                                           data/query/, /tmp/)
  Bash                                     anything that writes: Xero writes
                                           (.post/.put/.delete, create_*,
                                           update_*, void_*, upload_*),
                                           database writes, non-GET HTTP,
                                           Slack posts (the runner replies; the
                                           helper uploads), Gmail modify/trash,
                                           Drive uploads, git mutations,
                                           rm/mv/cp/tee/sed -i, redirects and
                                           file writes outside the scratch
                                           folder, the repo's own loader/build
                                           scripts, and any attempt to unset
                                           the flag

It is the second lock. The first is the transport layer: XeroClient refuses
non-GET whenever the flag is up (src/accounting_agent/readonly.py). The third
is the CLI itself, which run-query.sh starts with the Write/Edit tools
disallowed. Three independent locks because an unattended session has no
human to say no.

Every denial is appended to data/logs/readonly-denials.log so the runner can
report how many times a session pushed against the rule.

Tested by scripts/test_readonly_guard.py. Run it after touching this file.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import pathlib
import re
import sys

ENV = "AGENT_READONLY"
REPO = pathlib.Path(os.environ.get("CLAUDE_PROJECT_DIR") or pathlib.Path(__file__).resolve().parents[2])
EDIT_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}

HOW = ("Nothing may be created, changed, uploaded, labelled or deleted in a "
       "query session. Answer from what you can read. An extract you build "
       "goes under $QUERY_DIR and is delivered with "
       "`.venv/bin/python scripts/slack_query.py upload <path>`. "
       "Do not rewrite the command to get around this.")


def _active() -> bool:
    return os.environ.get(ENV, "").strip().lower() not in ("", "0", "false", "no")


def _scratch_roots() -> list[pathlib.Path]:
    roots = [REPO / "data" / "query", pathlib.Path("/tmp")]
    q = os.environ.get("QUERY_DIR")
    if q:
        p = pathlib.Path(q)
        roots.append(p if p.is_absolute() else REPO / p)
    return [r.resolve() for r in roots]


def _under_scratch(path: str) -> bool:
    if not path:
        return False
    p = pathlib.Path(path)
    p = (p if p.is_absolute() else REPO / p).resolve()
    return any(p == r or r in p.parents for r in _scratch_roots())


def _mentions_scratch(text: str) -> bool:
    return bool(re.search(r"data/query|QUERY_DIR|/tmp/", text))


# (name, regex, extra note). Order matters only for the reason shown.
RULES: list[tuple[str, re.Pattern, str]] = [
    ("read-only flag tampering",
     re.compile(r"AGENT_READONLY|\bunset\b|\benv\s+-i\b|\bexport\s+-n\b"),
     "the flag stays up for the whole session."),
    ("Xero write",
     re.compile(r"\.(post|put|patch|delete)\s*\("
                r"|request\(\s*['\"](POST|PUT|PATCH|DELETE)"
                r"|\b(create|update|void|delete|allocate|mark|attach|upload|approve"
                r"|authorise|authorize|post|archive|merge|reconcile|unreconcile)_\w*\s*\("
                r"|upload_attachment|Idempotency-Key"),
     "the Xero client only answers GET in this session."),
    ("database write",
     re.compile(r"\.transaction\s*\(|\bexecute\s*\(|\bexecutemany\s*\(|\bpsql\b"
                r"|\b(INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM|TRUNCATE\s+"
                r"|DROP\s+(TABLE|SCHEMA|INDEX|ROLE|DATABASE|VIEW|FUNCTION)"
                r"|ALTER\s+(TABLE|SCHEMA|ROLE|USER|DATABASE)"
                r"|CREATE\s+(TABLE|SCHEMA|INDEX|ROLE|USER|DATABASE|VIEW|OR\s+REPLACE|FUNCTION|EXTENSION)"
                r"|GRANT\s+|REVOKE\s+|COPY\s+\w+\s+FROM)\b", re.I),
     "a database is read, never written, in this session."),
    ("non-GET HTTP",
     re.compile(r"\b(curl|wget)\b[^\n]*?(\s-X\s*\w|\s--request\b|\s-d\s|\s--data\b|\s--data-\w+"
                r"|\s-F\s|\s--form\b|\s-T\s|\s--upload-file\b|\s--json\b|\s--post-data\b"
                r"|\s--post-file\b|\s--method\b|\s--body\b)"
                r"|requests\.(post|put|patch|delete)\s*\("
                r"|httpx\.(post|put|patch|delete)\s*\("
                r"|method\s*=\s*['\"](POST|PUT|PATCH|DELETE)"
                r"|urlopen\([^)]*\bdata\s*=|Request\([^)]*\bdata\s*=", re.I),
     "GET only, for every API."),
    ("Drive upload",
     re.compile(r"upload_file\s*\(|publish_to_drive\s*\(|gdrive\s+put\b|upload/drive/v3"),
     "Drive is read-only in this session; publishing is an admin run's job."),
    ("Slack post",
     re.compile(r"chat\.postMessage|chat_postMessage|chat\.update|chat\.delete|chat_update"
                r"|files\.upload|files_upload|files\.getUploadURLExternal"
                r"|files\.completeUploadExternal|reactions\.add|reactions_add"
                r"|conversations\.open|conversations_open"),
     "you never message anyone: the runner posts your final answer, "
     "scripts/slack_query.py sends files, both into the asker's thread."),
    ("Gmail change",
     re.compile(r"googleapis\.com[^\n]*(/modify|/trash|/untrash|/batchModify|/batchDelete"
                r"|/send|/drafts|/import|/insert|/watch|/settings)"
                r"|gmail[^\n]*\b(modify|trash|untrash|batchModify|batchDelete|drafts)\b", re.I),
     "mail is read, never labelled, moved, sent or deleted."),
    ("git mutation",
     re.compile(r"\bgit\s+(-C\s+\S+\s+)?(add|commit|push|pull|fetch|checkout|switch|reset|rebase"
                r"|merge|stash|clean|rm|mv|tag|restore|cherry-pick|revert|am|apply|gc|prune"
                r"|filter-branch|remote\s+(add|remove|set-url)|branch\s+(-[dDmM]|--delete|--move)"
                r"|config\s+(?!--get|--list|-l\b))"),
     "git log / status / diff / show are fine; nothing that moves the tree."),
    ("shell write",
     re.compile(r"(^|[;&|(`]\s*|\bsudo\s+|\bxargs\s+(-\S+\s+)*|\bthen\s+|\bdo\s+)"
                r"(rm|rmdir|truncate|chmod|chown|chgrp|dd|shred|install|rsync|scp|sftp|crontab"
                r"|systemctl|service|kill|pkill|killall|nohup|setsid|pip3?|npm|npx|apt|apt-get"
                r"|brew|reboot|shutdown|sudo)\b"
                r"|\bsed\s+(-\w*\s+)*-i\b|\bperl\s+(-\w*\s+)*-i\b|\s-delete\b|python3?\s+-m\s+pip\b"),
     ""),
    ("repo loader or build script",
     re.compile(r"scripts/(bankfeed|billfeed|ledger_prune|interco_recon|interco_matrix"
                r"|build_\w+|\w+_report|slack_agent)\.py|deploy/\S+\.sh|\.claude/hooks/"
                # the outstanding register and the interco open items may be
                # listed, never changed or rendered
                r"|scripts/(outstanding|interco_breaks)\.py(?!\s+list\b)"),
     "those scripts load, rebuild or move things. Read the workbook or the data "
     "they already produced instead."),
    ("write flag",
     re.compile(r"\s--(post|apply|write|commit|load|refresh|execute|delete|fix|label|upload|send)\b"),
     ""),
]

# Commands that are fine when they touch only the scratch folder.
SCRATCH_ONLY = re.compile(
    r"(^|[;&|(`]\s*|\bthen\s+|\bdo\s+)(cp|mv|touch|tee|ln|mkdir)\b")

# Python-side file writes, same rule: fine only towards the scratch folder.
FILE_WRITE = re.compile(
    r"\bopen\s*\([^)]*['\"][waxWAX]\+?b?['\"]|\.write_text\s*\(|\.write_bytes\s*\("
    r"|\.save\s*\(|\.to_excel\s*\(|\.to_csv\s*\(|\.to_parquet\s*\(|\.to_json\s*\("
    r"|shutil\.(copy\w*|move|rmtree)\s*\(|os\.(remove|unlink|rename|replace|rmdir|makedirs"
    r"|mkdir|chmod|truncate)\s*\(|\.unlink\s*\(|\.rename\s*\(|\.replace_with|\.rmdir\s*\("
    r"|\.touch\s*\(|json\.dump\s*\(|pickle\.dump\s*\(|np\.save\w*\s*\(|\.dump\s*\(")

# The lookbehind skips ->, >=, <>, != and a Python format spec such as {v:>12,.2f}.
REDIRECT = re.compile(r"(?<![<>=!\-\\:{])>{1,2}\s*([^\s;|&)]+)")


def _redirect_targets(cmd: str) -> list[str]:
    out = []
    for m in REDIRECT.finditer(cmd):
        t = m.group(1).strip("\"'").rstrip(":,")
        if not t or t.startswith(("&", "=", "/dev/null", "/dev/stderr", "/dev/stdout")):
            continue
        if "(" in t or t.startswith("$(") or t.startswith("<") or t.endswith("}"):
            continue
        try:
            float(t)
            continue
        except ValueError:
            pass
        if not ("/" in t or "." in t or "$" in t or "~" in t):
            continue                     # `x > y` in a heredoc, not a file
        if _mentions_scratch(t):
            continue
        out.append(t)
    return out


def check_command(cmd: str) -> str | None:
    for name, rx, note in RULES:
        if rx.search(cmd):
            return f"{name}. {note}".strip()
    if SCRATCH_ONLY.search(cmd) and not _mentions_scratch(cmd):
        return "file move or copy outside the scratch folder."
    if FILE_WRITE.search(cmd) and not _mentions_scratch(cmd):
        return "file write outside the scratch folder."
    bad = _redirect_targets(cmd)
    if bad:
        return f"redirect outside the scratch folder ({bad[0]})."
    return None


def check_edit(path: str) -> str | None:
    if _under_scratch(path):
        return None
    return f"file edit outside the scratch folder ({path or '?'})."


def _log(tool: str, why: str, subject: str) -> None:
    try:
        d = REPO / "data" / "logs"
        d.mkdir(parents=True, exist_ok=True)
        stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        job = os.environ.get("QUERY_JOB", "?")
        one = " ".join(subject.split())[:200]
        with (d / "readonly-denials.log").open("a") as fh:
            fh.write(f"{stamp} [{job}] {tool}: {why} :: {one}\n")
    except OSError:
        pass


def main() -> int:
    if not _active():
        return 0
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise ValueError("payload is not a JSON object")
    except (ValueError, OSError) as e:
        # Fail closed: a payload the guard cannot read is denied, never waved through.
        why = f"the read-only guard could not parse the tool call ({e.__class__.__name__})."
        _log("?", why, "")
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": f"BLOCKED, read-only session: {why} {HOW}",
        }}))
        return 0
    tool = payload.get("tool_name") or ""
    inp = payload.get("tool_input") or {}
    why = subject = None
    if tool in EDIT_TOOLS:
        subject = inp.get("file_path") or inp.get("notebook_path") or ""
        why = check_edit(subject)
    elif tool == "Bash":
        subject = inp.get("command") or ""
        why = check_command(subject)
    if not why:
        return 0
    _log(tool, why, subject or "")
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": f"BLOCKED, read-only session: {why} {HOW}",
    }}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
