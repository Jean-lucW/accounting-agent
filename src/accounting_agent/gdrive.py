"""Google Drive client covering My Drive AND shared drives: read everything,
write one thing.

Plain urllib over the Drive v3 REST API, same style as gmail_auth.py; no
google-api-python-client dependency. Every call passes supportsAllDrives /
includeItemsFromAllDrives so shared-drive content is visible. Every call is a
GET except publishing: upload_file() and the folder, shortcut, rename and move
helpers it needs. A workbook is published after each build, overwriting its
previous version, into the Drive publish folder (config/group.toml [drive]
publish_folder_id; publish.py picks the subfolder). That write is fenced
three ways: the token is consented drive.readonly + drive.file so it can only
create files and update files it created itself; the destination folder must
be on the publish allowlist (the publish folder plus any IDs in
DRIVE_WRITE_ALLOWLIST, comma-separated, in .env or the environment), or a
folder created under one this process; and a read-only session
(AGENT_READONLY) refuses it before any request leaves the process.

    gdrive.upload_file("data/reports/Bills Payable.xlsx")
    .venv/bin/python -m accounting_agent.gdrive put <file> [folder-id]

    import sys; sys.path.insert(0, "src")
    from accounting_agent import gdrive

    gdrive.whoami()                              # the authorised account
    gdrive.shared_drives()                       # [{id, name}]
    gdrive.list_folder("<folder-id>")
    gdrive.search("name contains 'Invoice'", drive_id="0AB...")
    gdrive.search_all("Contoso Cloud")              # free-text across everything
    path = gdrive.download("1abc...", "data/drive")   # PDF/xlsx/... as-is,
                                                      # Google Docs exported
    text = gdrive.read_text("1abc...")           # Google Doc/Sheet as text/csv

CLI:  .venv/bin/python -m accounting_agent.gdrive whoami
      .venv/bin/python -m accounting_agent.gdrive drives
      .venv/bin/python -m accounting_agent.gdrive ls <folder-id> [-r]
      .venv/bin/python -m accounting_agent.gdrive find "<text>"
      .venv/bin/python -m accounting_agent.gdrive get <file-id> [dest-dir]
      .venv/bin/python -m accounting_agent.gdrive cat <file-id>
      .venv/bin/python -m accounting_agent.gdrive put <file> [folder-id]

Scanned PDFs (approval stamps, signed copies) often have no text layer: after
download() read them visually rather than with pypdf.
Setup guide: docs/setup/GOOGLE_DRIVE.md.
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

import certifi

from . import readonly
from .gdrive_auth import TOKENS_PATH, get_access_token

_SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
BASE = "https://www.googleapis.com/drive/v3"

FILE_FIELDS = (
    "id,name,mimeType,size,modifiedTime,createdTime,parents,driveId,"
    "webViewLink,md5Checksum"
)

# Google-native types cannot be downloaded raw; they are exported.
EXPORT = {
    "application/vnd.google-apps.document": ("application/pdf", ".pdf"),
    "application/vnd.google-apps.spreadsheet": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".xlsx",
    ),
    "application/vnd.google-apps.presentation": ("application/pdf", ".pdf"),
    "application/vnd.google-apps.drawing": ("application/pdf", ".pdf"),
}
TEXT_EXPORT = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
    "application/vnd.google-apps.presentation": "text/plain",
}
FOLDER = "application/vnd.google-apps.folder"


class DriveError(RuntimeError):
    pass


def _get(url: str, params: dict | None = None, raw: bool = False,
         retries: int = 5) -> dict | bytes:
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(
            {k: v for k, v in params.items() if v is not None}
        )
    delay = 1.0
    for attempt in range(retries):
        req = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {get_access_token()}"}
        )
        try:
            with urllib.request.urlopen(req, context=_SSL_CONTEXT, timeout=120) as r:
                body = r.read()
                return body if raw else json.loads(body)
        except urllib.error.HTTPError as e:
            text = e.read().decode(errors="replace")
            # Google reports quota as 403 or 429; transient failures as 5xx.
            transient = e.code in (429, 500, 502, 503, 504) or (
                e.code == 403 and "rate" in text.lower()
            )
            if transient and attempt < retries - 1:
                time.sleep(delay)
                delay = min(delay * 2, 30)
                continue
            raise DriveError(f"HTTP {e.code} for {url}: {text[:500]}") from None
    raise DriveError(f"gave up after {retries} attempts: {url}")


def _all_drives(**extra) -> dict:
    return {"supportsAllDrives": "true", "includeItemsFromAllDrives": "true", **extra}


def _paged(url: str, params: dict, key: str) -> list[dict]:
    out: list[dict] = []
    token = None
    while True:
        page = _get(url, {**params, "pageToken": token})
        out.extend(page.get(key, []))
        token = page.get("nextPageToken")
        if not token:
            return out


# ---------------------------------------------------------------- discovery

def whoami() -> str:
    return _get(f"{BASE}/about", {"fields": "user(emailAddress)"})["user"]["emailAddress"]


def shared_drives() -> list[dict]:
    """Every shared drive the authorised account is a member of."""
    return _paged(f"{BASE}/drives", {"pageSize": 100, "fields": "nextPageToken,drives(id,name)"},
                  "drives")


def get(file_id: str) -> dict:
    """Metadata for one file or folder (works on shared drives)."""
    return _get(f"{BASE}/files/{file_id}", _all_drives(fields=FILE_FIELDS))


def search(q: str, drive_id: str | None = None, page_size: int = 200,
           order_by: str = "modifiedTime desc") -> list[dict]:
    """Drive query-language search, e.g.
        "'<folder-id>' in parents and trashed = false"
        "name contains 'Invoice' and mimeType = 'application/pdf'"
    drive_id restricts to one shared drive; None searches everything visible.
    """
    params = _all_drives(
        q=q, pageSize=page_size, orderBy=order_by,
        fields=f"nextPageToken,files({FILE_FIELDS})",
    )
    if drive_id:
        params.update(corpora="drive", driveId=drive_id)
    else:
        params.update(corpora="allDrives")
    return _paged(f"{BASE}/files", params, "files")


def search_all(text: str, drive_id: str | None = None) -> list[dict]:
    """Free-text search across name and content, untrashed only."""
    safe = text.replace("\\", "\\\\").replace("'", "\\'")
    return search(f"fullText contains '{safe}' and trashed = false", drive_id)


def list_folder(folder_id: str, recursive: bool = False) -> list[dict]:
    """Children of a folder. recursive=True walks subfolders and adds a
    'path' key (relative to folder_id) to each entry."""
    items = search(f"'{folder_id}' in parents and trashed = false", order_by="name")
    if not recursive:
        return items
    out: list[dict] = []
    for it in items:
        it["path"] = it["name"]
        out.append(it)
        if it["mimeType"] == FOLDER:
            for child in list_folder(it["id"], recursive=True):
                child["path"] = f"{it['name']}/{child['path']}"
                out.append(child)
    return out


# ---------------------------------------------------------------- content

def _safe_name(name: str) -> str:
    return re.sub(r"[^\w.\- ()&,]+", "_", name).strip() or "file"


def download(file_id: str, dest_dir: str | Path = "data/drive",
             filename: str | None = None) -> Path:
    """Save a file locally and return its path. Binary files (PDF, xlsx,
    images) come down byte-for-byte; Google Docs/Sheets/Slides are exported
    (Docs and Slides to PDF, Sheets to xlsx)."""
    meta = get(file_id)
    mime = meta["mimeType"]
    if mime == FOLDER:
        raise DriveError(f"{file_id} is a folder; use list_folder()")
    name = _safe_name(filename or meta["name"])
    if mime in EXPORT:
        export_mime, ext = EXPORT[mime]
        if not name.lower().endswith(ext):
            name += ext
        body = _get(f"{BASE}/files/{file_id}/export", {"mimeType": export_mime}, raw=True)
    else:
        body = _get(f"{BASE}/files/{file_id}", _all_drives(alt="media"), raw=True)
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    path = dest / name
    path.write_bytes(body)
    return path


def read_bytes(file_id: str) -> bytes:
    """File content in memory (raw, or PDF/xlsx export for Google-native types)."""
    meta = get(file_id)
    mime = meta["mimeType"]
    if mime in EXPORT:
        return _get(f"{BASE}/files/{file_id}/export",
                    {"mimeType": EXPORT[mime][0]}, raw=True)
    return _get(f"{BASE}/files/{file_id}", _all_drives(alt="media"), raw=True)


def read_text(file_id: str) -> str:
    """Google Doc as plain text, Sheet as CSV (first tab), or a text file."""
    meta = get(file_id)
    mime = meta["mimeType"]
    if mime in TEXT_EXPORT:
        body = _get(f"{BASE}/files/{file_id}/export",
                    {"mimeType": TEXT_EXPORT[mime]}, raw=True)
    elif mime.startswith("text/") or mime in ("application/json", "application/csv"):
        body = _get(f"{BASE}/files/{file_id}", _all_drives(alt="media"), raw=True)
    else:
        raise DriveError(f"{meta['name']} is {mime}; use download() and parse it")
    return body.decode("utf-8", errors="replace")


# ---------------------------------------------------------------- publish
# The one write. An upload may land only in an allowlisted root, or in a
# folder this process created or resolved under one: the Drive publish folder
# from config/group.toml, plus any folder ids in DRIVE_WRITE_ALLOWLIST.
# Publishing is by id throughout, so renaming a folder on Drive never
# interrupts it; folder_name() reads the name back for a report line.
UPLOAD_BASE = "https://www.googleapis.com/upload/drive/v3"
SHORTCUT = "application/vnd.google-apps.shortcut"
WRITE_SCOPES = ("https://www.googleapis.com/auth/drive.file",
                "https://www.googleapis.com/auth/drive")


# The type an upload is served as. mimetypes knows all of these on a normal
# host, but its table is read from the system's mime.types, which a minimal
# host may not carry - and a workbook or a register uploaded as
# application/octet-stream is a file Drive will not preview. So the
# extensions this repo actually publishes are pinned here and mimetypes is
# only the fallback.
_MIME_BY_EXT = {
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xlsm": "application/vnd.ms-excel.sheet.macroEnabled.12",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".json": "application/json",
    ".csv": "text/csv",
    ".md": "text/markdown",
    ".txt": "text/plain",
    ".pdf": "application/pdf",
}


def mime_for(name: str | Path) -> str:
    """The content type an upload of `name` is sent with, from its extension."""
    ext = Path(name).suffix.lower()
    return (_MIME_BY_EXT.get(ext)
            or mimetypes.guess_type(str(name))[0]
            or "application/octet-stream")


def publish_root() -> str:
    """The Drive publish folder id from config/group.toml, or "" when unset."""
    try:
        from . import config
        return str(config.drive_settings().get("publish_folder_id", "") or "").strip()
    except Exception:  # noqa: BLE001 - no config means nothing to publish into
        return ""


def _extra_allowlist() -> str:
    extra = os.environ.get("DRIVE_WRITE_ALLOWLIST", "")
    if not extra:
        try:
            from .gdrive_auth import _read_env
            extra = _read_env().get("DRIVE_WRITE_ALLOWLIST", "")
        except OSError:
            extra = ""
    return extra


def publish_folders() -> set[str]:
    """Every root folder the agent may write into."""
    roots = {f.strip() for f in _extra_allowlist().split(",") if f.strip()}
    root = publish_root()
    if root:
        roots.add(root)
    return roots


def folder_name(folder_id: str) -> str:
    """The folder's name as Drive holds it now, for a report line. Falls back
    to the id, so a report is never lost to an unreachable Drive."""
    try:
        return get(folder_id)["name"]
    except (DriveError, KeyError):
        return folder_id


def _token_can_write() -> bool:
    try:
        scope = json.loads(TOKENS_PATH.read_text()).get("scope", "")
    except (OSError, ValueError):
        return False
    return any(s in scope.split() for s in WRITE_SCOPES)


def _send(method: str, url: str, params: dict, body: bytes,
          content_type: str, retries: int = 4) -> dict:
    """A non-GET request. Only upload_file calls it, and only after the
    read-only lock and the folder allowlist have been checked."""
    url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    delay = 1.0
    for attempt in range(retries):
        req = urllib.request.Request(
            url, data=body, method=method,
            headers={"Authorization": f"Bearer {get_access_token()}",
                     "Content-Type": content_type,
                     "Content-Length": str(len(body))},
        )
        try:
            with urllib.request.urlopen(req, context=_SSL_CONTEXT, timeout=300) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            text = e.read().decode(errors="replace")
            transient = e.code in (429, 500, 502, 503, 504)
            if transient and attempt < retries - 1:
                time.sleep(delay)
                delay = min(delay * 2, 30)
                continue
            raise DriveError(f"HTTP {e.code} for {method} {url}: {text[:500]}") from None
    raise DriveError(f"gave up after {retries} attempts: {method} {url}")


def find_in_folder(folder_id: str, name: str) -> list[dict]:
    safe = name.replace("\\", "\\\\").replace("'", "\\'")
    return search(f"'{folder_id}' in parents and name = '{safe}' and trashed = false")


# Folders the agent created (or resolved) under an allowlisted root this
# process; uploads into them are allowed because their root is.
_UNDER_ALLOWED: set[str] = set()


def _send_json(method: str, url: str, params: dict, obj: dict) -> dict:
    return _send(method, url, params, json.dumps(obj).encode(),
                 "application/json; charset=UTF-8")


def ensure_folder(path: list[str] | tuple[str, ...], root: str | None = None) -> str:
    """Id of the folder at root/path[0]/path[1]/..., creating what is
    missing (drive.file lets the agent create folders; it can then add files
    to them). An existing folder of that name is reused whoever made it."""
    readonly.refuse(f"Drive folder {'/'.join(path)}")
    root = root or publish_root()
    if not root or root not in publish_folders():
        raise DriveError(f"folder {root} is not on the publish allowlist")
    parent = root
    for name in path:
        safe = name.replace("\\", "\\\\").replace("'", "\\'")
        found = search(f"'{parent}' in parents and name = '{safe}' and "
                       f"mimeType = '{FOLDER}' and trashed = false", order_by="createdTime")
        if found:
            parent = found[0]["id"]
        else:
            meta = _send_json("POST", f"{BASE}/files",
                              {"supportsAllDrives": "true", "fields": FILE_FIELDS},
                              {"name": name, "mimeType": FOLDER, "parents": [parent]})
            parent = meta["id"]
        _UNDER_ALLOWED.add(parent)
    return parent


def ensure_shortcut(name: str, target_id: str, parent_id: str) -> dict:
    """A Drive shortcut called `name` in `parent_id` pointing at `target_id`,
    created if it is not there already. Idempotent: an existing shortcut of
    that name is returned untouched, whatever it points at, so a rerun never
    leaves two."""
    readonly.refuse(f"Drive shortcut {name}")
    if parent_id not in publish_folders() | _UNDER_ALLOWED:
        raise DriveError(f"folder {parent_id} is not on the publish allowlist")
    safe = name.replace("\\", "\\\\").replace("'", "\\'")
    found = search(f"'{parent_id}' in parents and name = '{safe}' and "
                   f"mimeType = '{SHORTCUT}' and trashed = false", order_by="createdTime")
    if found:
        found[0]["action"] = "exists"
        return found[0]
    meta = _send_json("POST", f"{BASE}/files",
                      {"supportsAllDrives": "true",
                       "fields": f"{FILE_FIELDS},shortcutDetails"},
                      {"name": name, "mimeType": SHORTCUT, "parents": [parent_id],
                       "shortcutDetails": {"targetId": target_id,
                                           "targetMimeType": FOLDER}})
    meta["action"] = "created"
    return meta


def rename_file(file_id: str, name: str) -> dict:
    """Rename a file the agent created, in place. The id does not change, so
    the Drive link, the sharing and the version history all hold - which is
    the point: a published workbook that is renamed keeps being the same
    file rather than becoming a second one beside the old name."""
    readonly.refuse(f"Drive rename of {file_id}")
    return _send_json("PATCH", f"{BASE}/files/{file_id}",
                      {"supportsAllDrives": "true", "fields": FILE_FIELDS},
                      {"name": name})


def move_file(file_id: str, to_folder: str, from_folder: str) -> dict:
    """Move a file the agent created into another folder under the root."""
    readonly.refuse(f"Drive move of {file_id}")
    if to_folder not in publish_folders() | _UNDER_ALLOWED:
        raise DriveError(f"folder {to_folder} is not under the publish allowlist")
    return _send_json("PATCH", f"{BASE}/files/{file_id}",
                      {"supportsAllDrives": "true", "addParents": to_folder,
                       "removeParents": from_folder, "fields": FILE_FIELDS}, {})


def upload_file(path: str | Path, folder_id: str | None = None,
                name: str | None = None) -> dict:
    """Publish a local file into an allowlisted Drive folder, overwriting the
    copy of the same name the agent published before (same file id, so links
    and version history hold). Returns the file's metadata plus 'action'
    ('updated' or 'created') and, when a same-named copy the token may not
    touch already sits in the folder (one uploaded by hand before the agent
    took over), 'legacy_copies' with their ids and links."""
    readonly.refuse(f"Drive upload of {path}")
    folder_id = folder_id or publish_root()
    if not folder_id:
        raise DriveError("no Drive publish folder: set [drive] publish_folder_id "
                         "in config/group.toml (docs/setup/GOOGLE_DRIVE.md)")
    folders = publish_folders() | _UNDER_ALLOWED
    if folder_id not in folders:
        raise DriveError(f"folder {folder_id} is not on the publish allowlist "
                         f"({', '.join(sorted(publish_folders()))}) or under it; "
                         f"resolve it with ensure_folder(), or add a root to "
                         f"DRIVE_WRITE_ALLOWLIST in .env")
    if not _token_can_write():
        raise DriveError("the Drive token is read-only (consented without "
                         "drive.file). Re-consent with "
                         ".venv/bin/python -m accounting_agent.gdrive_auth "
                         "and copy .gdrive/tokens.json to the server.")
    src = Path(path)
    data = src.read_bytes()
    name = name or src.name
    mime = mime_for(name)
    legacy: list[dict] = []
    for existing in sorted(find_in_folder(folder_id, name),
                           key=lambda f: f.get("modifiedTime", ""), reverse=True):
        try:
            meta = _send("PATCH", f"{UPLOAD_BASE}/files/{existing['id']}",
                         {"uploadType": "media", "supportsAllDrives": "true",
                          "fields": FILE_FIELDS}, data, mime)
            meta["action"] = "updated"
            meta["legacy_copies"] = legacy
            return meta
        except DriveError as e:
            if "HTTP 403" not in str(e) and "HTTP 404" not in str(e):
                raise
            # a copy the token did not create: drive.file cannot touch it
            legacy.append({"id": existing["id"], "webViewLink": existing.get("webViewLink"),
                           "modifiedTime": existing.get("modifiedTime")})
    boundary = f"agent-{uuid.uuid4().hex}"
    metadata = json.dumps({"name": name, "parents": [folder_id]}).encode()
    body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n".encode()
            + metadata
            + f"\r\n--{boundary}\r\nContent-Type: {mime}\r\n\r\n".encode()
            + data + f"\r\n--{boundary}--".encode())
    meta = _send("POST", f"{UPLOAD_BASE}/files",
                 {"uploadType": "multipart", "supportsAllDrives": "true",
                  "fields": FILE_FIELDS}, body,
                 f"multipart/related; boundary={boundary}")
    meta["action"] = "created"
    meta["legacy_copies"] = legacy
    return meta


# ---------------------------------------------------------------- CLI

def _fmt(f: dict) -> str:
    kind = "dir " if f["mimeType"] == FOLDER else "file"
    size = f.get("size", "")
    return f"{kind}  {f['id']}  {f.get('modifiedTime','')[:10]}  {size:>9}  {f.get('path') or f['name']}"


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd, *rest = argv
    if cmd == "whoami":
        print(whoami())
    elif cmd == "drives":
        for d in shared_drives():
            print(f"{d['id']}  {d['name']}")
    elif cmd == "ls":
        recursive = "-r" in rest
        ids = [a for a in rest if a != "-r"]
        for f in list_folder(ids[0], recursive=recursive):
            print(_fmt(f))
    elif cmd == "find":
        for f in search_all(" ".join(rest)):
            print(_fmt(f))
    elif cmd == "q":
        for f in search(" ".join(rest)):
            print(_fmt(f))
    elif cmd == "get":
        print(download(rest[0], rest[1] if len(rest) > 1 else "data/drive"))
    elif cmd == "cat":
        print(read_text(rest[0]))
    elif cmd == "put":
        meta = upload_file(rest[0], rest[1] if len(rest) > 1 else None)
        print(f"{meta['action']}  {meta['id']}  {meta.get('modifiedTime','')[:19]}  "
              f"{meta['name']}  {meta.get('webViewLink','')}")
        for lc in meta.get("legacy_copies", []):
            print(f"legacy copy not overwritable by this token (move or archive it): "
                  f"{lc['id']}  {lc.get('webViewLink','')}")
    else:
        print(f"unknown command {cmd!r}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
