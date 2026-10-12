"""Revolut Business API OAuth 2.0 bootstrap and token management.

Revolut does not use a client secret. Every token call is authenticated by a
JWT "client assertion" signed with the RSA private key whose X509 certificate
was uploaded to the Revolut Business API settings page. Holding that key IS
holding the credential: there is nothing else to steal.

Run once per Revolut Business account to authorise, naming its slug (the
`slug` of a provider = "revolut" bank account in config/group.toml):

    python -m accounting_agent.revolut_auth holdco

It prints the consent URL, you approve it in the browser as a Revolut Business
admin, Revolut redirects to the app's OAuth redirect URI with ?code=... in the
query string, and you paste that code back. Tokens land in
.revolut/tokens.json (git-ignored, chmod 600).

Config lives in .env, one block per account, keyed by the slug in upper case:

    REVOLUT_HOLDCO_CLIENT_ID=...
    REVOLUT_HOLDCO_ISSUER=example.com          # the "iss" claim = your domain
    REVOLUT_HOLDCO_REDIRECT_URI=https://example.com/
    REVOLUT_HOLDCO_PRIVATE_KEY=.revolut/holdco-privatecert.pem
    # or REVOLUT_HOLDCO_PRIVATE_KEY_B64=<base64 of the PEM, one line>
    # optional: REVOLUT_HOLDCO_PUBLIC_KEY_B64, REVOLUT_HOLDCO_SANDBOX=1

Token lifetimes: the access token lasts 40 minutes and refreshing it
immediately invalidates the previous one, so never run two agents against the
same stored token. The refresh token does NOT rotate (unlike Xero) and stays
valid until the X509 certificate expires.

Scope: this module requests READ only. See revolut/client.py, which refuses
any non-GET request at the transport layer.

Stdlib only apart from `cryptography` for the RS256 signature, imported only
when a key is actually loaded, so the module (and mercury/, which borrows its
.env loader) imports without it. Setup guide: docs/setup/REVOLUT.md.
"""

from __future__ import annotations

import base64
import binascii
import functools
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

print = functools.partial(print, flush=True)

API_BASE = "https://b2b.revolut.com/api/1.0"
SANDBOX_API_BASE = "https://sandbox-b2b.revolut.com/api/1.0"
CONSENT_URL = "https://business.revolut.com/app-confirm"
SANDBOX_CONSENT_URL = "https://sandbox-business.revolut.com/app-confirm"

# Revolut requires this exact audience regardless of environment.
JWT_AUDIENCE = "https://revolut.com"
ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"

# READ alone. WRITE and PAY are deliberately never requested: this integration
# exists to read the bank feed, and the rules in CLAUDE.md mean the agent must
# never move money or mark anything paid.
SCOPES = "READ"

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TOKEN_PATH = PROJECT_ROOT / ".revolut" / "tokens.json"

try:
    import certifi

    _SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CONTEXT = ssl.create_default_context()


class RevolutAuthError(Exception):
    """Consent, token exchange or refresh failed."""


@dataclass(frozen=True)
class EntityConfig:
    """One Revolut Business account's API credential.

    The signing key arrives either as PEM bytes carried in .env (base64, so it
    survives being a single line) or as a path to a file on disk. Exactly one
    of `private_key_pem` / `private_key_path` is set; `key_source` names which,
    for error messages.
    """

    slug: str
    client_id: str
    issuer: str
    redirect_uri: str
    private_key_pem: bytes | None = None
    private_key_path: Path | None = None
    public_key_pem: bytes | None = None
    key_source: str = ""
    sandbox: bool = False

    @property
    def api_base(self) -> str:
        return SANDBOX_API_BASE if self.sandbox else API_BASE

    @property
    def consent_base(self) -> str:
        return SANDBOX_CONSENT_URL if self.sandbox else CONSENT_URL


def _load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    env_file = PROJECT_ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                env[key.strip()] = value.strip().strip('"').strip("'")
    env.update({k: v for k, v in os.environ.items() if k.startswith("REVOLUT_")})
    return env


