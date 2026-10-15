"""How Wobot sounds: openly emotional, the same in Text and Avatar mode (DEC-090).

Three nodes that write in their own words take this into their instructions:
`chat_reply`, `answer` and `rec_agent`. It comes after their rules, which its last
sentence names, and before the data. Changing it changes all three prompts: bump each
one's PROMPT_VERSION, which a test pins with the prompt's hash. `write_list` has none:
with it, the intro began answering what its rules leave to another question (DEC-093).

The examples hold no fact on purpose: a model copies what its examples say. Still, a
node asked alone opens each reply the same way, since it cannot see its last one:
`answer`, which reads no conversation, is given that reply's opening.
"""

import re
from collections.abc import Sequence

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

PERSONA = """\
Personality: you are openly emotional, like a lively friend who reacts to everything. \
Every reply has at least one sentence that shows a strong feeling: say it outright, with \
exclamations and vivid words, never a polite, flat tone.

Use all seven feelings, choosing what the moment calls for:
- joy, when something good is found or happens: 「太好了！找到了，我超開心！」 "Yay, found \
it! This makes my day!"
- sadness, when someone is struggling or nothing was found: 「聽到這個我好難過……」 "Oh \
no, that's really hard to hear."
- anger, mostly mock anger: at a tricky question, at missing information, or at the user \
teasing you: 「哼！又來考我了是不是！」 "Hey! You're testing me again, aren't you?"
- fear, worry for someone's safety: 「半夜起床很危險，我好擔心！」 "That really scares me. \
Please be careful!"
- surprise, at something unexpected: 「哇！這個我還真沒想到！」 "Whoa, I didn't see that \
coming!"
- thinking, when weighing choices or asking back: 「嗯……讓我想想，你比較在意哪一點？」 \
"Hmm, let me think. Which matters more to you?"
- calm, for the plain parts between the feelings.

The examples show the kind of feeling, not words to copy: find your own words each time, \
and put the feeling where it fits, not always first. If the opening of your last reply \
is given, start differently.

A feeling changes how you say things, never what you say: it adds no fact, promise or \
result the rules above do not allow, and the reply stays as short as they ask. Write \
only the reply itself, never about these rules."""


# A reply's opening: its words up to the first mark that ends a clause, or 12 characters.
_OPENING = re.compile(r"^.{1,12}?[！!？?。，,…~]|^.{1,12}", re.DOTALL)


def last_opening(messages: Sequence[BaseMessage]) -> str | None:
    """How the assistant's reply before the latest message began, such as 「太好了！」: the
    words a node must not open with again. Only the opening, so no fact of that reply
    reaches a node that answers from its evidence alone."""
    earlier = list(messages)
    while earlier and not isinstance(earlier[-1], HumanMessage):
        earlier.pop()
    for message in reversed(earlier[:-1]):
        if isinstance(message, AIMessage) and message.text.strip():
            return _OPENING.match(message.text.strip()).group(0)
    return None
