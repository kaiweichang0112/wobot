"""Download a stored file from Google Drive as the runtime identity.

Drive access is not an IAM role: the file is shared with the service account's email, and
the token must carry the drive.readonly scope.
"""

import asyncio
import hashlib
from dataclasses import dataclass

import google.auth
import google.auth.transport.requests
import httpx2

DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
FILES_URL = "https://www.googleapis.com/drive/v3/files"
# A catalog or a brochure is a few megabytes; far more means the wrong file.
MAX_BYTES = 50 * 1024 * 1024


class DriveError(Exception):
    """The file cannot be read, or what arrived is not what Drive described."""


@dataclass(frozen=True)
class DriveFile:
    name: str
    modified_time: str
    sha256: str
    content: bytes


async def drive_token() -> str:
    """A short-lived token with the Drive scope, from Application Default Credentials."""

    def refresh() -> str:
        credentials, _ = google.auth.default(scopes=[DRIVE_SCOPE])
        credentials.refresh(google.auth.transport.requests.Request())
        return credentials.token

    return await asyncio.to_thread(refresh)


async def fetch_drive_file(client: httpx2.AsyncClient, file_id: str, token: str) -> DriveFile:
    headers = {"Authorization": f"Bearer {token}"}
    url = f"{FILES_URL}/{file_id}"
    metadata = await client.get(
        url,
        headers=headers,
        params={
            "fields": "name,mimeType,size,sha256Checksum,modifiedTime",
            "supportsAllDrives": "true",
        },
    )
    if metadata.status_code == 404:
        # Drive answers 404, not 403, for a file the caller may not see.
        raise DriveError("file not found: is it shared with the service account?")
    metadata.raise_for_status()
    described = metadata.json()
    if "sha256Checksum" not in described:
        # Google Docs, Sheets and Slides have no bytes of their own, only exports.
        raise DriveError(f"{described.get('mimeType')} is not a stored file")
    if int(described["size"]) > MAX_BYTES:
        raise DriveError(f"{described['size']} bytes is over the {MAX_BYTES} byte limit")

    download = await client.get(
        url, headers=headers, params={"alt": "media", "supportsAllDrives": "true"}
    )
    download.raise_for_status()
    sha256 = hashlib.sha256(download.content).hexdigest()
    if sha256 != described["sha256Checksum"]:
        # The file changed between the two requests, or the download was cut short.
        raise DriveError("downloaded bytes do not match the checksum Drive reported")
    return DriveFile(
        name=described["name"],
        modified_time=described["modifiedTime"],
        sha256=sha256,
        content=download.content,
    )
