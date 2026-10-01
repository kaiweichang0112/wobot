"""Which pages are ingested, and how each is read. Nothing outside these lists is fetched.

A page added to a site is never ingested on its own: each run compares the site's
sitemap with these lists and reports pages it does not know.
"""

import re
from dataclasses import dataclass
from typing import Literal

GRC_HOST = "www.grc.yzu.edu.tw"
GRC_SITEMAP = f"https://{GRC_HOST}/sitemap.xml"


@dataclass(frozen=True)
class PageProfile:
    url: str
    parser: Literal["profile", "publications", "projects", "students_master", "students_phd"]


GRC_PAGES = (
    PageProfile(f"https://{GRC_HOST}/%E5%BE%90%E6%A5%AD%E8%89%AFyehlianghsu", "profile"),
    PageProfile(f"https://{GRC_HOST}/publications", "publications"),
    PageProfile(f"https://{GRC_HOST}/projects", "projects"),
    PageProfile(f"https://{GRC_HOST}/students-masters", "students_master"),
    PageProfile(f"https://{GRC_HOST}/students-masters/students-phd", "students_phd"),
)
# In scope, read by a later step of phase A: the home page and the speeches.
GRC_LATER = (f"https://{GRC_HOST}", f"https://{GRC_HOST}/speeches")
# Out of scope by decision: courses and activities, the textbook chapters, the archive.
GRC_EXCLUDED = (
    re.compile(r"/courses-activities(/|$)"),
    re.compile(r"/chapter\d+$"),
    re.compile(r"/archive$"),
)
