"""Product catalog rows → normalized product drafts, ready to become records."""

import re

from wobot.knowledge.records.drafts import RecordDraft, record_draft, separate_collisions
from wobot.knowledge.records.text import clean_line, clean_text, key_text
from wobot.knowledge.sources.xlsx import URL_HEADER, CatalogRow

RECORD_TYPE = "product"
ROC_YEAR_OFFSET = 1911  # ROC year 1 is 1912 CE
_CATEGORY = re.compile(r"^\((?P<code>[\d-]+)\)\s*(?P<label>.+)$", re.DOTALL)
_YEAR_SEPARATORS = re.compile(r"[、,，/\s]+")
_ROC_YEAR = re.compile(r"^\d{2,3}$")


def parse_category(value: str | None) -> tuple[str | None, str | None]:
    """'(1-3) 臥床監測…' → ('1-3', '臥床監測…'); text without a code becomes the label."""
    text = clean_text(value)
    if text is None:
        return None, None
    match = _CATEGORY.match(text)
    if match is None:
        return None, text
    return match["code"], match["label"].strip()


def parse_adoption_years(value: str | None) -> tuple[list[int], str | None]:
    """ROC years such as '112、113' → [2023, 2024], or no years and a warning.

    All or nothing: one unreadable part leaves the years empty rather than half-guessed.
    """
    text = clean_text(value)
    if text is None:
        return [], None
    parts = [part for part in _YEAR_SEPARATORS.split(text) if part]
    if not parts or not all(_ROC_YEAR.match(part) for part in parts):
        return [], f"unparsed adoption years {value!r}"
    return sorted({int(part) + ROC_YEAR_OFFSET for part in parts}), None


def parse_product_url(row: CatalogRow) -> tuple[str | None, str | None]:
    """The hyperlink target, else the cell text, if either is an http(s) URL."""
    candidate = (row.product_url_target or row.cells[URL_HEADER] or "").strip()
    if candidate.lower().startswith(("http://", "https://")):
        return candidate, None
    return None, f"no http(s) product URL in {candidate!r}"


def normalize_product(row: CatalogRow) -> RecordDraft:
    cells = row.cells
    warnings = []
    l1_code, l1_label = parse_category(cells["產品第一層分類"])
    l2_code, l2_label = parse_category(cells["產品第二層分類"])
    for name, code, label in (("level 1", l1_code, l1_label), ("level 2", l2_code, l2_label)):
        if label is not None and code is None:
            warnings.append(f"no code in {name} category {label!r}")
    years, years_warning = parse_adoption_years(cells["本計畫導入年分"])
    url, url_warning = parse_product_url(row)
    warnings += [w for w in (years_warning, url_warning) if w]

    fields = {
        "product_name": clean_line(cells["產品名稱"]),
        "company_name": clean_line(cells["公司名稱"]),
        "company_address": clean_line(cells["公司地址"]),
        "contact_phone": clean_line(cells["連絡電話"]),
        "product_url": url,
        "product_url_text": cells[URL_HEADER],
        "features_text": clean_text(cells["主要功能"]),
        "usage_text": clean_text(cells["使用方式"]),
        "summary_text": clean_text(cells["100字簡介"]),
        "category_l1_code": l1_code,
        "category_l1_label": l1_label,
        "category_l2_code": l2_code,
        "category_l2_label": l2_label,
        "adoption_years": years,
        "adoption_years_raw": cells["本計畫導入年分"],
    }
    return record_draft(
        RECORD_TYPE,
        f"product:{key_text(fields['company_name'])}:{key_text(fields['product_name'])}",
        raw={"cells": dict(cells), "product_url_target": row.product_url_target},
        fields=fields,
        locator={"row": row.row_number},
        warnings=warnings,
    )


def normalize_catalog(rows: list[CatalogRow]) -> list[RecordDraft]:
    """Drafts for every row; colliding keys get a suffix so no row is silently merged."""
    return separate_collisions([normalize_product(row) for row in rows])
