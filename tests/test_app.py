"""Holy Sound web app, end to end against the fake Live and a scripted Claude.

    uv run python -m unittest discover tests

Needs neither Ableton nor an API key. unittest rather than pytest because
pytest isn't an approved dependency yet (CLAUDE.md).
"""

import copy
import http.client
import http.client as http_client
import json
import math
import struct
import tempfile
import threading
import unittest
import wave
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app import audio_files, fake_live
from app.actions import AddTrack, Proposal, run_all, to_rigspec
from app.assistant import AssistantUnavailable, Conversation, session_notes
from app.live import LiveLink, LiveUnavailable
from app.room import RoomMemory
from app.server import App, make_handler


class Block(SimpleNamespace):
    def model_dump(self, mode=None, exclude_none=False, **_):
        return {k: v for k, v in vars(self).items() if not (exclude_none and v is None)}


def text(t):
    return Block(type="text", text=t)


def call(call_id, actions):
    return Block(type="tool_use", id=call_id, name="propose_changes", input={"actions": actions})


def reply(*blocks, stop="end_turn"):
    return SimpleNamespace(stop_reason=stop, content=list(blocks))


class ScriptedClaude:
    """Stands in for anthropic.Anthropic: returns canned replies, records requests."""

    def __init__(self, *script):
        self.script = list(script)
        self.requests = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.requests.append(copy.deepcopy(kwargs))
        return self.script.pop(0)


class FakeLiveCase(unittest.TestCase):
    def setUp(self):
        self.server, self.fake = fake_live.serve(port=0, latency=0)
        self.live = LiveLink(port=self.server.server_address[1])

    def tearDown(self):
        self.live.close()
        self.server.shutdown()
        self.server.server_close()


class ActionsTest(FakeLiveCase):
    def run_actions(self, *actions):
        return run_all(self.live, Proposal.model_validate({"actions": list(actions)}).actions)

    def test_add_track_with_input_devices_and_send(self):
        results = self.run_actions(
            {"action": "add_track", "name": "Lead Vocal", "input": "1", "volume_db": -3,
             "devices": [{"device": "EQ Eight"}, {"device": "Compressor", "preset": "Gentle Squeeze"}]},
            {"action": "set_send", "track": "Lead Vocal", "to_return": "Reverb", "db": -12},
        )
        self.assertTrue(all(r["ok"] and not r["partial"] for r in results), results)
        snap = self.live.snapshot(max_age=0)
        vocal = snap["tracks"][0]
        self.assertEqual(vocal["input"], {"type": "Ext. In", "channel": "1"})
        self.assertEqual(vocal["devices"], ["EQ Eight", "Compressor"])
        self.assertEqual(vocal["volume"], "-3.0 dB")
        self.assertEqual(vocal["sends"][0]["level"], "-12.0 dB")

    def test_playback_track_has_no_input_and_goes_to_in_ears(self):
        results = self.run_actions(
            {"action": "add_track", "name": "Click", "input": None,
             "output": {"destination": "Ext. Out", "channel": "3/4"}},
        )
        self.assertTrue(results[0]["ok"] and not results[0]["partial"], results)
        click = self.live.snapshot(max_age=0)["tracks"][0]
        self.assertEqual(click["input"]["type"], "No Input")
        self.assertEqual(click["output"], {"type": "Ext. Out", "channel": "3/4"})

    def test_unknown_preset_falls_back_to_default_settings(self):
        results = self.run_actions(
            {"action": "add_track", "name": "Keys", "input": "5/6",
             "devices": [{"device": "Reverb", "preset": "Made Up Preset"}]},
        )
        self.assertTrue(results[0]["partial"])
        self.assertIn("default settings", results[0]["text"])
        self.assertEqual(self.live.snapshot(max_age=0)["tracks"][0]["devices"], ["Reverb"])

    def test_failures_read_as_sentences_and_dont_stop_the_batch(self):
        results = self.run_actions(
            {"action": "set_volume", "track": "Nobody", "db": -6},
            {"action": "add_track", "name": "Bass", "input": "99"},
            {"action": "set_tempo", "bpm": 68},
        )
        self.assertFalse(results[0]["ok"])
        self.assertIn("There's no track called Nobody", results[0]["text"])
        self.assertTrue(results[1]["partial"])
        self.assertIn("couldn't set input 99", results[1]["text"])
        self.assertTrue(results[2]["ok"])
        self.assertEqual(self.live.snapshot(max_age=0)["song"]["tempo"], 68)

    def test_tracks_resolve_by_number_and_return_letter(self):
        self.run_actions({"action": "add_track", "name": "Pad"})
        results = self.run_actions(
            {"action": "set_mute", "track": "1", "on": True},
            {"action": "add_device", "track": "B", "device": {"device": "Delay"}},
        )
        self.assertTrue(all(r["ok"] for r in results), results)
        snap = self.live.snapshot(max_age=0)
        self.assertTrue(snap["tracks"][0]["mute"])
        self.assertEqual(snap["returns"][1]["devices"], ["Delay"])

    def test_songs(self):
        results = self.run_actions(
            {"action": "add_song", "name": "Way Maker", "bpm": 68},
            {"action": "update_song", "song": "Way Maker", "bpm": 70},
            {"action": "start_song", "song": "Way Maker"},
        )
        self.assertTrue(all(r["ok"] for r in results), results)
        snap = self.live.snapshot(max_age=0)
        self.assertEqual(snap["scenes"][-1], {"index": 1, "name": "Way Maker", "tempo": 70.0})
        self.assertEqual(snap["song"]["tempo"], 70)

    def test_to_rigspec_keeps_tracks_and_explains_the_rest(self):
        actions = Proposal.model_validate({"actions": [
            {"action": "add_track", "name": "Vox", "input": "2", "devices": [{"device": "Compressor"}]},
            {"action": "add_track", "name": "Keys", "input": "3/4"},
            {"action": "set_tempo", "bpm": 72},
        ]}).actions
        spec, notes = to_rigspec(actions)
        self.assertEqual([t.name for t in spec.tracks], ["Vox", "Keys"])
        self.assertEqual(spec.tracks[0].input, 2)
        self.assertIsNone(spec.tracks[1].input)
        self.assertEqual(len(notes), 2)

    def test_lost_connection_marks_the_rest_not_done(self):
        actions = Proposal.model_validate({"actions": [
            {"action": "set_tempo", "bpm": 70},
            {"action": "set_tempo", "bpm": 71},
        ]}).actions
        self.server.shutdown()
        self.server.server_close()
        self.live._drop()
        results = run_all(self.live, actions)
        self.assertEqual([r["ok"] for r in results], [False, False])
        self.assertIn("Ableton Live", results[0]["text"])


