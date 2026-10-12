"""Offline checks for the encrypted Xero token file (src/accounting_agent/xero_auth.py).

No Xero, no network, no .env: TOKEN_PATH is pointed at a temp dir and the key
is passed in, so the real .xero/ is never read or written. Run it after
touching xero_auth.py, on the laptop or the server:

    .venv/bin/python scripts/test_xero_token_store.py

Covers: a write and read round trip with nothing in plain text on disk and the
file at 0600, a plain-text file from before encryption rewritten encrypted on
first read, refusal without a key or with a malformed one, refusal with the
wrong key or an altered file, an atomic write that fails midway leaving the old
file intact and no temp file behind, expire_access_token, and --new-key.
"""
from __future__ import annotations

import base64
import contextlib
import io
import json
import os
import pathlib
import shutil
import stat
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from accounting_agent import xero_auth as XA  # noqa: E402

T = pathlib.Path(tempfile.mkdtemp(prefix="xero-tokens-"))
REAL_PATH = XA.TOKEN_PATH
XA.TOKEN_PATH = T / ".xero" / "tokens.json"
assert XA.TOKEN_PATH != REAL_PATH

failures: list[str] = []


def check(ok: bool, label: str, detail: object = "") -> None:
    print(("PASS " if ok else "FAIL ") + label + ("" if ok else f": {detail}"))
    if not ok:
        failures.append(label)


def refuses(fn) -> str | None:
    """The SystemExit message if fn refuses, else None."""
    try:
        fn()
    except SystemExit as e:
        return str(e)
    return None


def new_env() -> dict[str, str]:
    return {"XERO_TOKEN_KEY": base64.b64encode(os.urandom(32)).decode()}


def leftovers() -> list[str]:
    return sorted(p.name for p in XA.TOKEN_PATH.parent.iterdir() if p.name != "tokens.json")


TOKENS = {"access_token": "AT-secret-123", "refresh_token": "RT-secret-456",
          "expires_in": 1800, "obtained_at": 1_700_000_000}
ENV = new_env()

