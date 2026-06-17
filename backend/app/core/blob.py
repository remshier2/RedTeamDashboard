"""Azure Blob Storage helper for engagement exports.

Uses DefaultAzureCredential (managed identity in Container Apps, env/CLI creds
in dev). If AZURE_STORAGE_ACCOUNT_NAME is unset all operations return None —
lifecycle endpoints degrade gracefully without blob config so local dev works.
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

log = logging.getLogger(__name__)


def upload_engagement_export(slug: str, data: dict[str, Any]) -> str | None:
    """Serialize export dict to JSON and upload to blob. Returns blob URL or None."""
    from app.core.config import settings

    if not settings.azure_storage_account_name:
        return None

    try:
        from azure.identity import DefaultAzureCredential
        from azure.storage.blob import BlobServiceClient

        credential = DefaultAzureCredential()
        client = BlobServiceClient(
            account_url=f"https://{settings.azure_storage_account_name}.blob.core.windows.net",
            credential=credential,
        )
        ts = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
        blob_name = f"{slug}/{ts}.json"
        payload = json.dumps(data, default=str, indent=2).encode()
        container = client.get_container_client(settings.azure_storage_container_name)
        container.upload_blob(blob_name, payload, overwrite=True)
        return (
            f"https://{settings.azure_storage_account_name}.blob.core.windows.net"
            f"/{settings.azure_storage_container_name}/{blob_name}"
        )
    except Exception:
        log.exception("blob upload failed for engagement %s — continuing without export URL", slug)
        return None
