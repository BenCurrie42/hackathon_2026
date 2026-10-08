"""EQ Eight in words a volunteer (and the assistant) can use, and what's wrong with a curve.

RigLink reports a track's EQ Eight band by band: on, Live's own name for the filter
type, frequency in Hz, gain in dB, Q. Here those become a fixed vocabulary of eight
filter types ("low cut", "bell", "high shelf"...), a one-line description for the
assistant, and a list of problems spotted by rule rather than by the model, so a
messed-up EQ gets the same diagnosis every time.

A band is {"band": 1-8, "on", "type", "freq_hz", "gain_db", "q"} with type from TYPES.
"""

from __future__ import annotations

from app import parts

TYPES = ("low cut 48", "low cut", "low shelf", "bell", "notch", "high shelf", "high cut", "high cut 48")
# EQ Eight's type menu order (Live 12.4.6: "High Pass 48dB", "High Pass 12dB", "Low Shelf",
# "Bell", "Notch", "High Shelf", "Low Pass 12dB", "Low Pass 48dB"), for when Live gives no names.
GUESSED_ORDER = TYPES
BANDS = 8

# EQ Eight's own default (Defaults/Audio Effects/EQ Eight.adv): four bands on, all flat.
FLAT = [
    {"band": 1, "on": True, "type": "low shelf", "freq_hz": 30.0, "gain_db": 0.0, "q": 0.71},
    {"band": 2, "on": True, "type": "bell", "freq_hz": 200.0, "gain_db": 0.0, "q": 0.71},
    {"band": 3, "on": True, "type": "bell", "freq_hz": 1000.0, "gain_db": 0.0, "q": 0.71},
    {"band": 4, "on": True, "type": "high shelf", "freq_hz": 5000.0, "gain_db": 0.0, "q": 0.71},
    {"band": 5, "on": False, "type": "bell", "freq_hz": 100.0, "gain_db": 0.0, "q": 0.71},
    {"band": 6, "on": False, "type": "bell", "freq_hz": 10000.0, "gain_db": 0.0, "q": 0.71},
    {"band": 7, "on": False, "type": "bell", "freq_hz": 5000.0, "gain_db": 0.0, "q": 0.71},
    {"band": 8, "on": False, "type": "high cut", "freq_hz": 18000.0, "gain_db": 0.0, "q": 0.71},
]

GAINED = ("low shelf", "bell", "high shelf")  # types whose gain does anything
LOW_CUTS = ("low cut", "low cut 48")
HIGH_CUTS = ("high cut", "high cut 48")

# Readback tolerance: Live rounds what it shows, so smaller gaps aren't changes.
FREQ_RATIO = 0.01
GAIN_STEP = 0.1
Q_RATIO = 0.03

# Where a curve stops being a matter of taste (see problems()).
MAX_BOOST_DB = 6.0
MAX_CUT_DB = -12.0
NARROW_Q = 3.0
LOW_PARTS = {"Bass", "Synth Bass", "Drums", "Perc", "Loops"}
VOCAL_PARTS = {"Lead Vocal", "BGVs", "Choir"}


def kind_of(text):
    """One of TYPES for Live's name of a filter type, or None if it isn't recognisable."""
    t = (text or "").casefold()
    steep = "48" in t
    if "notch" in t:
        return "notch"
    if "bell" in t or "peak" in t:
        return "bell"
    if "shelf" in t:
        return "low shelf" if "low" in t else "high shelf" if "high" in t else None
    if "pass" in t:  # a high-pass filter is a low cut
        side = "low cut" if "high" in t else "high cut" if "low" in t else None
    elif "cut" in t:
        side = "low cut" if "low" in t else "high cut" if "high" in t else None
    else:
        return None
    return side and (side + " 48" if steep else side)


def type_index(kind, live_types):
    """The index in Live's type menu for one of TYPES."""
    for i, text in enumerate(live_types or []):
        if kind_of(text) == kind:
            return i
    return GUESSED_ORDER.index(kind)


def bands_of(live_eq):
    """RigLink's EQ (get_eq, or a snapshot row's "eq") as bands in TYPES words."""
    live_types = live_eq.get("types") or []
    rows = []
    for b in live_eq["bands"]:
        kind = kind_of(b.get("type"))
        if kind is None:
            i = b.get("type_index", 3)
            kind = kind_of(live_types[i]) if i < len(live_types) else None
            kind = kind or (GUESSED_ORDER[i] if 0 <= i < len(GUESSED_ORDER) else "bell")
        rows.append({"band": b["band"], "on": bool(b["on"]), "type": kind, "freq_hz": float(b["freq_hz"]),
                     "gain_db": float(b["gain_db"]), "q": float(b["q"])})
    return rows


def flat():
    return [dict(b) for b in FLAT]