class SnapshotTest(FakeLiveCase):
    def test_falls_back_when_riglink_has_no_snapshot_command(self):
        self.fake.create_audio_track("Old")
        real = self.fake.handle
        self.fake.handle = lambda req: (
            {"ok": False, "error": "unknown cmd: get_snapshot"} if req["cmd"] == "get_snapshot" else real(req)
        )
        snap = self.live.snapshot(max_age=0)
        self.assertEqual(snap["tracks"][0]["name"], "Old")
        self.assertEqual(snap["tracks"][0]["volume_db"], 0.0)
        self.assertEqual(len(snap["returns"]), 2)

    def test_unreachable_live_is_a_sentence(self):
        live = LiveLink(port=1)
        self.addCleanup(live.close)
        with self.assertRaises(LiveUnavailable) as ctx:
            live.snapshot()
        self.assertIn("RigLink", str(ctx.exception))


class ConversationTest(unittest.TestCase):
    def test_question_then_proposal_then_outcome_reported_back(self):
        claude = ScriptedClaude(
            reply(text("Which input is the lead vocal on?")),
            reply(text("Here's the vocal."), call("tu_1", [
                {"action": "add_track", "name": "Lead Vocal", "input": "1"},
            ])),
            reply(text("Done.")),
        )
        chat = Conversation(client_factory=lambda: claude)
        chat.send("Set up a lead vocal", "notes")
        chat.send("Input 1", "notes")
        pid = chat.transcript[-1]["proposal_id"]
        self.assertEqual(chat.proposal(pid)["status"], "pending")

        chat.record_outcome(pid, "applied", [{"ok": True, "text": "Added Lead Vocal."}])
        chat.send("Thanks", "notes")

        last_user = claude.requests[-1]["messages"][-1]
        self.assertEqual(last_user["content"][0]["type"], "tool_result")
        self.assertEqual(last_user["content"][0]["tool_use_id"], "tu_1")
        self.assertIn("applied", last_user["content"][0]["content"])
        self.assertIn("<session>", last_user["content"][1]["text"])
        # History is append-only: earlier turns are sent back exactly as received.
        self.assertEqual(claude.requests[-1]["messages"][:4], claude.requests[1]["messages"][:3] + [
            {"role": "assistant", "content": [
                {"type": "text", "text": "Here's the vocal."},
                {"type": "tool_use", "id": "tu_1", "name": "propose_changes",
                 "input": {"actions": [{"action": "add_track", "name": "Lead Vocal", "input": "1"}]}},
            ]},
        ])

    def test_invalid_proposal_is_sent_back_to_fix(self):
        claude = ScriptedClaude(
            reply(call("tu_bad", [{"action": "set_volume", "track": "Vox", "db": 40}])),
            reply(call("tu_good", [{"action": "set_volume", "track": "Vox", "db": 3}])),
        )
        chat = Conversation(client_factory=lambda: claude)
        chat.send("Louder vocal", "notes")
        retry = claude.requests[1]["messages"][-1]["content"][0]
        self.assertTrue(retry["is_error"])
        self.assertIn("db", retry["content"])
        self.assertIn("proposal_id", chat.transcript[-1])

    def test_a_new_message_supersedes_an_unapplied_proposal(self):
        claude = ScriptedClaude(
            reply(text("Try this."), call("tu_1", [{"action": "set_tempo", "bpm": 70}])),
            reply(text("OK.")),
        )
        chat = Conversation(client_factory=lambda: claude)
        chat.send("Tempo 70", "notes")
        pid = chat.transcript[-1]["proposal_id"]
        chat.send("Actually never mind", "notes")
        self.assertEqual(chat.proposal(pid)["status"], "superseded")
        self.assertIn("hasn't applied", claude.requests[1]["messages"][-1]["content"][0]["content"])

    def test_failed_request_leaves_history_valid(self):
        def boom(**_):
            raise RuntimeError("network down")

        claude = ScriptedClaude(reply(text("Hello again.")))
        real = claude.beta.messages.create
        chat = Conversation(client_factory=lambda: claude)
        claude.beta.messages.create = boom
        with self.assertRaises(RuntimeError):
            chat.send("Hi", "notes")
        self.assertEqual(chat.messages, [])
        self.assertEqual(chat.transcript, [])
        claude.beta.messages.create = real
        chat.send("Hi", "notes")
        self.assertEqual([m["role"] for m in chat.messages], ["user", "assistant"])

    def test_missing_key_is_explained(self):
        def no_key(**_):
            raise TypeError('"Could not resolve authentication method. Expected one of api_key..."')

        claude = ScriptedClaude()
        claude.beta.messages.create = no_key
        chat = Conversation(client_factory=lambda: claude)
        with self.assertRaises(AssistantUnavailable) as ctx:
            chat.send("Hi", "notes")
        self.assertIn("ANTHROPIC_API_KEY", str(ctx.exception))
        self.assertEqual(chat.setup_error, str(ctx.exception))
        self.assertEqual(chat.messages, [])

    def test_session_notes_describe_the_set(self):
        server, fake = fake_live.serve(port=0, latency=0)
        try:
            fake.create_audio_track("Lead Vocal")
            fake.load_device(0, "Compressor")
            live = LiveLink(port=server.server_address[1])
            notes = session_notes(live.snapshot(), live.stock_devices())
            live.close()
        finally:
            server.shutdown()
            server.server_close()
        self.assertIn("1. Lead Vocal | colour orange | audio | in: Ext. In 1 | out: Master", notes)
        self.assertIn("effects: Compressor", notes)
        self.assertIn("A. A-Reverb", notes)
        self.assertIn("audio effects: Auto Filter", notes)
        self.assertIn("NOT connected", session_notes(None, None, "Live is closed"))


