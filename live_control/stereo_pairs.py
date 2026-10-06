"""Spotting stereo pairs among stems, so both sides get the same level.

Tracks rigs split wide parts into two mono stems: "GTR 1 L" and "GTR 1 R",
"Crowds 4 Left" and "Crowds 4 Right". Matching each side's loudness on its
own would tip the image towards whichever side happens to be quieter, so a
pair is levelled as one part.

Only exact base names pair. Real folders have near misses like "Loops Synths
L" next to "Loops Synth R"; guessing those would pair the wrong stems.
"""

import re

# A separator is required, or "Vocal" would read as "Voca" + L.
_SIDE = re.compile(r"^(?P<base>.*?\S)[\s_-]+(?P<side>l|r|left|right)$", re.IGNORECASE)


def stereo_pairs(names):
    """(left, right) for each pair of names that differ only by an L/R suffix."""
    sides = {}
    for name in names:
        match = _SIDE.match(name.strip())
        if not match:
            continue
        side = "left" if match["side"].casefold().startswith("l") else "right"
        sides.setdefault(match["base"].casefold(), {}).setdefault(side, []).append(name)
    pairs = []
    for found in sides.values():
        left, right = found.get("left", []), found.get("right", [])
        if len(left) == 1 and len(right) == 1:
            pairs.append((left[0], right[0]))
    return pairs
