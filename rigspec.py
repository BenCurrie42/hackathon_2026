"""The Rig Spec: a session described the way a person would describe it.

This is the contract. The model writes one of these; the renderer turns it into
a .als. Everything else in the project is replaceable around it.

Deliberately absent: device parameters. The spec names a device and lets the
renderer pick a concrete stock preset. A model that writes `Ratio: 3.5` is a
model writing Ableton internals, which is the failure mode this whole design
exists to prevent.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class TrackSpec(BaseModel):
    """One track in the session."""

    name: str = Field(min_length=1, max_length=64)
    type: Literal["audio", "midi"] = "audio"

    input: int | None = Field(default=None, ge=1, le=64)
    """Hardware input channel, 1-based, as printed on the interface."""

    output: str | None = None
    """Output routing. Only "Main" is supported so far."""

    devices: list[str] = Field(default_factory=list)
    """Stock Live device display names, in chain order, e.g. ["Compressor"]."""

    volume_db: float = Field(default=0.0, ge=-70.0, le=6.0)
    """Live's fader range. Stored internally as a linear gain, not as dB."""

    pan: float = Field(default=0.0, ge=-1.0, le=1.0)
    """-1 hard left, 0 centre, 1 hard right."""


class RigSpec(BaseModel):
    """A whole session."""

    tracks: list[TrackSpec] = Field(min_length=1)