def merged(base, changes):
    """base bands with changes (each {"band", and any of on/type/freq_hz/gain_db/q}) laid over."""
    out = {b["band"]: dict(b) for b in base}
    for change in changes:
        band = out.setdefault(change["band"], dict(FLAT[change["band"] - 1]))
        band.update({k: v for k, v in change.items() if v is not None})
    return [out[n] for n in sorted(out)]


def differs(a, b):
    """The fields of band b that differ from band a, as set_eq_band arguments (without type_index)."""
    out = {}
    if bool(a["on"]) != bool(b["on"]):
        out["on"] = bool(b["on"])
    if a["type"] != b["type"]:
        out["type"] = b["type"]
    if abs(a["freq_hz"] - b["freq_hz"]) > FREQ_RATIO * b["freq_hz"]:
        out["freq_hz"] = b["freq_hz"]
    if abs(a["gain_db"] - b["gain_db"]) > GAIN_STEP:
        out["gain_db"] = b["gain_db"]
    if abs(a["q"] - b["q"]) > Q_RATIO * b["q"]:
        out["q"] = b["q"]
    return out


def same(a, b):
    """True if two band lists sound alike."""
    if len(a) != len(b):
        return False
    return not any(differs(x, y) for x, y in zip(a, b))


def commands(where, live_eq, wanted):
    """The set_eq_band calls that turn live_eq (RigLink's) into the wanted bands.

    where: {"track_index", "is_return"}.
    """
    now = {b["band"]: b for b in bands_of(live_eq)}
    calls = []
    for band in wanted:
        current = now.get(band["band"])
        if current is None:
            continue
        change = differs(current, band)
        if not change:
            continue
        if "type" in change:
            change["type_index"] = type_index(change.pop("type"), live_eq.get("types"))
        calls.append(("set_eq_band", dict(where, device_index=live_eq["device_index"], band=band["band"], **change)))
    return calls


# -- in words ----------------------------------------------------------------


def hz(f):
    return f"{f / 1000:.3g} kHz" if f >= 1000 else f"{f:.0f} Hz"


def band_words(b):
    if b["type"] in LOW_CUTS or b["type"] in HIGH_CUTS:
        steep = " (steep)" if b["type"].endswith("48") else ""
        return f"{b['type'].removesuffix(' 48')} {hz(b['freq_hz'])}{steep}"
    if b["type"] == "notch":
        return f"notch {hz(b['freq_hz'])}"
    q = f" Q {b['q']:.1f}" if b["type"] == "bell" else ""
    return f"{b['type']} {hz(b['freq_hz'])} {b['gain_db']:+.1f} dB{q}"


def audible(b):
    """Bands that change the sound: on, and a cut, a notch or some gain."""
    return b["on"] and (b["type"] not in GAINED or abs(b["gain_db"]) > GAIN_STEP)


def describe(bands, on=True):
    """One line: 'flat', or each band that does something."""
    if not on:
        return "switched off"
    doing = [f"{b['band']}: {band_words(b)}" for b in bands if audible(b)]
    return "; ".join(doing) if doing else "flat"


def problems(bands, track_name):
    """What's wrong with a curve, as short sentences. Empty when nothing stands out.

    Rules, not taste: the same curve always gets the same list.
    """
    part = parts.part_for(track_name) or track_name
    low = part in LOW_PARTS or any(w in track_name.casefold() for w in ("kick", "bass", "sub", "808", "tom"))
    vocal = part in VOCAL_PARTS
    found = []
    for b in bands:
        if not b["on"]:
            continue
        n, kind, f, g = b["band"], b["type"], b["freq_hz"], b["gain_db"]
        if kind in GAINED and g > MAX_BOOST_DB:
            found.append(f"band {n} boosts {hz(f)} by {g:.1f} dB (more than {MAX_BOOST_DB:g} dB is harsh or boomy)")
        if kind in GAINED and g < MAX_CUT_DB:
            found.append(f"band {n} cuts {hz(f)} by {-g:.1f} dB (a hole in the sound)")
        if kind == "bell" and g >= 3 and b["q"] > NARROW_Q:
            found.append(f"band {n} is a narrow boost at {hz(f)} (Q {b['q']:.1f}; it rings or whistles)")
        if kind == "notch" and not 100 <= f <= 8000:
            found.append(f"band {n} notches out {hz(f)}, which is rarely a problem frequency")
        if kind in LOW_CUTS and f > (80 if low else 250):
            found.append(f"band {n} cuts everything below {hz(f)} (thin; takes the body out)")
        if kind in HIGH_CUTS and f < (3000 if low else 9000):
            found.append(f"band {n} cuts everything above {hz(f)} (dull, muffled)")
        if kind == "low shelf" and not low and g > 3 and f < 300:
            found.append(f"band {n} adds {g:.1f} dB of low end below {hz(f)} (boomy, muddy)")
    if vocal and not any(b["on"] and b["type"] in LOW_CUTS for b in bands):
        found.append("no low cut on a vocal (rumble and mic pops come through)")
    return found
