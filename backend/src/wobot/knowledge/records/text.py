"""The two normalizations every extractor uses: text to show, and text to identify by."""

import unicodedata


def clean_text(value: str | None) -> str | None:
    """Text to store and display: no-break spaces become spaces and edges are trimmed.

    Full-width punctuation and line breaks stay, as part of the original text.
    """
    if value is None:
        return None
    cleaned = value.replace("\xa0", " ").strip()
    return cleaned or None


def clean_line(value: str | None) -> str | None:
    """A one-line value such as a name: every whitespace run, line breaks too, is one space."""
    text = clean_text(value)
    return " ".join(text.split()) if text else None


def key_text(value: str | None) -> str:
    """Text for identity only, never displayed: NFKC, case-folded, whitespace collapsed."""
    return " ".join(unicodedata.normalize("NFKC", value or "").casefold().split())
