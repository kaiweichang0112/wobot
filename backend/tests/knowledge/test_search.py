"""Fusing searches is pure: ranks in, ranks out."""

import uuid

from wobot.knowledge.search import SearchHit, fuse


def hits(*names):
    return [SearchHit(uuid.uuid5(uuid.NAMESPACE_URL, n), 0.5, n, "", [], 10) for n in names]


def headers(found):
    return [hit.context_header for hit in found]


def test_one_search_keeps_its_order():
    assert headers(fuse([hits("a", "b", "c")], k=2)) == ["a", "b"]


def test_agreement_outranks_one_first_place():
    # b is second in both: 2 / 62 beats a's 1 / 61 from one search alone.
    assert headers(fuse([hits("a", "b"), hits("c", "b")], k=3)) == ["b", "a", "c"]


def test_a_chunk_both_searches_return_is_kept_once():
    assert headers(fuse([hits("a", "b"), hits("a", "b")], k=5)) == ["a", "b"]
