"""Moving the published version by hand: accept a held version, or roll back to an older one.

Both move the pointer by compare-and-swap, so neither can undo a publish it did not see.
Each refuses, changing nothing, when the state does not allow the move.
"""

from typing import Any

from wobot.knowledge import repository
from wobot.knowledge.repository import Database


class MaintenanceError(Exception):
    """A move the state does not allow; nothing was changed."""


async def accept(db: Database, version_id: int) -> None:
    """Publish a held version, or a dry run's, as built: against the pointer it was built
    from, so a version built before another publish can no longer go out."""
    async with db.begin() as conn:
        version = await _version(conn, version_id)
        if version.status not in ("held", "validated"):
            raise MaintenanceError(
                f"version {version_id} is {version.status}; only a held or validated version "
                "can be accepted"
            )
        built_on = (version.validation_report or {}).get("built_on_revision")
        if built_on is None:
            raise MaintenanceError(f"version {version_id} does not say what it was built on")
        if not await repository.publish(conn, version_id, built_on):
            raise MaintenanceError(
                f"another version was published after version {version_id} was built; "
                "run ingestion again"
            )


async def rollback(db: Database, version_id: int) -> int | None:
    """Point back at a version published before; returns the version it replaced."""
    async with db.begin() as conn:
        version = await _version(conn, version_id)
        if version.published_at is None:
            raise MaintenanceError(f"version {version_id} was never published")
        pointer = await repository.read_pointer(conn)
        if pointer.index_version_id == version_id:
            raise MaintenanceError(f"version {version_id} is already the active one")
        if version.embedding_config_id != pointer.embedding_config_id:
            # The API embeds questions with the configured model: a version embedded with
            # another would answer from the wrong space.
            raise MaintenanceError(
                f"version {version_id} was embedded with {version.embedding_config_id}, "
                f"the active one with {pointer.embedding_config_id}"
            )
        if not await repository.point_to(conn, version_id, pointer.revision):
            raise MaintenanceError("another version was published meanwhile; check and retry")
        return pointer.index_version_id


async def _version(conn: Any, version_id: int) -> Any:
    version = await repository.read_version(conn, version_id)
    if version is None:
        raise MaintenanceError(f"no version {version_id}")
    return version
