"""Chunk strategy product_row@1: one chunk per product, from the fields people search by."""

from wobot.knowledge.chunking.drafts import ChunkDraft, build_chunk
from wobot.knowledge.records.products import ProductDraft

STRATEGY = "product_row"
STRATEGY_VERSION = 1
CATALOG_TITLE = "產品目錄"


def product_chunk(draft: ProductDraft) -> ChunkDraft:
    fields = draft.fields
    categories = [c for c in (fields["category_l1_label"], fields["category_l2_label"]) if c]
    category_path = " › ".join(categories)
    # Address, phone and URL stay out: nobody searches by them, and they would dilute the
    # vector. An answer that needs them reads them from the record.
    lines = [
        ("產品名稱", fields["product_name"]),
        ("公司", fields["company_name"]),
        ("主要功能", fields["features_text"]),
        ("使用方式", fields["usage_text"]),
        ("簡介", fields["summary_text"]),
        ("導入年份", "、".join(str(year) for year in fields["adoption_years"])),
    ]
    url = fields["product_url"]
    return build_chunk(
        strategy=STRATEGY,
        strategy_version=STRATEGY_VERSION,
        heading_path=[CATALOG_TITLE, *categories],
        context_header=f"{CATALOG_TITLE}｜{category_path}" if categories else CATALOG_TITLE,
        body="\n".join(f"{label}：{value}" for label, value in lines if value),
        links=[{"kind": "product_page", "url": url}] if url else [],
        record_revisions=[(draft.logical_key, draft.content_hash)],
    )
