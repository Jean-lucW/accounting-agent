"""Xero OAuth 2.0 bootstrap and token management.

Run once interactively to authorise the app against all group entities:

    python -m accounting_agent.xero_auth

Opens the Xero consent page in a browser; tick every organisation you want
the agent to manage (re-run any time to add more). Tokens are stored in
.xero/tokens.json (git-ignored, chmod 600). The refresh token rotates on every
use and expires after 60 days of disuse, so any regular agent run keeps it
alive. Because it rotates, only ONE machine may hold and use the token file:
two copies refreshing independently knock each other out.

The redirect URI comes from XERO_REDIRECT_URI in .env (for example
http://localhost:8400/callback) and must match the app's redirect URI on
developer.xero.com exactly; this script listens on that URI's port.
Setup guide: docs/setup/XERO.md.

Stdlib only, no third-party dependencies.
"""

from __future__ import annotations

import functools
import base64
import json
import os
import secrets
import signal
import ssl
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

AUTHORIZE_URL = "https://login.xero.com/identity/connect/authorize"
TOKEN_URL = "https://identity.xero.com/connect/token"
CONNECTIONS_URL = "https://api.xero.com/connections"

# Apps created on/after 2026-03-02 must use granular scopes; the broad
# accounting.transactions and accounting.reports.read are rejected outright.
SCOPES = (
    "offline_access openid profile email "
    "accounting.invoices accounting.payments accounting.banktransactions "
    "accounting.manualjournals "
    "accounting.contacts accounting.settings accounting.attachments "
    "assets "  # Fixed Assets API: asset types and draft asset registration
    # accounting.journals.read is rejected for apps created after 2026-03-02
    # (premium scope, requires Xero approval); GL review falls back on
    # reports + per-endpoint reads until/unless Xero grants it.
    "accounting.budgets.read "
    "accounting.reports.aged.read accounting.reports.balancesheet.read "
    "accounting.reports.banksummary.read accounting.reports.executivesummary.read "
    "accounting.reports.profitandloss.read accounting.reports.trialbalance.read "
    "accounting.reports.taxreports.read"
)

print = functools.partial(print, flush=True)

# macOS framework Pythons often lack system root certificates; prefer certifi.
try:
    import certifi

    _SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CONTEXT = ssl.create_default_context()


def _urlopen(request: urllib.request.Request):
    return urllib.request.urlopen(request, context=_SSL_CONTEXT)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TOKEN_PATH = PROJECT_ROOT / ".xero" / "tokens.json"


def _load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    env_file = PROJECT_ROOT / ".env"
    if not env_file.exists():
        raise SystemExit(f"Missing {env_file}: copy .env.example and fill in Xero credentials.")
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip()
    for required in ("XERO_CLIENT_ID", "XERO_CLIENT_SECRET", "XERO_REDIRECT_URI"):
        if not env.get(required):
            raise SystemExit(f"{required} is not set in .env")
    return env


def _basic_auth_header(env: dict[str, str]) -> str:
    raw = f"{env['XERO_CLIENT_ID']}:{env['XERO_CLIENT_SECRET']}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def _post_token(env: dict[str, str], form: dict[str, str]) -> dict:
    request = urllib.request.Request(
        TOKEN_URL,
        data=urllib.parse.urlencode(form).encode(),
        headers={
            "Authorization": _basic_auth_header(env),
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    with _urlopen(request) as response:
        return json.load(response)


def _save_tokens(tokens: dict) -> None:
    tokens["obtained_at"] = int(time.time())
    TOKEN_PATH.parent.mkdir(exist_ok=True)
    TOKEN_PATH.write_text(json.dumps(tokens, indent=2))
    TOKEN_PATH.chmod(0o600)


def get_access_token(env: dict[str, str] | None = None) -> str:
    """Return a valid access token, refreshing (and rotating) if expired."""
    env = env or _load_env()
    if not TOKEN_PATH.exists():
        raise SystemExit("No stored tokens. Run: python -m accounting_agent.xero_auth")
    tokens = json.loads(TOKEN_PATH.read_text())
    # Access tokens last 30 minutes; refresh with a 60-second safety margin.
    if time.time() < tokens["obtained_at"] + tokens["expires_in"] - 60:
        return tokens["access_token"]
    tokens = _post_token(
        env,
        {"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]},
    )
    _save_tokens(tokens)
    return tokens["access_token"]


def list_connections(access_token: str) -> list[dict]:
    """Return all organisations (tenants) this app is authorised against."""
    request = urllib.request.Request(
        CONNECTIONS_URL,
        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
    )
    with _urlopen(request) as response:
        return json.load(response)


def _run_consent_flow(env: dict[str, str]) -> str:
    """Open the Xero consent page and capture the auth code via localhost."""
    state = secrets.token_urlsafe(24)
    port = urllib.parse.urlparse(env["XERO_REDIRECT_URI"]).port or 80
    result: dict[str, str] = {}
    done = threading.Event()

    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 (http.server API)
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if params.get("state", [""])[0] != state:
                self.send_error(400, "State mismatch, restart the flow.")
                return
            if "error" in params:
                result["error"] = params["error"][0]
            else:
                result["code"] = params.get("code", [""])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h2>Xero authorisation received. You can close this tab.</h2>")
            done.set()

        def log_message(self, *args: object) -> None:
            pass

    consent_url = AUTHORIZE_URL + "?" + urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": env["XERO_CLIENT_ID"],
            "redirect_uri": env["XERO_REDIRECT_URI"],
            "scope": SCOPES,
            "state": state,
        }
    )
    try:
        server = HTTPServer(("localhost", port), CallbackHandler)
    except OSError:
        # A previous run of this script is still waiting on the port, replace it.
        stale = subprocess.run(
            ["lsof", "-ti", f"tcp:{port}"], capture_output=True, text=True
        ).stdout.split()
        for pid in stale:
            if int(pid) != os.getpid():
                print(f"Stopping stale auth flow (pid {pid}) holding port {port}.")
                os.kill(int(pid), signal.SIGTERM)
        time.sleep(1)
        server = HTTPServer(("localhost", port), CallbackHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print("Opening Xero consent page: log in and tick EVERY entity to manage.")
    print(f"If the browser doesn't open, visit:\n\n{consent_url}\n")
    webbrowser.open(consent_url)
    # Generous window: the URL stays valid while this run waits, and Xero's
    # 5-minute auth-code clock only starts once the user clicks Allow.
    if not done.wait(timeout=1800):
        raise SystemExit("Timed out waiting for the Xero callback.")
    server.shutdown()
    if "error" in result:
        raise SystemExit(f"Xero returned an error: {result['error']}")
    return result["code"]


def main() -> None:
    import sys
    if any(a in ("-h", "--help") for a in sys.argv[1:]):
        print(__doc__)
        return
    env = _load_env()
    code = _run_consent_flow(env)
    tokens = _post_token(
        env,
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": env["XERO_REDIRECT_URI"],
        },
    )
    _save_tokens(tokens)
    print(f"Tokens saved to {TOKEN_PATH.relative_to(PROJECT_ROOT)}")

    connections = list_connections(tokens["access_token"])
    print(f"\nAuthorised organisations ({len(connections)}):")
    for connection in connections:
        print(f"  {connection['tenantName']}: {connection['tenantId']}")
    print("\nRe-run this command any time to add more entities.")


if __name__ == "__main__":
    main()
