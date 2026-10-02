"""Which pages are ingested, and how each is read. Nothing outside these lists is fetched.

A page added to a site is never ingested on its own: each run compares the site's
sitemap with these lists and reports pages it does not know. The one exception is the
WhizToys documentation, whose pages are found in its sitemap under one path. Documents
linked from the pages are listed the same way, and images are read where a listed page
shows them.
"""

import re
from dataclasses import dataclass
from typing import Literal
from urllib.parse import quote

GRC_HOST = "www.grc.yzu.edu.tw"
GRC_SITEMAP = f"https://{GRC_HOST}/sitemap.xml"


@dataclass(frozen=True)
class PageProfile:
    url: str
    parser: Literal[
        "home",
        "profile",
        "publications",
        "projects",
        "students_master",
        "students_phd",
        "speeches",
    ]


@dataclass(frozen=True)
class ProsePage:
    """A page read as sections under its headings."""

    url: str
    title: str
    key: str  # the page in record keys


GRC_PAGES = (
    PageProfile(f"https://{GRC_HOST}", "home"),
    PageProfile(f"https://{GRC_HOST}/%E5%BE%90%E6%A5%AD%E8%89%AFyehlianghsu", "profile"),
    PageProfile(f"https://{GRC_HOST}/publications", "publications"),
    PageProfile(f"https://{GRC_HOST}/projects", "projects"),
    PageProfile(f"https://{GRC_HOST}/students-masters", "students_master"),
    PageProfile(f"https://{GRC_HOST}/students-masters/students-phd", "students_phd"),
    PageProfile(f"https://{GRC_HOST}/speeches", "speeches"),
)
# Out of scope by decision: courses and activities, the textbook chapters, the archive.
GRC_EXCLUDED = (
    re.compile(r"/courses-activities(/|$)"),
    re.compile(r"/chapter\d+$"),
    re.compile(r"/archive$"),
)

GTECH_HOST = "www.seda-gtech.com.tw"
GTECH_SITEMAP = f"https://{GTECH_HOST}/sitemap.xml"
GTECH_SITE = "世大智科 G-Tech"
GTECH_PAGES = (
    ProsePage(f"https://{GTECH_HOST}", "首頁", "home"),
    ProsePage(f"https://{GTECH_HOST}/whizpad", "WhizPad 安心臥智慧床墊", "whizpad"),
    ProsePage(f"https://{GTECH_HOST}/whiztoys", "WhizToys 運動地墊遊戲平台", "whiztoys"),
    # Two images and no text: only the visual step finds anything here.
    ProsePage(f"https://{GTECH_HOST}/{quote('資深的新創公司')}", "資深的新創公司", "about"),
)
# Out of scope by decision: the security policy and the app's privacy notice.
GTECH_EXCLUDED = (re.compile(r"/資訊安全政策$"), re.compile(r"隱私權聲明$"))
# Every page ends with the same contact form; it is read once, on the home page.
GTECH_CONTACT_HEADING = "聯絡我們"


@dataclass(frozen=True)
class DocumentProfile:
    """A PDF the site links to, read page by page. The link is listed, never followed: the
    website source reports a document link it does not find here."""

    key: str  # the document in record keys
    title: str  # as the site's link names it
    url: str  # as the site links it
    drive_file_id: str | None = None  # stored on Google Drive, read through its API


GTECH_DOCUMENTS = (
    DocumentProfile(
        "whizpad_catalog",
        "WhizPad 線上型錄",
        "https://drive.google.com/file/d/12iXIUEfVvVvPHPo468QpZXyP2DVOKE_X/view",
        drive_file_id="12iXIUEfVvVvPHPo468QpZXyP2DVOKE_X",
    ),
    DocumentProfile(
        "whiztoys_catalog",
        "WhizToys 線上型錄（中文版）",
        f"https://{GTECH_HOST}/_files/ugd/2927b2_d8142d8a2d4248aca03996a339e16c3e.pdf",
    ),
    DocumentProfile(
        "whiztoys_manual",
        "WhizToys 操作說明書",
        f"https://{GTECH_HOST}/_files/ugd/2927b2_27c6d06e70eb472f9ee162217be1b30f.pdf",
    ),
)
# Linked to, not read, by decision: the English catalog repeats the Chinese one.
GTECH_LINKED_ONLY = ("https://drive.google.com/file/d/1riXW5NKj8O5dzKOTIsZroOqJ8nTq3wfj/view",)

DOCS_HOST = "docusaurus.seda-gtech.com.tw"
DOCS_SITEMAP = f"https://{DOCS_HOST}/sitemap.xml"
DOCS_SITE = "WhizToys 技術文件"
# The WhizToys pages, without the tag and category listings or a translation.
DOCS_PREFIX = "/docs/whiztoys/"
DOCS_EXCLUDED = (re.compile(r"/tags(/|$)"), re.compile(r"/category/"), re.compile(r"^/en/"))
