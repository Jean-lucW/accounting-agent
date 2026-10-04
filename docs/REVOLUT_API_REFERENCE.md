# Revolut Business API: read-only bank feed

Read access to the group's Revolut Business accounts, so a bookkeeping or
reconciliation run can see the bank feed directly instead of waiting for a
statement export.

One Revolut Business account = one API credential, so each account has its
own certificate, its own client ID and its own `REVOLUT_<SLUG>_*` block in
`.env`, where `<SLUG>` is the upper-cased `slug` of the `provider = "revolut"`
bank account in `config/group.toml`. The tokens for every account live in the
server's `.revolut/tokens.json`. Step-by-step setup: `docs/setup/REVOLUT.md`.

Verify every new token is READ-only: send a write-shaped request with the
live token straight to Revolut (bypassing the client) and expect
`403 code 9002 The following scopes are required: [WRITE]`. A token can come
back with WRITE scope when the same certificate was earlier authorised with
full access; change its permissions in Revolut Business and re-consent. A
`400` or `404` on such a probe means the scope check passed and the token can
write: re-consent it.

## Key constraints: read before using

- **Read-only, twice over.** The consent is granted at scope `READ`, and
  `revolut/client.py` refuses any method other than GET at the transport
  layer. The Revolut API that returns the feed is the same API that moves
  money; per `CLAUDE.md` the agent never creates payments, so writes are
  locked out in code, not just by convention. Do not add a write path.
- **No client secret exists.** Authentication is a JWT signed with the RSA
  private key whose X509 certificate is uploaded in Revolut Business >
  Settings > APIs. Whoever holds that key holds the credential. It must never
  enter the git tree: keep it in `.revolut/` (git-ignored, chmod 600).
- **Refreshing invalidates the previous access token.** Two processes sharing
  one `.revolut/tokens.json` will knock each other offline. `access_token()`
  therefore reuses a live token and refreshes only when it has expired.
