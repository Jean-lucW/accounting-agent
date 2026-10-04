# Connecting Revolut Business (read-only)

For each Revolut Business account in the group, the agent reads the live bank
feed through the Revolut Business API, at scope **READ only**. It never moves
money: the consent is READ, and `src/accounting_agent/revolut/client.py`
refuses every non-GET request before it leaves the process.

Repeat this guide once per Revolut Business account. Allow about 30 minutes
for the first, 10 for each after. Background and diagnostics:
`docs/REVOLUT_API_REFERENCE.md`.

You need:

- A Revolut Business **admin** for each account (only an admin can approve
  the consent).
- A domain you control for the redirect URI and issuer, for example your
  company website (`example.com`). Nothing needs to run there: Revolut only
  sends the browser to it with a code in the address bar.
- `openssl` (macOS and Linux have it).

## 1. Pick the slug

In `config/group.toml`, the Revolut bank account has a `slug`:

```toml
[[entities.bank_accounts]]
label = "Revolut"
provider = "revolut"
slug = "holdco"
```

The slug names the credentials: `.env` variables are `REVOLUT_<SLUG>_*` with
the slug upper-cased (a hyphen becomes `_`), here `REVOLUT_HOLDCO_*`. The
examples below use `holdco`; substitute yours.

## 2. Generate the key pair and certificate

Revolut has no client secret: every token request is a JWT signed with your
private RSA key, and Revolut checks it against a certificate you upload.
Holding the private key IS holding the credential.

```bash
mkdir -p .revolut && chmod 700 .revolut
cd .revolut
openssl genrsa -out holdco-privatecert.pem 2048
openssl req -new -x509 -key holdco-privatecert.pem -out holdco-publiccert.cer -days 1825
chmod 600 holdco-privatecert.pem
cd ..
```

`openssl req` asks a few questions (country, organisation...); any answers
work. `-days 1825` makes the certificate valid for five years, and **the
refresh token lives exactly as long as the certificate**, so a longer
certificate means fewer re-authorisations.

`.revolut/` is git-ignored. Check: `git check-ignore .revolut/holdco-privatecert.pem`
prints the path. **Never commit anything in `.revolut/`.**

## 3. Upload the certificate in Revolut Business

1. Log in to Revolut Business on the web as an admin.
2. Go to **Settings > APIs > Business API** and click **Add API
   certificate** (wording varies slightly).
3. Fill in:
   - **Title**: `Accounting Agent (read-only)`.
   - **OAuth redirect URI**: `https://example.com/` (your domain). Note it
     exactly, trailing slash included.
   - **X509 public key**: paste the whole content of
     `.revolut/holdco-publiccert.cer`, from `-----BEGIN CERTIFICATE-----` to
     `-----END CERTIFICATE-----`.
   - If Revolut offers an **IP allowlist**, add the server's public IP. Then
     the token exchange in step 6 must run on the server.
4. Save. Revolut shows a **Client ID** for this certificate. Copy it.

## 4. Fill in .env

```
REVOLUT_HOLDCO_CLIENT_ID=<the client id>
REVOLUT_HOLDCO_ISSUER=example.com
REVOLUT_HOLDCO_REDIRECT_URI=https://example.com/
REVOLUT_HOLDCO_PRIVATE_KEY=.revolut/holdco-privatecert.pem
```

- `ISSUER` is your redirect URI's domain, without `https://` and without the
  path. It becomes the JWT `iss` claim.
- `REDIRECT_URI` must match what you entered in Revolut character for
  character.
- `PRIVATE_KEY` is a path, relative to the repo root or absolute.

**Base64 option (no key file on the server).** Instead of the path, carry the
key in `.env` as one line:

```bash
base64 -i .revolut/holdco-privatecert.pem | tr -d '\n'
```

```
REVOLUT_HOLDCO_PRIVATE_KEY_B64=<that output>
```

`_B64` wins when both are set. Optionally also add the public half, so a
mismatched pair is caught locally instead of by Revolut's opaque error 9001:

```bash
openssl x509 -in .revolut/holdco-publiccert.cer -pubkey -noout | base64 | tr -d '\n'
```

```
REVOLUT_HOLDCO_PUBLIC_KEY_B64=<that output>
```

`REVOLUT_HOLDCO_SANDBOX=1` points the account at Revolut's sandbox, for
testing with a sandbox certificate.

Signing needs the `cryptography` package, which `pip install -e .` installs.
If the check in step 5 says it is missing, re-run `.venv/bin/pip install -e .`.

## 5. Check the credential before anyone approves anything

```bash
.venv/bin/python scripts/revolut_check.py holdco
```

It runs six stages and stops at the first failure. Before consent, the
correct result is: stages 1 to 4 pass, stage 5 stops at `FAIL authorised`.
That proves the client ID, key and issuer agree. If stage 4 fails, the key
does not pair with the uploaded certificate (or the issuer is wrong): fix
that first. See "Diagnosing a failure" in `docs/REVOLUT_API_REFERENCE.md`.

## 6. Authorise

```bash
.venv/bin/python -m accounting_agent.revolut_auth holdco
```

1. It prints the settings it will use and a consent URL.
2. A Revolut Business **admin** opens the URL and approves. The scope shown
   is READ.
3. Revolut sends the browser to `https://example.com/?code=...`. The page
   itself may show nothing or an error: that does not matter. Copy the
   `code` value from the address bar (or the whole address).
4. Paste it at the `Paste the code here:` prompt. The code works once and
   expires within minutes, so do this straight away.
5. It prints `Authorised. Tokens written to .../.revolut/tokens.json`.

When the admin is someone else, or the exchange must run on the server
(IP allowlist), get everything ready on the server first (step 5 showing
`FAIL authorised`), send the admin the URL, and exchange the code they send
back in one command:

```bash
.venv/bin/python -m accounting_agent.revolut_auth holdco --code <code>
# or the whole redirect address:
.venv/bin/python -m accounting_agent.revolut_auth holdco --url 'https://example.com/?code=...'
```

An expired code breaks nothing: ask the admin to open the URL again.

## 7. Token lifetimes and where they live

- `.revolut/tokens.json`, mode 600, git-ignored, one entry per slug.
- The access token lasts 40 minutes. Refreshing it **invalidates the previous
  one**, so the token file must live on one machine only (the server). Two
  machines sharing an authorisation knock each other offline.
- The refresh token does **not** rotate. It stays valid until the X509
  certificate expires (step 2). Then generate a new pair, upload the new
  certificate and repeat steps 4 to 6.

## 8. Verify

```bash
.venv/bin/python scripts/revolut_check.py holdco
```

All six stages pass. Then a quick read:

```bash
.venv/bin/python -c "import sys; sys.path.insert(0,'src'); \
from accounting_agent.revolut import RevolutClient; \
print([(a['name'], a['currency'], a['balance']) for a in RevolutClient('holdco').accounts()])"
```

Finally confirm the token cannot write: see the probe described at the top of
`docs/REVOLUT_API_REFERENCE.md` (a write-shaped request must come back
`403 code 9002 ... [WRITE]`).

## Security checklist

- `chmod 600` on every `.pem` and on `.revolut/tokens.json`; `chmod 700
  .revolut`.
- Never commit `.revolut/` or `.env`; never paste the private key into chat
  or a ticket.
- If the private key leaks, delete the certificate in Revolut Business
  (Settings > APIs) at once, then start again from step 2.
