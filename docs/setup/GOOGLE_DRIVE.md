# Connecting Google Drive

Drive is optional. With it the agent can read source documents on Drive
(payroll reports, contracts, statements people file there) and **publish**
the workbooks it builds (bills payable, intercompany reconciliation, standing
reports, the outstanding-items register) into one folder your team opens.
Without it, workbooks stay on the server under `data/reports/`.

Do `docs/setup/GMAIL.md` first: Drive reuses the same Google Cloud project and
OAuth client. Allow about 15 minutes.

Code involved: `src/accounting_agent/gdrive_auth.py` (the consent),
`src/accounting_agent/gdrive.py` (the client), `src/accounting_agent/publish.py`
(where each workbook goes).

## 1. Enable the Drive API

1. Open https://console.cloud.google.com/ and select the project from the
   Gmail guide.
2. **APIs & Services > Library**, search **Google Drive API**, click
   **Enable**.
3. On the OAuth consent screen's scopes (**Data access**), make sure
   `https://www.googleapis.com/auth/drive.readonly` and
   `https://www.googleapis.com/auth/drive.file` are listed. Save.

## 2. What the two scopes allow

| Scope | Allows |
|---|---|
| `drive.readonly` | read every file and folder the consenting account can see, including shared drives it is a member of |
| `drive.file` | create files and folders, and change only the files and folders **this app created** |

Together: the agent reads everything it can see, and the only things it can
write are its own published workbooks and their folders. It cannot edit,
move or delete anything a person made. On top of that the code only writes
into the publish folder (and folders it creates under it), plus any folder
IDs you list in `DRIVE_WRITE_ALLOWLIST`, and refuses all writes in a
read-only session.

## 3. Choose the consenting account

The token reads what its account can see, so consent as an account that is a
**member of every shared drive** the agent should read. By default the
bootstrap suggests the accounting inbox from `config/group.toml`; to use a
different account, add it to `.env`:

```
GDRIVE_LOGIN_HINT=finance-bot@example.com
```

To use a separate OAuth client for Drive, set `GDRIVE_CLIENT_ID` and
`GDRIVE_CLIENT_SECRET` in `.env`; otherwise `GMAIL_CLIENT_ID` and
`GMAIL_CLIENT_SECRET` are used.

## 4. Run the bootstrap

```bash
.venv/bin/python -m accounting_agent.gdrive_auth
```

1. Open the printed URL (it opens by itself on a desktop) and sign in as the
   account from step 3.
2. Approve "See and download all your Google Drive files" and "See, edit,
   create and delete only the specific Google Drive files you use with this
   app".
3. The terminal prints the token path, the account, and the shared drives it
   can see:

   ```
   Tokens saved to .../.gdrive/tokens.json
   Authorised as: accounting@example.com
   Shared drives visible:
     Finance  0AB...
   ```

   "No shared drives visible" means the account is not a member of any: add
   it in Drive (shared drive > Manage members) and re-run.

The redirect is `http://localhost:8402/callback`. On a headless server use
`ssh -L 8402:localhost:8402 <server>` and open the URL on your laptop, or run
the bootstrap on the laptop and copy `.gdrive/tokens.json` to the server
(`chmod 600`). Google does not rotate the Drive refresh token, so both copies
keep working.

## 5. Create the publish folder

1. In Google Drive, open the shared drive your finance team uses (a shared
   drive, so files do not belong to one person's My Drive).
2. **New > New folder**, name it, for example, `Accounting Agent`.
3. Make sure the consenting account is a member of that shared drive with
   at least **Contributor** access (it must be able to add files).
4. Open the folder. Its ID is the last part of the address bar:
   `https://drive.google.com/drive/folders/<THIS PART>`.

## 6. Put the folder in config/group.toml

```toml
[drive]
publish_folder_id = "<the folder id>"
  [drive.subfolders]
  reports = "Reports"
  bills = "Bills Payable"
  intercompany = "Intercompany"
  bank_fees = "Bank and FX Fees"
  outstanding = ""            # blank = the root of the publish folder
```

Each workbook kind lands in its subfolder; the agent creates a subfolder the
first time it publishes into it. A blank `publish_folder_id` turns publishing
off: builders print a note and keep the file local. To steer a file name to a
kind that the built-in patterns do not catch, add:

```toml
  [drive.routes]
  reports = ["^Cash Forecast"]
```

(kind -> list of case-insensitive regular expressions, tried first). Files
built with a date in the name publish under the name without it, so Drive
keeps one copy that every build overwrites, with links and version history
intact.

Other folders the agent may write into, outside the publish folder, go in
`.env` as comma-separated folder IDs:

```
DRIVE_WRITE_ALLOWLIST=1AbC...,1XyZ...
```

## 7. Verify

```bash
.venv/bin/python -m accounting_agent.gdrive whoami      # the consenting account
.venv/bin/python -m accounting_agent.gdrive drives      # shared drives it sees
.venv/bin/python -m accounting_agent.gdrive ls <publish folder id>
```

Then publish a test file:

```bash
echo test > /tmp/drive-test.txt
.venv/bin/python -m accounting_agent.gdrive put /tmp/drive-test.txt
```

It prints `created <id> ... drive-test.txt <link>`. Open the link, then
delete the file in Drive. Running `put` again on the same name prints
`updated` instead: the agent overwrites its own copy rather than adding a
second one.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Google Drive API is disabled on this project` | step 1, wait a minute, run `gdrive whoami` |
| `the Drive token is read-only` | the token was consented without `drive.file`; re-run step 4 |
| `folder ... is not on the publish allowlist` | set `publish_folder_id` (step 6) or add the folder to `DRIVE_WRITE_ALLOWLIST` |
| `HTTP 403` on upload | the account is not a Contributor on the shared drive |
| "a hand-uploaded copy of the same name is still in the folder" | a person uploaded a file with that name; `drive.file` cannot overwrite it. Move it aside once |