def _decode_key_material(value: str) -> bytes:
    """Accept a PEM pasted raw, base64-wrapped, or with literal \\n escapes.

    A PEM is multi-line and .env is not, so the usual fix is to base64 the
    whole PEM into one line. Some tools instead escape the newlines. Both
    arrive here as a single string, so sniff rather than insist.
    """
    text = value.strip().strip('"').strip("'")
    if "-----BEGIN" in text:
        return text.replace("\\n", "\n").encode()
    try:
        decoded = base64.b64decode(text, validate=True)
    except (ValueError, binascii.Error) as e:
        raise RevolutAuthError(
            "Key material is neither a PEM nor valid base64. Produce it with:\n"
            "  base64 -i privatecert.pem | tr -d '\\n'"
        ) from e
    if b"-----BEGIN" not in decoded:
        # Base64 of DER rather than of a PEM, re-wrap it as a PEM.
        body = base64.encodebytes(decoded).decode().strip()
        return f"-----BEGIN PRIVATE KEY-----\n{body}\n-----END PRIVATE KEY-----\n".encode()
    return decoded


def config(slug: str) -> EntityConfig:
    """Read one entity's Revolut credential block out of .env.

    Every setting may be named per-entity (REVOLUT_HOLDCO_ISSUER) or bare
    (REVOLUT_ISSUER); the per-entity name wins. The bare form is the shorthand
    for a single-entity setup and stops working sensibly the moment a second
    Revolut Business account is added, so prefer the per-entity spelling.
    """
    env = _load_env()
    key = slug.upper().replace("-", "_")

    def get(suffix: str) -> str:
        return env.get(f"REVOLUT_{key}_{suffix}", "") or env.get(f"REVOLUT_{suffix}", "")

    def need(suffix: str) -> str:
        value = get(suffix)
        if not value:
            raise RevolutAuthError(
                f"Missing REVOLUT_{key}_{suffix} in .env, see .env.example for "
                f"the block this entity needs."
            )
        return value

    # Key material in .env wins over a path, so a deployment that ships the key
    # as an environment variable needs no file on the server at all.
    pem = get("PRIVATE_KEY_B64")
    key_pem: bytes | None = None
    key_path: Path | None = None
    if pem:
        key_pem = _decode_key_material(pem)
        source = f"REVOLUT_{key}_PRIVATE_KEY_B64" if env.get(
            f"REVOLUT_{key}_PRIVATE_KEY_B64") else "REVOLUT_PRIVATE_KEY_B64"
    else:
        key_path = Path(need("PRIVATE_KEY")).expanduser()
        if not key_path.is_absolute():
            key_path = PROJECT_ROOT / key_path
        source = str(key_path)

    public = get("PUBLIC_KEY_B64")
    return EntityConfig(
        slug=slug.lower(),
        client_id=need("CLIENT_ID"),
        issuer=need("ISSUER"),
        redirect_uri=need("REDIRECT_URI"),
        private_key_pem=key_pem,
        private_key_path=key_path,
        public_key_pem=_decode_key_material(public) if public else None,
        key_source=source,
        sandbox=get("SANDBOX").lower() in ("1", "true", "yes"),
    )


def config_slugs() -> list[str]:
    """Slugs of the provider = "revolut" bank accounts in config/group.toml."""
    try:
        from . import config as group
        return sorted({b.slug.lower() for b in group.bank_accounts()
                       if b.provider == "revolut" and b.slug})
    except Exception:  # noqa: BLE001 - no config: fall back to .env alone
        return []


def configured_entities() -> list[str]:
    """Entity slugs that have a REVOLUT_<SLUG>_CLIENT_ID in .env."""
    slugs = []
    for name in _load_env():
        if name.startswith("REVOLUT_") and name.endswith("_CLIENT_ID"):
            slugs.append(name[len("REVOLUT_") : -len("_CLIENT_ID")].lower())
    return sorted(slugs)


def _b64url(raw: bytes) -> bytes:
    return base64.urlsafe_b64encode(raw).rstrip(b"=")


