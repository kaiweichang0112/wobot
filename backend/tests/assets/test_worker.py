"""The asset worker against the database, as its own role, with ElevenLabs faked: a
version is made once however many executions start for it, and a voice made by an
attempt that died is found again rather than made twice (DEC-086)."""

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator

import httpx2
import pytest
from sqlalchemy import URL
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from tests.database import ASSETS_USER, run_as_migrator
from wobot.assets.eleven import ElevenLabs, voice_name
from wobot.assets.media import LocalMediaStore, voice_sample_key
from wobot.assets.worker import Worker
from wobot.config import get_settings

OWNER = "uid-asset-worker-tests"
ADD_VOICE = (
    "INSERT INTO app.voice_versions (voice_id, owner_id, status, sample_seconds) "
    "VALUES ($1, $2, 'queued', 90)"
)


class FakeElevenLabs:
    """ElevenLabs' four endpoints, keeping the voices it holds and the calls it got."""

    def __init__(self) -> None:
        self.voices: dict[str, str] = {}  # name -> provider ID
        self.calls: list[str] = []
        self.fail_with: int | None = None
        self.on_add = None  # awaited while a clone is made

    async def handler(self, request: httpx2.Request) -> httpx2.Response:
        call = f"{request.method} {request.url.path}"
        self.calls.append(call)
        assert request.headers["xi-api-key"] == "test-key"
        if self.fail_with is not None:
            status_code, error_type = self.fail_with
            detail = {"type": error_type, "code": "c", "message": "m"} if error_type else None
            return httpx2.Response(status_code, json={"detail": detail})
        if call == "GET /v2/voices":
            # Matches parts of names, as ElevenLabs' search does.
            search = request.url.params["search"]
            found = [{"voice_id": v, "name": n} for n, v in self.voices.items() if search in n]
            return httpx2.Response(200, json={"voices": found, "has_more": False})
        if call == "POST /v1/voices/add":
            name = _form_field(await request.aread(), "name")
            if self.on_add is not None:
                await self.on_add()
            self.voices[name] = f"provider-{len(self.voices) + 1}"
            return httpx2.Response(200, json={"voice_id": self.voices[name]})
        if request.method == "DELETE":
            provider_voice_id = request.url.path.rsplit("/", 1)[-1]
            if provider_voice_id not in self.voices.values():
                return httpx2.Response(404)
            self.voices = {n: v for n, v in self.voices.items() if v != provider_voice_id}
            return httpx2.Response(200, json={"status": "ok"})
        return httpx2.Response(500)

    def client(self) -> ElevenLabs:
        transport = httpx2.MockTransport(self.handler)
        return ElevenLabs(httpx2.AsyncClient(transport=transport), "test-key")


def _form_field(content: bytes, field: str) -> str:
    body = content.decode("latin-1")
    marker = f'name="{field}"\r\n\r\n'
    return body.split(marker, 1)[1].split("\r\n", 1)[0]


