"""Mess up an EQ, ask the assistant to fix it, and score the fix.

    uv run python scripts/eq_eval.py              # every case
    uv run python scripts/eq_eval.py honky harsh  # some of them

Runs against the pretend Live (app/fake_live.py) and the model .env picks, so
it costs a few model calls per case and needs no Ableton. Each case wrecks one
track's EQ Eight by hand, sends the volunteer's complaint, applies whatever the
assistant proposes, and checks the curve it leaves:

  clean   no EQ problems left (app/eq.py's rules)
  gentle  no boost over +4 dB, no shelf over 3 dB, no cut deeper than -6 dB
  small   at most four bands doing anything
  heard   the complaint's frequency range was actually dealt with
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["HOLYSOUND_HOME"] = tempfile.mkdtemp(prefix="holysound-eq-eval-")  # never the real ~/.holysound

from app import eq, fake_live  # noqa: E402
from app.assistant import Conversation  # noqa: E402
from app.folders import FolderMemory  # noqa: E402
from app.live import LiveLink  # noqa: E402
from app.room import RoomMemory  # noqa: E402
from app.server import App, load_dotenv  # noqa: E402
from app.song_mixes import SongMixMemory  # noqa: E402

TRACKS = ["Lead Vocal", "Acoustic", "Bass", "Keys"]

# name: (track, set_eq_band calls that wreck it, what the volunteer says, (low Hz, high Hz) it must touch)
CASES = {
    "honky": ("Lead Vocal", [dict(band=2, freq_hz=900, gain_db=12, q=6)],
              "The lead vocal sounds honky and weird. Can you fix the EQ?", (500, 1500)),
    "harsh": ("Lead Vocal", [dict(band=4, freq_hz=3000, gain_db=10)],
              "Lead vocal is really harsh, it hurts.", (2000, 6000)),
    "muffled": ("Acoustic", [dict(band=1, type_index=0, freq_hz=500), dict(band=8, on=True, freq_hz=3000)],
                "The acoustic sounds thin and muffled.", (20, 20000)),
    "boomy": ("Keys", [dict(band=1, freq_hz=250, gain_db=9)],
              "Keys are boomy and muddy, fix it please.", (60, 400)),
    "gutted": ("Bass", [dict(band=1, type_index=0, freq_hz=220)],
               "The bass has no low end at all.", (20, 300)),
    "wrecked": ("Lead Vocal", [dict(band=1, type_index=0, freq_hz=400), dict(band=2, freq_hz=200, gain_db=-14),
                               dict(band=3, freq_hz=2500, gain_db=11, q=8), dict(band=8, on=True, freq_hz=5000)],
                "Someone messed with the vocal EQ during rehearsal. Can you fix it?", (20, 20000)),
}


def score(bands, track, touched_range, before):
    doing = [b for b in bands if eq.audible(b)]
    boosts = [b for b in doing if b["type"] in eq.GAINED and b["gain_db"] > 0]
    cuts = [b for b in doing if b["type"] in eq.GAINED and b["gain_db"] < 0]
    lo, hi = touched_range
    changed = [b for b, was in zip(bands, before) if eq.differs(was, b)]
    return {
        "clean": not eq.problems(bands, track),
        "gentle": all(b["gain_db"] <= (3 if "shelf" in b["type"] else 4) for b in boosts)
                  and all(b["gain_db"] >= -6 for b in cuts),
        "small": len(doing) <= 4,
        "heard": any(lo <= x["freq_hz"] <= hi for x in changed),
    }


def run_case(app, live, name):
    track, wreck, message, touched = CASES[name]
    index = TRACKS.index(track)
    for device in reversed(live.call("list_devices", track_index=index)):
        live.call("delete_device", track_index=index, device_index=device["index"])
    live.call("load_device", track_index=index, device_name="EQ Eight")
    for change in wreck:
        live.call("set_eq_band", track_index=index, **change)
    before = eq.bands_of(live.call("get_eq", track_index=index))
    app.chat.reset()
    app.send_message(message)
    reply = next((e["text"] for e in reversed(app.chat.transcript) if e.get("role") == "assistant"), "")
    pending = [pid for pid, p in app.chat.proposals.items() if p["status"] == "pending"]
    if not pending:
        return {"proposed": False}, reply, before, before
    for pid in pending:
        app.apply(pid)
    after = eq.bands_of(live.call("get_eq", track_index=index))
    return dict(proposed=True, **score(after, track, touched, before)), reply, before, after


def main(names):
    load_dotenv()
    server, _fake = fake_live.serve(port=0, latency=0)
    live = LiveLink(port=server.server_address[1])
    home = Path(os.environ["HOLYSOUND_HOME"])
    app = App(live, Conversation(room=RoomMemory(home / "room.json")),
              folders=FolderMemory(home / "folders.json"), mixes=SongMixMemory(home / "song_mixes.json"))
    for track in TRACKS:
        live.call("create_audio_track", name=track)
    live.call("set_scene", scene_index=0, name="Living Hope")
    app.pick_song_mix(0)

    passed = 0
    for name in names:
        result, reply, before, after = run_case(app, live, name)
        ok = all(result.values())
        passed += ok
        print(f"\n== {name}: {'PASS' if ok else 'FAIL'}  " + "  ".join(f"{k}={'y' if v else 'n'}" for k, v in result.items()))
        print(f"   before: {eq.describe(before)}")
        print(f"   after:  {eq.describe(after)}")
        left = eq.problems(after, CASES[name][0])
        if left:
            print(f"   still:  {'; '.join(left)}")
        print(f"   said:   {reply.strip()[:300]}")
    print(f"\n{passed}/{len(names)} passed")
    live.close()
    server.shutdown()
    return 0 if passed == len(names) else 1


if __name__ == "__main__":
    wanted = sys.argv[1:] or list(CASES)
    unknown = [n for n in wanted if n not in CASES]
    if unknown:
        sys.exit(f"No case called {', '.join(unknown)}. Cases: {', '.join(CASES)}")
    sys.exit(main(wanted))
