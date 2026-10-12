"""Gmail OAuth bootstrap + token management for the accounting inbox.

The mailbox is config/group.toml [company] accounting_inbox. Mirrors
xero_auth.py: run `python -m accounting_agent.gmail_auth` once to consent
(signed in as that mailbox), then use get_access_token() everywhere. The
OAuth client is GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET in .env (a Desktop app
client in Google Cloud); the consent redirect is http://localhost:8401/callback.

Scope is gmail.modify: read messages and attachments, and add or remove
labels (the processed label marks an email once its invoice is in Xero). It
cannot permanently delete mail. Tokens live in .gmail/tokens.json
(git-ignored, chmod 600). Google refresh tokens for Internal apps do not
expire on a timer; they die only if revoked or unused for about 6 months.
Setup guide: docs/setup/GMAIL.md.
"""

from __future__ import annotations

import json
import os
import ssl
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import certifi

_SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())

REPO_ROOT = Path(__file__).resolve().parents[2]
TOKENS_PATH = REPO_ROOT / ".gmail" / "tokens.json"

SCOPES = "https://www.googleapis.com/auth/gmail.modify"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
PORT = 8401
REDIRECT_URI = f"http://localhost:{PORT}/callback"


def login_hint() -> str:
    """The accounting inbox from config/group.toml, or "" when not set."""
    try:
        from . import config
        return config.company("accounting_inbox", "") or ""
    except Exception:  # noqa: BLE001 - a missing config must not block consent
        return ""


def _load_env() -> tuple[str, str]:
    env: dict[str, str] = {}
    for line in (REPO_ROOT / ".env").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    try:
        return env["GMAIL_CLIENT_ID"], env["GMAIL_CLIENT_SECRET"]
    except KeyError as e:
        raise SystemExit(f"Missing {e} in .env (docs/setup/GMAIL.md)") from None


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
    """One-time consent flow. Approve in the browser signed in as the inbox."""
    client_id, client_secret = _load_env()
    _kill_stale_listener()

    params = {
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPES,
        "access_type": "offline",
        "prompt": "consent",
    }
    hint = login_hint()
    if hint:
        params["login_hint"] = hint
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

    print(f"Open this URL and approve, SIGNED IN AS {hint or 'the accounting inbox'}:\n")
    print(url + "\n")
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

    # verify: whoami
    profile = _api_get("https://gmail.googleapis.com/gmail/v1/users/me/profile",
                       tokens["access_token"])
    print(f"Authorised mailbox: {profile.get('emailAddress')} "
          f"({profile.get('messagesTotal')} messages)")
    if hint and profile.get("emailAddress", "").lower() != hint.lower():
        print(f"WARNING: config/group.toml names {hint} as the accounting inbox, "
              "but the token is for the mailbox above. Re-run signed in as the inbox.")


def _api_get(url: str, access_token: str) -> dict:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {access_token}"})
    with urllib.request.urlopen(req, context=_SSL_CONTEXT) as r:
        return json.load(r)


def get_access_token() -> str:
    """Return a valid access token, refreshing if expired/near-expiry."""
    if not TOKENS_PATH.exists():
        raise SystemExit("No Gmail tokens - run: python -m accounting_agent.gmail_auth")
    tokens = json.loads(TOKENS_PATH.read_text())
    age = time.time() - tokens.get("obtained_at", 0)
    if age < tokens.get("expires_in", 3600) - 120:
        return tokens["access_token"]
    client_id, client_secret = _load_env()
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
