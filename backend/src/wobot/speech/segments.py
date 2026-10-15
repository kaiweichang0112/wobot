"""Streamed words cut into sentences to speak, each as soon as it is whole.

Ported from the reference app's SpeechSentenceBuffer: a sentence ends at a full stop,
and a long one without any is cut at a comma. The first is cut at its first comma, so
the voice starts while the rest is still being written.
"""

HARD = "。！？!?；;\n"
SOFT = "，,、：:"
FIRST_AT_LEAST = 6  # characters before the first sentence may end at a comma
SOFT_LIMIT = 48  # a sentence this long ends at its last comma
HARD_LIMIT = 80  # and one this long without a comma is cut here


class Segmenter:
    def __init__(self) -> None:
        self._buffer = ""
        self._first = True

    def add(self, text: str) -> list[str]:
        """The sentences the words so far complete."""
        self._buffer += text
        return self._take(force=False)

    def flush(self) -> list[str]:
        """The rest, once no more words come."""
        return self._take(force=True)

    def _take(self, force: bool) -> list[str]:
        sentences = []
        while True:
            self._buffer = self._buffer.lstrip()
            if not self._buffer:
                break
            end = self._hard_end(force)
            if self._first and (soft := self._first_soft_end()) is not None:
                end = soft if end is None else min(end, soft)
            if end is None and len(self._buffer) >= SOFT_LIMIT:
                end = self._last_soft_end()
            if end is None and len(self._buffer) >= HARD_LIMIT:
                end = SOFT_LIMIT
            if end is None:
                if force:
                    end = len(self._buffer)
                else:
                    break
            sentence, self._buffer = self._buffer[:end].strip(), self._buffer[end:]
            if any(c.isalnum() for c in sentence):
                sentences.append(sentence)
                self._first = False
        return sentences

    def _hard_end(self, force: bool) -> int | None:
        text = self._buffer
        for i, c in enumerate(text):
            if c in HARD:
                return i + 1
            # An English full stop ends a sentence only before a space: not in 3.5 or a
            # web address. At the end of the words so far, the next word decides.
            if c == ".":
                if i + 1 < len(text) and text[i + 1].isspace():
                    return i + 1
                if i + 1 == len(text) and force:
                    return i + 1
        return None

    def _first_soft_end(self) -> int | None:
        for i in range(FIRST_AT_LEAST - 1, min(SOFT_LIMIT, len(self._buffer))):
            if self._soft(i):
                return i + 1
        return None

    def _last_soft_end(self) -> int | None:
        for i in range(min(SOFT_LIMIT, len(self._buffer)) - 1, 17, -1):
            if self._soft(i):
                return i + 1
        return None

    def _soft(self, i: int) -> bool:
        c = self._buffer[i]
        if c not in SOFT:
            return False
        # Not the colon of a web address, which is spoken as nothing anyway.
        after = self._buffer[i + 1 : i + 2]
        return not (c == ":" and after == "/")