class ServerTest(FakeLiveCase):
    def setUp(self):
        super().setUp()
        self.claude = ScriptedClaude(
            reply(text("Adding a click for the drummer."), call("tu_1", [
                {"action": "add_track", "name": "Click", "output": {"destination": "Ext. Out", "channel": "3/4"}},
            ])),
        )
        self.app = App(self.live, Conversation(client_factory=lambda: self.claude))
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.app))
        threading.Thread(target=self.http.serve_forever, daemon=True).start()

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        super().tearDown()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection(*self.http.server_address)
        headers = {"Content-Type": "application/json", **(headers or {})}
        conn.request(method, path, json.dumps(body) if body is not None else None, headers)
        response = conn.getresponse()
        data = response.read()
        conn.close()
        try:
            return response.status, json.loads(data)
        except ValueError:
            return response.status, data

    def test_chat_propose_apply(self):
        status, state = self.request("POST", "/api/chat", {"message": "Add a click"})
        self.assertEqual(status, 200)
        proposal = state["chat"][-1]["proposal"]
        self.assertEqual(proposal["steps"][0]["text"], "Add an audio track “Click” with no input going to outputs 3/4")
        self.assertTrue(proposal["exportable"])

        status, state = self.request("POST", f"/api/proposals/{proposal['id']}/apply", {})
        self.assertEqual(status, 200)
        applied = state["chat"][-1]["proposal"]
        self.assertEqual(applied["status"], "applied")
        self.assertTrue(applied["results"][0]["ok"])
        self.assertEqual(state["live"]["snapshot"]["tracks"][0]["name"], "Click")

        status, body = self.request("POST", f"/api/proposals/{proposal['id']}/apply", {})
        self.assertEqual(status, 400)
        self.assertIn("already", body["error"])

    def test_direct_mixer_commands_are_whitelisted(self):
        self.fake.create_audio_track("Vox")
        status, body = self.request("POST", "/api/live", {"cmd": "set_volume", "args": {"track_index": 0, "db": -6}})
        self.assertEqual(status, 200)
        self.assertEqual(body["live"]["snapshot"]["tracks"][0]["volume"], "-6.0 dB")
        status, body = self.request("POST", "/api/live", {"cmd": "delete_track", "args": {"track_index": 0}})
        self.assertEqual(status, 400)

    def test_live_errors_are_sentences(self):
        status, body = self.request("POST", "/api/live", {"cmd": "load_device", "args": {
            "track_index": 0, "device_name": "Reverb"}})
        self.assertEqual(status, 400)
        self.assertIn("doesn't exist", body["error"])

    def test_cross_site_posts_are_refused(self):
        status, _ = self.request("POST", "/api/reset", {}, {"Origin": "http://evil.example"})
        self.assertEqual(status, 403)
        status, _ = self.request("POST", "/api/reset", {}, {"Content-Type": "text/plain"})
        self.assertEqual(status, 415)

    def test_serves_the_page_and_state(self):
        status, page = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"Holy Sound", page)
        status, state = self.request("GET", "/api/state")
        self.assertTrue(state["live"]["connected"])
        status, _ = self.request("GET", "/../rig.py")
        self.assertEqual(status, 404)


