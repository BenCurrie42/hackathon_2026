"""Spotting timecode stems, which start muted.

Tracks rigs often ship a SMPTE (LTC) stem for syncing lights or video. Through
the speakers it's a loud screech, so a new track for one starts muted; whoever
needs it routes it to its own output and unmutes it.
"""

import re

_TIMECODE = re.compile(r"\b(smpte|ltc|timecode|time code)\b", re.IGNORECASE)


def is_timecode(stem_name):
    """True if a stem's name says it's timecode."""
    return bool(_TIMECODE.search(stem_name))
