import pytest

from tests.knowledge.workbooks import catalog_row
from wobot.knowledge.hashing import canonical_json
from wobot.knowledge.records.products import (
    clean_text,
    key_text,
    normalize_catalog,
    normalize_product,
    parse_adoption_years,
    parse_category,
)


def test_clean_text_keeps_full_width_punctuation_and_line_breaks():
    assert clean_text("\xa0離床偵測，即時提醒。\n第二行 ") == "離床偵測，即時提醒。\n第二行"
    assert clean_text("  ") is None
    assert clean_text(None) is None


def test_key_text_ignores_width_case_and_spacing():
    assert key_text("ＷｈｉｚＰａｄ\n  安心臥") == key_text("whizpad 安心臥")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("(1)\xa0 長者日常照顧輔助/安全監測科技產品", ("1", "長者日常照顧輔助/安全監測科技產品")),
        ("(1-3) 臥床監測、離床預警、壓傷防護", ("1-3", "臥床監測、離床預警、壓傷防護")),
        ("未分類", (None, "未分類")),
        (None, (None, None)),
    ],
)
def test_parse_category(value, expected):
    assert parse_category(value) == expected


@pytest.mark.parametrize(
    ("value", "years", "warns"),
    [
        (None, [], False),
        ("112", [2023], False),
        ("112、113", [2023, 2024], False),
        ("113, 112", [2023, 2024], False),
        ("112年", [], True),
        ("一一二", [], True),
        ("112、abc", [], True),
    ],
)
def test_parse_adoption_years(value, years, warns):
    parsed, warning = parse_adoption_years(value)
    assert parsed == years
    assert (warning is not None) == warns


def test_normalizes_the_columns():
    draft = normalize_product(catalog_row({"公司名稱": " 範例科技股份有限公司 "}))

    assert draft.fields["company_name"] == "範例科技股份有限公司"
    assert draft.fields["category_l1_code"] == "1"
    assert draft.fields["category_l2_label"] == "臥床監測、離床預警、壓傷防護"
    assert draft.fields["adoption_years"] == [2023, 2024]
    assert draft.fields["adoption_years_raw"] == "112、113"
    assert draft.warnings == ()


def test_empty_adoption_year_is_unknown_not_zero():
    draft = normalize_product(catalog_row({"本計畫導入年分": None}))

    assert draft.fields["adoption_years"] == []
    assert draft.fields["adoption_years_raw"] is None


def test_prefers_the_hyperlink_target_and_falls_back_to_text():
    linked = normalize_product(
        catalog_row({"產品網址": "產品頁"}, url_target="https://example.com/linked")
    )
    text_only = normalize_product(catalog_row())
    invalid = normalize_product(catalog_row({"產品網址": "產品頁"}))

    assert linked.fields["product_url"] == "https://example.com/linked"
    assert linked.fields["product_url_text"] == "產品頁"
    assert text_only.fields["product_url"] == "https://example.com/tm-1"
    assert invalid.fields["product_url"] is None
    assert invalid.warnings


def test_logical_key_survives_width_and_spacing_edits():
    plain = normalize_product(catalog_row({"產品名稱": "WhizPad 安心臥"}))
    edited = normalize_product(catalog_row({"產品名稱": "ＷｈｉｚＰａｄ  安心臥"}))

    assert plain.logical_key == edited.logical_key
    assert plain.content_hash != edited.content_hash  # same product, new revision


def test_moving_a_row_keeps_its_hash():
    assert (
        normalize_product(catalog_row(row_number=2)).content_hash
        == normalize_product(catalog_row(row_number=90)).content_hash
    )


def test_editing_a_column_changes_the_hash():
    assert (
        normalize_product(catalog_row()).content_hash
        != normalize_product(catalog_row({"主要功能": "跌倒偵測。"})).content_hash
    )


def test_canonical_json_ignores_key_order():
    assert canonical_json({"a": 1, "b": [2, 3]}) == canonical_json({"b": [2, 3], "a": 1})


def test_collisions_stay_separate_records():
    drafts = normalize_catalog([catalog_row(row_number=2), catalog_row(row_number=3)])

    assert drafts[0].logical_key != drafts[1].logical_key
    assert drafts[1].logical_key.endswith("#2")
    assert drafts[1].warnings


def test_one_line_fields_drop_cell_line_breaks():
    draft = normalize_product(
        catalog_row({"產品名稱": "測試床墊\nTM-1", "主要功能": "離床偵測。\n即時提醒。"})
    )

    assert draft.fields["product_name"] == "測試床墊 TM-1"
    assert draft.fields["features_text"] == "離床偵測。\n即時提醒。"
