"""Read-only access to the Revolut Business API for the group's bank feeds.

Authorise once per Revolut Business account, by the slug given to its bank
account in config/group.toml (docs/setup/REVOLUT.md):

    python -m accounting_agent.revolut_auth holdco

then:

    from accounting_agent.revolut import RevolutClient
    rev = RevolutClient("holdco")
    rev.accounts()
    rev.statement_lines(from_date="2026-09-01")

The client refuses every non-GET request; see client.py for why.
"""

from .client import RevolutClient, RevolutError, RevolutReadOnlyError

__all__ = ["RevolutClient", "RevolutError", "RevolutReadOnlyError"]
