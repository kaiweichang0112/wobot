"""When a run goes ahead: alone, holding the run lock, and when started by the schedule,
only on the first Sunday of the month in Taipei."""

from datetime import UTC, datetime

import pytest

from tests.database import INGEST_USER
from wobot.config import Settings
from wobot.db import create_engine
from wobot.knowledge import cli
from wobot.knowledge.locks import exclusive_run
from wobot.knowledge.schedule import scheduled_run_due


async def test_only_one_run_holds_the_lock():
    first, _ = await create_engine(Settings(db_user=INGEST_USER), pooled=False)
    second, _ = await create_engine(Settings(db_user=INGEST_USER), pooled=False)
    try:
        async with exclusive_run(first) as alone:
            async with exclusive_run(second) as also:
                assert (alone, also) == (True, False)
        async with exclusive_run(second) as after:
            assert after
    finally:
        await first.dispose()
        await second.dispose()


# --- The schedule -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("utc", "due"),
    [
        # 03:00 on Sunday 4 October in Taipei is 19:00 on Saturday in UTC.
        (datetime(2026, 10, 3, 19, 0, tzinfo=UTC), True),
        (datetime(2026, 10, 10, 19, 0, tzinfo=UTC), False),  # the second Sunday
        (datetime(2026, 6, 6, 19, 0, tzinfo=UTC), True),  # Sunday 7 June, still the first
        (datetime(2026, 10, 4, 19, 0, tzinfo=UTC), False),  # Monday in Taipei
    ],
)
def test_a_scheduled_run_goes_ahead_on_the_first_sunday_in_taipei(utc, due):
    assert scheduled_run_due(utc) is due


def test_a_scheduled_run_on_another_sunday_ends_before_any_setting(monkeypatch):
    monkeypatch.setattr(cli, "_now", lambda: datetime(2026, 10, 10, 19, 0, tzinfo=UTC))
    monkeypatch.setattr(cli, "get_settings", lambda: pytest.fail("settings were read"))

    assert cli.main(["run", "--scheduled"]) == 0
