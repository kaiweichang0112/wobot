"""Start the asset worker for one version.

In the cloud, an execution of the `wobot-assets` Cloud Run job, through the Run Admin
API's `jobs.run` with the arguments overridden (P6). A start is at least once: the
response can be lost after the execution began, and `GET` starts it again later. The
worker's claim makes the second one end at once, so the work happens once.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Protocol

import google.auth
import google.auth.transport.requests
import httpx2

CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
RUN_API = "https://run.googleapis.com/v2"

logger = logging.getLogger(__name__)


class DispatchError(Exception):
    """The job could not be started; the version stays queued for a later start."""


class Dispatcher(Protocol):
    async def dispatch(self, *args: str) -> str | None:
        """Start the work the arguments name; returns the execution's name, if known."""
        ...


class GoogleToken:
    """An OAuth token of the runtime identity, refreshed once expired."""

    def __init__(self, credentials=None) -> None:
        if credentials is None:
            credentials, _ = google.auth.default(scopes=[CLOUD_PLATFORM_SCOPE])
        self._credentials = credentials

    async def __call__(self) -> str:
        if not self._credentials.valid:
            request = google.auth.transport.requests.Request()
            # google-auth is synchronous: refresh off the event loop.
            await asyncio.to_thread(self._credentials.refresh, request)
        return self._credentials.token


class CloudRunJob:
    """Starts executions of one job. The caller needs Cloud Run Jobs Executor With
    Overrides on that job alone, and nothing on the job's service account."""

    def __init__(
        self,
        client: httpx2.AsyncClient,
        project: str,
        region: str,
        job: str,
        token: Callable[[], Awaitable[str]],
    ) -> None:
        self._client = client
        self._url = f"{RUN_API}/projects/{project}/locations/{region}/jobs/{job}:run"
        self._token = token

    async def dispatch(self, *args: str) -> str | None:
        body = {"overrides": {"containerOverrides": [{"args": list(args)}]}}
        try:
            response = await self._client.post(
                self._url, json=body, headers={"Authorization": f"Bearer {await self._token()}"}
            )
        except httpx2.HTTPError as exc:
            raise DispatchError(type(exc).__name__) from exc
        if response.is_error:
            raise DispatchError(f"HTTP {response.status_code}")
        # A long-running operation whose metadata is the execution it started.
        name = response.json().get("metadata", {}).get("name", "")
        return name.rsplit("/", 1)[-1] or None


class InProcess:
    """Runs the work as a task of this process, for development, where there is no job.
    Unlike a job, it stops with the process."""

    def __init__(self, work: Callable[..., Awaitable[str]]) -> None:
        self._work = work
        self._tasks: set[asyncio.Task] = set()  # held, or the loop may drop them

    async def dispatch(self, *args: str) -> str | None:
        task = asyncio.create_task(self._run(args))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return "in-process"

    async def _run(self, args: tuple[str, ...]) -> None:
        try:
            outcome = await self._work(*args)
            logger.info("asset work done", extra={"work": " ".join(args), "outcome": outcome})
        except Exception:
            logger.exception("asset work failed")