try:
    # 1. Round trip.
    XA._write_tokens(dict(TOKENS), ENV)
    raw = XA.TOKEN_PATH.read_text()
    check(XA._read_tokens(ENV) == TOKENS, "a written file reads back the same tokens")
    check("RT-secret-456" not in raw and "AT-secret-123" not in raw,
          "neither token appears in plain text on disk", raw[:120])
    check(json.loads(raw).get("scheme") == "aes-256-gcm", "the file is the encrypted envelope")
    check(stat.S_IMODE(XA.TOKEN_PATH.stat().st_mode) == 0o600, "the file is 0600",
          oct(XA.TOKEN_PATH.stat().st_mode))
    XA._write_tokens(dict(TOKENS), ENV)
    check(json.loads(XA.TOKEN_PATH.read_text())["nonce"] != json.loads(raw)["nonce"],
          "each write uses a fresh nonce")

    # 2. A plain-text file from before encryption is migrated on first read.
    XA.TOKEN_PATH.write_text(json.dumps(TOKENS, indent=2))
    check(XA._read_tokens(ENV) == TOKENS, "a plain-text file reads")
    raw = XA.TOKEN_PATH.read_text()
    check("RT-secret-456" not in raw and json.loads(raw).get("scheme") == "aes-256-gcm",
          "a plain-text file is rewritten encrypted on first read", raw[:120])
    check(XA._read_tokens(ENV) == TOKENS, "the migrated file reads back the same tokens")

    # 3. No key, or a malformed one: every read and write refuses.
    msg = refuses(lambda: XA._read_tokens({}))
    check(msg is not None and "XERO_TOKEN_KEY" in msg, "a read without the key refuses", msg)
    msg = refuses(lambda: XA._write_tokens(dict(TOKENS), {}))
    check(msg is not None and "XERO_TOKEN_KEY" in msg, "a write without the key refuses", msg)
    for bad in ("", "not base64!", base64.b64encode(os.urandom(16)).decode()):
        check(refuses(lambda: XA._read_tokens({"XERO_TOKEN_KEY": bad})) is not None,
              f"a malformed key refuses ({len(bad)} chars)")
    XA.TOKEN_PATH.write_text(json.dumps(TOKENS))
    check(refuses(lambda: XA._read_tokens({})) is not None
          and "scheme" not in XA.TOKEN_PATH.read_text()
          and json.loads(XA.TOKEN_PATH.read_text()) == TOKENS,
          "a plain-text file read without the key refuses and is left as it was")
    XA._write_tokens(dict(TOKENS), ENV)

    # 4. Wrong key, or an altered file.
    msg = refuses(lambda: XA._read_tokens(new_env()))
    check(msg is not None and "Cannot decrypt" in msg, "the wrong key refuses", msg)
    env_blob = json.loads(XA.TOKEN_PATH.read_text())
    ct = bytearray(base64.b64decode(env_blob["ciphertext"]))
    ct[0] ^= 1
    env_blob["ciphertext"] = base64.b64encode(bytes(ct)).decode()
    XA.TOKEN_PATH.write_text(json.dumps(env_blob))
    msg = refuses(lambda: XA._read_tokens(ENV))
    check(msg is not None and "Cannot decrypt" in msg, "an altered file refuses", msg)
    XA.TOKEN_PATH.write_text(json.dumps({"scheme": "rot13", "data": "x"}))
    msg = refuses(lambda: XA._read_tokens(ENV))
    check(msg is not None and "unknown format" in msg, "an unknown envelope refuses", msg)

    # 5. A write that fails midway leaves the old file whole and no temp file.
    XA._write_tokens(dict(TOKENS), ENV)
    before = XA.TOKEN_PATH.read_bytes()
    real_fsync = XA.os.fsync

    def boom(fd: int) -> None:
        raise OSError("disk gone")

    XA.os.fsync = boom
    try:
        XA._write_tokens(dict(TOKENS, refresh_token="RT-new"), ENV)
        crashed = False
    except OSError:
        crashed = True
    finally:
        XA.os.fsync = real_fsync
    check(crashed, "the failing write raises")
    check(XA.TOKEN_PATH.read_bytes() == before and XA._read_tokens(ENV) == TOKENS,
          "the old file is untouched after a failed write")
    check(leftovers() == [], "no temp file is left behind", leftovers())

    # 6. expire_access_token keeps the refresh token and forces a refresh.
    XA.expire_access_token(ENV)
    after = XA._read_tokens(ENV)
    check(after["obtained_at"] == 0 and after["refresh_token"] == "RT-secret-456",
          "expire_access_token zeroes obtained_at and keeps the refresh token", after)
    check("RT-secret-456" not in XA.TOKEN_PATH.read_text(), "the expired file is still encrypted")

    # 7. A missing file names the bootstrap.
    XA.TOKEN_PATH.unlink()
    msg = refuses(lambda: XA._read_tokens(ENV))
    check(msg is not None and "No stored tokens" in msg, "a missing file refuses with the bootstrap", msg)

    # 8. --new-key prints a usable key.
    out = io.StringIO()
    argv = sys.argv
    sys.argv = ["xero_auth", "--new-key"]
    try:
        with contextlib.redirect_stdout(out):
            XA.main()
    finally:
        sys.argv = argv
    line = out.getvalue().strip()
    key = line.partition("=")[2]
    check(line.startswith("XERO_TOKEN_KEY=") and len(base64.b64decode(key)) == 32,
          "--new-key prints a 32-byte base64 key", line)
    XA._write_tokens(dict(TOKENS), {"XERO_TOKEN_KEY": key})
    check(XA._read_tokens({"XERO_TOKEN_KEY": key}) == TOKENS, "the printed key works")
finally:
    XA.TOKEN_PATH = REAL_PATH
    shutil.rmtree(T, ignore_errors=True)

if failures:
    print(f"\n{len(failures)} token store check(s) failed")
    sys.exit(1)
print("\nall token store checks passed")
