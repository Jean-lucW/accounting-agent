"""Publish a built workbook to its folder on Drive, the one step every
workbook builder ends with.

    from accounting_agent.publish import publish_to_drive
    print(publish_to_drive(out_path))

Behind it is gdrive.upload_file: the copy of the same name the agent
published before is overwritten (same file id, so links and version history
hold); a copy uploaded by hand cannot be touched by the agent's token and is
named for a person to move aside. This wrapper never raises: a build must
not be lost because Drive was unreachable, so it returns one line saying what
happened ("published", or "NOT published" and why) for the caller to print
and the run report to carry. A read-only session cannot publish (the upload
refuses under AGENT_READONLY) and gets the same one line.

Publishing is optional. When config/group.toml [drive] publish_folder_id is
blank (and no folder_id is passed) this prints a note and returns "".

Where each file lands: under the publish folder, in the subfolder named for
its kind in [drive.subfolders]:

    kind           matched by file name                   example
    bills          starts "Bills Payable"                 Bills Payable.xlsx
    intercompany   starts "Intercompany" / "Interco"      Intercompany Reconciliation.xlsx
    bank_fees      "Bank ... Fees" / "Bank and FX Fees"   Bank and FX Fees.xlsx
    outstanding    starts "Outstanding Items"             Outstanding Items.docx
    reports        Prepayments / Accruals / Deposits /    Prepayments.xlsx
                   anything with "report" in it

A subfolder name of "" means the root of the publish folder, as does a name
matching no kind. A caller may name the kind itself (kind="reports"), and a
group may add or override patterns in config/group.toml:

    [drive.routes]
    reports = ["^Monthly Pack", "^Board Summary"]

(kind -> list of case-insensitive regular expressions, tried before the
built-in ones). Folders are created on first use; a copy the agent published
earlier at the root is moved into its folder rather than duplicated.

A file built with a date in its name (Intercompany Reconciliation
2026-09-30.xlsx, interco_matrix_2026-09-30.xlsx) publishes under the name
without the date, so Drive holds one copy that every build overwrites; the
as-at date belongs on the workbook's own summary sheet.

NO_DRIVE_PUBLISH=1 in the environment turns publishing off for a run.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

KINDS = ("reports", "bills", "intercompany", "bank_fees", "outstanding")

# Tried in this order: bank_fees before reports, because a fees analysis may
# also call itself a report.
DEFAULT_ROUTES: dict[str, tuple[str, ...]] = {
    "bills": (r"^bills[ _-]?payable",),
    "intercompany": (r"^intercompany", r"^interco[ _-]"),
    "bank_fees": (r"bank.*fees", r"bank[ _-]and[ _-]fx"),
    "outstanding": (r"^outstanding[ _-]items",),
    "reports": (r"prepayment", r"accrual", r"deposit", r"report"),
}

# A trailing ISO date, with its separator, just before the extension.
_DATED = re.compile(r"^(?P<stem>.*?)[ _-]?(?P<date>\d{4}-\d{2}-\d{2})(?P<ext>\.[^.]+)$")


def _settings() -> dict:
    from . import config
    return config.drive_settings()


def _routes() -> list[tuple[str, str]]:
    """(kind, pattern) pairs: configured ones first, then the built-ins."""
    out: list[tuple[str, str]] = []
    try:
        configured = _settings().get("routes") or {}
    except Exception:  # noqa: BLE001 - a missing config falls back to built-ins
        configured = {}
    for kind, patterns in configured.items():
        if isinstance(patterns, str):
            patterns = [patterns]
        out.extend((kind, p) for p in patterns)
    for kind, patterns in DEFAULT_ROUTES.items():
        out.extend((kind, p) for p in patterns)
    return out


def drive_kind(name: str) -> str | None:
    """The workbook kind a file name routes to, or None for the root."""
    for kind, pattern in _routes():
        if re.search(pattern, name, re.IGNORECASE):
            return kind
    return None


def drive_subfolder(name: str, kind: str | None = None) -> tuple[str, ...]:
    """The subfolder path a file publishes into; empty for the root."""
    kind = kind or drive_kind(name)
    if not kind:
        return ()
    sub = (_settings().get("subfolders") or {}).get(kind, "")
    return tuple(part for part in str(sub).split("/") if part) if sub else ()


def drive_name(name: str) -> str:
    """The name the file is published under. Most files keep their own; a
    file dated per build publishes under one fixed name so Drive holds a
    single copy that every build overwrites."""
    m = _DATED.match(name)
    if m and m.group("stem").strip(" _-"):
        return m.group("stem").rstrip(" _-") + m.group("ext")
    return name


def publish_to_drive(path: str | Path, folder_id: str | None = None,
                     kind: str | None = None) -> str:
    """Upload `path` into its folder (or `folder_id`); one summary line.

    Returns "" (after printing a note) when no publish folder is configured.
    """
    path = Path(path)
    if os.environ.get("NO_DRIVE_PUBLISH", "").strip().lower() in ("1", "true", "yes"):
        return f"{path.name}: NOT published (NO_DRIVE_PUBLISH set)"
    try:
        root = str(_settings().get("publish_folder_id", "") or "").strip()
    except Exception as e:  # noqa: BLE001 - the line carries the reason
        return f"{path.name}: NOT published - config: {e}"
    if not folder_id and not root:
        print(f"{path.name}: Drive publishing is off ([drive] publish_folder_id is blank "
              "in config/group.toml); the file stays local.")
        return ""
    if not path.exists():
        return f"{path.name}: NOT published - file not found at {path}"
    try:
        from . import gdrive
        name = drive_name(path.name)
        sub = drive_subfolder(name, kind) if not folder_id else ()
        target = folder_id
        moved = []
        if target is None:
            target = gdrive.ensure_folder(sub, root) if sub else root
            if sub:
                # a copy published at the root before the folders existed
                for f in gdrive.find_in_folder(root, name):
                    try:
                        gdrive.move_file(f["id"], target, root)
                        moved.append(f["id"])
                    except gdrive.DriveError:
                        pass    # a hand-uploaded copy: not ours to move
        meta = gdrive.upload_file(path, target, name=name)
        where = (gdrive.folder_name(folder_id) if folder_id
                 else " / ".join((gdrive.folder_name(root), *sub)))
    except Exception as e:   # noqa: BLE001 - the line carries the reason
        return f"{path.name}: NOT published - {e.__class__.__name__}: {e}"
    shown = path.name if name == path.name else f"{path.name} as {name}"
    line = f"{shown}: {meta['action']} on Drive in {where}, {meta.get('webViewLink', '')}"
    if moved:
        line += f"\n  moved {len(moved)} earlier copy(ies) from the folder root into {where}"
    for lc in meta.get("legacy_copies", []):
        line += (f"\n  a hand-uploaded copy of the same name is still in the folder and "
                 f"cannot be overwritten by this token; move it aside once: "
                 f"{lc.get('webViewLink')}")
    return line
