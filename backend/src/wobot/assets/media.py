"""Private media: the photos and recordings an asset is made from, and the frames it is.

Keys follow from version IDs (`incoming/voices/{voice_id}`), so no table records an
upload. An object is created once and never replaced: a request sent again finds what the
first one stored, and a frame made again gets a key of its own, from its hash.
"""

import asyncio
import os
import shutil
import uuid
from pathlib import Path
from typing import Protocol

from google.api_core.exceptions import NotFound, PreconditionFailed
from google.cloud import storage
from google.cloud.storage import Bucket

from wobot.config import Settings

# What the bucket's lifecycle deletes after a day (infra/README.md, section 6): a photo or
# recording left behind by a failed job does not stay.
INCOMING = "incoming"


def voice_sample_key(voice_id: uuid.UUID) -> str:
    return f"{INCOMING}/voices/{voice_id}"


def avatar_photo_key(avatar_id: uuid.UUID) -> str:
    return f"{INCOMING}/avatars/{avatar_id}.png"


def avatar_prefix(avatar_id: uuid.UUID) -> str:
    """Where an avatar's frames are kept; removed whole once the avatar is unused."""
    return f"avatars/{avatar_id}/"


def frame_key_of(avatar_id: uuid.UUID, frame_key: str, sha256: str) -> str:
    return f"{avatar_prefix(avatar_id)}{frame_key}-{sha256[:16]}.png"


class MediaStore(Protocol):
    async def put(self, key: str, content: bytes, content_type: str) -> None:
        """Store the bytes under the key unless something is already there."""
        ...

    async def get(self, key: str) -> bytes | None:
        """The bytes, or None when the key holds nothing."""
        ...

    async def delete(self, key: str) -> None:
        """Remove the key; removing what is not there is no error."""
        ...

    async def delete_prefix(self, prefix: str) -> None:
        """Remove every key under the prefix."""
        ...


class LocalMediaStore:
    """Media in a local directory, for development; `.data/` is never committed."""

    def __init__(self, root: Path) -> None:
        self._root = root

    async def put(self, key: str, content: bytes, content_type: str) -> None:
        await asyncio.to_thread(self._write_once, self._root / key, content)

    async def get(self, key: str) -> bytes | None:
        path = self._root / key
        return await asyncio.to_thread(lambda: path.read_bytes() if path.exists() else None)

    async def delete(self, key: str) -> None:
        await asyncio.to_thread((self._root / key).unlink, missing_ok=True)

    async def delete_prefix(self, prefix: str) -> None:
        await asyncio.to_thread(shutil.rmtree, self._root / prefix, ignore_errors=True)

    @staticmethod
    def _write_once(path: Path, content: bytes) -> None:
        if path.exists():
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        temporary.write_bytes(content)
        temporary.replace(path)  # atomic: a reader sees the whole file or none


class GcsMediaStore:
    """Media in the private media bucket. The API may create and read objects; only the
    asset worker may delete them."""

    def __init__(self, bucket: Bucket) -> None:
        self._bucket = bucket

    async def put(self, key: str, content: bytes, content_type: str) -> None:
        await asyncio.to_thread(self._upload_once, key, content, content_type)

    async def get(self, key: str) -> bytes | None:
        try:
            return await asyncio.to_thread(self._bucket.blob(key).download_as_bytes)
        except NotFound:
            return None

    async def delete(self, key: str) -> None:
        try:
            await asyncio.to_thread(self._bucket.blob(key).delete)
        except NotFound:
            pass

    async def delete_prefix(self, prefix: str) -> None:
        for blob in await asyncio.to_thread(lambda: list(self._bucket.list_blobs(prefix=prefix))):
            await self.delete(blob.name)

    def _upload_once(self, key: str, content: bytes, content_type: str) -> None:
        try:
            # Generation 0 means "no such object": create it, never replace one.
            self._bucket.blob(key).upload_from_string(
                content, content_type=content_type, if_generation_match=0
            )
        except PreconditionFailed:
            pass  # the same request, sent again, stored it first


def media_store(settings: Settings) -> MediaStore:
    """The media bucket in the cloud; a local directory when none is set."""
    if settings.media_bucket is None:
        return LocalMediaStore(settings.media_local_dir)
    client = storage.Client(project=settings.google_cloud_project)
    return GcsMediaStore(client.bucket(settings.media_bucket))
