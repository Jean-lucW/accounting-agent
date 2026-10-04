# Mercury API: read-only bank feed

Read access to the group's Mercury accounts. With it, a bookkeeping or
reconciliation run can read the bank feed directly instead of waiting for a
statement export. Client: `src/accounting_agent/mercury/client.py`.
Step-by-step setup: `docs/setup/MERCURY.md`.

Each Mercury business needs its own API token. The token goes in `.env` as
`MERCURY_<SLUG>_KEY`, where `<SLUG>` is the upper-cased `slug` of that
business's `provider = "mercury"` bank account in `config/group.toml`. There
is no token file and nothing to refresh.

## Key constraints: read before using

- **Read-only, twice over.**
  - The token is created as **Read Only** in Mercury > Settings > API Tokens.
  - `MercuryClient._request` refuses any method other than GET before a
    network call is made, raising `MercuryReadOnlyError`.
  - Why both: the Mercury API that returns the feed is the same API that
    moves money, and per `CLAUDE.md` the agent never creates payments. Do not
    add a write path.
- **The token goes in a header.** Every call sends
  `Authorization: Bearer <token>` and `Accept: application/json`. The token
  does not expire on a schedule. To rotate one, create the new token, update
  `.env`, verify, then delete the old one in Mercury.
- **A read-only token needs no IP allowlist.** It works from the server
  without registering the server's address.
- **Amounts are signed USD floats.** Negative is money out. Mercury accounts
  are USD only, so the client sets `currency` to `"USD"` on every line.
- **The credit card is not in the account list.** `GET /accounts` returns
  checking, savings and treasury accounts but not the card. The card is
  behind `GET /credit`, which returns only its id, status and balances.
  `accounts()` merges it in with `kind = "credit"` and the name
  `Mercury Credit`. Its transactions come from the same
  `/account/{id}/transactions` endpoint as any other account. A card balance
  is negative (money owed).
- **Transactions default to the last 30 days.** Without a `start` date,
  Mercury returns only the last 30 days. Always pass `from_date` for
  anything older.
- **Pagination is by offset.** Pages hold up to 1000 rows. `transactions()`
  follows the offset to the end, stops on a short page or when `offset`
  reaches the reported `total`, and de-duplicates by transaction id.
- **429 is retried once.** The client waits the `Retry-After` seconds
  (default 5) and tries again once. A second 429 raises `MercuryError`.
- **Mercury's own categories are not authoritative.** A transaction's
  `kind`, `mercuryCategory` and any GL suggestion are Mercury's guesses.
  Code every cost from `rules/EXPENSES.md`.

## Endpoints the agent calls

Base URL: `https://api.mercury.com/api/v1`. These are the only calls the
client makes, all GET.

| Endpoint | Client method | Returns |
|---|---|---|
| `GET /accounts` | `accounts()` | `{"accounts": [...]}`: each with `id`, `name`, `nickname`, `kind`, `type`, `status`, `currentBalance`, `availableBalance`, `accountNumber`, `routingNumber`, `legalBusinessName` |
| `GET /credit` | `credit_accounts()`; merged in by `accounts()` | `{"accounts": [...]}`: the card account or accounts, with `id`, `status` and balances only |
| `GET /account/{id}` | `account(id)` | one account |
| `GET /account/{id}/transactions` | `transactions(id, from_date, to_date, status)` | `{"transactions": [...], "total": n}` |
| `GET /transaction/{id}` | `transaction(id)` | one transaction |

Query parameters on `/account/{id}/transactions`:

| Parameter | Set from | Notes |
|---|---|---|
| `start` | `from_date` | `YYYY-MM-DD`. Mercury's default is 30 days back |
| `end` | `to_date` | `YYYY-MM-DD` |
| `status` | `status` | optional filter, for example `sent` or `pending` |
| `limit` | fixed | 1000, the maximum |
| `offset` | managed by the client | advanced by the number of rows on each page |
| `order` | fixed | `desc` |

`accounts()` drops any account whose `status` is not `active` unless
`include_closed=True`.

## Transaction status: what counts as cash

| Mercury `status` | Meaning for the bank feed |
|---|---|
| `sent` | settled: cash has moved |
| `pending` | not settled yet; not cash |
| `failed`, `cancelled`, `reversed`, `blocked` | not cash |

## `statement_lines()`: the shape the rest of the agent uses

`statement_lines(from_date, to_date, account_id=None)` reads every account,
or just the one given, and returns one row per transaction. The rows have
the same columns as `RevolutClient.statement_lines()`, so the bank feed,
the bills report and the bank fees report treat both banks alike.

| Column | From the Mercury transaction |
|---|---|
| `id` | `id` |
| `leg_id` | always `None`: a Mercury transaction belongs to one account |
| `date` | `postedAt` if set, else `createdAt` (date part) |
| `completed_at` | `postedAt` |
| `state` | `status` |
| `type` | `kind` |
| `account_id`, `account_name`, `account_kind` | the account it was read from; the name is its `nickname`, else `name`; the kind is `credit` for the card |
| `amount` | `amount` (signed, USD) |
| `currency` | always `"USD"` |
| `bill_amount`, `bill_currency` | always `None` |
| `description` | `bankDescription`, else `counterpartyName` |
| `reference` | `note`, else `externalMemo` |
| `counterparty` | `counterpartyName` |
| `merchant` | `merchant.name`, else `counterpartyName` |
| `card_number` | `details.debitCardInfo.lastFour`, when present |
| `cardholder` | `details.debitCardInfo.cardholderName`, when present |
| `mercury_category` | `mercuryCategory` (Mercury's guess, not a coding rule) |
| `dashboard_link` | `dashboardLink` |

## Usage

```python
import sys; sys.path.insert(0, "src")
from accounting_agent.mercury import MercuryClient

mer = MercuryClient("opcous")                         # slug from config/group.toml
mer.accounts()                                        # every active account, card included
mer.transactions(account_id, from_date="2026-09-01")  # one account, all pages
mer.statement_lines(from_date="2026-09-01")           # every account, statement shape
```

Check a slug end to end:

```bash
.venv/bin/python scripts/mercury_check.py opcous
```

## Fees

Mercury charges a fee on foreign-currency card spend. The fee arrives as its
own statement line, usually a fixed percentage of the purchase it belongs
to. [docs/BANKING.md](BANKING.md) explains how to match each fee to its
purchase. Check your account's fee schedule for the current rate.
`scripts/bank_fees.py` collects and charts Mercury fees with every other
bank fee, in the reporting currency.

## Diagnosing a failure

| Symptom | Cause and fix |
|---|---|
| `Missing MERCURY_<SLUG>_KEY in .env` | no token for that slug: add it (`docs/setup/MERCURY.md` step 3) and check the slug's spelling against `config/group.toml` |
| `HTTP 401 (token rejected ...)` | wrong token, revoked, or created under a different Mercury business |
| `HTTP 403` | the token's access level does not cover the endpoint: recreate it as Read Only |
| `MercuryReadOnlyError` | code tried a non-GET: remove that call, it must never be made |
| Older transactions missing | `from_date` was not passed, so Mercury returned only the last 30 days |
| The card's lines missing | `accounts(include_credit=False)` was used, or the business has no card |
