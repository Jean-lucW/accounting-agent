"""Google Drive OAuth bootstrap + token management (shared drives included).

Mirrors gmail_auth.py and reuses the same Google Cloud OAuth client
(GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET in .env, or GDRIVE_CLIENT_ID /
GDRIVE_CLIENT_SECRET to use a separate one). Run once:

    .venv/bin/python -m accounting_agent.gdrive_auth

and approve in the browser signed in as an account that is a MEMBER of the
shared drives the agent must read (default hint: the accounting inbox from
config/group.toml; override with GDRIVE_LOGIN_HINT in .env). The consent
redirect is http://localhost:8402/callback. Tokens live in .gdrive/tokens.json
(git-ignored, chmod 600). Google does not rotate the refresh token, so the
same file can be copied to the server and works in both places.

Scopes are drive.readonly plus drive.file: the agent reads every document the
account can see, and the ONLY things it can write are files and folders it
created itself, the workbooks it publishes into the Drive publish folder
(config/group.toml [drive] publish_folder_id) after each build
(gdrive.upload_file). It cannot edit, move or delete anything else on Drive.
A token consented with drive.readonly alone cannot publish: re-run this
bootstrap. Setup guide: docs/setup/GOOGLE_DRIVE.md.
"""

from __future__ import annotations

import json
import os
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import certifi

_SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())

REPO_ROOT = Path(__file__).resolve().parents[2]
TOKENS_PATH = REPO_ROOT / ".gdrive" / "tokens.json"

SCOPES = ("https://www.googleapis.com/auth/drive.readonly "
          "https://www.googleapis.com/auth/drive.file")
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
PORT = 8402
REDIRECT_URI = f"http://localhost:{PORT}/callback"


def default_login_hint() -> str:
    """The accounting inbox from config/group.toml, or "" when not set."""
    try:
        from . import config
        return config.company("accounting_inbox", "") or ""
    except Exception:  # noqa: BLE001 - a missing config must not block consent
        return ""


def _read_env() -> dict[str, str]:
    env: dict[str, str] = {}
    for line in (REPO_ROOT / ".env").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def _load_client() -> tuple[str, str]:
    env = _read_env()
    cid = env.get("GDRIVE_CLIENT_ID") or env.get("GMAIL_CLIENT_ID")
    secret = env.get("GDRIVE_CLIENT_SECRET") or env.get("GMAIL_CLIENT_SECRET")
    if not cid or not secret:
        raise SystemExit("Missing GMAIL_CLIENT_ID/GMAIL_CLIENT_SECRET (or GDRIVE_*) in .env")
    return cid, secret


def _post_token(data: dict) -> dict:
    req = urllib.request.Request(
        TOKEN_URL,
        data=urllib.parse.urlencode(data).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, context=_SSL_CONTEXT) as r:
        return json.load(r)


def _save_tokens(tokens: dict) -> None:
    tokens["obtained_at"] = time.time()
    TOKENS_PATH.parent.mkdir(exist_ok=True)
    TOKENS_PATH.write_text(json.dumps(tokens, indent=2))
    TOKENS_PATH.chmod(0o600)


def _kill_stale_listener() -> None:
    try:
        out = subprocess.run(
            ["lsof", "-ti", f"tcp:{PORT}"], capture_output=True, text=True
        ).stdout.split()
        for pid in out:
            subprocess.run(["kill", pid], capture_output=True)
        if out:
            time.sleep(1)
    except OSError:
        pass


def bootstrap() -> None:
    """One-time consent flow. Approve as a member of the shared drives."""
    client_id, client_secret = _load_client()
    login_hint = os.environ.get("GDRIVE_LOGIN_HINT") or _read_env().get(
        "GDRIVE_LOGIN_HINT", ""
    ) or default_login_hint()
    _kill_stale_listener()

    params = {
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPES,
        "access_type": "offline",
        "prompt": "consent",
    }
    if login_hint:
        params["login_hint"] = login_hint
    url = f"{AUTH_URL}?{urllib.parse.urlencode(params)}"

    code_holder: dict[str, str] = {}
    done = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if "code" in q:
                code_holder["code"] = q["code"][0]
                body = b"<h2>Authorised. You can close this tab.</h2>"
            else:
                body = f"<h2>Error: {q.get('error', ['unknown'])[0]}</h2>".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body)
            done.set()

        def log_message(self, *a):  # silence
            pass

    server = HTTPServer(("localhost", PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    print(f"Open this URL and approve, signed in as {login_hint or 'the accounting account'} "
          "(or any account that is a member of the shared drives):\n")
    print(url + "\n", flush=True)
    webbrowser.open(url)

    if not done.wait(timeout=1800):
        raise SystemExit("Timed out waiting for consent (30 min).")
    server.shutdown()
    if "code" not in code_holder:
        raise SystemExit("Consent failed or was denied.")

    tokens = _post_token(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code_holder["code"],
            "grant_type": "authorization_code",
            "redirect_uri": REDIRECT_URI,
        }
    )
    if "refresh_token" not in tokens:
        raise SystemExit(f"No refresh token in response: {tokens}")
    _save_tokens(tokens)
    print(f"Tokens saved to {TOKENS_PATH}")

    # verify: whoami + which shared drives are visible. The token is already
    # saved; a 403 here almost always means the Drive API is not enabled on
    # the Google Cloud project that owns the OAuth client.
    try:
        _verify(tokens["access_token"])
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        if e.code == 403 and "has not been used in project" in body:
            print("Token saved, but the Google Drive API is disabled on this "
                  "project. Enable it (link below), wait a minute, then run:\n"
                  "  .venv/bin/python -m accounting_agent.gdrive whoami")
            print("  " + body.split("visiting ")[1].split(" then")[0])
            return
        raise


def _verify(access_token: str) -> None:
    about = _api_get(
        "https://www.googleapis.com/drive/v3/about?fields=user(emailAddress)",
        access_token,
    )
    print(f"Authorised as: {about.get('user', {}).get('emailAddress')}")
    drives = _api_get(
        "https://www.googleapis.com/drive/v3/drives?pageSize=100&fields=drives(id,name)",
        access_token,
    ).get("drives", [])
    if drives:
        print("Shared drives visible:")
        for d in drives:
            print(f"  {d['name']}  {d['id']}")
    else:
        print("No shared drives visible to this account. Add it as a member "
              "of each shared drive, or re-run signed in as a member.")


def _api_get(url: str, access_token: str) -> dict:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {access_token}"})
    with urllib.request.urlopen(req, context=_SSL_CONTEXT) as r:
        return json.load(r)


def get_access_token() -> str:
    """Return a valid access token, refreshing if expired/near-expiry."""
    if not TOKENS_PATH.exists():
        raise SystemExit(
            "No Google Drive tokens - run: python -m accounting_agent.gdrive_auth"
        )
    tokens = json.loads(TOKENS_PATH.read_text())
    age = time.time() - tokens.get("obtained_at", 0)
    if age < tokens.get("expires_in", 3600) - 120:
        return tokens["access_token"]
    client_id, client_secret = _load_client()
    fresh = _post_token(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": tokens["refresh_token"],
            "grant_type": "refresh_token",
        }
    )
    fresh["refresh_token"] = tokens["refresh_token"]  # Google doesn't rotate it
    _save_tokens(fresh)
    return fresh["access_token"]


if __name__ == "__main__":
    import sys
    if any(a in ("-h", "--help") for a in sys.argv[1:]):
        print(__doc__)
    else:
        bootstrap()
