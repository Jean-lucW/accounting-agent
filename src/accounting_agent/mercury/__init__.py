"""Read-only access to the Mercury API for the group's US bank feeds.

One API token per Mercury business, kept in .env as MERCURY_<SLUG>_KEY, where
<SLUG> is the slug of a provider = "mercury" bank account in
config/group.toml, upper-cased. Create it in Mercury > Settings > API Tokens
as a READ ONLY token (no IP allowlist needed for read-only). Setup guide:
docs/setup/MERCURY.md.

    from accounting_agent.mercury import MercuryClient
    mer = MercuryClient("opcous")
    mer.accounts()
    mer.statement_lines(from_date="2026-09-01")

The client refuses every non-GET request; see client.py for why.
"""

from .client import (MercuryClient, MercuryError, MercuryReadOnlyError, config_slugs,
                     configured_entities)

__all__ = ["MercuryClient", "MercuryError", "MercuryReadOnlyError", "config_slugs",
           "configured_entities"]