def load_private_key(cfg: EntityConfig):
    """The RSA signing key, from .env key material or from a file on disk."""
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
    except ImportError:
        raise RevolutAuthError(
            "The `cryptography` package is needed to sign Revolut assertions: "
            ".venv/bin/pip install cryptography"
        ) from None
    if cfg.private_key_pem is not None:
        pem = cfg.private_key_pem
    else:
        path = cfg.private_key_path
        if path is None or not path.exists():
            raise RevolutAuthError(
                f"Private key not found at {path}. This is the key whose X509 certificate "
                f"is uploaded in Revolut Business > Settings > APIs. Either point "
                f"REVOLUT_{cfg.slug.upper()}_PRIVATE_KEY at it, or carry it in .env as "
                f"REVOLUT_{cfg.slug.upper()}_PRIVATE_KEY_B64."
            )
        if path.stat().st_mode & 0o077:
            print(f"warning: {path} is readable beyond its owner, chmod 600 it.")
        pem = path.read_bytes()
    try:
        key = serialization.load_pem_private_key(pem, password=None)
    except Exception as e:
        raise RevolutAuthError(f"{cfg.key_source} did not parse as a private key: {e}") from None
    if not isinstance(key, rsa.RSAPrivateKey):
        raise RevolutAuthError(f"{cfg.key_source} is not an RSA key; Revolut signs RS256.")
    return key


def keypair_matches(cfg: EntityConfig) -> bool | None:
    """Does the configured public key belong to the configured private key?

    None when no public key is configured, that is unknown, not a mismatch.
    """
    if cfg.public_key_pem is None:
        return None
    from cryptography.hazmat.primitives import serialization
    blob = cfg.public_key_pem
    if b"BEGIN CERTIFICATE" in blob:
        from cryptography import x509

        public = x509.load_pem_x509_certificate(blob).public_key()
    else:
        public = serialization.load_pem_public_key(blob)
    return public.public_numbers() == load_private_key(cfg).public_key().public_numbers()


def client_assertion(cfg: EntityConfig, ttl: int = 3600) -> str:
    """Sign the short-lived JWT that authenticates every token call.

    iss must match the domain registered against the certificate, sub is the
    client ID, aud is always https://revolut.com.
    """
    now = int(time.time())
    header = {"alg": "RS256", "typ": "JWT"}
    payload = {
        "iss": cfg.issuer,
        "sub": cfg.client_id,
        "aud": JWT_AUDIENCE,
        "iat": now,
        "exp": now + ttl,
    }
    segments = [
        _b64url(json.dumps(header, separators=(",", ":")).encode()),
        _b64url(json.dumps(payload, separators=(",", ":")).encode()),
    ]
    signing_input = b".".join(segments)
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    signature = load_private_key(cfg).sign(
        signing_input, padding.PKCS1v15(), hashes.SHA256()
    )
    return (signing_input + b"." + _b64url(signature)).decode()


def consent_url(cfg: EntityConfig, state: str = "") -> str:
    """The page a Revolut Business admin must approve to authorise the app."""
    params = {
        "client_id": cfg.client_id,
        "redirect_uri": cfg.redirect_uri,
        "response_type": "code",
        "scope": SCOPES,
    }
    if state:
        params["state"] = state
    return f"{cfg.consent_base}?{urllib.parse.urlencode(params)}"


