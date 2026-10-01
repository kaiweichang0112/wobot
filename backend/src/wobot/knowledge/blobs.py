"""Content-addressed storage for the bytes each snapshot was read from."""

import asyncio
import hashlib
import os
from pathlib import Path
from typing import Protocol

from google.api_core.exceptions import PreconditionFailed
from google.cloud.storage import Bucket


def blob_key(content: bytes) -> str:
    return f"sha256/{hashlib.sha256(content).hexdigest()}"


class BlobStore(Protocol):
    async def put(self, content: bytes) -> str:
        """Store the bytes under their SHA-256 unless already there, and return the key."""
        ...


class LocalBlobStore:
    """Blobs in a local directory, for development."""

    def __init__(self, root: Path) -> None:
        self._root = root

    async def put(self, content: bytes) -> str:
        key = blob_key(content)
        await asyncio.to_thread(self._write_once, self._root / key, content)
        return key

    @staticmethod
    def _write_once(path: Path, content: bytes) -> None:
        if path.exists():
            return  # the key is the content's hash: the same key holds the same bytes
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        temporary.write_bytes(content)
        # A rename is atomic: a reader sees the whole file or no file, never half of one.
        temporary.replace(path)


class GcsBlobStore:
    """Blobs in a Cloud Storage bucket, for an identity that may create and read objects.

    Nothing is ever overwritten, so the identity needs no right to delete.
    """

    def __init__(self, bucket: Bucket) -> None:
        self._bucket = bucket

    async def put(self, content: bytes) -> str:
        key = blob_key(content)
        await asyncio.to_thread(self._upload_once, key, content)
        return key

    def _upload_once(self, key: str, content: bytes) -> None:
        blob = self._bucket.blob(key)
        if blob.exists():
            return  # the key is the content's hash: the same key holds the same bytes
        try:
            # Generation 0 means "no such object": create it, never replace one.
            blob.upload_from_string(
                content, content_type="application/octet-stream", if_generation_match=0
            )
        except PreconditionFailed:
            pass  # another run stored the same bytes between the check and the upload
