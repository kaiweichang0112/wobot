"""Fetch a listed document: from a local copy when given one, from Google Drive when it
is stored there, or from the website.

Whichever way the bytes come, they are kept under the document's public link, so equal
bytes from a local copy and from the site are one snapshot.
"""

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path

import httpx2

from wobot.knowledge.profiles import GTECH_DOCUMENTS, GTECH_HOST, DocumentProfile
from wobot.knowledge.source import Snapshot
from wobot.knowledge.sources.drive import drive_token, fetch_drive_file
from wobot.knowledge.sources.http import PageFetcher

# A catalog or a manual runs to a few megabytes.
DOCUMENT_MAX_BYTES = 50 * 1024 * 1024
DRIVE_TIMEOUT_SECONDS = 60

FetchDocument = Callable[[DocumentProfile], Awaitable[Snapshot]]


def document_files(values: Sequence[str]) -> dict[str, Path]:
    """`KEY=PATH` arguments: local copies of listed documents, by key."""
    keys = {document.key for document in GTECH_DOCUMENTS}
    files = {}
    for value in values:
        key, separator, path = value.partition("=")
        if not separator or key not in keys:
            raise ValueError(f"--document-file takes KEY=PATH with a key from {sorted(keys)}")
        files[key] = Path(path)
    return files


def document_fetcher(client: httpx2.AsyncClient, files: dict[str, Path]) -> FetchDocument:
    web = PageFetcher(client, {GTECH_HOST}, max_bytes=DOCUMENT_MAX_BYTES)

    async def fetch(document: DocumentProfile) -> Snapshot:
        if (path := files.get(document.key)) is not None:
            return Snapshot(document.url, await asyncio.to_thread(path.read_bytes), {"via": "file"})
        if document.drive_file_id is not None:
            transport = httpx2.AsyncHTTPTransport(retries=2)  # connection failures only
            async with httpx2.AsyncClient(
                timeout=DRIVE_TIMEOUT_SECONDS, transport=transport
            ) as drive:
                file = await fetch_drive_file(drive, document.drive_file_id, await drive_token())
            details = {"via": "drive", "name": file.name, "modified_time": file.modified_time}
            return Snapshot(document.url, file.content, details)
        page = await web.fetch(document.url)
        return Snapshot(document.url, page.content, {"via": "web", "final_url": page.url})

    return fetch
