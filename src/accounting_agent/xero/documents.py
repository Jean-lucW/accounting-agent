"""Supporting documents: attachments on Xero records.

Constraints (docs/XERO_API_REFERENCE.md section 5.3):
- Max 10 attachments per document. Per-file size limits conflict across
  Xero's docs (3 MB for ManualJournals/Accounts, 10 MB per the Attachments
  page, 25 MB per Contacts/Invoices), treat the lowest figure quoted for
  the endpoint as the safe ceiling.
- Upload body is the raw file bytes (no JSON wrapper) with the file's MIME
  type; re-uploading an existing filename REPLACES that attachment.
- Filenames containing < > : " / \\ | ? * NUL + are rejected with 400.
- IncludeOnline=true is valid only for AR invoices and AR credit notes.
- Scopes: accounting.attachments / accounting.attachments.read.
"""

from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Any

from .client import XeroClient

# Endpoints that accept attachments (docs/XERO_API_REFERENCE.md 5.3).
ATTACHMENT_ENDPOINTS = (
    "Invoices",
    "Receipts",
    "CreditNotes",
    "RepeatingInvoices",
    "BankTransactions",
    "BankTransfers",
    "Contacts",
    "Accounts",
    "ManualJournals",
    "PurchaseOrders",
    "Quotes",
)


def attach_file(
    client: XeroClient,
    endpoint: str,
    guid: str,
    file_path: str | Path,
    include_online: bool = False,
) -> dict[str, Any]:
    """Upload a local file as an attachment on {endpoint}/{guid}.

    Content type is guessed from the filename (application/octet-stream if
    unknown). The parent record must already exist; an existing attachment
    with the same filename is replaced. include_online only applies to AR
    invoices/credit notes.
    """
    if endpoint not in ATTACHMENT_ENDPOINTS:
        raise ValueError(
            f"{endpoint!r} does not accept attachments; supported: {ATTACHMENT_ENDPOINTS}"
        )
    path = Path(file_path)
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return client.upload_attachment(
        endpoint, guid, path.name, path.read_bytes(),
        content_type=content_type, include_online=include_online,
    )


def get_attachments(client: XeroClient, endpoint: str, guid: str) -> list[dict[str, Any]]:
    """List attachments on {endpoint}/{guid}.

    Each entry has AttachmentID, FileName, Url, MimeType, ContentLength.
    """
    return client.list_attachments(endpoint, guid)


def download_all(
    client: XeroClient, endpoint: str, guid: str, out_dir: str | Path
) -> list[Path]:
    """Download every attachment on {endpoint}/{guid} into out_dir.

    Returns the list of written file paths.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for attachment in get_attachments(client, endpoint, guid):
        data = client.download_attachment(endpoint, guid, attachment)
        target = out_dir / attachment["FileName"]
        target.write_bytes(data)
        paths.append(target)
    return paths
