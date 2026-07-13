"""Microsoft Graph client for the SharePoint document library."""

from __future__ import annotations

import logging
import os
from typing import Iterator

import msal
import requests

logger = logging.getLogger(__name__)

TENANT_ID = os.getenv("SHAREPOINT_TENANT_ID")
CLIENT_ID = os.getenv("SHAREPOINT_CLIENT_ID")
CLIENT_SECRET = os.getenv("SHAREPOINT_CLIENT_SECRET")
SITE_ID = os.getenv("SHAREPOINT_SITE_ID")
DRIVE_ID = os.getenv("SHAREPOINT_DRIVE_ID")
# Optional subfolder inside the drive; empty means the drive root.
FOLDER_PATH = os.getenv("SHAREPOINT_FOLDER_PATH", "").strip("/")
RECURSIVE = os.getenv("SHAREPOINT_RECURSIVE", "0") == "1"

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
GRAPH_SCOPE = ["https://graph.microsoft.com/.default"]


class SharePointError(Exception):
    """Raised for SharePoint/Graph failures."""


def _require_config() -> None:
    missing = [
        name
        for name, val in (
            ("SHAREPOINT_TENANT_ID", TENANT_ID),
            ("SHAREPOINT_CLIENT_ID", CLIENT_ID),
            ("SHAREPOINT_CLIENT_SECRET", CLIENT_SECRET),
            ("SHAREPOINT_SITE_ID", SITE_ID),
            ("SHAREPOINT_DRIVE_ID", DRIVE_ID),
        )
        if not val
    ]
    if missing:
        raise SharePointError(
            "Missing SharePoint env vars: " + ", ".join(missing)
        )


def _get_access_token() -> str:
    """Acquire a Graph access token with MSAL."""
    _require_config()
    authority = f"https://login.microsoftonline.com/{TENANT_ID}"
    app = msal.ConfidentialClientApplication(
        CLIENT_ID, authority=authority, client_credential=CLIENT_SECRET
    )
    result = app.acquire_token_for_client(scopes=GRAPH_SCOPE)
    if not isinstance(result, dict) or "access_token" not in result:
        # This description is safe to log.
        detail = result.get("error_description") if isinstance(result, dict) else result
        raise SharePointError(f"Failed to acquire Graph token: {detail}")
    return result["access_token"]


def _folder_children_url(folder_rel_path: str) -> str:
    """Build the Graph URL for a folder's children."""
    if folder_rel_path:
        # Use the Graph path syntax for subfolders.
        return f"{GRAPH_BASE}/drives/{DRIVE_ID}/root:/{folder_rel_path}:/children"
    return f"{GRAPH_BASE}/drives/{DRIVE_ID}/root/children"


def _walk(folder_rel_path: str, headers: dict) -> Iterator[dict]:
    """Yield raw driveItem dicts for files under a folder."""
    url = _folder_children_url(folder_rel_path)
    while url:
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        for item in data.get("value", []):
            if "folder" in item:
                if RECURSIVE:
                    sub = f"{folder_rel_path}/{item['name']}".strip("/")
                    yield from _walk(sub, headers)
                continue
            if "file" in item:
                # Attach the folder path so callers can build a stable source path.
                item["_folder_rel_path"] = folder_rel_path
                yield item
        url = data.get("@odata.nextLink")


def list_files() -> list[dict]:
    """Return files under the configured folder as a normalized dict list."""
    token = _get_access_token()
    headers = {"Authorization": f"Bearer {token}"}
    files: list[dict] = []
    try:
        for item in _walk(FOLDER_PATH, headers):
            folder = item.get("_folder_rel_path") or ""
            path = f"{folder}/{item['name']}".strip("/") if folder else item["name"]
            download_url = item.get("@microsoft.graph.downloadUrl")
            if not download_url:
                # Skip files without a download URL.
                logger.warning("Skipping %s: no downloadUrl in Graph response", path)
                continue
            files.append(
                {
                    "id": item["id"],
                    "name": item["name"],
                    "path": path,
                    "last_modified": item.get("lastModifiedDateTime"),
                    "download_url": download_url,
                }
            )
    except requests.HTTPError as e:
        raise SharePointError(f"Graph list failed: {e}") from e
    return files


def download_file(download_url: str) -> bytes:
    """Fetch raw bytes for a drive item from its pre-authenticated URL."""
    try:
        resp = requests.get(download_url, timeout=60)
        resp.raise_for_status()
        return resp.content
    except requests.HTTPError as e:
        raise SharePointError(f"Graph download failed: {e}") from e
