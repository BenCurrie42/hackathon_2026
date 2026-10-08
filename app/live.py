"""One shared, self-healing connection to Live for the web app.

runtime.LiveConnection is one socket with one command in flight. The web server
has many threads (a laptop and a phone polling at once), so everything goes
through a lock here, and a dropped connection is reopened on the next call
instead of taking the app down.
"""

from __future__ import annotations

import socket
import threading
import time

from live_control.live_connection import LiveConnection, RigLinkError

# Loading a device walks Live's browser tree, which can take a while.
TIMEOUT_SECONDS = 30.0

# Several viewers polling at once shouldn't each cost Live a round trip.
SNAPSHOT_MAX_AGE = 0.5

NOT_CONNECTED = (
    "Can't reach Ableton Live. Make sure Live is open and RigLink is selected "
    "as a Control Surface in Settings → Link, Tempo & MIDI."
)


class LiveUnavailable(RuntimeError):
    """Live isn't running, or RigLink isn't selected as a Control Surface."""


class LiveLink:
    def __init__(self, host="127.0.0.1", port=9877):
        self._host = host
        self._port = port
        self._conn = None
        self._lock = threading.Lock()
        self._snapshot = None
        self._snapshot_at = 0.0
        self._has_live_meters_cmd = True
        self._stock_devices = None
        self._has_snapshot_cmd = True

    # -- plumbing -------------------------------------------------------

    def _connection(self):
        if self._conn is None:
            try:
                self._conn = LiveConnection(self._host, self._port, timeout=TIMEOUT_SECONDS)
            except OSError as e:
                raise LiveUnavailable(NOT_CONNECTED) from e
        return self._conn

    def _drop(self):
        if self._conn is not None:
            try:
                self._conn.close()
            except OSError:
                pass
        self._conn = None
        self._stock_devices = None
        self._has_live_meters_cmd = True  # a reconnect may be a newer RigLink

    def _send(self, cmd, **args):
        """Send one command. Caller holds the lock."""
        conn = self._connection()
        try:
            return conn.send(cmd, **args)
        except RigLinkError:
            raise
        except (OSError, ValueError) as e:
            # Live closed, RigLink was reloaded, or the reply was cut short.
            self._drop()
            if isinstance(e, socket.timeout):
                raise LiveUnavailable("Live stopped responding before it finished.") from e
            raise LiveUnavailable(NOT_CONNECTED) from e

    def close(self):
        with self._lock:
            self._drop()

    def call(self, cmd, **args):
        """Run one RigLink command and invalidate the cached snapshot."""
        with self._lock:
            try:
                return self._send(cmd, **args)
            finally:
                self._snapshot_at = 0.0

    # -- reads ----------------------------------------------------------

    def snapshot(self, max_age=SNAPSHOT_MAX_AGE):
        """The whole set as the UI draws it. Raises LiveUnavailable."""
        if self._snapshot is not None and time.monotonic() - self._snapshot_at < max_age:
            return self._snapshot
        # A device load can hold the lock for seconds. Don't queue every poll
        # behind it: hand back what we last saw.
        if not self._lock.acquire(timeout=0.2):
            if self._snapshot is not None:
                return self._snapshot
            self._lock.acquire()
        try:
            snap = self._fetch_snapshot()
            self._snapshot = snap
            self._snapshot_at = time.monotonic()
            return snap
        except LiveUnavailable:
            self._snapshot = None
            raise
        finally:
            self._lock.release()

    def _fetch_snapshot(self):
        if self._has_snapshot_cmd:
            try:
                return self._send("get_snapshot")
            except RigLinkError as e:
                if "unknown cmd" not in str(e):
                    raise
                # An older RigLink without get_snapshot. Build it the slow way.
                self._has_snapshot_cmd = False
        return self._assemble_snapshot()

    def _assemble_snapshot(self):
        tracks = []
        for row in self._send("list_tracks"):
            i = row["index"]
            tracks.append(self._assemble_strip(i, row["name"], False) | {
                "is_midi": row["is_midi"],
                "input": _current(self._send("get_routing", track_index=i)["input"]),
            })
        returns = [self._assemble_strip(r["index"], r["name"], True) for r in self._send("list_returns")]
        return {
            "song": self._send("get_song"),
            "tracks": tracks,
            "returns": returns,
            "master": None,
            "scenes": self._send("list_scenes"),
            "locators": self._send("list_locators"),
        }

    def _assemble_strip(self, i, name, is_return):
        mixer = self._send("get_mixer", track_index=i, is_return=is_return)
        routing = self._send("get_routing", track_index=i, is_return=is_return)
        devices = self._send("list_devices", track_index=i, is_return=is_return)
        return {
            "index": i,
            "name": name,
            "is_return": is_return,
            "volume": mixer["volume"],
            "volume_db": _db_or_none(mixer["volume"]),
            "pan": mixer["pan"],
            "pan_value": None,
            "mute": mixer["mute"],
            "solo": mixer["solo"],
            "sends": [s | {"level_db": _db_or_none(s["level"])} for s in mixer["sends"]],
            "devices": [d["name"] for d in devices],
            "output": _current(routing["output"]),
        }

    def meters(self):
        """Each meter's peak since the last read, or None when there's nothing new to draw.

        Never waits behind a long command and leaves the snapshot cache alone:
        the page asks for this many times a second.
        """
        if not self._has_live_meters_cmd or not self._lock.acquire(timeout=0.05):
            return None
        try:
            meters = self._send("get_live_meters")
        except RigLinkError as e:
            if "unknown cmd" in str(e):
                # An older RigLink; the page falls back to the snapshot's meters.
                self._has_live_meters_cmd = False
            return None
        except LiveUnavailable:
            return None
        finally:
            self._lock.release()
        return meters if meters.get("fresh") else None

    def stock_devices(self):
        """Device names this copy of Live has, or None if RigLink can't say."""
        if self._stock_devices is None:
            try:
                self._stock_devices = self.call("list_stock_devices")
            except RigLinkError:
                return None
        return self._stock_devices

    def presets(self, device_name):
        return self.call("list_presets", device_name=device_name)


def _current(side):
    return {"type": side["type"], "channel": side["channel"]}


def _db_or_none(text):
    try:
        db = float(text.replace("−", "-").split()[0])
    except (ValueError, IndexError):
        return None
    return None if db == float("-inf") else db
