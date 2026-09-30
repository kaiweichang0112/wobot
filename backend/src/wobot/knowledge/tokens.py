"""Token counts as the embedding model sees them."""

from functools import cache

import tiktoken

# The tokenizer of text-embedding-3-small.
ENCODING_NAME = "cl100k_base"


@cache
def _encoding() -> tiktoken.Encoding:
    # Loaded on first use, not at import: tiktoken downloads the file unless it is cached.
    return tiktoken.get_encoding(ENCODING_NAME)


def count_tokens(text: str) -> int:
    # Source text that happens to contain "<|endoftext|>" is data, not a control token.
    return len(_encoding().encode(text, disallowed_special=()))