def _token_request(cfg: EntityConfig, form: dict[str, str]) -> dict:
    body = urllib.parse.urlencode(
        {
            **form,
            "client_id": cfg.client_id,
            "client_assertion_type": ASSERTION_TYPE,
            "client_assertion": client_assertion(cfg),
        }
    ).encode()
    request = urllib.request.Request(
        f"{cfg.api_base}/auth/token",
        data=body,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(request, context=_SSL_CONTEXT) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        raise RevolutAuthError(
            f"{form.get('grant_type')} failed: HTTP {e.code} {detail}"
        ) from None


def _read_store() -> dict:
    if not TOKEN_PATH.exists():
        return {}
    return json.loads(TOKEN_PATH.read_text())


def _write_store(store: dict) -> None:
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_PATH.write_text(json.dumps(store, indent=2))
    TOKEN_PATH.chmod(0o600)


def _save(slug: str, payload: dict, *, keep_refresh: str = "") -> dict:
    store = _read_store()
    record = {
        "access_token": payload["access_token"],
        # 40 minutes in practice; trust the server and keep 60s of headroom.
        "expires_at": time.time() + int(payload.get("expires_in", 2400)) - 60,
        "refresh_token": payload.get("refresh_token") or keep_refresh,
        "token_type": payload.get("token_type", "bearer"),
        "obtained_at": time.time(),
    }
    store[slug] = record
    _write_store(store)
    return record


def exchange_code(cfg: EntityConfig, code: str) -> dict:
    """Trade the one-time authorisation code for an access + refresh token."""
    payload = _token_request(cfg, {"grant_type": "authorization_code", "code": code.strip()})
    if "refresh_token" not in payload:
        raise RevolutAuthError(
            "Revolut returned no refresh_token, the consent was not granted offline access."
        )
    return _save(cfg.slug, payload)


def refresh(cfg: EntityConfig) -> dict:
    """Mint a new access token. The refresh token itself does not rotate."""
    record = _read_store().get(cfg.slug)
    if not record or not record.get("refresh_token"):
        raise RevolutAuthError(
            f"No Revolut refresh token for {cfg.slug!r}. "
            f"Run: python -m accounting_agent.revolut_auth {cfg.slug}"
        )
    payload = _token_request(
        cfg, {"grant_type": "refresh_token", "refresh_token": record["refresh_token"]}
    )
    return _save(cfg.slug, payload, keep_refresh=record["refresh_token"])


def access_token(slug: str, *, force_refresh: bool = False) -> str:
    """A valid bearer token for the entity, refreshing only when it must.

    Refreshing invalidates the token already in the store, so this deliberately
    reuses a live one rather than refreshing on every call.
    """
    cfg = config(slug)
    record = _read_store().get(cfg.slug)
    if record and not force_refresh and record.get("expires_at", 0) > time.time():
        return record["access_token"]
    if not record:
        raise RevolutAuthError(
            f"Revolut is not authorised for {slug!r}. "
            f"Run: python -m accounting_agent.revolut_auth {slug}"
        )
    return refresh(cfg)["access_token"]


def extract_code(pasted: str) -> str:
    """The code out of whatever got pasted, bare, or the whole redirect URL."""
    pasted = pasted.strip().strip('"').strip("'")
    if pasted.startswith("http"):
        query = urllib.parse.urlparse(pasted).query
        return urllib.parse.parse_qs(query).get("code", [""])[0]
    return pasted


def _bootstrap(slug: str, code: str = "") -> None:
    cfg = config(slug)
    print(f"Revolut Business authorisation, entity {cfg.slug}")
    print(f"  client_id   {cfg.client_id}")
    print(f"  issuer      {cfg.issuer}")
    print(f"  redirect    {cfg.redirect_uri}")
    print(f"  signing key {cfg.key_source}")
    print(f"  scope       {SCOPES} (read-only)")
    print()
    load_private_key(cfg)  # fail fast before sending anyone to a browser
    if not code:
        print("1. Open this URL as a Revolut Business admin and approve:")
        print()
        print(f"   {consent_url(cfg)}")
        print()
        print("2. Revolut redirects to the OAuth redirect URI with ?code=... in the address bar.")
        print("   Copy that code value (it is single-use and expires in minutes).")
        print()
        code = input("Paste the code here: ")
    code = extract_code(code)
    if not code:
        raise SystemExit("No code supplied.")
    record = exchange_code(cfg, code)
    print()
    print(f"Authorised. Tokens written to {TOKEN_PATH}")
    print(f"Access token expires in {int(record['expires_at'] - time.time())}s; "
          f"the refresh token lasts until the X509 certificate expires.")


if __name__ == "__main__":
    # `--code <value>` exchanges without prompting, for the server: the code is
    # single-use and dies in minutes, so the whole thing wants to be one
    # pasteable command rather than an interactive session held open over SSH.
    argv = sys.argv[1:]
    if any(a in ("-h", "--help") for a in argv):
        print(__doc__)
        print("usage: python -m accounting_agent.revolut_auth <slug> "
              "[--code <code> | --url '<redirect url>']")
        raise SystemExit(0)
    supplied = ""
    if "--code" in argv:
        i = argv.index("--code")
        supplied = argv[i + 1] if i + 1 < len(argv) else ""
        argv = argv[:i] + argv[i + 2:]
    if "--url" in argv:  # the whole redirect URL, quoted
        i = argv.index("--url")
        supplied = argv[i + 1] if i + 1 < len(argv) else ""
        argv = argv[:i] + argv[i + 2:]
    slugs = configured_entities()
    if len(argv) != 1:
        raise SystemExit(f"usage: python -m accounting_agent.revolut_auth <slug> "
                         f"[--code <code> | --url '<redirect url>']\n"
                         f"in config/group.toml: {', '.join(config_slugs()) or '(none)'}\n"
                         f"in .env: {', '.join(slugs) or '(none)'}")
    _bootstrap(argv[0], supplied)
