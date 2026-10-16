"""The daily sweep (E2): each step moves rows on from how they stand, so whatever a
request or a job left undone is finished the next day, and a pass may run again."""

import asyncio
import uuid
from datetime import timedelta

import pytest

from tests.api import start
from tests.database import run_as_migrator
from tests.test_conversations_api import checkpoints_of, save_checkpoint
from tests.test_turn_control_api import running_turn
from tests.test_voices_api import assets, created, made, status_of  # noqa: F401
from wobot.api.conversations import thread_of
from wobot.api.main import app
from wobot.assets.dispatch import DispatchError
from wobot.maintenance.sweep import Report, sweep

pytestmark = pytest.mark.usefixtures("members")


def migrate(sql: str, *args) -> list:
    return asyncio.run(run_as_migrator(sql, *args))


def run_sweep(client, dispatcher="app") -> Report:
    chosen = app.state.dispatcher if dispatcher == "app" else dispatcher
    return client.portal.call(sweep, app.state.sessionmaker, chosen)


def delete_account(uid: str, ago: timedelta) -> None:
    """Marks the account deleted the interval given ago, as `DELETE /v1/me` does."""
    migrate(
        "UPDATE app.accounts SET deleted_at = now() - $2::interval WHERE account_id = $1",
        uid,
        ago,
    )


def set_voice(voice_id: uuid.UUID, assignments: str) -> None:
    migrate(f"UPDATE app.voice_versions SET {assignments} WHERE voice_id = $1", voice_id)


def test_a_turn_left_running_is_failed_and_one_within_its_time_is_not(client, as_member):
    as_member("uid-a")
    conversation = start(client)
    lost = running_turn(conversation, minutes_ago=10)
    other = start(client)
    fresh = running_turn(other, minutes_ago=0)

    report = run_sweep(client, None)

    statuses = dict(
        (row["turn_id"], (row["status"], row["error_code"]))
        for row in migrate(
            "SELECT turn_id, status, error_code FROM app.turns WHERE turn_id = ANY($1)",
            [lost, fresh],
        )
    )
    assert statuses[lost] == ("failed", "STREAM_INTERRUPTED")
    assert statuses[fresh] == ("running", None)
    assert report.stale_turns >= 1


def test_a_version_whose_worker_stopped_beating_fails(client, as_member, assets):  # noqa: F811
    as_member("uid-a")
    voice_id = created(client)
    set_voice(
        voice_id,
        "status = 'running', started_at = now() - interval '10 minutes', "
        "heartbeat_at = now() - interval '10 minutes'",
    )
    assets.dispatcher.started.clear()

    run_sweep(client)

    assert status_of(voice_id) == "failed"
    # Failed but neither replaced nor old: a retry may still want it.
    assert ("clean", "voice", str(voice_id)) not in assets.dispatcher.started


def test_a_failed_version_is_cleaned_once_replaced_or_old(client, as_member, assets):  # noqa: F811
    as_member("uid-a")
    replaced = created(client)
    set_voice(replaced, "status = 'failed', finished_at = now()")
    newer = created(client)
    set_voice(newer, "status = 'failed', finished_at = now()")
    as_member("uid-b")
    old = created(client)
    set_voice(old, "status = 'failed', finished_at = now() - interval '8 days'")
    assets.dispatcher.started.clear()

    run_sweep(client)

    cleaned = assets.dispatcher.started
    assert ("clean", "voice", str(replaced)) in cleaned
    assert ("clean", "voice", str(old)) in cleaned
    assert ("clean", "voice", str(newer)) not in cleaned  # still the account's candidate


def test_a_retired_version_left_with_its_files_is_cleaned_again(client, as_member, assets):  # noqa: F811
    as_member("uid-a")
    left = created(client)
    made(left)
    set_voice(left, "status = 'retired'")
    gone = created(client)
    made(gone)
    set_voice(gone, "status = 'retired', cleaned_at = now()")
    assets.dispatcher.started.clear()

    report = run_sweep(client)

    assert ("clean", "voice", str(left)) in assets.dispatcher.started
    assert ("clean", "voice", str(gone)) not in assets.dispatcher.started
    assert report.cleans_started >= 1


