"""Starting the asset job through the Run Admin API: one execution per call, with the
work named in its arguments (P6)."""

import asyncio
import json

import httpx2
import pytest

from wobot.assets.dispatch import CloudRunJob, DispatchError, InProcess

URL = "https://run.googleapis.com/v2/projects/p/locations/asia-east1/jobs/wobot-assets:run"


async def token() -> str:
    return "token-1"


def job(handler) -> CloudRunJob:
    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    return CloudRunJob(client, "p", "asia-east1", "wobot-assets", token)


async def test_a_dispatch_runs_the_job_with_the_work_as_its_arguments():
    sent = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        sent.append(request)
        execution = "projects/p/locations/asia-east1/jobs/wobot-assets/executions/wobot-assets-x1"
        return httpx2.Response(200, json={"name": "operations/o1", "metadata": {"name": execution}})

    execution = await job(handler).dispatch("make", "voice", "v-1")

    assert execution == "wobot-assets-x1"
    [request] = sent
    assert (request.method, str(request.url)) == ("POST", URL)
    assert request.headers["Authorization"] == "Bearer token-1"
    assert json.loads(request.content) == {
        "overrides": {"containerOverrides": [{"args": ["make", "voice", "v-1"]}]}
    }


@pytest.mark.parametrize("status_code", [403, 429, 503])
async def test_a_refused_dispatch_is_an_error_the_caller_can_survive(status_code):
    with pytest.raises(DispatchError):
        await job(lambda _request: httpx2.Response(status_code)).dispatch("make", "voice", "v")


async def test_an_unreachable_api_is_a_dispatch_error():
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("no route", request=request)

    with pytest.raises(DispatchError):
        await job(handler).dispatch("make", "voice", "v")


async def test_in_process_runs_the_work_in_the_background():
    done = asyncio.Event()
    got = []

    async def work(*args: str) -> str:
        got.append(args)
        done.set()
        return "ready"

    assert await InProcess(work).dispatch("make", "voice", "v-1") == "in-process"
    await asyncio.wait_for(done.wait(), 1)
    assert got == [("make", "voice", "v-1")]
