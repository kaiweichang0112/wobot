from pathlib import Path

import pytest

from tests.knowledge.workbooks import catalog_workbook, product_row
from wobot.knowledge.sources.xlsx import HEADERS, CatalogSchemaError, read_catalog

# The local reference snapshot; gitignored, so absent in CI and fresh clones.
REFERENCE_CATALOG = Path(__file__).resolve().parents[3] / "references/smart-care-products.xlsx"


def test_reads_each_row_with_its_excel_row_number():
    rows = read_catalog(
        catalog_workbook([product_row(), product_row({"產品名稱": "測試地墊 TM-2"})])
    )

    assert [row.row_number for row in rows] == [2, 3]
    assert rows[1].cells["產品名稱"] == "測試地墊 TM-2"
    assert list(rows[0].cells) == list(HEADERS)


def test_keeps_cell_text_untouched():
    rows = read_catalog(
        catalog_workbook([product_row({"公司名稱": " 範例科技 ", "本計畫導入年分": None})])
    )

    cells = rows[0].cells
    assert cells["公司名稱"] == " 範例科技 "
    assert cells["產品第一層分類"].startswith("(1)\xa0 ")
    assert cells["本計畫導入年分"] is None


def test_turns_numeric_cells_into_text():
    rows = read_catalog(catalog_workbook([product_row({"本計畫導入年分": 113})]))

    assert rows[0].cells["本計畫導入年分"] == "113"


def test_reads_the_hyperlink_target_apart_from_the_text():
    rows = read_catalog(
        catalog_workbook(
            [product_row({"產品網址": "產品頁"}), product_row()],
            url_links={0: "https://example.com/linked"},
        )
    )

    assert rows[0].cells["產品網址"] == "產品頁"
    assert rows[0].product_url_target == "https://example.com/linked"
    assert rows[1].product_url_target is None
    assert rows[1].cells["產品網址"] == "https://example.com/tm-1"


def test_puts_the_fragment_back_on_the_hyperlink():
    rows = read_catalog(
        catalog_workbook(
            [product_row(), product_row()],
            url_links={0: "https://example.com/products/#tm-1", 1: "#產品整理!A1"},
        )
    )

    assert rows[0].product_url_target == "https://example.com/products/#tm-1"
    assert rows[1].product_url_target is None  # points inside the workbook


def test_skips_empty_rows_below_the_data():
    empty = dict.fromkeys(HEADERS)
    rows = read_catalog(catalog_workbook([product_row(), empty, {"產品名稱": "  "}]))

    assert len(rows) == 1


def test_rejects_a_renamed_sheet():
    with pytest.raises(CatalogSchemaError, match="sheet"):
        read_catalog(catalog_workbook([product_row()], sheet_name="工作表1"))


@pytest.mark.parametrize(
    "headers",
    [
        [*HEADERS[:-1], "導入年分"],  # renamed
        [HEADERS[1], HEADERS[0], *HEADERS[2:]],  # reordered
        [*HEADERS, "價格"],  # added
        HEADERS[:-1],  # removed
    ],
    ids=["renamed", "reordered", "added", "removed"],
)
def test_rejects_any_column_change(headers):
    with pytest.raises(CatalogSchemaError, match="columns"):
        read_catalog(catalog_workbook([product_row()], headers=headers))


@pytest.mark.skipif(not REFERENCE_CATALOG.exists(), reason="local reference snapshot only")
def test_reads_the_reference_snapshot():
    rows = read_catalog(REFERENCE_CATALOG.read_bytes())

    assert len(rows) == 163
    without_link = [row for row in rows if row.product_url_target is None]
    # One row carries its URL as text only; reading only hyperlink objects would drop it.
    assert [row.row_number for row in without_link] == [28]
    assert without_link[0].cells["產品網址"].startswith("http")
    # Row 81 links to one product on a multi-product page; the anchor must survive.
    by_number = {row.row_number: row for row in rows}
    assert by_number[81].product_url_target.endswith("/cloud_medical/#mio")
