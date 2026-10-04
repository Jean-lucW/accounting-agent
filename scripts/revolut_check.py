#!/usr/bin/env python3
"""Revolut Business API connection check for one account.

    .venv/bin/python scripts/revolut_check.py <slug>
    .venv/bin/python scripts/revolut_check.py <slug> --cert publiccert.cer

<slug> is the `slug` of a provider = "revolut" bank account in
config/group.toml. Run with no slug to list the configured ones.

Runs in stages and stops at the first failure, so the output names the one
thing that is wrong rather than a wall of tracebacks:

  1. config        the account's block in .env resolves
  2. key           the private key loads, and (with --cert) its public half
                   matches the X509 certificate uploaded to Revolut
  3. assertion     the RS256 client assertion verifies against its own key
  4. credential    Revolut accepts the assertion (proves client_id + key + iss
                   agree): this does NOT need the app to be authorised yet
  5. authorised    a stored refresh token mints a live access token
  6. live          read the accounts and last 7 days of the feed, read-only

Stage 4 is the one that distinguishes "wrong key" from "not authorised yet".
"""

from __future__ import annotations

import base64
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

OK, BAD, INFO = "  ok  ", " FAIL ", "      "


def fail(stage: str, message: str, *fixes: str) -> None:
    print(f"{BAD}{stage}: {message}")
    for fix in fixes:
        print(f"{INFO}  -> {fix}")
    raise SystemExit(1)


def configured_slugs() -> str:
    """The revolut slugs in config/group.toml, marked with whether .env has them."""
    from accounting_agent import config
    from accounting_agent import revolut_auth as ra
    try:
        in_env = set(ra.configured_entities())
    except Exception:  # noqa: BLE001 - a broken .env must not hide the list
        in_env = set()
    rows = []
    for b in config.bank_accounts():
        if b.provider == "revolut":
            mark = "" if b.slug in in_env else "  (no REVOLUT_<SLUG>_* block in .env yet)"
            rows.append(f"  {b.slug:16} {config.entity(b.entity).short} · {b.label}{mark}")
    return "\n".join(rows) or "  (none: add a provider = \"revolut\" bank account to config/group.toml)"


