"""Content-addressed storage for the bytes each snapshot was read from."""

import asyncio
import hashlib
import os
from pathlib import Path
from typing import Protocol


def blob_key(content: bytes) -> str:
    return f"sha256/{hashlib.sha256(content).hexdigest()}"


class BlobStore(Protocol):
    async def put(self, content: bytes) -> str:
        """Store the bytes under their SHA-256 unless already there, and return the key."""
        ...


class LocalBlobStore:
    """Blobs in a local directory, for development; A3 adds Cloud Storage."""

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
