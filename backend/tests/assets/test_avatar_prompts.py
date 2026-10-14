"""The avatar's prompts and the frames' tree (spec 21)."""

import re

from wobot.assets import avatar_prompts
from wobot.assets.avatar_prompts import prompt_for, prompt_hash
from wobot.assets.frames import BASE, EXPRESSIONS, FRAME_KEYS, descendants, parent_of


def test_the_prompt_version_moves_with_the_prompts():
    assert (avatar_prompts.PROMPT_VERSION, prompt_hash()[:12]) == (2, "49f8be7bac85")


def test_every_frame_has_a_prompt_and_every_closed_frame_keeps_the_lips_together():
    for key in FRAME_KEYS:
        prompt = prompt_for(key)
        if key.endswith("_closed"):
            assert "CLOSED" in prompt, key


def test_a_mouth_prompt_never_names_an_expression():
    # The input already is the expression; naming one invites the model to redraw it.
    names = "|".join(e for e in EXPRESSIONS if e != "neutral")
    for key in FRAME_KEYS:
        if not key.endswith("_closed"):
            assert not re.search(names, prompt_for(key), re.IGNORECASE), key


def test_every_frame_keeps_a_transparent_background():
    for key in FRAME_KEYS:
        assert "TRANSPARENT background" in prompt_for(key), key
    assert "pastel" not in prompt_for(BASE)


def test_fear_and_surprise_are_told_apart_by_the_brows():
    assert "drawn together" in prompt_for("fear_closed")
    assert "not drawn together" in prompt_for("surprise_closed")


def test_the_tree_is_three_levels_from_the_photo():
    assert parent_of(BASE) is None
    assert {parent_of(f"{e}_closed") for e in EXPRESSIONS if e != "neutral"} == {BASE}
    assert parent_of("anger_half") == parent_of("anger_wide") == "anger_closed"
    assert parent_of("neutral_wide") == BASE
    assert sorted(descendants("joy_closed")) == ["joy_half", "joy_wide"]
    assert len(descendants(BASE)) == 20
    assert descendants("joy_half") == []
