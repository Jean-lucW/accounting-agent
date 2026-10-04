"""Multi-entity Xero client layer for the accounting agent."""

from . import banking, contacts, documents, journals, purchases, reports, sales
from .client import (XeroClient, XeroError, XeroValidationError, client_for, entities,
                     resolve_entity)

__all__ = [
    "XeroClient", "XeroError", "XeroValidationError", "client_for", "entities",
    "resolve_entity",
    "banking", "contacts", "documents", "journals", "purchases", "reports", "sales",
]