@pytest.fixture
async def sessionmaker() -> AsyncIterator[async_sessionmaker]:
    """Connections as the worker's role, which cannot add or remove rows."""
    settings = get_settings()
    url = URL.create(
        "postgresql+asyncpg",
        username=ASSETS_USER,
        password=settings.db_password,
        host=settings.db_host,
        port=settings.db_port,
        database=settings.db_name,
    )
    engine = create_async_engine(url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
def owner() -> Iterator[str]:
    asyncio.run(
        run_as_migrator(
            "INSERT INTO app.accounts (account_id, email) VALUES ($1, 'w@example.com') "
            "ON CONFLICT DO NOTHING",
            OWNER,
        )
    )
    yield OWNER
    asyncio.run(run_as_migrator("DELETE FROM app.accounts WHERE account_id = $1", OWNER))


@pytest.fixture
def eleven() -> FakeElevenLabs:
    return FakeElevenLabs()


@pytest.fixture
def media(tmp_path) -> LocalMediaStore:
    return LocalMediaStore(tmp_path)


async def queued_voice(media: LocalMediaStore, owner: str) -> uuid.UUID:
    voice_id = uuid.uuid4()
    await media.put(voice_sample_key(voice_id), b"sample", "audio/mp4")
    await run_as_migrator(ADD_VOICE, voice_id, owner)
    return voice_id


async def row(voice_id: uuid.UUID) -> dict:
    rows = await run_as_migrator(
        "SELECT status, provider_voice_id, error_code, execution, cleaned_at "
        "FROM app.voice_versions WHERE voice_id = $1",
        voice_id,
    )
    return dict(rows[0])


def worker(sessionmaker, media, eleven: FakeElevenLabs, execution: str = "e-1") -> Worker:
    return Worker(sessionmaker, media, eleven.client(), execution)


async def test_a_queued_voice_is_cloned_and_its_sample_removed(sessionmaker, owner, eleven, media):
    voice_id = await queued_voice(media, owner)

    outcome = await worker(sessionmaker, media, eleven).make_voice(voice_id)

    assert outcome == "ready"
    assert await row(voice_id) | {"cleaned_at": None} == {
        "status": "ready",
        "provider_voice_id": eleven.voices[voice_name(voice_id)],
        "error_code": None,
        "execution": "e-1",
        "cleaned_at": None,
    }
    assert await media.get(voice_sample_key(voice_id)) is None


async def test_two_executions_started_for_one_voice_clone_it_once(
    sessionmaker, owner, eleven, media
):
    voice_id = await queued_voice(media, owner)

    outcomes = await asyncio.gather(
        worker(sessionmaker, media, eleven, "e-1").make_voice(voice_id),
        worker(sessionmaker, media, eleven, "e-2").make_voice(voice_id),
    )

    assert sorted(outcomes) == ["not_queued", "ready"]
    assert eleven.calls.count("POST /v1/voices/add") == 1


async def test_a_voice_an_earlier_attempt_cloned_is_found_not_cloned_again(
    sessionmaker, owner, eleven, media
):
    voice_id = await queued_voice(media, owner)
    # An attempt cloned it and died before recording the ID; a version with a longer
    # name is not mistaken for it.
    eleven.voices[voice_name(voice_id)] = "provider-earlier"
    eleven.voices[voice_name(voice_id) + "-other"] = "provider-other"

    await worker(sessionmaker, media, eleven).make_voice(voice_id)

    assert (await row(voice_id))["provider_voice_id"] == "provider-earlier"
    assert "POST /v1/voices/add" not in eleven.calls


@pytest.mark.parametrize(
    ("failure", "error_code"),
    [
        ((422, "validation_error"), "VOICE_REJECTED"),
        ((500, None), "PROVIDER_UNAVAILABLE"),
        ((429, None), "PROVIDER_UNAVAILABLE"),
        # A wrong key is ours to fix, not the sample: ElevenLabs answers it with 400.
        ((400, "authentication_error"), "PROVIDER_UNAVAILABLE"),
    ],
)
async def test_a_clone_elevenlabs_refuses_fails_the_version(
    sessionmaker, owner, eleven, media, failure, error_code
):
    voice_id = await queued_voice(media, owner)
    eleven.fail_with = failure

    outcome = await worker(sessionmaker, media, eleven).make_voice(voice_id)

    assert outcome == "failed"
    assert (await row(voice_id))["error_code"] == error_code
    # Kept for a retry; the bucket's lifecycle removes it within a day.
    assert await media.get(voice_sample_key(voice_id)) == b"sample"


async def test_a_voice_without_its_sample_fails(sessionmaker, owner, eleven, media):
    voice_id = await queued_voice(media, owner)
    await media.delete(voice_sample_key(voice_id))

    await worker(sessionmaker, media, eleven).make_voice(voice_id)

    assert (await row(voice_id))["error_code"] == "SAMPLE_MISSING"


async def test_a_voice_deleted_while_cloned_is_not_overwritten(sessionmaker, owner, eleven, media):
    voice_id = await queued_voice(media, owner)

    async def delete() -> None:  # the user deletes it from another device
        await run_as_migrator(
            "UPDATE app.voice_versions SET status = 'deleted', finished_at = now() "
            "WHERE voice_id = $1",
            voice_id,
        )

    eleven.on_add = delete

    outcome = await worker(sessionmaker, media, eleven).make_voice(voice_id)

    assert outcome == "superseded"
    assert (await row(voice_id))["status"] == "deleted"
    assert eleven.voices == {}  # the clone it made is removed


async def test_cleaning_removes_a_retired_voice_once(sessionmaker, owner, eleven, media):
    voice_id = await queued_voice(media, owner)
    w = worker(sessionmaker, media, eleven)
    await w.make_voice(voice_id)
    await run_as_migrator(
        "UPDATE app.voice_versions SET status = 'retired' WHERE voice_id = $1", voice_id
    )

    assert await w.clean_voice(voice_id) == "cleaned"
    assert await w.clean_voice(voice_id) == "nothing_to_clean"
    assert eleven.voices == {}
    assert (await row(voice_id))["cleaned_at"] is not None


async def test_cleaning_finds_a_clone_whose_version_ended_first(sessionmaker, owner, eleven, media):
    voice_id = await queued_voice(media, owner)
    eleven.voices[voice_name(voice_id)] = "provider-orphan"
    await run_as_migrator(
        "UPDATE app.voice_versions SET status = 'deleted', finished_at = now() WHERE voice_id = $1",
        voice_id,
    )

    assert await worker(sessionmaker, media, eleven).clean_voice(voice_id) == "cleaned"
    assert eleven.voices == {}


async def test_a_voice_in_use_is_never_cleaned(sessionmaker, owner, eleven, media):
    voice_id = await queued_voice(media, owner)
    w = worker(sessionmaker, media, eleven)
    await w.make_voice(voice_id)
    await run_as_migrator(
        "UPDATE app.voice_versions SET status = 'active' WHERE voice_id = $1", voice_id
    )

    assert await w.clean_voice(voice_id) == "nothing_to_clean"
    assert eleven.voices != {}


async def test_a_voice_already_gone_at_elevenlabs_counts_as_removed(
    sessionmaker, owner, eleven, media
):
    voice_id = await queued_voice(media, owner)
    w = worker(sessionmaker, media, eleven)
    await w.make_voice(voice_id)
    eleven.voices.clear()  # removed by hand in ElevenLabs' console
    await run_as_migrator(
        "UPDATE app.voice_versions SET status = 'retired' WHERE voice_id = $1", voice_id
    )

    assert await w.clean_voice(voice_id) == "cleaned"
