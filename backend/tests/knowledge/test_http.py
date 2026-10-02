import httpx2
import pytest

from wobot.knowledge.sources.http import FetchError, PageFetcher

SITE = "www.example.edu.tw"


def fetcher(routes, requests, **options):
    """A fetcher whose client answers from `routes`: URL → (status, headers, body)."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(str(request.url))
        status, headers, body = routes.get(str(request.url), (404, {}, b""))
        return httpx2.Response(status, headers=headers, content=body)

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    return PageFetcher(client, {SITE}, min_interval=0, **options)


ROBOTS = (200, {}, b"User-agent: *\nDisallow: /private\n")


async def test_reads_a_listed_page_after_checking_robots():
    requests = []
    page = await fetcher(
        {f"https://{SITE}/robots.txt": ROBOTS, f"https://{SITE}/projects": (200, {}, b"<html/>")},
        requests,
    ).fetch(f"https://{SITE}/projects")

    assert page.content == b"<html/>"
    assert requests == [f"https://{SITE}/robots.txt", f"https://{SITE}/projects"]


async def test_never_requests_another_host():
    requests = []

    with pytest.raises(FetchError, match="not an allowed host"):
        await fetcher({}, requests).fetch("https://doi.org/10.4017/gt.2026.25.1.1257.03")
    assert requests == []


async def test_a_redirect_off_the_site_is_refused_before_it_is_followed():
    requests = []
    routes = {
        f"https://{SITE}/robots.txt": ROBOTS,
        f"https://{SITE}/old": (301, {"location": "https://drive.google.com/file/d/x"}, b""),
    }

    with pytest.raises(FetchError, match="drive.google.com is not an allowed host"):
        await fetcher(routes, requests).fetch(f"https://{SITE}/old")
    assert not any("drive.google.com" in url for url in requests)


async def test_respects_robots_txt():
    with pytest.raises(FetchError, match="robots.txt disallows"):
        await fetcher({f"https://{SITE}/robots.txt": ROBOTS}, []).fetch(f"https://{SITE}/private/x")


@pytest.mark.parametrize("status", [403, 404])
async def test_a_robots_txt_that_cannot_be_read_sets_no_rules(status):
    routes = {
        f"https://{SITE}/robots.txt": (status, {}, b"Forbidden"),
        f"https://{SITE}/media/a.png": (200, {}, b"png"),
    }

    page = await fetcher(routes, []).fetch(f"https://{SITE}/media/a.png")

    assert page.content == b"png"


async def test_a_robots_txt_that_fails_on_the_server_stops_the_fetch():
    routes = {f"https://{SITE}/robots.txt": (503, {}, b"")}

    with pytest.raises(FetchError, match="answered 503"):
        await fetcher(routes, []).fetch(f"https://{SITE}/projects")


async def test_a_listed_page_that_is_gone_fails_the_fetch():
    with pytest.raises(FetchError, match="answered 404"):
        await fetcher({f"https://{SITE}/robots.txt": ROBOTS}, []).fetch(f"https://{SITE}/gone")


async def test_refuses_a_page_over_the_size_limit():
    routes = {f"https://{SITE}/robots.txt": ROBOTS, f"https://{SITE}/big": (200, {}, b"x" * 2048)}

    with pytest.raises(FetchError, match="over 1024 bytes"):
        await fetcher(routes, [], max_bytes=1024).fetch(f"https://{SITE}/big")