def check(slug: str, cert_path: str | None) -> None:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    from accounting_agent import revolut_auth as ra
    from accounting_agent.revolut import RevolutClient, RevolutReadOnlyError

    print(f"Revolut connection check: account {slug}\n")

    # 1. config
    try:
        cfg = ra.config(slug)
    except ra.RevolutAuthError as e:
        fail("config", str(e))
    print(f"{OK}config     client_id {cfg.client_id[:12]}... iss {cfg.issuer} "
          f"{'SANDBOX' if cfg.sandbox else 'production'}")

    # 2. key, and the public half it must pair with
    try:
        key = ra.load_private_key(cfg)
    except ra.RevolutAuthError as e:
        fail("key", str(e))
    print(f"{OK}key        RSA {key.key_size} bit, from {cfg.key_source}")

    paired = ra.keypair_matches(cfg)
    if paired is False:
        fail("keypair", "the configured public key is NOT the private key's public half",
             "these two must be one keypair: the public key here should be the one "
             "whose certificate is uploaded to Revolut")
    if paired:
        print(f"{OK}keypair    public key matches the private key")

    # Fall back to the conventional location, so the pair is checked by default.
    if not cert_path and paired is None:
        conventional = ra.PROJECT_ROOT / ".revolut" / f"{cfg.slug}-publiccert.pem"
        if conventional.exists():
            cert_path = str(conventional)

    if cert_path:
        raw = Path(cert_path).read_bytes()
        try:
            cert_pub = x509.load_pem_x509_certificate(raw).public_key()
        except ValueError:
            cert_pub = x509.load_der_x509_certificate(raw).public_key()
        if cert_pub.public_numbers() != key.public_key().public_numbers():
            fail("cert", f"{cert_path} was NOT generated from this private key",
                 "these two must be one keypair: regenerate both together and re-upload the cert")
        print(f"{OK}cert       {Path(cert_path).name} matches the private key")

    # 3. the assertion verifies against its own key
    assertion = ra.client_assertion(cfg)
    head, body, sig = assertion.split(".")
    key.public_key().verify(
        base64.urlsafe_b64decode(sig + "=" * (-len(sig) % 4)),
        f"{head}.{body}".encode(), padding.PKCS1v15(), hashes.SHA256())
    print(f"{OK}assertion  RS256 JWT signs and verifies")

    # 4. does Revolut accept the credential? Deliberately paired with an unusable
    #    refresh token, so this proves the certificate without needing consent.
    form = urllib.parse.urlencode({
        "grant_type": "refresh_token", "refresh_token": "oa_prod_placeholder",
        "client_id": cfg.client_id, "client_assertion_type": ra.ASSERTION_TYPE,
        "client_assertion": assertion}).encode()
    request = urllib.request.Request(
        f"{cfg.api_base}/auth/token", data=form, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    detail = ""
    try:
        urllib.request.urlopen(request, context=ra._SSL_CONTEXT)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
    # Revolut returns code 9001 for every auth failure, so the code says nothing
    # about which half failed: only the message does. A complaint about the
    # REFRESH TOKEN means the assertion was accepted, which is what we are
    # testing; a complaint about the CLIENT ASSERTION means it was not.
    if "client assertion" in detail.lower():
        fail("credential", f"Revolut rejected the client assertion: {detail[:200]}",
             "'signature mismatch' -> the private key does not pair with the certificate "
             "uploaded for this client_id",
             "'iss' -> the issuer claim must be the bare domain, no scheme, no www",
             "'aud' -> must be exactly https://revolut.com")
    print(f"{OK}credential Revolut accepts the assertion (client_id + key + iss + aud agree)")

    # 5. has the app been authorised?
    try:
        token = ra.access_token(slug)
    except ra.RevolutAuthError as e:
        fail("authorised", str(e), f"python -m accounting_agent.revolut_auth {slug}")
    print(f"{OK}authorised access token live ({len(token)} chars)")

    # 6. live read, and confirm writes are still refused
    rev = RevolutClient(slug)
    accounts = rev.accounts()
    print(f"{OK}live       {len(accounts)} account(s)")
    for a in accounts:
        print(f"{INFO}  {a.get('name', '(unnamed)'):28} {a.get('currency')} "
              f"{a.get('balance', 0):>14,.2f}  {a.get('state')}")

    since = (date.today() - timedelta(days=7)).isoformat()
    lines = rev.statement_lines(from_date=since)
    print(f"{OK}feed       {len(lines)} statement line(s) since {since}")
    for line in lines[:5]:
        print(f"{INFO}  {line['date']}  {line['currency']} {line['amount']:>12,.2f}  "
              f"{(line['merchant'] or line['description'] or '')[:40]}")

    try:
        rev._request("POST", "pay")
        fail("read-only", "a POST was NOT blocked: do not use this client")
    except RevolutReadOnlyError:
        print(f"{OK}read-only  writes refused at the transport layer")


if __name__ == "__main__":
    if any(a in ("-h", "--help") for a in sys.argv[1:]):
        print(__doc__)
        print("configured revolut slugs:\n" + configured_slugs())
        raise SystemExit(0)
    positional = [a for a in sys.argv[1:] if not a.startswith("--")]
    cert = None
    if "--cert" in sys.argv:
        idx = sys.argv.index("--cert") + 1
        cert = sys.argv[idx] if idx < len(sys.argv) else None
        positional = [a for a in positional if a != cert]
    if len(positional) != 1:
        raise SystemExit("usage: revolut_check.py <slug> [--cert publiccert.cer]\n"
                         "configured revolut slugs:\n" + configured_slugs())
    check(positional[0], cert)
