"""What the image model is asked for each of the avatar's 21 frames (spec 21).

Adapted from the reference app's prompts (`references/avatar-image/index.ts`): the base
and mouth prompts are its own; the six expressions follow spec 21's table, every one
with closed lips, so that each expression's half and wide frames open a closed mouth.
The background is transparent (P24): the app draws its own dark backdrop, so no frame
brings a colour of its own that could shift from one frame to the next.
A frame's mouth prompt never names an expression: the input is already that expression,
and naming one gives the model a reason to redraw it.

Changing any prompt makes avatars that look different: bump PROMPT_VERSION, which a test
pins together with the prompts' hash. Each avatar version records both.
"""

import hashlib
import json

from wobot.assets.frames import EXPRESSIONS, FRAME_KEYS, MOUTHS

PROMPT_VERSION = 2

BASE_PROMPT = (
    "Turn the person in this photo into a cute chibi character — FACE ONLY: a big "
    "head with the face filling the frame, NO neck, no shoulders, no body, "
    "front-facing, looking straight at the camera. Art style: flat 2D cartoon / "
    "vector illustration, clean bold dark outlines, simple flat cel shading (NO "
    "glossy 3D, NO realistic rendering), big round expressive eyes. Keep them "
    "recognizable: keep original face features, preserve hair color and hairstyle, "
    "keep glasses if present, same skin tone. Expression: friendly, gentle "
    "CLOSED-mouth smile, lips together. Even soft lighting. Fully TRANSPARENT "
    "background: nothing behind the head, no backdrop color, no shadow. Clean, crisp, "
    "high quality. No text, no watermark, no border."
)

EXPRESSION_PREFIX = (
    "Keep this the SAME chibi character — same identity, hair, glasses, skin, and "
    "the same FLAT 2D cartoon style, outlines, colors and lighting, and the same fully "
    "TRANSPARENT background. "
    "Keep the same head position, size and framing. Change only the expression to: "
)

# The closed frame of each expression but neutral, which is the base itself. Lips stay
# together in all of them (spec 21); Fear and Surprise differ by the brows.
EXPRESSION_PROMPTS = {
    "joy": (
        "JOY — raised cheeks, happy eyes curved into a joyful (^_^) shape, eyebrows "
        "relaxed and slightly up, a big CLOSED-mouth smile with the lips together."
    ),
    "sadness": (
        "SADNESS — the inner ends of the eyebrows raised, eyes looking down, the corners "
        "of the mouth turned down, lips CLOSED together."
    ),
    "anger": (
        "ANGER — eyebrows pulled down and together, a focused glare, lips CLOSED and "
        "pressed slightly tight."
    ),
    "fear": (
        "FEAR — eyebrows raised AND drawn together in a tense, worried shape, eyes wide "
        "open, lips CLOSED together and tense."
    ),
    "surprise": (
        "SURPRISE — eyebrows raised high in smooth round arches (not drawn together), big "
        "wide round eyes, lips CLOSED together."
    ),
    "thinking": (
        "THINKING — eyes looking up to one side, one eyebrow slightly raised, lips CLOSED "
        "in a small pensive line."
    ),
}

MOUTH_PREFIX = (
    "Keep this EXACT same cartoon image unchanged — same identity, hair, glasses, "
    "skin, EXPRESSION, eyes, eyebrows, cheeks, the same flat 2D cartoon style, "
    "outlines, colors, shading and lighting, the same fully TRANSPARENT background, and "
    "the SAME head "
    "position, size, rotation and framing. Change ONE thing only: "
)

# "Mouth width" where the reference said "smile width": a sad or angry mouth is no smile.
MOUTH_PROMPTS = {
    # v2: the reference's "slightly open" came out barely parted, too close to closed for
    # the mouth to be seen moving; this is a third of the way to wide open.
    "half": (
        "the MOUTH is now about ONE-THIRD OPEN — lips clearly parted with a visible dark "
        "gap and the upper teeth just showing, halfway between closed and wide open, as if "
        "caught mid-word. Keep the same lip color and the same mouth width."
    ),
    "wide": (
        "the MOUTH is now WIDE OPEN — a clearly open, rounded mouth as if speaking "
        "loudly. Keep the same lip color and the same mouth width."
    ),
}


def prompt_for(frame_key: str) -> str:
    expression, mouth = frame_key.split("_")
    if expression not in EXPRESSIONS or mouth not in MOUTHS:
        raise ValueError(f"unknown frame: {frame_key}")
    if mouth != "closed":
        return MOUTH_PREFIX + MOUTH_PROMPTS[mouth]
    if expression == "neutral":
        return BASE_PROMPT
    return EXPRESSION_PREFIX + EXPRESSION_PROMPTS[expression]


def prompt_hash() -> str:
    """What every frame is asked, as one hash: two avatars with the same hash were made
    from the same prompts."""
    prompts = {key: prompt_for(key) for key in FRAME_KEYS}
    return hashlib.sha256(json.dumps(prompts, sort_keys=True).encode()).hexdigest()
