"""When ingestion runs on its own: once a month, on its first Sunday, Taipei time.

Cloud Scheduler cannot name "the first Sunday": a cron schedule restricting both the day
of the month and the day of the week runs when either matches. So the scheduler triggers
the job every Sunday, and a scheduled run goes ahead only on the month's first.
"""

from datetime import datetime, timedelta, timezone

# Taiwan has kept UTC+8 without daylight saving since 1980, so a fixed offset is exact and
# no time zone database is needed.
TAIPEI = timezone(timedelta(hours=8), "Asia/Taipei")


def scheduled_run_due(now: datetime) -> bool:
    """Whether `now`, an aware time, falls on the first Sunday of its month in Taipei."""
    local = now.astimezone(TAIPEI)
    return local.weekday() == 6 and local.day <= 7