- **The refresh token does not rotate** (unlike Xero's). It stays valid until
  the X509 certificate expires. Losing it means redoing the browser consent,
  nothing worse.
- **Transactions have no cursor.** Pagination walks the `to` boundary
  backwards, 1000 rows at a time; `transactions()` handles this and
  de-duplicates by id because the boundary is inclusive.
- **A transaction is not a statement line.** Each transaction carries a `legs`
  list: an internal transfer between two of your own accounts has two legs,
  one negative and one positive. Use `statement_lines()` for the flattened,
  one-row-per-movement view that lines up with a bank statement and with
  Xero's bank statement lines.

## Setup

1. In Revolut Business > Settings > APIs, create an API certificate. Generate
   the keypair yourself, upload the **public** X509 certificate, and keep the
   private key. Revolut issues a client ID against that certificate.
2. Put the private key at `.revolut/<slug>-privatecert.pem`, chmod 600.
3. Add the account's block to `.env` (see `.env.example`), here for the slug
   `holdco`:

   ```
   REVOLUT_HOLDCO_CLIENT_ID=...
   REVOLUT_HOLDCO_ISSUER=example.com
   REVOLUT_HOLDCO_REDIRECT_URI=https://example.com/
   REVOLUT_HOLDCO_PRIVATE_KEY=.revolut/holdco-privatecert.pem
   ```

   `ISSUER` is the domain registered against the certificate and becomes the
   JWT `iss` claim. `REDIRECT_URI` must match the app's OAuth redirect URI
   character for character.

   The key can be carried in `.env` instead of on disk, which suits a server:
   nothing to copy, nothing to chmod:

   ```
   REVOLUT_HOLDCO_PRIVATE_KEY_B64=$(base64 -i privatecert.pem | tr -d '\n')
   REVOLUT_HOLDCO_PUBLIC_KEY_B64=...     # optional, see below
   ```

   `_B64` wins over the path when both are set. A raw PEM or one with escaped
   `\n` is accepted too; base64 is just the shape that survives a single line.
   The public key is optional and used for one thing: `revolut_check.py`
   compares it against the private key's own public half, so a mismatched pair
   is caught locally with a clear message instead of surfacing as Revolut's
   opaque 9001. It must be the public half of that same key: the one whose
   certificate is uploaded to Revolut.

   Both spellings work for every setting: `REVOLUT_HOLDCO_ISSUER` or bare
   `REVOLUT_ISSUER`, per-account winning. The bare form is shorthand for a
   single-account setup and stops being meaningful as soon as a second Revolut
   Business account is added, so prefer the per-account name.
4. Verify the credential before authorising anything:

   ```bash
   .venv/bin/python scripts/revolut_check.py holdco
   ```

5. Authorise once, as a Revolut Business admin:

   ```bash
   .venv/bin/python -m accounting_agent.revolut_auth holdco
   ```

   It prints a consent URL. Approving it redirects to the OAuth redirect URI
   with `?code=...` in the address bar; paste that code back. The code is
   single-use and expires within minutes. Tokens land in
   `.revolut/tokens.json`.

## Authorising when you are not the Revolut admin

The consent is a browser action and only a Revolut Business **admin** can grant
it. The token exchange that follows is an API call, and when the certificate
carries an IP allowlist it must leave from an allowlisted IP, normally the
server, not a laptop. So the approval and the exchange happen in different
places, connected by a code that **expires in minutes and works once**.

Sequence:

1. Get the server ready first, before anyone opens a browser. The code is the
   perishable part, so everything else is done in advance.
2. Send the admin the consent URL. They approve and send back the code.
3. Exchange it on the server, immediately.

Step 1, on the server (`.env` is not synced by the deploy, so the server
needs its own copy of the Revolut block):

```bash
ssh <server>
cd /opt/accounting-agent
# add REVOLUT_HOLDCO_CLIENT_ID / _ISSUER / _REDIRECT_URI and the key to .env
.venv/bin/python scripts/revolut_check.py holdco
```

Expect it to reach stage 5 and stop at `FAIL authorised`: that is the correct
state before consent, and it proves everything except the consent is right. Do
not send the admin anything until you have seen that.

Step 3, on the server, within a couple of minutes of the admin replying:

```bash
.venv/bin/python -m accounting_agent.revolut_auth holdco --code <code>
```

`--url '<whole redirect URL>'` works too, if the admin pastes the address bar
rather than picking the code out of it. Then re-run `revolut_check.py holdco`;
it should now pass all six stages.

If the code expires before you exchange it, nothing is broken and nothing is
consumed: ask the admin to open the same URL again.

**The token lives on the server only.** Not because the refresh token rotates
(it does not, unlike Xero's) but because refreshing an access token
invalidates the previous one: two machines sharing one authorisation knock
each other offline. Same conclusion as the Xero rule in `CLAUDE.md`, different
reason.

## Usage

```python
from accounting_agent.revolut import RevolutClient

rev = RevolutClient("holdco")

rev.accounts()                              # id, name, currency, balance, state
rev.bank_details(account_id)                # IBAN / account number / sort code
rev.transactions(from_date="2026-09-01")    # raw, nested legs, auto-paginated
rev.statement_lines(from_date="2026-09-01") # flattened, one row per movement
rev.counterparties()
```

`statement_lines()` returns `date`, `amount` (signed, negative is money out),
`currency`, `description`, `reference`, `merchant`, `cardholder`, `state` and
`type` per row. Pending rows carry `state != "completed"` and no
`completed_at`: exclude them when comparing against a Xero bank balance.

## Diagnosing a failure

`scripts/revolut_check.py <slug>` runs six stages and stops at the first
failure. Stage 4 is the useful one: it asks Revolut to accept the signed
assertion while deliberately pairing it with an unusable refresh token, which
proves the client ID, key and issuer agree **without** needing the app to be
authorised. So:

- fails at stage 4: the credential itself is wrong (usually the private key
  does not pair with the uploaded certificate)
- passes stage 4, fails stage 5: the credential is fine, the app has simply
  not been authorised yet; run the consent flow

`{"code":9001,"message":"Client assertion signature mismatch"}` means exactly
what it says: the key does not match the certificate on that client ID. It is
not an `iss` problem and not a consent problem: a wrong `iss` and a broken
signature both surface as this same 9001.

Pass `--cert publiccert.cer` to check a candidate key against a certificate
offline, before touching the network.

## Endpoints

Production base `https://b2b.revolut.com/api/1.0`, sandbox
`https://sandbox-b2b.revolut.com/api/1.0` (set `REVOLUT_<SLUG>_SANDBOX=1`).

| Endpoint | Returns |
| --- | --- |
| `GET /accounts` | every account with balance and currency |
| `GET /accounts/{id}` | one account |
| `GET /accounts/{id}/bank-details` | IBAN, account number, sort code, BIC |
| `GET /transactions` | `from`, `to`, `account`, `type`, `count` (max 1000) |
| `GET /transaction/{id}` | one transaction |
| `GET /counterparties` | saved counterparties |

Token endpoint `POST /auth/token`, consent page
`https://business.revolut.com/app-confirm`. Access tokens last 40 minutes.
