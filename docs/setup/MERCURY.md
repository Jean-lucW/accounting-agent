# Connecting Mercury (read-only)

For each Mercury business in the group, the agent reads the bank feed
(checking, savings, treasury and the Mercury credit card) through the Mercury
API with a **read-only** token. It never moves money: the token is created
READ ONLY in Mercury, and `src/accounting_agent/mercury/client.py` refuses
every non-GET request before it leaves the process.

Repeat this guide once per Mercury business. Allow about 5 minutes each.

You need a Mercury login that can manage API tokens for the business (an
admin).

## 1. Pick the slug

In `config/group.toml`, the Mercury bank account has a `slug`:

```toml
[[entities.bank_accounts]]
label = "Mercury"
provider = "mercury"
slug = "opcous"
```

The token goes in `.env` as `MERCURY_<SLUG>_KEY`, slug upper-cased (a hyphen
becomes `_`), here `MERCURY_OPCOUS_KEY`.

## 2. Create a READ ONLY token

1. Log in to Mercury on the web.
2. Go to **Settings > API Tokens** (under the business's settings).
3. Click **Create token** (or **Add API token**).
4. Name it `Accounting Agent (read-only)`.
5. Choose the **Read Only** access level. Do not choose a level that can
   send money or manage recipients.
6. Read-only tokens need **no IP allowlist**, so leave that empty.
7. Create it and copy the token now. Mercury shows it once.

Mercury also narrows a token to the permissions it actually uses after 45
days, so a token that only ever reads stays read-only on Mercury's side too.

## 3. Put it in .env

```
MERCURY_OPCOUS_KEY=<the token>
```

No quotes. `.env` is git-ignored: never commit it. There is nothing to
refresh: the token is sent as a bearer token on every request until you
revoke it.

## 4. Verify

```bash
.venv/bin/python scripts/mercury_check.py opcous
```

Or a quick read by hand:

```bash
.venv/bin/python -c "import sys; sys.path.insert(0,'src'); \
from accounting_agent.mercury import MercuryClient; \
print([(a['name'], a.get('currentBalance')) for a in MercuryClient('opcous').accounts()])"
```

You should see every active account, plus `Mercury Credit` if the business
has the card. Amounts are USD; the card's balance is negative (money owed).

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Missing MERCURY_OPCOUS_KEY in .env` | step 3, and check the slug spelling |
| `HTTP 401 (token rejected ...)` | wrong token, revoked, or created under a different Mercury business |
| `HTTP 403` | the token's access level does not cover the endpoint; recreate it as Read Only |

API details (endpoints, pagination, status values and the statement-line
mapping) are in [docs/MERCURY_API_REFERENCE.md](../MERCURY_API_REFERENCE.md).

To rotate a token: create the new one, update `.env`, run step 4, then delete
the old token in Mercury.
