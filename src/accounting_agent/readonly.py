"""Read-only mode for the whole agent, switched on by one environment variable.

    AGENT_READONLY=1

Set by `deploy/run-query.sh` for every `query` session started from Slack by
the read-only tier (and by admins using `query`). While it is set:

  - `XeroClient.request` refuses every method except GET, before it even
    fetches an access token, so no token is spent and nothing reaches Xero.
  - `.claude/hooks/guard-readonly.py` denies Bash commands and file edits that
    would write anywhere but the session's own scratch folder.

The Revolut and Mercury clients are GET-only at all times already; Google
Drive's writes (`gdrive.upload_file` and the folder, rename and move helpers
the publish step uses) call `refuse()` first. Gmail has no Python write path
in this repo, so the hook is what stops a session labelling or trashing mail.

This is the same pattern as the Revolut client's transport lock: one place,
below every helper, so no code path present or future can write while the
flag is up.
"""

from __future__ import annotations

import os

ENV = "AGENT_READONLY"


class ReadOnlyError(RuntimeError):
    """A write was attempted in a read-only session."""


def active() -> bool:
    return os.environ.get(ENV, "").strip().lower() not in ("", "0", "false", "no")


def refuse(what: str) -> None:
    """Raise if read-only mode is on. `what` names the blocked operation."""
    if active():
        raise ReadOnlyError(
            f"{what} blocked: this is a read-only session ({ENV} is set). "
            "Answer from what you can read; nothing may be created, changed, "
            "uploaded or deleted. Do not unset the variable or work around it."
        )
