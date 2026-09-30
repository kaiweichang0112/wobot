from tests.knowledge.workbooks import catalog_row
from wobot.knowledge.chunking.products import product_chunk
from wobot.knowledge.records.products import normalize_product
from wobot.knowledge.tokens import count_tokens


def chunk_of(overrides=None):
    return product_chunk(normalize_product(catalog_row(overrides)))


def test_header_names_the_category_path():
    chunk = chunk_of()

    assert chunk.heading_path == (
        "產品目錄",
        "長者日常照顧輔助/安全監測科技產品",
        "臥床監測、離床預警、壓傷防護",
    )
    assert chunk.context_header == (
        "產品目錄｜長者日常照顧輔助/安全監測科技產品 › 臥床監測、離床預警、壓傷防護"
    )


def test_embeds_what_people_search_by_and_nothing_else():
    chunk = chunk_of()

    assert chunk.embedding_input == f"{chunk.context_header}\n{chunk.body}"
    assert chunk.body.splitlines() == [
        "產品名稱：測試床墊 TM-1",
        "公司：範例科技股份有限公司",
        "主要功能：離床偵測，即時提醒照護者。",
        "使用方式：鋪於床墊下方，透過 Wi-Fi 連線。",
        "簡介：測試用的智慧床墊。",
        "導入年份：2023、2024",
    ]
    for left_out in ("臺北市範例路", "02-0000-0000", "https://"):
        assert left_out not in chunk.embedding_input


def test_leaves_out_empty_lines():
    body = chunk_of({"使用方式": None, "本計畫導入年分": None}).body

    assert "使用方式" not in body
    assert "導入年份" not in body


def test_keeps_the_product_page_as_a_link():
    assert chunk_of().links == ({"kind": "product_page", "url": "https://example.com/tm-1"},)
    assert chunk_of({"產品網址": "產品頁"}).links == ()


def test_contact_edit_keeps_the_chunk_and_its_embedding():
    before = normalize_product(catalog_row())
    after = normalize_product(catalog_row({"連絡電話": "03-1111-1111"}))

    assert before.content_hash != after.content_hash  # a new record revision
    assert product_chunk(before).content_hash == product_chunk(after).content_hash
    assert product_chunk(after).record_revisions == ((after.logical_key, after.content_hash),)


def test_feature_edit_makes_a_new_chunk():
    assert chunk_of().content_hash != chunk_of({"主要功能": "跌倒偵測。"}).content_hash


def test_counts_the_tokens_that_get_embedded():
    chunk = chunk_of()

    assert chunk.token_count == count_tokens(chunk.embedding_input) > 0


def test_special_token_text_counts_as_plain_text():
    assert count_tokens("<|endoftext|>") > 1
