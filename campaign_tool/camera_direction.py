"""DF0237-11: the grammar of an OpenStreetMap camera facing tag kept in cameras.geojson properties.direction.

A nonnull value is a string of 1 to 4 tokens joined by ``;`` (no whitespace, no empty token). A token is a whole
number 0 to 359 (no leading zero, no sign, no fraction) or one of the 16 compass words N, NNE, NE, ENE, E, ESE, SE,
SSE, S, SSW, SW, WSW, W, WNW, NW, NNW in any ASCII case. The value is kept exactly as tagged: it is never converted,
normalised or interpreted, and it says nothing about a road, a plate, coverage or who runs the camera.

``DIRECTION_PATTERN`` is an unanchored regex source with the case already spelled out, so the same text works in
Python (``re.fullmatch``) and in JavaScript (``new RegExp("^(?:" + source + ")$")``) with no flags.
"""
import re

COMPASS = ("N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW")
MAX_TOKENS = 4
MAX_LENGTH = 15  # four 3-character tokens and three separators

_WORDS = "|".join("".join(f"[{c}{c.lower()}]" for c in word) for word in COMPASS)
_TOKEN = "(?:0|[1-9][0-9]?|[12][0-9]{2}|3[0-4][0-9]|35[0-9]|" + _WORDS + ")"
DIRECTION_PATTERN = _TOKEN + "(?:;" + _TOKEN + "){0,%d}" % (MAX_TOKENS - 1)
_FULL = re.compile(DIRECTION_PATTERN)


def valid_direction(value):
    """True for a string in the grammar above; False for everything else, None and a missing tag included."""
    return isinstance(value, str) and len(value) <= MAX_LENGTH and _FULL.fullmatch(value) is not None