def test_a_start_lost_long_ago_is_made_again(client, as_member, assets):  # noqa: F811
    as_member("uid-a")
    lost = created(client)
    set_voice(lost, "updated_at = now() - interval '5 minutes'")
    as_member("uid-b")
    recent = created(client)
    assets.dispatcher.started.clear()

    report = run_sweep(client)

    assert ("make", "voice", str(lost)) in assets.dispatcher.started
    assert ("make", "voice", str(recent)) not in assets.dispatcher.started
    assert report.makes_restarted >= 1
    # Bumped: a request reading it now leaves it to the sweep's start.
    assert migrate(
        "SELECT updated_at > now() - interval '1 minute' FROM app.voice_versions "
        "WHERE voice_id = $1",
        lost,
    )[0][0]


def test_a_deleted_account_goes_once_its_files_are_removed(client, as_member, assets):  # noqa: F811
    as_member("uid-a")
    conversation = start(client)  # started while the account was being deleted
    asyncio.run(save_checkpoint(thread_of(uuid.UUID(conversation))))
    voice_id = created(client)
    made(voice_id)
    delete_account("uid-a", timedelta(hours=2))
    assets.dispatcher.started.clear()

    first = run_sweep(client)

    assert migrate("SELECT count(*) FROM app.conversations WHERE owner_id = 'uid-a'")[0][0] == 0
    assert asyncio.run(checkpoints_of(conversation)) == 0
    assert status_of(voice_id) == "deleted"
    assert ("clean", "voice", str(voice_id)) in assets.dispatcher.started
    # Kept while the job removes the voice at ElevenLabs.
    assert migrate("SELECT count(*) FROM app.accounts WHERE account_id = 'uid-a'")[0][0] == 1
    assert first.accounts_waiting >= 1

    set_voice(voice_id, "cleaned_at = now()")  # what the job does
    second = run_sweep(client)

    assert migrate("SELECT count(*) FROM app.accounts WHERE account_id = 'uid-a'")[0][0] == 0
    assert (
        migrate("SELECT count(*) FROM app.voice_versions WHERE voice_id = $1", voice_id)[0][0] == 0
    )
    assert second.deleted_accounts >= 1


def test_an_account_deleted_within_the_hour_is_left_alone(client, as_member):
    # Its last token may still be valid: without the row, it would register anew.
    as_member("uid-a")
    start(client)
    delete_account("uid-a", timedelta(minutes=10))

    run_sweep(client, None)

    assert migrate("SELECT count(*) FROM app.accounts WHERE account_id = 'uid-a'")[0][0] == 1
    assert migrate("SELECT count(*) FROM app.conversations WHERE owner_id = 'uid-a'")[0][0] == 1


class DownDispatcher:
    async def dispatch(self, *args: str) -> str | None:
        raise DispatchError("HTTP 503")


def test_work_the_job_cannot_take_is_counted_and_the_pass_goes_on(client, as_member, assets):  # noqa: F811
    as_member("uid-a")
    voice_id = created(client)
    made(voice_id)
    set_voice(voice_id, "status = 'retired'")
    lost = running_turn(start(client), minutes_ago=10)

    report = run_sweep(client, DownDispatcher())

    assert report.not_started >= 1
    assert report.cleans_started == 0
    assert migrate("SELECT status FROM app.turns WHERE turn_id = $1", lost)[0][0] == "failed"


def test_the_checkpoint_tables_are_measured(client):
    report = run_sweep(client, None)

    assert set(report.checkpoint_bytes) == {"checkpoints", "checkpoint_blobs", "checkpoint_writes"}
    assert all(size >= 0 for size in report.checkpoint_bytes.values())
