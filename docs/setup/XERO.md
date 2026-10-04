# Connecting Xero

The agent talks to Xero through one OAuth 2.0 app that you create yourself. One
app, one consent, one token file covers every organisation in the group: each
API call names the organisation it is for (the `xero-tenant-id` header). This
guide takes you from nothing to a passing smoke test. Allow about 20 minutes.

You need:

- A Xero login that is an **adviser** (or standard user with reports access)
  in every organisation the agent will bookkeep.
- The repo cloned, `.venv` set up, and `.env` copied from `.env.example`.
- `config/group.toml` copied from `config/group.example.toml`, with one
  `[[entities]]` block per organisation (it does not need to be right yet;
  step 8 fixes the names).

Code involved: `src/accounting_agent/xero_auth.py` (the consent flow and token
refresh), `src/accounting_agent/xero/client.py` (the API client). API
background: `docs/XERO_API_REFERENCE.md`.

## 1. Create the Xero app

1. Open https://developer.xero.com/app/manage and log in with your Xero login.
2. Click **New app**.
3. Fill in the form:
   - **App name**: anything you will recognise, for example
     `<Your group> Accounting Agent`. Xero shows this name on the consent
     screen.
   - **Integration type**: **Web app**. This is the standard OAuth 2.0
     authorisation code flow with a client secret, which is what
     `xero_auth.py` implements. Do not pick "Custom connection" (one
     organisation each, paid) or "Mobile or desktop app" (PKCE, no secret).
   - **Company or application URL**: your company website. It is only shown
     to users.
   - **Redirect URI**: `http://localhost:8400/callback`
4. Tick the terms and click **Create app**.

About the redirect URI: `xero_auth.py` reads it from `XERO_REDIRECT_URI` in
`.env`, starts a small web server on `localhost` at that URI's port (8400 in
this example) and waits there for Xero to send the browser back. Any free
localhost port works, as long as the value in `.env` and the value in the Xero
app are **character for character identical**. Use `localhost`, not
`127.0.0.1` (Xero rejects the latter for http). Avoid 8401 and 8402, which the
Gmail and Drive bootstraps use.

## 2. Copy the client ID and secret

1. In the app's page, open **Configuration**.
2. Copy the **Client id**.
3. Click **Generate a secret** and copy it immediately. Xero shows it once.
4. Open `.env` in the repo and fill in:

   ```
   XERO_CLIENT_ID=<the client id>
   XERO_CLIENT_SECRET=<the secret>
   XERO_REDIRECT_URI=http://localhost:8400/callback
   ```

   No quotes, no spaces around `=`. `.env` is git-ignored: never commit it.

## 3. Understand the scopes (nothing to click)

The app asks for its scopes at consent time; the list is `SCOPES` in
`xero_auth.py`:

| Scope | What the agent uses it for |
|---|---|
| `offline_access` | a refresh token, so runs work without a browser |
| `openid profile email` | identifies the user who consented |
| `accounting.invoices` | bills, sales invoices, credit notes, purchase orders |
| `accounting.payments` | reading payments (the agent never creates one) |
| `accounting.banktransactions` | spend/receive money and bank transfers |
| `accounting.manualjournals` | manual journals |
| `accounting.contacts` | suppliers and customers |
| `accounting.settings` | chart of accounts, tax rates, tracking, organisation |
| `accounting.attachments` | attaching the source invoice PDF to each bill |
| `assets` | Fixed Assets API: asset types and draft asset registration |
| `accounting.budgets.read` | budgets |
| `accounting.reports.*.read` | aged, balance sheet, bank summary, executive summary, P&L, trial balance and tax reports |

**Apps created on or after 2 March 2026 must use these granular scopes.** The
older broad scopes (`accounting.transactions`, `accounting.reports.read`) are
rejected for new apps, which is why the code does not ask for them.
`accounting.journals.read` (the raw general-ledger feed) is a premium scope
that new apps only get with Xero's approval; the agent works without it and
reviews the ledger through reports instead. If the developer portal shows a
scope selection for your app, enable the same set as the table.

## 4. Run the consent flow

Run this on the machine that will run the agent (see step 6 for a headless
server):

```bash
.venv/bin/python -m accounting_agent.xero_auth
```

1. A browser tab opens on Xero's consent page. If it does not, copy the URL
   the script prints into a browser.
2. Log in as the Xero user from "You need".
3. On the consent screen, **tick every organisation** in the group. An
   organisation you leave unticked is invisible to the agent.
4. Click **Allow access**.
5. The tab says "Xero authorisation received. You can close this tab." Back
   in the terminal the script prints:

   ```
   Tokens saved to .xero/tokens.json

   Authorised organisations (3):
     Example Holdings Limited: <tenant id>
     Example Operations Inc.: <tenant id>
     Example Operations B.V.: <tenant id>
   ```

The script waits up to 30 minutes for you to approve. Xero's authorisation
code itself expires 5 minutes after you click Allow, which the script handles
instantly. If it says the port is in use, it stops the previous waiting run
and retries.

## 5. Protect the token file

`.xero/tokens.json` is written with mode 600 and is git-ignored. Check:

