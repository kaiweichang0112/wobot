"""Sitemaps: the pages a site lists, to find new pages and to discover documentation."""

import re
from collections.abc import Iterable, Sequence
from urllib.parse import unquote, urlsplit

from wobot.knowledge.sources.http import FetchError, PageFetcher

_LOC = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>")


async def sitemap_urls(fetcher: PageFetcher, url: str) -> list[str]:
    """Every page the sitemap lists, through a sitemap index one level down."""
    body = (await fetcher.fetch(url)).content.decode()
    found = _LOC.findall(body)
    if "<sitemapindex" not in body:
        return found
    urls = []
    for sitemap in found:
        urls += _LOC.findall((await fetcher.fetch(sitemap)).content.decode())
    return urls


async def unlisted_pages(
    fetcher: PageFetcher, sitemap: str, known: Iterable[str], excluded: Sequence[re.Pattern[str]]
) -> list[str]:
    """Pages the sitemap lists that no profile covers: new pages a person should see.

    A sitemap that cannot be read is reported the same way, never fatal: the listed pages
    were read, and only the check for new ones is missing.
    """
    listed = {normal_url(url) for url in known}
    try:
        urls = await sitemap_urls(fetcher, sitemap)
    except FetchError as error:
        return [f"sitemap not read: {error}"]
    return [
        f"page not in any profile: {unquote(url)}"
        for url in urls
        if normal_url(url) not in listed
        and not any(pattern.search(unquote(urlsplit(url).path)) for pattern in excluded)
    ]


def normal_url(url: str) -> str:
    return unquote(url).rstrip("/")
