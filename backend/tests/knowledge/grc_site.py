"""A synthetic GRC site behind a mock transport: every listed page, robots.txt, sitemaps."""

import httpx2

from tests.knowledge.fakes import FakeLectureReader, fake_lectures
from tests.knowledge.wix_pages import BLANK, HOST, a, br_p, h, link_p, ol, p, page, rich, spans
from wobot.knowledge.grc import GrcWebsiteSource
from wobot.knowledge.profiles import GRC_HOST
from wobot.knowledge.records.grc import PROFILE_SECTIONS, PUBLICATION_CATEGORIES
from wobot.knowledge.records.lectures import LECTURE_CATEGORIES
from wobot.knowledge.sources.http import PageFetcher

DOI = "https://doi.org/10.4017/gt.2026.25.1.1257.03"
DRIVE_PDF = "https://drive.google.com/file/d/example/view"
KEYNOTES, INVITED = LECTURE_CATEGORIES


def site_pages(project_amount: str = "NTD600,000") -> dict[str, bytes]:
    nav = rich("comp-nav", link_p("Publications", f"{HOST}/publications"))
    profile = page(
        nav,
        rich("comp-intro", p("徐教授在元智大學服務三十餘年。")),
        rich(
            "comp-lists",
            *(h(6, name) + ol(f"{name}, 2016/04 - present") + BLANK for name in PROFILE_SECTIONS),
        ),
        rich("comp-bio", p("Biography"), p("Professor Hsu has served 30+ years.")),
    )
    publications = page(
        nav,
        rich(
            "comp-pubs",
            *(
                h(6, name)
                + ol(f"Hsu, Y. L. (2026). {name} item. {a(DOI, DOI)} {a('PDF', DRIVE_PDF)}")
                for name in PUBLICATION_CATEGORIES
            ),
        ),
    )
    projects = page(
        rich("comp-h1", p("2025\xa0Research projects")),
        rich(
            "comp-b1",
            br_p(
                "插座電表測試",
                "Smart plug testing",
                f"工業技術研究院 ITRI 2025/10/28~2026/08/31 {project_amount}",
            ),
        ),
    )
    masters = page(
        rich("comp-y__item-1", p("2022")),
        rich(
            "comp-l__item-1",
            p("王小明"),
            p("論文名稱：智慧床墊"),
            link_p("電子全文", "https://hdl.handle.net/11296/a"),
        ),
    )
    phd = page(
        rich("comp-y__item-2", p("2014")),
        rich(
            "comp-l__item-2",
            p("劉大同"),
            p("Thesis title: Bed-centered telehealth"),
            link_p("電子全文"),
        ),
    )
    speeches = page(
        nav,
        rich("comp-k", h(1, KEYNOTES)),
        rich("comp-ky__item1", p("2024~2025")),
        rich(
            "comp-kl__item1",
            BLANK,
            ol(
                "“Smart care in practice,” keynote speech, Example Symposium, Seoul, Korea, "
                f"{spans('202', '5/12/05')} {a('PDF', DRIVE_PDF)}"
            ),
        ),
        rich("comp-i", h(1, INVITED)),
        # The same item ID as the keynote year: each repeater numbers its own items.
        rich("comp-iy__item1", p("2025")),
        rich(
            "comp-il__item1",
            ol(f'"長者運動遊戲設計，"範例大學護理系課程專題演講，2025/11/12 {a("PDF")}'),
        ),
    )
    return {
        "/%E5%BE%90%E6%A5%AD%E8%89%AFyehlianghsu": profile,
        "/publications": publications,
        "/projects": projects,
        "/students-masters": masters,
        "/students-masters/students-phd": phd,
        "/speeches": speeches,
    }


def sitemaps(extra: tuple[str, ...] = ()) -> dict[str, bytes]:
    pages = ["", "/chapter3", "/courses-activities/news", *site_pages(), *extra]
    locs = "".join(f"<url><loc>{HOST}{path}</loc></url>" for path in pages)
    index = f"<sitemap><loc>{HOST}/pages-sitemap.xml</loc></sitemap>"
    return {
        "/sitemap.xml": f"<sitemapindex>{index}</sitemapindex>".encode(),
        "/pages-sitemap.xml": f"<urlset>{locs}</urlset>".encode(),
    }


def grc_fetcher(requests: list[str], **site_options) -> PageFetcher:
    routes = {
        "/robots.txt": b"User-agent: *\nAllow: /\n",
        **site_pages(**site_options),
        **sitemaps(),
    }

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(str(request.url))
        if request.url.host != GRC_HOST or request.url.raw_path.decode() not in routes:
            return httpx2.Response(404)
        return httpx2.Response(200, content=routes[request.url.raw_path.decode()])

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    return PageFetcher(client, {GRC_HOST}, min_interval=0)


def grc_source(
    requests: list[str], reader: FakeLectureReader | None = None, **site_options
) -> GrcWebsiteSource:
    """The source over the synthetic site, with a stand-in model for the speeches."""
    return GrcWebsiteSource(grc_fetcher(requests, **site_options), fake_lectures(reader))
