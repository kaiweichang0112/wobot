"""Synthetic product catalog workbooks, shaped like the real one but with made-up products."""

from collections.abc import Mapping, Sequence
from io import BytesIO

from openpyxl import Workbook
from openpyxl.worksheet.hyperlink import Hyperlink

from wobot.knowledge.sources.xlsx import HEADERS, SHEET_NAME, URL_HEADER, CatalogRow


def product_row(overrides: Mapping[str, object] | None = None) -> dict[str, object]:
    """One catalog row with every column filled; the categories copy the real format."""
    row: dict[str, object] = {
        "產品名稱": "測試床墊 TM-1",
        "公司名稱": "範例科技股份有限公司",
        "公司地址": "臺北市範例路 1 號",
        "連絡電話": "02-0000-0000",
        "產品網址": "https://example.com/tm-1",
        "主要功能": "離床偵測，即時提醒照護者。",
        "使用方式": "鋪於床墊下方，透過 Wi-Fi 連線。",
        "100字簡介": "測試用的智慧床墊。",
        "產品第一層分類": "(1)\xa0 長者日常照顧輔助/安全監測科技產品",
        "產品第二層分類": "(1-3) 臥床監測、離床預警、壓傷防護",
        "本計畫導入年分": "112、113",
    }
    row.update(overrides or {})
    return row


def catalog_row(
    overrides: Mapping[str, object] | None = None,
    *,
    row_number: int = 2,
    url_target: str | None = None,
) -> CatalogRow:
    """The row as the reader returns it, for tests that start after reading."""
    cells = {h: None if v is None else str(v) for h, v in product_row(overrides).items()}
    return CatalogRow(row_number=row_number, cells=cells, product_url_target=url_target)


def catalog_workbook(
    rows: Sequence[Mapping[str, object]],
    *,
    url_links: Mapping[int, str] | None = None,
    headers: Sequence[str] = HEADERS,
    sheet_name: str = SHEET_NAME,
) -> bytes:
    """The workbook as bytes.

    `url_links` maps a 0-based index into `rows` to a hyperlink on its URL cell, stored the
    way Excel stores it: a `#fragment` goes to the location attribute, apart from the
    target. Rows not in it get plain text only.
    """
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = sheet_name
    sheet.append(list(headers))
    for row in rows:
        sheet.append([row.get(header) for header in headers])
    if url_links:
        url_column = list(headers).index(URL_HEADER) + 1
        for index, url in url_links.items():
            cell = sheet.cell(row=index + 2, column=url_column)
            target, _, fragment = url.partition("#")
            cell.hyperlink = Hyperlink(
                ref=cell.coordinate, target=target or None, location=fragment or None
            )
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()