```bash
ls -l .xero/tokens.json        # -rw-------
git check-ignore .xero/tokens.json   # prints the path = ignored
```

The access token lasts 30 minutes and is refreshed automatically. The
refresh token **rotates on every refresh** and expires after 60 days without
use; any scheduled run keeps it alive. Because it rotates, **only one machine
may use the token file**. If a laptop and the server both refresh from copies
of the same file, the first refresh invalidates the other copy and that
machine must redo step 4.

## 6. On a headless server

The consent needs a browser, but the script listens on `localhost` of the
machine it runs on. Two ways:

- **Port forward.** From your laptop:

  ```bash
  ssh -L 8400:localhost:8400 <server>
  cd /opt/accounting-agent
  .venv/bin/python -m accounting_agent.xero_auth
  ```

  Open the printed URL in your laptop's browser. Xero redirects to
  `localhost:8400`, which the tunnel carries to the server. The token file is
  written on the server.
- **Bootstrap on the laptop, then move the file.** Run step 4 on the laptop,
  copy `.xero/tokens.json` to the same path on the server (`chmod 600`), then
  **delete the laptop copy** so only the server ever refreshes it.

## 7. How organisation names are mapped

The first API call writes `.xero/connections.json`, a cache that maps each
connected organisation's name, exactly as Xero shows it, to its tenant id:

```json
{
  "Example Holdings Limited": "<tenant id>",
  "Example Operations Inc.": "<tenant id>"
}
```

When a name is not found the client refreshes this cache from Xero once, so
a renamed or newly connected organisation is picked up automatically. You can
also delete the file; it is rebuilt on the next call.

## 8. Make `xero_name` in config/group.toml match exactly

Every script resolves an entity through `config/group.toml` and then asks Xero
for the organisation named **exactly** `xero_name` (case is ignored, nothing
else is). Exact matching matters: one organisation's name is often a substring
of another's ("Example Operations" and "Example Operations Inc."), and a loose
match would post to the wrong company.

1. Open `.xero/connections.json` (or look at the names step 4 printed).
2. For each `[[entities]]` block in `config/group.toml`, paste the name into
   `xero_name`, punctuation and all:

   ```toml
   [[entities]]
   key = "OPCO_US"
   xero_name = "Example Operations Inc."
   ```

3. Check that every entity resolves:

   ```bash
   .venv/bin/python -c "import sys; sys.path.insert(0,'src'); \
   from accounting_agent import config; from accounting_agent.xero import client_for; \
   [print(e.key, '->', client_for(e.key)) for e in config.entities()]"
   ```

   A mismatch fails with `No connected Xero organisation is named exactly ...`
   and lists the names that are connected.

## 9. Verify with the smoke test

```bash
.venv/bin/python scripts/smoke_test.py
```

It is strictly read-only (GET requests only). For every entity in
`config/group.toml` it prints the number of bank accounts, unreconciled bank
transactions, the first page of bills and sales invoices, and whether the
trial balance balances. Any entity that fails prints an `ERROR` row with the
reason; the script exits 1 if any did. `.venv/bin/python scripts/smoke_test.py
OPCO_US` tests one entity.

## Rate limits

Xero allows, **per organisation**: 60 calls a minute, 5 calls at once and
**5,000 calls a day** (1,000 a day for Starter-tier apps), plus 10,000 calls a
minute across the whole app. The client waits out a minute-limit 429 on its
own (honouring `Retry-After`, at most 65 seconds), but a run that hits the
daily cap fails rather than waits. Check what is left:

```bash
.venv/bin/python scripts/xero_rate_limit.py            # table per organisation
.venv/bin/python scripts/xero_rate_limit.py --retry-at # epoch to retry at, exit 3 if capped
```

## Adding an entity later

1. In Xero, make sure your user has access to the new organisation.
2. Re-run `.venv/bin/python -m accounting_agent.xero_auth` and tick the new
   organisation (consent is additive; existing ones stay connected). On a
   server, use the port forward from step 6.
3. Add an `[[entities]]` block to `config/group.toml` with `xero_name` copied
   exactly from the printed list, plus its bank accounts and any intercompany
   pairs.
4. Write its rules file (`rules/entities/<KEY>.md`).
5. Run `scripts/smoke_test.py <KEY>`.

An uncertified app can connect up to 25 organisations (fewer on the Starter
tier); each organisation can connect at most two uncertified apps.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `XERO_CLIENT_ID is not set in .env` | step 2 |
| Xero shows "Invalid redirect_uri" | `.env` and the app's redirect URI differ; make them identical |
| `invalid_scope` on the consent page | the app is a granular-scope app and something asked for a broad scope; use the code's `SCOPES` unchanged |
| `No stored tokens. Run: ...` | step 4 has not been run on this machine |
| 401 / `invalid_grant` on refresh | the refresh token expired (60 days unused) or another machine rotated it; redo step 4 |
| `No connected Xero organisation is named exactly ...` | step 8, or the organisation was not ticked in step 4 |
| 403 on Reports | the consenting user lacks the reports permission in that organisation |
