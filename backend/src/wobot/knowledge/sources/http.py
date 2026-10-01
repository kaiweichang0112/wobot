"""Fetch the listed pages of a website politely: allowed hosts, robots.txt, one request a second.

Only URLs from a page profile are requested. Links found on those pages are stored, never
followed, so link-only sites such as Google Drive or doi.org see no request from us.
"""

import asyncio
import time
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx2

USER_AGENT = "wobot-ingest/1.0 (+https://github.com/kaiweichang0112/wobot_app)"
MAX_REDIRECTS = 3
MAX_BYTES = 5 * 1024 * 1024
MIN_INTERVAL_SECONDS = 1.0
TIMEOUT_SECONDS = 20


class FetchError(Exception):
    """A listed page could not be read; the run must not publish without it."""


@dataclass(frozen=True)
class FetchedPage:
    url: str  # after redirects
    status: int
    content: bytes
    content_type: str


class PageFetcher:
    def __init__(
        self,
        client: httpx2.AsyncClient,
        allowed_hosts: set[str],
        *,
        min_interval: float = MIN_INTERVAL_SECONDS,
        max_bytes: int = MAX_BYTES,
    ) -> None:
        self._client = client
        self._allowed_hosts = allowed_hosts
        self._min_interval = min_interval
        self._max_bytes = max_bytes
        self._robots: dict[str, RobotFileParser] = {}
        self._last_request: dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def fetch(self, url: str) -> FetchedPage:
        for _ in range(MAX_REDIRECTS + 1):
            # Every hop is checked: a redirect must not lead outside the allowed hosts.
            host = self._check_host(url)
            if not await self._robots_allow(host, url):
                raise FetchError(f"robots.txt disallows {url}")
            async with self._client.stream(
                "GET", url, headers={"User-Agent": USER_AGENT}, follow_redirects=False
            ) as response:
                if response.is_redirect:
                    url = urljoin(url, response.headers["location"])
                    continue
                if response.status_code != 200:
                    raise FetchError(f"{url} answered {response.status_code}")
                content = await self._read_capped(response, url)
                return FetchedPage(
                    url=url,
                    status=response.status_code,
                    content=content,
                    content_type=response.headers.get("content-type", ""),
                )
        raise FetchError(f"more than {MAX_REDIRECTS} redirects from {url}")

    def _check_host(self, url: str) -> str:
        host = urlsplit(url).hostname or ""
        if host not in self._allowed_hosts:
            raise FetchError(f"{host} is not an allowed host")
        return host

    async def _robots_allow(self, host: str, url: str) -> bool:
        if host not in self._robots:
            robots_url = f"https://{host}/robots.txt"
            await self._wait_turn(host)
            response = await self._client.get(robots_url, headers={"User-Agent": USER_AGENT})
            parser = RobotFileParser(robots_url)
            if response.status_code in (401, 403):
                parser.disallow_all = True
            elif response.status_code >= 500:
                raise FetchError(f"{robots_url} answered {response.status_code}")
            else:
                # A missing robots.txt (404) allows everything.
                parser.parse(response.text.splitlines() if response.status_code == 200 else [])
            self._robots[host] = parser
        await self._wait_turn(host)
        return self._robots[host].can_fetch(USER_AGENT, url)

    async def _wait_turn(self, host: str) -> None:
        async with self._lock:
            wait = self._last_request.get(host, 0) + self._min_interval - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request[host] = time.monotonic()

    async def _read_capped(self, response: httpx2.Response, url: str) -> bytes:
        chunks, size = [], 0
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > self._max_bytes:
                raise FetchError(f"{url} is over {self._max_bytes} bytes")
            chunks.append(chunk)
        return b"".join(chunks)


def new_client() -> httpx2.AsyncClient:
    transport = httpx2.AsyncHTTPTransport(retries=2)  # connection failures only
    return httpx2.AsyncClient(timeout=TIMEOUT_SECONDS, transport=transport)
