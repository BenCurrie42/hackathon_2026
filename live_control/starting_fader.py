"""Where to put the fader on a track that an import just created.

Level matching brings every stem to about the same loudness, which is right for
one stem on its own and far too loud for fifty playing at once: the sum of n
equally loud stems is 10*log10(n) dB louder than one. So new tracks start low
enough that the whole song lands near the loudness of a single stem, with a few
dB spare for peaks that line up. Tracks that already existed keep their fader;
that's the volunteer's mix.
"""

import math

# Headroom beyond the plain sum, for drum hits and accents that land together.
PEAK_MARGIN_DB = 3.0


def starting_fader_db(stem_count):
    """Fader in dB for each new track when a song brings stem_count stems in at once."""
    if stem_count < 1:
        return 0.0
    db = -10 * math.log10(stem_count) - PEAK_MARGIN_DB
    return round(db * 2) / 2