def write_stem(folder, name, peak_db=-6.0, seconds=2.0, rate=8000, bits=24):
    """A WAV whose first half is silence and second half a sine at peak_db."""
    amp = 10 ** (peak_db / 20)
    n = int(rate * seconds)
    scale = 2 ** (bits - 1) - 1
    frames = b"".join(
        int(scale * amp * math.sin(2 * math.pi * 220 * i / rate) if i >= n // 2 else 0)
        .to_bytes(bits // 8, "little", signed=True)
        for i in range(n)
    )
    path = Path(folder) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(bits // 8)
        w.setframerate(rate)
        w.writeframes(frames)
    return path


class StemFolderCase(unittest.TestCase):
    """A folder of stems inside the home folder (the only place imports may look)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=Path.home())
        self.folder = Path(self._tmp.name) / "Sunday Stems"
        write_stem(self.folder / "Way Maker", "Click.wav", peak_db=-3)
        write_stem(self.folder / "Way Maker", "Pad.wav", peak_db=-20, bits=16)
        (self.folder / "Way Maker" / "Silence.wav").write_bytes(b"")
        with wave.open(str(self.folder / "Way Maker" / "Silence.wav"), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(8000)
            w.writeframes(b"\0\0" * 8000)

    def tearDown(self):
        self._tmp.cleanup()


class AudioFilesTest(StemFolderCase):
    def test_measures_peak_and_loud_parts(self):
        m = audio_files.measure(self.folder / "Way Maker" / "Click.wav")
        self.assertAlmostEqual(m["peak_dbfs"], -3.0, delta=0.1)
        self.assertAlmostEqual(m["loud_dbfs"], -6.0, delta=0.2)  # a sine's RMS is 3 dB under its peak
        self.assertAlmostEqual(m["sounding_pct"], 50, delta=10)  # measured in 0.4 s windows
        self.assertEqual(m["format"], "WAV 24-bit")

    def test_float_wav_and_aiff(self):
        rate, n = 8000, 8000
        samples = [0.5 * math.sin(2 * math.pi * 220 * i / rate) for i in range(n)]
        data = struct.pack(f"<{n}f", *samples)
        fmt = struct.pack("<HHIIHH", 3, 1, rate, rate * 4, 4, 32)
        wav = self.folder / "float.wav"
        wav.write_bytes(b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVE" + b"fmt "
                        + struct.pack("<I", 16) + fmt + b"data" + struct.pack("<I", len(data)) + data)
        self.assertAlmostEqual(audio_files.measure(wav)["peak_dbfs"], -6.0, delta=0.1)

        pcm = b"".join(int(32767 * v).to_bytes(2, "big", signed=True) for v in samples)
        rate80 = struct.pack(">H", 16383 + 12) + int(rate / 2 ** 12 * 2 ** 63).to_bytes(8, "big")
        comm = struct.pack(">hIh", 1, n, 16) + rate80
        ssnd = struct.pack(">II", 0, 0) + pcm
        body = b"AIFF" + b"COMM" + struct.pack(">I", len(comm)) + comm + b"SSND" + struct.pack(">I", len(ssnd)) + ssnd
        aiff = self.folder / "big-endian.aif"
        aiff.write_bytes(b"FORM" + struct.pack(">I", len(body)) + body)
        m = audio_files.measure(aiff)
        self.assertEqual(m["sample_rate"], 8000)
        self.assertAlmostEqual(m["peak_dbfs"], -6.0, delta=0.1)

    def test_scan_and_describe(self):
        folder, found = audio_files.scan(self.folder)
        ids = [fid for fid, _ in found]
        self.assertEqual(ids, ["Sunday Stems/Way Maker/Click.wav", "Sunday Stems/Way Maker/Pad.wav",
                               "Sunday Stems/Way Maker/Silence.wav"])
        lines = [audio_files.describe(fid, m) for (fid, _), m in zip(found, audio_files.measure_many([p for _, p in found]))]
        self.assertIn("SILENT", lines[2])
        self.assertIn("peak -20.0 dBFS", lines[1])

    def test_only_home_and_drives(self):
        with self.assertRaises(audio_files.AudioFileError):
            audio_files.browse("/etc")
        listing = audio_files.browse(str(self.folder))
        self.assertEqual([f["name"] for f in listing["folders"]], ["Way Maker"])
        self.assertEqual(listing["folders"][0]["audio"], 3)


class AudioActionsTest(StemFolderCase):
    def setUp(self):
        super().setUp()
        self.server, self.fake = fake_live.serve(port=0, latency=0)
        self.live = LiveLink(port=self.server.server_address[1])
        _, found = audio_files.scan(self.folder)
        self.files = dict(found)

    def tearDown(self):
        self.live.close()
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def run_actions(self, *actions):
        parsed = Proposal.model_validate({"actions": list(actions)}).actions
        with mock.patch("app.actions.time.sleep"):
            return run_all(self.live, parsed, self.files)

    def test_import_colour_gain_and_listen(self):
        results = self.run_actions(
            {"action": "add_song", "name": "Way Maker", "bpm": 68},
            {"action": "add_track", "name": "Click", "color": "grey",
             "output": {"destination": "Ext. Out", "channel": "3/4"}},
            {"action": "add_track", "name": "Pad", "color": "blue"},
            {"action": "import_audio", "track": "Click", "file": "Sunday Stems/Way Maker/Click.wav",
             "song": "Way Maker", "gain_db": -6},
            {"action": "import_audio", "track": "Pad", "file": "sunday stems/way maker/pad.wav", "song": "Way Maker"},
            {"action": "set_clip_gain", "track": "Pad", "song": "Way Maker", "db": 4},
            {"action": "set_color", "track": "Pad", "color": "teal"},
            {"action": "listen", "song": "Way Maker", "seconds": 5},
        )
        self.assertTrue(all(r["ok"] and not r["partial"] for r in results), results)
        snap = self.live.snapshot(max_age=0)
        click, pad = snap["tracks"]
        self.assertEqual(click["color"], 0x7A7A7A)
        self.assertEqual(pad["color"], 0x00BFAF)
        self.assertEqual(click["clips"][0]["name"], "Click")
        self.assertEqual(click["clips"][0]["gain"], "-6.0 dB")
        self.assertEqual(pad["clips"][0]["gain"], "4.0 dB")
        listen = results[-1]
        self.assertIn("Loudest: Click", listen["text"])
        self.assertIn("1. Click: peak", listen["detail"])
        self.assertFalse(snap["song"]["is_playing"])  # it stopped what it started

    def test_only_imported_files(self):
        results = self.run_actions(
            {"action": "add_track", "name": "Pad"},
            {"action": "import_audio", "track": "Pad", "file": "/etc/passwd", "song": "1"},
        )
        self.assertFalse(results[1]["ok"])
        self.assertIn("Import its folder first", results[1]["text"])

    def test_listen_needs_something_playing(self):
        results = self.run_actions({"action": "listen"})
        self.assertFalse(results[0]["ok"])
        self.assertIn("Nothing is playing", results[0]["text"])


class ImportServerTest(StemFolderCase):
    def setUp(self):
        super().setUp()
        self.server, self.fake = fake_live.serve(port=0, latency=0)
        self.live = LiveLink(port=self.server.server_address[1])
        self.claude = ScriptedClaude(
            reply(text("Here's your stems."), call("tu_1", [
                {"action": "add_song", "name": "Way Maker"},
                {"action": "add_track", "name": "Click", "color": "grey"},
                {"action": "import_audio", "track": "Click", "file": "Sunday Stems/Way Maker/Click.wav",
                 "song": "Way Maker"},
                {"action": "listen", "song": "Way Maker", "seconds": 3},
            ])),
            reply(text("The click is the loudest thing; that's fine for in-ears.")),
        )
        self.app = App(self.live, Conversation(client_factory=lambda: self.claude))

    def tearDown(self):
        self.live.close()
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def test_import_then_apply_then_automatic_follow_up(self):
        self.app.import_folder(str(self.folder), "Click goes to in-ears.")
        first = self.claude.requests[0]["messages"][-1]["content"][-1]["text"]
        self.assertIn('<imported_folder name="Sunday Stems" files="3">', first)
        self.assertIn('"Sunday Stems/Way Maker/Click.wav"', first)
        self.assertEqual(self.app.chat.transcript[0]["text"],
                         "Import the audio in “Sunday Stems” (3 files). Click goes to in-ears.")

        pid = self.app.chat.transcript[-1]["proposal_id"]
        with mock.patch("app.actions.time.sleep"):
            results = self.app.apply(pid)
        self.assertTrue(all(r["ok"] for r in results), results)

        follow = self.claude.requests[1]["messages"][-1]["content"]
        self.assertEqual(follow[0]["type"], "tool_result")
        self.assertIn("Meter readings", follow[0]["content"])
        self.assertIn("(Automatic:", follow[1]["text"])
        self.assertIn("Imported audio folders", follow[1]["text"])
        self.assertEqual(self.app.chat.transcript[-1]["text"], "The click is the loudest thing; that's fine for in-ears.")
        self.assertEqual([e["role"] for e in self.app.chat.transcript], ["user", "assistant", "assistant"])

    def test_empty_folder_is_a_sentence(self):
        empty = Path(self._tmp.name) / "Nothing"
        empty.mkdir()
        from app.server import UserError

        with self.assertRaises(UserError) as ctx:
            self.app.import_folder(str(empty))
        self.assertIn("no audio", str(ctx.exception))


class RoomMemoryTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "room.json"

    def tearDown(self):
        self._tmp.cleanup()

    def test_saves_survive_a_restart(self):
        room = RoomMemory(self.path)
        added = room.add(["Interface: Scarlett 18i20.", "  Sarah sings lead on input 1. ", "interface: scarlett 18i20."])
        self.assertEqual([f["text"] for f in added], ["Interface: Scarlett 18i20.", "Sarah sings lead on input 1."])
        again = RoomMemory(self.path)
        self.assertEqual([f["text"] for f in again.facts()], ["Interface: Scarlett 18i20.", "Sarah sings lead on input 1."])
        again.remove([1])
        self.assertEqual(again.add(["Drummer in-ears on outputs 3/4."])[0]["id"], 3)
        self.assertIn("#2. Sarah sings lead on input 1.", RoomMemory(self.path).notes())

    def test_remember_tool_then_answer_in_one_turn(self):
        room = RoomMemory(self.path)
        room.add(["Lead vocal is on input 2."])
        claude = ScriptedClaude(
            reply(text("Got it."), Block(type="tool_use", id="tu_mem", name="remember",
                                          input={"facts": ["Lead vocal (Sarah) is on input 1."], "forget": [1]})),
            reply(text("Sarah is on input 1 from now on."), call("tu_p", [
                {"action": "set_input", "track": "Lead Vocal", "input": "1"},
            ])),
        )
        chat = Conversation(client_factory=lambda: claude, room=room)
        chat.send("Sarah moved to input 1", session_notes(None, None, room=room))

        self.assertEqual([f["text"] for f in room.facts()], ["Lead vocal (Sarah) is on input 1."])
        roles = [e["role"] for e in chat.transcript]
        self.assertEqual(roles, ["user", "assistant", "note", "assistant"])
        self.assertIn("Remembered: Lead vocal (Sarah) is on input 1.", chat.transcript[2]["text"])
        self.assertIn("Forgot: Lead vocal is on input 2.", chat.transcript[2]["text"])
        result = claude.requests[1]["messages"][-1]["content"][0]
        self.assertEqual((result["type"], result["tool_use_id"]), ("tool_result", "tu_mem"))
        self.assertIn("#1. Lead vocal is on input 2.", claude.requests[0]["messages"][0]["content"][-1]["text"])
        self.assertEqual([t["name"] for t in claude.requests[0]["tools"]], ["propose_changes", "remember"])

    def test_giving_up_leaves_history_valid(self):
        bad = {"action": "set_volume", "track": "Vox", "db": 99}
        claude = ScriptedClaude(*[reply(call(f"tu_{i}", [bad])) for i in range(3)], reply(text("OK")))
        chat = Conversation(client_factory=lambda: claude, room=RoomMemory(self.path))
        chat.send("Make it loud", "notes")
        self.assertIn("couldn't work out", chat.transcript[-1]["text"])
        self.assertEqual(chat.messages[-1]["content"][0]["tool_use_id"], "tu_2")
        chat.send("Never mind", "notes")
        self.assertEqual([m["role"] for m in chat.messages][-2:], ["user", "assistant"])

    def test_room_endpoint(self):
        app = App(None, Conversation(client_factory=lambda: None, room=RoomMemory(self.path)))
        http = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
        threading.Thread(target=http.serve_forever, daemon=True).start()
        self.addCleanup(http.server_close)
        self.addCleanup(http.shutdown)

        def post(body):
            c = http_client.HTTPConnection(*http.server_address)
            c.request("POST", "/api/room", json.dumps(body), {"Content-Type": "application/json"})
            r = c.getresponse()
            data = json.loads(r.read())
            c.close()
            return data

        app.live_state = lambda: {"connected": False, "snapshot": None, "message": "closed"}
        state = post({"add": "Keys are stereo on 5/6."})
        self.assertEqual(state["room"][0]["text"], "Keys are stereo on 5/6.")
        state = post({"remove": state["room"][0]["id"]})
        self.assertEqual(state["room"], [])


class AddTrackWordingTest(unittest.TestCase):
    def test_describe(self):
        track = AddTrack(action="add_track", name="Keys", input="5/6", pan=-0.5,
                         devices=[{"device": "Reverb", "preset": "Large Hall"}])
        self.assertEqual(track.describe(), "Add an audio track “Keys” on input 5/6, with Reverb (Large Hall), panned 25L")


if __name__ == "__main__":
    unittest.main()
