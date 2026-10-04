# Connecting the Gmail accounting inbox

Suppliers email invoices to one mailbox, the **accounting inbox** named in
`config/group.toml`.

**Gmail is required, and it is the only mail provider supported for now.**
The inbox must be a Gmail or Google Workspace mailbox; `deploy/preflight.sh`
fails without its token. If your invoices arrive somewhere else, forward them
to a Workspace mailbox, or send the files to the Slack app instead (both are
read by every bookkeeping run).

```toml
[company]
accounting_inbox = "accounting@example.com"
processed_label = "Bookkept"
```

The agent reads that mailbox through the Gmail API with an OAuth token you
create once. This guide sets up the Google Cloud project, the OAuth client and
the token. The same project and client are reused for Google Drive
(`docs/setup/GOOGLE_DRIVE.md`). Allow about 20 minutes.

You need:

- A Google Workspace account with admin rights to create a Cloud project (or
  a colleague who has them), and the password or a session for the
  accounting inbox itself.
- The repo cloned, `.venv` set up, `.env` copied from `.env.example`, and
  `accounting_inbox` filled in as above.

Code involved: `src/accounting_agent/gmail_auth.py`.

## 1. Create a Google Cloud project

1. Open https://console.cloud.google.com/ signed in with your Workspace
   account.
2. In the project picker at the top, click **New project**.
3. Name it, for example `accounting-agent`. Leave the organisation as your
   Workspace domain. Click **Create**, then select the new project in the
   picker.

## 2. Enable the Gmail API

1. Go to **APIs & Services > Library**.
2. Search for **Gmail API**, open it, click **Enable**.
3. While you are here, also enable **Google Drive API** if you will publish
   workbooks to Drive (`docs/setup/GOOGLE_DRIVE.md`).

## 3. Configure the OAuth consent screen

1. Go to **APIs & Services > OAuth consent screen** (in newer consoles:
   **Google Auth Platform > Branding / Audience**).
2. **User type: Internal.** Only accounts in your Workspace can consent, the
   app needs no Google verification, and refresh tokens do not expire on a
   timer.
3. App name `Accounting Agent`, support email and developer contact email:
   yours. Save.
4. Under **Data access / Scopes**, add
   `https://www.googleapis.com/auth/gmail.modify` (and, for Drive,
   `.../auth/drive.readonly` and `.../auth/drive.file`). Save.

If your mailbox is a personal Gmail account rather than Workspace, Internal is
not offered: choose **External**, add the inbox as a **test user**, and know
that tokens for an External app in "Testing" status expire after 7 days, so
you would have to repeat step 6 weekly. Use Workspace if you can.

## 4. Create the OAuth client

1. Go to **APIs & Services > Credentials** (or **Google Auth Platform >
   Clients**).
2. Click **Create credentials > OAuth client ID**.
3. **Application type: Desktop app.** Name it `accounting-agent`.
4. Click **Create**. A dialog shows the **Client ID** and **Client secret**.

A Desktop client accepts any `http://localhost:<port>` redirect, so you do not
register one. The bootstrap uses `http://localhost:8401/callback` (Drive uses
8402).

## 5. Put the client in .env

`gmail_auth.py` reads the client from `.env`, not from a downloaded JSON file.
Copy the two values from the dialog (or from the client's page) into `.env`:

```
GMAIL_CLIENT_ID=1234567890-abc...apps.googleusercontent.com
GMAIL_CLIENT_SECRET=GOCSPX-...
```

You may download the JSON for safekeeping, but keep it out of the repo.

## 6. Run the bootstrap

```bash
.venv/bin/python -m accounting_agent.gmail_auth
```

1. It prints a URL and opens it. The URL carries a login hint for the
   accounting inbox from `config/group.toml`.
2. **Sign in as the accounting inbox itself**, not as your own account. The
   token belongs to whoever approves.
3. Google lists the permission "Read, compose, and send emails from your
   Gmail account... and manage labels" (the wording for `gmail.modify`).
   Click **Allow**.
4. The tab says "Authorised. You can close this tab." The terminal prints:

   ```
   Tokens saved to .../.gmail/tokens.json
   Authorised mailbox: accounting@example.com (12345 messages)
   ```

   If the mailbox printed is not the one in `config/group.toml`, the script
   warns you: re-run and sign in as the inbox.

The script waits up to 30 minutes. On a headless server, either forward the
port (`ssh -L 8401:localhost:8401 <server>`, run the command there, open the
URL on your laptop) or run it on the laptop and copy `.gmail/tokens.json` to
the server. Google does not rotate the Gmail refresh token, so both copies
keep working.

## Why gmail.modify

`gmail.modify` lets the agent read messages and download attachments (the
invoices), and add or remove **labels**. Once an invoice is in Xero the email
gets the processed label, so the next run can see at a glance what is done
and what is not. `gmail.readonly` could not do that. `gmail.modify` cannot
permanently delete mail or change settings. The repo's Python code never
writes to Gmail at all; label changes happen through the agent's tools, and in
a read-only session the project hook blocks them.

## 7. Create the processed label

In Gmail, signed in as the inbox: **Settings (gear) > See all settings >
Labels > Create new label**, and give it exactly the `processed_label` name
from `config/group.toml` (default `Bookkept`).

## 8. Where the token lives

- `.gmail/tokens.json`, mode 600, git-ignored. Check with
  `git check-ignore .gmail/tokens.json`.
- The access token lasts an hour and is refreshed automatically by
  `gmail_auth.get_access_token()`.
- For an Internal app the refresh token does not expire on a timer; it dies
  only if revoked, if the account's password policy forces it, or after about
  6 months unused. Then repeat step 6.

## 9. Verify

```bash
.venv/bin/python -c "import sys; sys.path.insert(0,'src'); \
from accounting_agent import gmail_auth; print(gmail_auth.get_access_token()[:12], '... ok')"
```

A token prefix and `ok` means the token loads and refreshes. The bootstrap's
"Authorised mailbox" line in step 6 already proved the API call works.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Missing 'GMAIL_CLIENT_ID' in .env` | step 5 |
| `Error 403: org_internal` | you signed in with an account outside the Workspace; use the inbox |
| `No refresh token in response` | revoke the app at https://myaccount.google.com/permissions and re-run (the script asks for `prompt=consent` to force a fresh refresh token) |
| `403 ... Gmail API has not been used in project` | step 2, then wait a minute |
| `invalid_grant` on refresh | token revoked or expired; repeat step 6 |
