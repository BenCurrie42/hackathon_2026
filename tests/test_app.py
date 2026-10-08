"""Holy Sound web app, end to end against the fake Live and a scripted Claude.

    uv run python -m unittest discover tests

Needs neither Ableton nor an API key. unittest rather than pytest because
pytest isn't an approved dependency yet (CLAUDE.md).
"""

import array
import copy
import os
import gzip
import http.client
import http.client as http_client
import io
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

from app import audio_files, eq, expert, fake_live, mcp, mixdown, parts, song_key, song_map, song_mixes, vendor_set
from app.actions import AddTrack, Proposal, run_all, to_rigspec
from app.assistant import AssistantUnavailable, Conversation, session_notes
from app.folders import FolderMemory, classify
from pydantic import ValidationError
from rig import TRACK_COLORS
from app.live import LiveLink, LiveUnavailable
from app.room import RoomMemory
from app import server as app_server
from app.server import App, make_handler
import rig
from live_control.starting_fader import starting_fader_db
from live_control.stem_level import StemLevel, pair_level
from live_control.stereo_pairs import stereo_pairs
from live_control.timecode import is_timecode


_home = None


def setUpModule():
    """Anything that remembers to ~/.holysound writes to a scratch folder instead."""
    global _home
    _home = tempfile.TemporaryDirectory()
    patcher = mock.patch.dict(os.environ, {"HOLYSOUND_HOME": _home.name})
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)
    unittest.addModuleCleanup(_home.cleanup)


class Block(SimpleNamespace):
    def model_dump(self, mode=None, exclude_none=False, **_):
        return {k: v for k, v in vars(self).items() if not (exclude_none and v is None)}


def text(t):
    return Block(type="text", text=t)


def call(call_id, actions):
    return Block(type="tool_use", id=call_id, name="propose_changes", input={"actions": actions})


def reply(*blocks, stop="end_turn"):
    return SimpleNamespace(stop_reason=stop, content=list(blocks))


def thinking(t):
    return Block(type="thinking", thinking=t, signature="sig")


class ScriptedStream:
    """Stands in for the SDK's MessageStream: replays a canned reply as stream events."""

    def __init__(self, message):
        self.message = message

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        for block in self.message.content:
            yield SimpleNamespace(type="content_block_start", content_block=block)
            if block.type == "thinking":
                yield SimpleNamespace(type="content_block_delta",
                                      delta=SimpleNamespace(type="thinking_delta", thinking=block.thinking))
            elif block.type == "text":
                yield SimpleNamespace(type="content_block_delta",
                                      delta=SimpleNamespace(type="text_delta", text=block.text))

    def get_final_message(self):
        return self.message


class ScriptedClaude:
    """Stands in for anthropic.Anthropic: streams canned replies, records requests."""

    def __init__(self, *script):
        self.script = list(script)
        self.requests = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))

    def _stream(self, **kwargs):
        self.requests.append(copy.deepcopy(kwargs))
        return ScriptedStream(self.script.pop(0))


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

    def test_steps_report_working_then_how_they_ended(self):
        seen = []
        actions = Proposal.model_validate({"actions": [
            {"action": "add_track", "name": "Click", "input": None},
            {"action": "set_volume", "track": "Nobody", "db": -3},
        ]}).actions
        run_all(self.live, actions, on_step=lambda n, state: seen.append((n, state)))
        self.assertEqual(seen, [(0, "working"), (0, "done"), (1, "working"), (1, "failed")])

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
        self.assertEqual(snap["scenes"][-1], {"index": 1, "name": "Way Maker", "tempo": 70.0, "transpose": 0})
        self.assertEqual(snap["song"]["tempo"], 70)

    def test_songs_go_in_a_chosen_slot_and_move_with_their_clips(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "Keys.wav"
            write_song_stem(path, 2, [(0, 2)])
            self.fake.create_audio_track("Keys")
            results = self.run_actions(
                {"action": "update_song", "song": "1", "new_name": "Opener"},
                {"action": "add_song", "name": "Closer"},
                {"action": "add_song", "name": "Way Maker", "bpm": 68, "position": 2},
            )
            self.assertTrue(all(r["ok"] for r in results), results)
            self.assertEqual(results[2]["text"], "Added song 2: Way Maker.")
            self.fake.import_audio(0, str(path), 1)  # Keys clip in Way Maker
            self.assertEqual([s["name"] for s in self.fake.list_scenes()], ["Opener", "Way Maker", "Closer"])

            results = self.run_actions({"action": "move_song", "song": "Way Maker", "position": 3})
            self.assertEqual(results[0]["text"], "Moved Way Maker to slot 3.")
            scenes = self.fake.list_scenes()
            self.assertEqual([s["name"] for s in scenes], ["Opener", "Closer", "Way Maker"])
            self.assertEqual(scenes[2]["tempo"], 68.0)
            self.assertEqual(list(self.fake.tracks[0]["clips"]), [2])  # the clip went with it

            self.run_actions({"action": "move_song", "song": "Way Maker", "position": 1})
            self.assertEqual([s["name"] for s in self.fake.list_scenes()], ["Way Maker", "Opener", "Closer"])
            self.assertEqual(list(self.fake.tracks[0]["clips"]), [0])

            results = self.run_actions({"action": "move_song", "song": "Closer", "position": 9})
            self.assertIn("only 3 song slots", results[0]["text"])

    def test_transpose_song_moves_every_audio_clip(self):
        with tempfile.TemporaryDirectory() as tmp:
            for i, name in enumerate(("Keys", "Bass")):
                path = Path(tmp) / f"{name}.wav"
                write_song_stem(path, 2, [(0, 2)])
                self.fake.create_audio_track(name)
                self.fake.import_audio(i, str(path), 0)
            self.fake.set_scene(0, name="Way Maker")
            results = self.run_actions({"action": "transpose_song", "song": "Way Maker", "semitones": -2})
            self.assertEqual(results[0]["text"], "Way Maker is down 2 semitones now: 2 clips.")
            snap = self.live.snapshot(max_age=0)
            self.assertEqual(snap["scenes"][0]["transpose"], -2)
            self.assertIn("1. Way Maker — transposed -2", session_notes(snap, None))
            self.fake.transpose_song(0, 0)
            self.fake.tracks[0]["clips"][0]["transpose"] = 3
            self.fake.tracks[1]["clips"][0]["transpose"] = -1
            self.assertIsNone(self.fake.list_scenes()[0]["transpose"])  # clips disagree

    def test_click_and_opted_out_tracks_keep_their_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            for i, name in enumerate(("Click", "Keys", "Loop Pad")):
                path = Path(tmp) / f"{name}.wav"
                write_song_stem(path, 2, [(0, 2)])
                self.fake.create_audio_track(name)
                self.fake.import_audio(i, str(path), 0)
            self.fake.set_scene(0, name="Way Maker")
            folders = FolderMemory(Path(tmp) / "folders.json")
            self.assertTrue(folders.keeps_key("Click"))
            folders.set_follows_key("Loop Pad", False)
            actions = Proposal.model_validate({"actions": [
                {"action": "transpose_song", "song": "Way Maker", "semitones": 3}]}).actions
            result = run_all(self.live, actions, folders=folders)[0]
            self.assertEqual(result["text"], "Way Maker is up 3 semitones now: 1 clip, 2 kept their key (like the click).")
            self.assertEqual([t["clips"][0]["transpose"] for t in self.fake.tracks], [0, 3, 0])
            self.assertEqual(self.fake.list_scenes()[0]["transpose"], 3)  # kept tracks don't make it "mixed"
            # Opting a track back in, then resetting, puts everything at the original key.
            folders.set_follows_key("Click", True)
            self.assertFalse(folders.keeps_key("Click"))
            folders.rename("Click", "Click Track")
            self.assertFalse(folders.keeps_key("Click Track"))
            self.assertEqual(FolderMemory(folders.path).keeps_key("Loop Pad"), True)
            result = run_all(self.live, Proposal.model_validate({"actions": [
                {"action": "transpose_song", "song": "Way Maker", "semitones": 0}]}).actions, folders=folders)[0]
            self.assertTrue(result["ok"], result)
            self.assertEqual([t["clips"][0]["transpose"] for t in self.fake.tracks], [0, 0, 0])

    def test_transpose_needs_audio_and_a_sane_range(self):
        results = self.run_actions({"action": "add_song", "name": "Empty"},
                                   {"action": "transpose_song", "song": "Empty", "semitones": 1})
        self.assertIn("has no audio clips", results[1]["text"])
        with self.assertRaises(ValidationError):
            Proposal.model_validate({"actions": [{"action": "transpose_song", "song": "1", "semitones": 13}]})

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
    def test_promising_a_change_without_proposing_it_is_sent_back_once(self):
        claude = ScriptedClaude(
            reply(text("Sending the BGVs 3 dB more into the reverb in Washed.")),
            reply(text("Here it is."), call("tu_1", [
                {"action": "set_send", "track": "BGVs", "to_return": "Reverb", "by_db": 3, "song": "Washed"},
            ])),
        )
        convo = Conversation(client_factory=lambda: claude)
        convo.send("More reverb on the BGVs in Washed.", "notes")
        [p] = convo.proposals.values()
        self.assertEqual(p["actions"][0].by_db, 3)
        nudge = convo.messages[2]["content"][0]["text"]
        self.assertIn("didn't call propose_changes", nudge)

    def test_a_silent_proposal_still_gets_a_sentence(self):
        claude = ScriptedClaude(
            reply(call("tu_1", [{"action": "set_eq", "track": "Acoustic", "flat_first": True,
                                 "bands": [{"band": 1, "type": "low cut", "freq_hz": 100}]}])),
            reply(call("tu_2", [{"action": "set_volume", "track": "Keys", "by_db": -3},
                                {"action": "set_volume", "track": "BGVs", "by_db": 2},
                                {"action": "set_mute", "track": "Keys", "on": True}])),
        )
        chat = Conversation(client_factory=lambda: claude)
        [_, said] = chat.send("Fix the acoustic EQ", "notes")
        self.assertEqual(said["text"], "Set the EQ on “Acoustic”. Press Apply to make the change.")
        [_, said] = chat.send("Keys down, BGVs up, then mute the keys", "notes")
        self.assertEqual(said["text"], "Here are 3 changes to Keys and BGVs. Press Apply to make them.")
        self.assertEqual(len(claude.requests), 2)  # no extra model call

    def test_a_question_back_is_not_a_promise(self):
        claude = ScriptedClaude(reply(text("I'll need to know: which output feeds the in-ears?")))
        convo = Conversation(client_factory=lambda: claude)
        entries = convo.send("Send the click to the in-ears.", "notes")
        self.assertEqual(entries[-1]["text"], "I'll need to know: which output feeds the in-ears?")
        self.assertEqual(len(claude.requests), 1)

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

    def test_reply_streams_to_the_feed_and_keeps_its_thinking(self):
        answer = reply(thinking("They haven't said which input."), text("Which input?"))
        answer.usage = SimpleNamespace(input_tokens=100, output_tokens=20,
                                       cache_read_input_tokens=1000, cache_creation_input_tokens=None)
        claude = ScriptedClaude(answer)
        chat = Conversation(client_factory=lambda: claude)
        start = chat.feed.cursor()
        chat.send("Set up a lead vocal", "notes")
        events = [(kind, data) for _seq, kind, data in chat.feed.since(start, 0)]
        self.assertEqual(events, [
            ("start", None), ("step", None),
            ("thinking", "They haven't said which input."), ("text", "Which input?"),
            ("usage", {"input": 1100, "output": 20}),
            ("end", None),
        ])
        self.assertEqual(chat.usage, {"input": 1100, "output": 20})
        self.assertEqual(chat.transcript[-1]["thinking"], "They haven't said which input.")
        self.assertEqual(claude.requests[0]["thinking"], {"type": "adaptive", "display": "summarized"})
        # The thinking block goes back unchanged, signature and all.
        self.assertEqual(chat.messages[-1]["content"][0]["signature"], "sig")
        # A page that connects after the turn doesn't replay it.
        self.assertEqual(chat.feed.since(chat.feed.cursor(), 0), [])
        chat.reset()
        self.assertEqual(chat.usage, {"input": 0, "output": 0})

    def test_failed_request_leaves_history_valid(self):
        def boom(**_):
            raise RuntimeError("network down")

        claude = ScriptedClaude(reply(text("Hello again.")))
        real = claude.beta.messages.stream
        chat = Conversation(client_factory=lambda: claude)
        claude.beta.messages.stream = boom
        with self.assertRaises(RuntimeError):
            chat.send("Hi", "notes")
        self.assertEqual(chat.messages, [])
        self.assertEqual(chat.transcript, [])
        claude.beta.messages.stream = real
        chat.send("Hi", "notes")
        self.assertEqual([m["role"] for m in chat.messages], ["user", "assistant"])

    def test_missing_key_is_explained(self):
        def no_key(**_):
            raise TypeError('"Could not resolve authentication method. Expected one of api_key..."')

        claude = ScriptedClaude()
        claude.beta.messages.stream = no_key
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
            self.assertEqual(fake.tracks[0]["input"], {"type": "No Input", "channel": ""})  # new tracks: no input
            fake.set_routing(0, "input", "Ext. In", "1")
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

    def test_session_notes_drop_a_picked_song_this_set_does_not_have(self):
        server, fake = fake_live.serve(port=0, latency=0)
        try:
            fake.set_scene(0, name="Way Maker")
            live = LiveLink(port=server.server_address[1])
            snap = live.snapshot()
            live.close()
        finally:
            server.shutdown()
            server.server_close()
        self.assertIn("Mixer is on: no song.", session_notes(snap, None, mix_song="Washed"))
        self.assertIn("Mixer is on: Way Maker.", session_notes(snap, None, mix_song="Way Maker"))

    def test_session_notes_show_pan_as_a_number_and_list_outputs(self):
        server, fake = fake_live.serve(port=0, latency=0)
        try:
            for name in ("Gtr L", "Keys R", "Bass"):
                fake.create_audio_track(name)
            fake.set_pan(0, -1)
            fake.set_pan(1, 0.5)
            live = LiveLink(port=server.server_address[1])
            notes = session_notes(live.snapshot(), live.stock_devices())
            live.close()
        finally:
            server.shutdown()
            server.server_close()
        self.assertIn("| pan -1 (50L)\n", notes)
        self.assertIn("| pan 0.5 (25R)\n", notes)
        self.assertIn("| 0.0 dB | pan 0 (C)\n", notes)
        self.assertIn("Outputs: Master; Ext. Out 1/2, 3/4, 5/6, 7/8, 1, 2,", notes)

    def test_session_notes_without_riglink_output_list(self):
        strip = {"name": "Click", "is_return": False, "input": None, "volume": "-12.0 dB", "mute": False,
                 "solo": False, "devices": [], "sends": [], "pan": "25L", "pan_value": None,
                 "output": {"type": "Ext. Out", "channel": "3/4"}}
        snapshot = {"song": {"tempo": 120, "numerator": 4, "denominator": 4, "is_playing": False},
                    "tracks": [strip | {"index": 0}], "returns": [], "scenes": []}
        notes = session_notes(snapshot, None)
        self.assertIn("| pan -0.5 (25L)\n", notes)
        self.assertIn("Outputs: Master; Ext. Out (in use: 3/4). Live didn't list every output", notes)


class ServerTest(FakeLiveCase):
    def setUp(self):
        super().setUp()
        self.claude = ScriptedClaude(
            reply(text("Adding a click for the drummer."), call("tu_1", [
                {"action": "add_track", "name": "Click", "output": {"destination": "Ext. Out", "channel": "3/4"}},
            ])),
        )
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        self.folders = FolderMemory(Path(self.home.name) / "folders.json")
        self.app = App(self.live, Conversation(client_factory=lambda: self.claude), folders=self.folders)
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.app))
        threading.Thread(target=self.http.serve_forever, daemon=True).start()

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        super().tearDown()

    def folder_of(self, state, name):
        return next(t["folder"] for t in state["live"]["snapshot"]["tracks"] if t["name"] == name)

    def test_mixer_groups_tracks_by_instrument_and_remembers_a_move(self):
        for name in ("Lead Vocal", "Kick", "Keys", "Click", "Acoustic DI", "Bass DI", "Mystery"):
            self.live.call("create_audio_track", name=name)
        status, state = self.request("GET", "/api/state")
        self.assertEqual([f["key"] for f in state["folders"]],
                         ["vocals", "instruments", "playback", "other"])
        self.assertEqual({n: self.folder_of(state, n) for n in
                          ("Lead Vocal", "Kick", "Keys", "Click", "Acoustic DI", "Bass DI", "Mystery")},
                         {"Lead Vocal": "vocals", "Kick": "instruments", "Keys": "instruments", "Click": "playback",
                          "Acoustic DI": "instruments", "Bass DI": "instruments", "Mystery": "other"})

        status, state = self.request("POST", "/api/track-folder", {"track": "Mystery", "folder": "vocals"})
        self.assertEqual(status, 200)
        self.assertEqual(self.folder_of(state, "Mystery"), "vocals")
        self.assertEqual(self.folder_of(self.request("GET", "/api/state")[1], "Mystery"), "vocals")
        # A new run of the app reads the same choice from disk.
        self.assertEqual(FolderMemory(self.folders.path).folder_for("Mystery"), "vocals")

        # Renaming over the direct mixer route keeps the track in its folder.
        index = next(i for i, t in enumerate(state["live"]["snapshot"]["tracks"]) if t["name"] == "Mystery")
        status, data = self.request("POST", "/api/live", {"cmd": "set_track_name",
                                    "args": {"track_index": index, "is_return": False, "name": "Pad"}})
        self.assertEqual(status, 200)
        self.assertEqual(self.folder_of({"live": data["live"]}, "Pad"), "vocals")

        status, state = self.request("POST", "/api/track-folder", {"track": "Pad", "folder": None})
        self.assertEqual(self.folder_of(state, "Pad"), "instruments")  # back to sorting by name

    def test_meter_feed_is_one_peak_per_channel_and_leaves_the_snapshot_cached(self):
        self.live.call("create_audio_track", name="Click")
        self.live.call("create_midi_track", name="Keys MIDI")
        self.live.snapshot()
        cached = self.live._snapshot_at
        status, data = self.request("GET", "/api/meters")
        self.assertEqual(status, 200)
        meters = data["meters"]
        self.assertEqual(len(meters["tracks"]), len(self.fake.tracks))
        self.assertIsNone(meters["tracks"][-1])  # a MIDI track with no instrument has no meter
        self.assertIsInstance(meters["master"], float)
        self.assertEqual(self.live._snapshot_at, cached)

    def test_meter_feed_goes_quiet_on_an_older_riglink(self):
        self.fake.get_live_meters = None  # handle() treats a missing command as unknown
        self.assertIsNone(self.request("GET", "/api/meters")[1]["meters"])
        self.assertFalse(self.live._has_live_meters_cmd)

    def test_assistant_moves_a_track_to_a_folder_and_colours_it_in_live(self):
        self.live.call("create_audio_track", name="Mystery")
        actions = Proposal.model_validate({"actions": [
            {"action": "move_to_folder", "track": "Mystery", "folder": "playback"},
            {"action": "rename_track", "track": "Mystery", "new_name": "Loop"},
        ]}).actions
        results = run_all(self.live, actions, folders=self.folders)
        self.assertTrue(all(r["ok"] and not r["partial"] for r in results), results)
        self.assertEqual(results[0]["text"], "Moved Mystery to Click & playback, teal in Ableton.")
        state = self.request("GET", "/api/state")[1]
        self.assertEqual(self.folder_of(state, "Loop"), "playback")  # kept through the rename
        loop = next(t for t in state["live"]["snapshot"]["tracks"] if t["name"] == "Loop")
        self.assertEqual(loop["color"], TRACK_COLORS["teal"])
        self.assertIn("Loop | folder playback | colour teal", self.app.notes())

    def test_move_to_folder_needs_a_mixer_and_a_real_folder(self):
        self.live.call("create_audio_track", name="Kick")
        move = Proposal.model_validate({"actions": [
            {"action": "move_to_folder", "track": "Kick", "folder": "vocals"}]}).actions
        self.assertIn("Folders aren't available", run_all(self.live, move)[0]["text"])
        with self.assertRaises(ValidationError):
            Proposal.model_validate({"actions": [{"action": "move_to_folder", "track": "Kick", "folder": "brass"}]})

    def test_a_folder_that_does_not_exist_is_refused(self):
        status, data = self.request("POST", "/api/track-folder", {"track": "Kick", "folder": "brass"})
        self.assertEqual(status, 400)
        self.assertIn("no folder", data["error"])
        status, _ = self.request("POST", "/api/track-folder", {"track": "", "folder": "vocals"})
        self.assertEqual(status, 400)

    def test_events_stream_the_reply_to_open_pages(self):
        conn = http.client.HTTPConnection(*self.http.server_address, timeout=5)
        conn.request("GET", "/api/events")
        stream = conn.getresponse()
        self.assertEqual(stream.getheader("Content-Type"), "text/event-stream")
        self.request("POST", "/api/chat", {"message": "Add a click"})
        kinds, texts = [], []
        while "end" not in kinds:
            line = stream.readline().decode().strip()
            if line.startswith("event: "):
                kinds.append(line[7:])
            elif line.startswith("data: ") and kinds[-1] == "text":
                texts.append(json.loads(line[6:]))
        conn.close()
        self.assertEqual(kinds, ["start", "step", "text", "tool", "usage", "end"])
        self.assertEqual(texts, ["Adding a click for the drummer."])

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
        self.assertEqual(proposal["steps"][0]["text"], "Add a track “Click” with no input going to outputs 3/4")
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

    def test_apply_reports_each_step_while_it_runs(self):
        status, state = self.request("POST", "/api/chat", {"message": "Add a click"})
        pid = state["chat"][-1]["proposal"]["id"]
        self.assertIsNone(state["applying"])
        seen = {}
        real = app_server.run_all

        def spy(live, actions, files=None, on_step=None, **kwargs):
            on_step(0, "working")
            seen["during"] = self.app.applying()
            return real(live, actions, files, on_step=on_step, **kwargs)

        with mock.patch.object(app_server, "run_all", spy):
            status, state = self.request("POST", f"/api/proposals/{pid}/apply", {})
        self.assertEqual(seen["during"], {"id": pid, "states": ["working"]})
        self.assertIsNone(state["applying"])  # and nothing is left over afterwards

    def test_tracks_the_assistant_changes_are_lit_for_a_few_seconds(self):
        now = [100.0]
        self.app.clock = lambda: now[0]
        status, state = self.request("POST", "/api/chat", {"message": "Add a click"})
        self.assertEqual(state["activity"], [])  # a suggestion nobody applied touches nothing

        pid = state["chat"][-1]["proposal"]["id"]
        status, state = self.request("POST", f"/api/proposals/{pid}/apply", {})
        self.assertEqual([a["track"] for a in state["activity"]], ["Click"])
        self.assertGreater(state["activity"][0]["ms"], 2000)

        now[0] += 1.0
        status, state = self.request("GET", "/api/state")
        self.assertEqual([a["track"] for a in state["activity"]], ["Click"])
        self.assertLess(state["activity"][0]["ms"], 2100)

        now[0] += 10.0
        status, state = self.request("GET", "/api/state")
        self.assertEqual(state["activity"], [])

        # Moving a fader by hand isn't the assistant, so it doesn't light anything.
        self.request("POST", "/api/live", {"cmd": "set_mute", "args": {"track_index": 0, "is_return": False, "on": True}})
        self.assertEqual(self.request("GET", "/api/state")[1]["activity"], [])

    def test_the_page_is_told_when_live_is_the_pretend_one(self):
        self.assertIs(self.request("GET", "/api/state")[1]["demo"], False)
        self.app.demo = True
        self.assertIs(self.request("GET", "/api/state")[1]["demo"], True)

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

    def test_a_bug_still_answers_in_a_sentence(self):
        with mock.patch.object(self.app.chat, "reset", side_effect=KeyError("boom")), \
                mock.patch("traceback.print_exc"):
            status, body = self.request("POST", "/api/reset", {})
        self.assertEqual(status, 500)
        self.assertIn("Something went wrong", body["error"])


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


def write_song_stem(path, seconds, sounding=(), clicks_per_second=0, rate=8000):
    """A mono 16-bit stem: a tone during each (start, end) in sounding, else silence."""
    frames = array.array("h", bytes(2 * seconds * rate))
    for start, end in sounding:
        for i in range(int(start * rate), int(end * rate)):
            frames[i] = int(8000 * math.sin(2 * math.pi * 220 * i / rate))
    if clicks_per_second:
        for n in range(int(seconds * clicks_per_second)):
            first = int(n * rate / clicks_per_second)
            for i in range(first, first + rate // 100):
                frames[i] = 20000
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(frames.tobytes())


class ListenToStemsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        song = Path(self._tmp.name) / "Way Maker"
        write_song_stem(song / "Click.wav", 60, clicks_per_second=2)  # 120 BPM
        write_song_stem(song / "Keys.wav", 60, [(0, 60)])
        write_song_stem(song / "Lead Vox.wav", 60, [(8, 56)])
        write_song_stem(song / "BGV.wav", 60, [(20, 32), (40, 52)])
        self.files = {f"Sunday Stems/Way Maker/{p.name}": str(p) for p in song.iterdir()}

    def tearDown(self):
        self._tmp.cleanup()

    def test_report_names_parts_sections_tempo_and_lead(self):
        report = song_map.listen(sorted((Path(k).stem, v) for k, v in self.files.items()))
        self.assertIn("Tempo from the click: about 120 BPM.", report)
        self.assertIn("  Lead Vox: 0:08-0:56", report)
        self.assertIn("  BGV: 0:20-0:32, 0:40-0:52", report)
        self.assertIn("0:20-0:32  in: BGV", report)
        self.assertIn("Probably the lead vocal: Lead Vox", report)
        self.assertNotIn("in: Click", report)  # the click isn't part of the arrangement

    def test_tool_runs_inside_the_turn_with_a_chat_note(self):
        claude = ScriptedClaude(
            reply(Block(type="tool_use", id="tu_1", name="listen_to_stems",
                        input={"files": ["Sunday Stems/Way Maker"]}), stop="tool_use"),
            reply(text("Lead Vox is the lead; the BGVs come in on the choruses.")),
        )
        chat = Conversation(client_factory=lambda: claude)
        chat.files.update(self.files)
        shown = chat.send("Which one is the lead?", "notes")
        self.assertEqual([e["role"] for e in shown], ["user", "heard", "assistant"])
        self.assertEqual(shown[1]["text"], "Listened to 4 stems")
        result = claude.requests[1]["messages"][-1]["content"][0]
        self.assertEqual(result["tool_use_id"], "tu_1")
        self.assertIn("Probably the lead vocal: Lead Vox", result["content"])

    def test_listens_to_a_song_already_in_the_set(self):
        server, fake = fake_live.serve(port=0, latency=0)
        live = LiveLink(port=server.server_address[1])
        try:
            fake.set_scene(0, name="Way Maker")
            for i, (fid, path) in enumerate(sorted(self.files.items())):
                fake.create_audio_track(Path(fid).stem + " Track")
                fake.import_audio(i, path, 0)
            app = App(live, Conversation(client_factory=lambda: None))
            outcome, note = app.chat._listen({"song": "way maker"})
            self.assertEqual(note, "Listened to 4 stems")
            self.assertIn("Probably the lead vocal: Lead Vox Track", outcome)
            outcome, note = app.chat._listen({"song": "Amazing Grace"})
            self.assertIn("There's no song called Amazing Grace", outcome)
            self.assertIsNone(note)
        finally:
            live.close()
            server.shutdown()
            server.server_close()

    def test_imports_are_remembered_between_runs(self):
        saved = Path(self._tmp.name) / "imports.json"
        first = App(mock.Mock(), Conversation(client_factory=lambda: None), imports_path=saved)
        first.files.update(self.files)
        first.imports["Sunday Stems"] = 4
        first._save_imports()
        again = App(mock.Mock(), Conversation(client_factory=lambda: None), imports_path=saved)
        self.assertEqual(again.files, self.files)
        self.assertEqual(again.imports, {"Sunday Stems": 4})
        outcome, _note = again.chat._listen({"files": ["Sunday Stems/Way Maker"]})
        self.assertIn("Probably the lead vocal: Lead Vox", outcome)

    def test_only_imported_files_can_be_heard(self):
        chat = Conversation(client_factory=lambda: None)
        outcome, note = chat._listen({"files": ["/etc/passwd"]})
        self.assertIn("No imported files match", outcome)
        self.assertIsNone(note)


def write_chords(path, chords, seconds_each=2.0, rate=8000):
    """A mono stem playing each chord (a list of MIDI notes) in turn."""
    frames = array.array("h")
    for notes in chords:
        freqs = [440 * 2 ** ((n - 69) / 12) for n in notes]
        for i in range(int(seconds_each * rate)):
            t = i / rate
            frames.append(int(6000 * sum(math.sin(2 * math.pi * f * t) for f in freqs) / len(freqs)))
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(frames.tobytes())


# I-V-vi-IV, the worship progression, as triads plus a bass note.
E_MAJOR = [[52, 56, 59, 64], [47, 54, 59, 63], [49, 56, 61, 64], [45, 57, 61, 64]] * 4
D_MAJOR = [[50, 54, 57, 62], [45, 57, 61, 64], [47, 54, 59, 62], [43, 55, 59, 62]] * 4


class SongKeyTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_finds_the_key_from_pitched_stems_and_skips_the_click(self):
        write_chords(self.root / "Keys.wav", E_MAJOR)
        write_song_stem(self.root / "Click.wav", 32, clicks_per_second=2)
        guess = song_key.detect([("Keys", str(self.root / "Keys.wav")), ("Click", str(self.root / "Click.wav"))])
        self.assertEqual(guess["key"], "E major")
        self.assertEqual(guess["stems"], ["Keys"])
        self.assertIn("probably E major", song_key.describe(guess))

    def test_another_key(self):
        write_chords(self.root / "Pad.wav", D_MAJOR)
        self.assertEqual(song_key.detect([("Pad", str(self.root / "Pad.wav"))])["key"], "D major")

    def test_nothing_pitched_is_said_plainly(self):
        write_song_stem(self.root / "Click.wav", 8, clicks_per_second=2)
        self.assertIsNone(song_key.detect([("Click", str(self.root / "Click.wav"))]))
        self.assertIn("couldn't tell", song_key.describe(None))

    def test_import_lists_a_key_per_song(self):
        write_chords(self.root / "Way Maker" / "Keys.wav", E_MAJOR)
        write_chords(self.root / "Goodness" / "Bass.wav", D_MAJOR)
        found = [("Stems/Way Maker/Keys.wav", str(self.root / "Way Maker" / "Keys.wav")),
                 ("Stems/Goodness/Bass.wav", str(self.root / "Goodness" / "Bass.wav"))]
        lines = App.song_keys(found)
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].startswith('- "Stems/Goodness": Key: probably D major'), lines)
        self.assertTrue(lines[1].startswith('- "Stems/Way Maker": Key: probably E major'), lines)


def write_vendor_set(path, stems, tempo=139, sections=(("Intro", 0), ("Verse 1", 16)), live8=True):
    """A minimal vendor set: one AudioTrack per (track name, file under path's folder)."""
    tracks = []
    for name, rel in stems:
        rel = Path(rel)
        if live8:
            ref = ("<RelativePath>" + "".join(f'<RelativePathElement Dir="{d}"/>' for d in rel.parent.parts)
                   + f'</RelativePath><Name Value="{rel.name}"/>')
        else:
            ref = f'<RelativePath Value="{rel.as_posix()}"/><Path Value="/nowhere/{rel.name}"/>'
        tracks.append(
            f'<AudioTrack Id="{len(tracks)}"><Name><EffectiveName Value="{name}"/></Name><DeviceChain><MainSequencer>'
            f'<Sample><ArrangerAutomation><Events><AudioClip Time="0"><IsWarped Value="false"/>'
            f'<PitchCoarse Value="0"/><SampleRef><FileRef>{ref}</FileRef></SampleRef></AudioClip>'
            f'</Events></ArrangerAutomation></Sample></MainSequencer></DeviceChain></AudioTrack>')
    tempo_xml = (f'<ArrangerAutomation><Events><FloatEvent Time="-63072000" Value="{tempo}"/></Events></ArrangerAutomation>'
                 if live8 else f'<Manual Value="{tempo}"/>')
    locators = "".join(f'<Locator><Time Value="{t}"/><Name Value="{n}"/></Locator>' for n, t in sections)
    xml = (f'<?xml version="1.0" encoding="UTF-8"?><Ableton Creator="Ableton Live {"8.4.2" if live8 else "12.4.5"}">'
           f'<LiveSet><Tracks>{"".join(tracks)}</Tracks><MasterTrack><MasterChain><Mixer><Tempo>{tempo_xml}</Tempo>'
           f'</Mixer></MasterChain></MasterTrack><Locators><Locators>{locators}</Locators></Locators></LiveSet></Ableton>')
    path.write_bytes(gzip.compress(xml.encode()))


class VendorSetTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=Path.home())  # imports only look under home
        self.addCleanup(self._tmp.cleanup)
        self.song = Path(self._tmp.name) / "WASHED-WASHED-B-139.00bpm"
        for name in ("Click Track", "Keys 1"):
            write_song_stem(self.song / "MultiTracks" / f"{name}.wav", 2, [(0, 2)])
        self.als = self.song / "MultiTrack.als"
        write_vendor_set(self.als, [("Click Track", "MultiTracks/Click Track.wav"), ("Keys 1", "MultiTracks/Keys 1.wav")])

    def test_finds_the_set_from_the_song_folder_or_its_stems_folder(self):
        self.assertEqual(vendor_set.find(self.song), self.als)
        self.assertEqual(vendor_set.find(self.song / "MultiTracks"), self.als)
        self.assertIsNone(vendor_set.find(Path(self._tmp.name) / "elsewhere"))

    def test_reads_tempo_sections_and_stems_without_changing_the_file(self):
        before = self.als.read_bytes()
        for live8 in (True, False):
            write_vendor_set(self.als, [("Click Track", "MultiTracks/Click Track.wav"),
                                        ("Keys 1", "MultiTracks/Keys 1.wav")], live8=live8)
            info = vendor_set.read(self.als)
            self.assertEqual(info["tempo"], 139)
            self.assertEqual(info["sections"], [("Intro", 0.0), ("Verse 1", 16.0)])
            self.assertEqual([t["name"] for t in info["tracks"]], ["Click Track", "Keys 1"])
            self.assertEqual(info["tracks"][1]["file"], (self.song / "MultiTracks" / "Keys 1.wav").resolve())
            self.assertEqual(info["layout_notes"], [])
        write_vendor_set(self.als, [("Click Track", "MultiTracks/Click Track.wav"), ("Keys 1", "MultiTracks/Keys 1.wav")])
        self.assertEqual(self.als.read_bytes(), before)  # reading never writes

    def test_import_tells_the_assistant_about_the_set(self):
        folder, found = audio_files.scan(self.song)
        text = App.vendor_song(folder, found)
        self.assertIn('<vendor_set file="MultiTrack.als" made_with="Ableton Live 8.4.2">', text)
        self.assertIn("Tempo 139 BPM.", text)
        self.assertIn("Sections: Intro 0:00, Verse 1 0:07", text)
        self.assertIn('- "Keys 1": "WASHED-WASHED-B-139.00bpm/MultiTracks/Keys 1.wav"', text)

    def test_a_set_for_other_stems_is_ignored_and_a_broken_one_is_a_sentence(self):
        write_vendor_set(self.als, [("Other", "Elsewhere/Other.wav")])
        folder, found = audio_files.scan(self.song)
        self.assertIsNone(App.vendor_song(folder, found))
        self.als.write_bytes(b"not a set")
        self.assertIn("isn't an Ableton set Holy Sound can read", App.vendor_song(folder, found))


class PartsTest(unittest.TestCase):
    def test_vendor_names_land_on_the_same_parts(self):
        washed = parts.plan(["W/AG 1.wav", "W/AG 2.wav", "W/EG 10.wav", "W/Keys 3.wav", "W/Synth Bass 2.wav",
                             "W/Click Track.wav", "W/Drums (Live).wav", "W/BGVS.wav", "W/Sax.wav"])
        church = parts.plan(["C/ACC 1.wav", "C/GTR 1 L.wav", "C/GTR 1 R.wav", "C/Keys 1 L.wav", "C/Kick In.wav",
                             "C/OH L.wav", "C/Click.wav", "C/Count.wav", "C/SMPTE.wav", "C/Chloe Gall.wav"])
        self.assertEqual(list(washed), ["Click", "Drums", "Synth Bass", "Acoustic", "Electric", "Keys", "Horns", "BGVs"])
        self.assertEqual(list(church), ["Click", "Guide", "SMPTE", "Drums", "Acoustic", "Electric", "Keys", "Chloe Gall"])
        self.assertEqual(church["Electric"], ["C/GTR 1 L.wav", "C/GTR 1 R.wav"])
        self.assertEqual(parts.pans(church["Electric"]), {"C/GTR 1 L.wav": -1, "C/GTR 1 R.wav": 1})
        self.assertIn("Chloe Gall (fits no part", parts.describe("C", church))

    def test_mixdown_keeps_balance_and_sides_and_reports_its_scaling(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            write_stem(tmp, "L.wav", peak_db=-12)
            write_stem(tmp, "R.wav", peak_db=-12)
            write_stem(tmp, "Mono.wav", peak_db=-6)
            r = mixdown.mix([(tmp / "L.wav", 0, -1), (tmp / "R.wav", 0, 1), (tmp / "Mono.wav", 0, 0)], tmp / "out.wav")
            self.assertEqual(r["channels"], 2)
            m = audio_files.measure(tmp / "out.wav")
            self.assertEqual((m["channels"], m["format"]), (2, "WAV 24-bit"))
            self.assertAlmostEqual(m["peak_dbfs"], -1.0, places=1)
            # The sum peaked above -6 dBFS (mono plus a side); scaling it to -1 means turning it up < 5 dB.
            self.assertLess(r["gain_db"], 5)
            self.assertGreater(r["gain_db"], 0)


class TrackLayoutGuardTest(unittest.TestCase):
    """The rules that keep a set from sprawling back to a track per stem. See CLAUDE.md."""

    def test_the_assistant_cannot_import_onto_a_track_per_stem(self):
        with self.assertRaises(ValidationError):  # the old one-file-per-track import is gone
            Proposal.model_validate({"actions": [
                {"action": "import_audio", "track": "Kick In", "file": "S/Kick In.wav", "song": "1"}]})
        names = {s["properties"]["action"]["const"] for s in Proposal.model_json_schema()["$defs"].values()
                 if "action" in s.get("properties", {})}
        self.assertIn("import_part", names)
        self.assertNotIn("import_audio", names)

    def test_the_prompt_keeps_the_part_and_per_song_mix_rules(self):
        from app.assistant import SYSTEM
        self.assertIn("Never make a track per stem", SYSTEM)
        self.assertIn("leave a part out of one song by muting it", SYSTEM)

    def test_new_tracks_start_with_no_input(self):
        fake = fake_live.FakeSet()
        fake.create_audio_track("Click")
        self.assertEqual(fake.tracks[-1]["input"], {"type": "No Input", "channel": ""})


class StartingFaderTest(unittest.TestCase):
    def test_more_stems_start_lower(self):
        self.assertEqual(starting_fader_db(1), -3.0)
        self.assertEqual(starting_fader_db(2), -6.0)
        self.assertEqual(starting_fader_db(8), -12.0)
        self.assertEqual(starting_fader_db(50), -20.0)
        self.assertEqual(starting_fader_db(0), 0.0)


class TimecodeTest(unittest.TestCase):
    def test_spots_timecode_names(self):
        for name in ("SMPTE", "smpte 30fps", "LTC", "Timecode", "Time Code"):
            self.assertTrue(is_timecode(name), name)
        for name in ("Click", "Count", "Keys 1 L", "Ltd Pad", "Bass"):
            self.assertFalse(is_timecode(name), name)


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

    def test_several_stems_become_one_part_track_shared_by_songs(self):
        for name, db in (("Kick In.wav", -10), ("Snare Top.wav", -14), ("OH L.wav", -20), ("OH R.wav", -20)):
            write_stem(self.folder / "Way Maker", name, peak_db=db)
        _, found = audio_files.scan(self.folder)
        self.files = dict(found)
        drums = [f"Sunday Stems/Way Maker/{n}" for n in ("Kick In.wav", "Snare Top.wav", "OH L.wav", "OH R.wav")]
        with tempfile.TemporaryDirectory() as out:
            parsed = Proposal.model_validate({"actions": [
                {"action": "add_song", "name": "Way Maker"},
                {"action": "add_song", "name": "Goodness"},
                {"action": "import_part", "part": "Drums", "files": drums, "song": "Way Maker"},
                {"action": "import_part", "part": "drums", "files": drums[:1], "song": "Goodness"},
            ]}).actions
            results = run_all(self.live, parsed, self.files, parts_dir=out)
            self.assertTrue(all(r["ok"] and not r["partial"] for r in results), results)
            self.assertEqual(results[2]["text"], "Put 4 stems mixed on Drums (new track) in Way Maker.")
            self.assertEqual(results[3]["text"], "Put Kick In on Drums in Goodness.")
            snap = self.live.snapshot(max_age=0)
            self.assertEqual([t["name"] for t in snap["tracks"]], ["Drums"])  # one track, not one per stem
            drums_track = snap["tracks"][0]
            self.assertEqual(drums_track["input"], {"type": "No Input", "channel": ""})
            self.assertEqual(drums_track["color"], TRACK_COLORS["blue"])  # the Instruments folder colour
            mixed = Path(out) / "Way Maker" / "Drums.wav"
            self.assertTrue(mixed.is_file())
            self.assertEqual(audio_files.measure(mixed)["channels"], 2)  # OH L/R kept their sides
            way_maker = next(c for c in drums_track["clips"] if c["scene_index"] == 1)
            # The stems sum to about -4.2 dBFS; the mix is written peaking at -1, so its clip gain
            # takes those 3.2 dB back off and the part plays exactly as loud as the stems together.
            self.assertAlmostEqual(way_maker["gain_db"], -3.2, delta=0.2)

    def test_tidy_rebuilds_songs_on_part_tracks_and_keeps_their_sound(self):
        song = self.folder / "Way Maker"
        for name, db in (("Kick In.wav", -10), ("OH L.wav", -20), ("OH R.wav", -20), ("Sax.wav", -12)):
            write_stem(song, name, peak_db=db)
        f = lambda n: str(song / n)
        self.fake.set_scene(0, name="Way Maker")
        self.fake.create_scene("Goodness", 70)
        layout = [("Click", ["Click.wav", "Click.wav"]), ("Kick In", ["Kick In.wav", None]),
                  ("OH L", ["OH L.wav", "OH L.wav"]), ("OH R", ["OH R.wav", "OH R.wav"]),
                  ("Sax", ["Sax.wav", None]), ("Pad", ["Pad.wav", "Pad.wav"])]
        for i, (track, clips) in enumerate(layout):
            self.fake.create_audio_track(track)
            for scene, file in enumerate(clips):
                if file:
                    self.fake.import_audio(i, f(file), scene)
        self.fake.set_volume(1, -6.0)                      # Kick In's fader: baked into the mix
        self.fake.set_clip_gain(2, 0, 3.0)                 # OH L clip gain in Way Maker
        self.fake.set_mute(4, True)                        # Sax muted everywhere: left out
        self.fake.set_routing(0, "output", "Ext. Out", "3/4")  # click to the in-ears
        self.fake.transpose_song(1, 2)                     # Goodness is up a step
        with tempfile.TemporaryDirectory() as out:
            parsed = Proposal.model_validate({"actions": [{"action": "tidy_into_parts", "assign": [
                {"song": "Goodness", "track": "Pad", "part": "Keys"}]}]}).actions
            [result] = run_all(self.live, parsed, self.files, parts_dir=out,
                               folders=FolderMemory(Path(out) / "folders.json"))
            self.assertTrue(result["ok"], result)
            self.assertIn("Rebuilt 2 songs on part tracks: 7 part clips, 5 stem tracks removed.", result["text"])
            self.assertIn("But in Way Maker, left Sax out of Horns", result["text"])
            tracks = self.live.snapshot(max_age=0)["tracks"]
            # Pad is Synths by name, but Keys in Goodness, where it was assigned.
            self.assertEqual([t["name"] for t in tracks], ["Click", "Drums", "Horns", "Synths", "Keys"])
            click, drums, horns, synths, keys = tracks
            self.assertEqual(([c["scene_index"] for c in synths["clips"]], [c["scene_index"] for c in keys["clips"]]),
                             ([0], [1]))
            self.assertEqual(click["output"], {"type": "Ext. Out", "channel": "3/4"})
            self.assertEqual([t["volume_db"] for t in tracks], [0.0] * 5)
            self.assertEqual(sorted(c["scene_index"] for c in drums["clips"]), [0, 1])
            self.assertTrue((Path(out) / "Way Maker" / "Drums.wav").is_file())
            self.assertFalse(next(c for c in horns["clips"])["active"])  # a muted stem stays silent
            scenes = self.fake.list_scenes()
            self.assertEqual(scenes[1]["transpose"], 2)                      # transpose put back
            self.assertEqual(self.fake.tracks[0]["clips"][1]["transpose"], 0)  # the click keeps its key

    def test_import_colour_gain_and_listen(self):
        results = self.run_actions(
            {"action": "add_song", "name": "Way Maker", "bpm": 68},
            {"action": "add_track", "name": "Click", "color": "grey",
             "output": {"destination": "Ext. Out", "channel": "3/4"}},
            {"action": "add_track", "name": "Pad", "color": "blue"},
            {"action": "import_part", "part": "Click", "files": ["Sunday Stems/Way Maker/Click.wav"],
             "song": "Way Maker", "gain_db": -6},
            {"action": "import_part", "part": "Pad", "files": ["sunday stems/way maker/pad.wav"], "song": "Way Maker"},
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

    def test_new_tracks_start_with_room_for_every_stem(self):
        results = self.run_actions(
            {"action": "add_song", "name": "Way Maker"},
            {"action": "add_track", "name": "Click"},
            {"action": "add_track", "name": "Pad", "volume_db": -4},
            {"action": "import_part", "part": "Click", "files": ["Sunday Stems/Way Maker/Click.wav"],
             "song": "Way Maker"},
            {"action": "import_part", "part": "Pad", "files": ["Sunday Stems/Way Maker/Pad.wav"],
             "song": "Way Maker"},
        )
        self.assertTrue(all(r["ok"] and not r["partial"] for r in results), results)
        self.assertIn("fader at 68%", results[1]["text"])
        self.assertEqual(self.live.call("get_mixer", track_index=0)["volume"], "-6.0 dB")
        self.assertEqual(self.live.call("get_mixer", track_index=1)["volume"], "-4.0 dB")  # its own choice

    def test_timecode_track_starts_muted(self):
        results = self.run_actions(
            {"action": "add_song", "name": "Way Maker"},
            {"action": "add_track", "name": "SMPTE"},
            {"action": "import_part", "part": "SMPTE", "files": ["Sunday Stems/Way Maker/Click.wav"],
             "song": "Way Maker"},
        )
        self.assertTrue(all(r["ok"] for r in results), results)
        self.assertIn("muted because it's timecode", results[1]["text"])
        self.assertTrue(self.live.call("get_mixer", track_index=0)["mute"])

    def test_only_imported_files(self):
        results = self.run_actions(
            {"action": "add_track", "name": "Pad"},
            {"action": "import_part", "part": "Pad", "files": ["/etc/passwd"], "song": "1"},
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
                {"action": "import_part", "part": "Click", "files": ["Sunday Stems/Way Maker/Click.wav"],
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

    def test_an_import_is_saved_for_next_time(self):
        saved = Path(self.folder).parent / "imports.json"
        app = App(self.live, Conversation(client_factory=lambda: self.claude), imports_path=saved)
        app.import_folder(str(self.folder))
        again = App(self.live, Conversation(client_factory=lambda: None), imports_path=saved)
        self.assertIn("Sunday Stems/Way Maker/Click.wav", again.files)
        self.assertIsInstance(again.files["Sunday Stems/Way Maker/Click.wav"], str)

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


class FolderTest(unittest.TestCase):
    def test_names_sort_into_instrument_folders(self):
        cases = {
            "Lead Vocal": "vocals", "BGV 2": "vocals", "Choir": "vocals", "Pastor Mic": "vocals",
            "Kick": "instruments", "Snare Top": "instruments", "OH L": "instruments", "Cajon": "instruments",
            "Bass": "instruments", "Bass DI": "instruments",
            "Acoustic Guitar": "instruments", "EG 1": "instruments",
            "Keys": "instruments", "Piano": "instruments", "Pad": "instruments", "Synth Lead": "instruments",
            "Click": "playback", "Guide": "playback", "Backing Track": "playback", "Loops": "playback",
            "Kick Drum Bass": "instruments", "Something Else": "other", "": "other",
        }
        for name, folder in cases.items():
            self.assertEqual(classify(name), folder, name)

    def test_a_move_beats_the_name_and_none_goes_back(self):
        with tempfile.TemporaryDirectory() as home:
            memory = FolderMemory(Path(home) / "folders.json")
            self.assertEqual(memory.folder_for("Kick"), "instruments")
            memory.move("kick", "other")
            self.assertEqual(memory.folder_for("Kick"), "other")  # not case sensitive
            memory.rename("Kick", "Kick In")
            self.assertEqual(memory.folder_for("Kick In"), "other")
            self.assertEqual(memory.folder_for("Kick"), "instruments")
            memory.move("Kick In", None)
            self.assertEqual(memory.folder_for("Kick In"), "instruments")
            with self.assertRaises(ValueError):
                memory.move("Kick", "brass")

    def test_a_broken_file_is_ignored(self):
        with tempfile.TemporaryDirectory() as home:
            path = Path(home) / "folders.json"
            path.write_text("{not json")
            self.assertEqual(FolderMemory(path).folder_for("Keys"), "instruments")
            path.write_text(json.dumps({"moved": {"keys": "brass"}}))
            self.assertEqual(FolderMemory(path).folder_for("Keys"), "instruments")


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
        self.assertEqual([t["name"] for t in claude.requests[0]["tools"]], ["propose_changes", "listen_to_stems", "remember"])

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


class ProposalLimitTest(unittest.TestCase):
    def test_a_big_import_fits_in_one_proposal(self):
        step = {"action": "set_tempo", "bpm": 120}
        self.assertEqual(len(Proposal.model_validate({"actions": [step] * 150}).actions), 150)
        with self.assertRaises(ValueError):
            Proposal.model_validate({"actions": [step] * 151})


class StereoPairsTest(unittest.TestCase):
    def test_pairs_only_exact_base_names(self):
        names = ["GTR 1 L", "GTR 1 R", "Crowds 4 Left", "Crowds 4 Right", "Keys_L", "Keys_R",
                 "Loops Synths L", "Loops Synth R", "Vocal", "Lead L", "Bass"]
        self.assertEqual(
            sorted(stereo_pairs(names)),
            [("Crowds 4 Left", "Crowds 4 Right"), ("GTR 1 L", "GTR 1 R"), ("Keys_L", "Keys_R")],
        )

    def test_ambiguous_sides_dont_pair(self):
        self.assertEqual(stereo_pairs(["Pad L", "Pad Left", "Pad R"]), [])

    def test_pair_level_averages_power_and_keeps_the_higher_peak(self):
        level = pair_level(StemLevel(-10.0, -2.0), StemLevel(-10.0, -6.0))
        self.assertAlmostEqual(level.active_rms_db, -10.0)
        self.assertEqual(level.peak_db, -2.0)
        self.assertEqual(pair_level(None, StemLevel(-12.0, -3.0)), StemLevel(-12.0, -3.0))

    def test_song_import_gives_a_pair_one_gain(self):
        with tempfile.TemporaryDirectory() as tmp:
            left = write_stem(tmp, "GTR 1 L.wav", peak_db=-6)
            right = write_stem(tmp, "GTR 1 R.wav", peak_db=-12)
            solo = write_stem(tmp, "Bass.wav", peak_db=-12)
            gains = rig._level_match_gains([left, right, solo])
            alone_left, _ = rig._level_match_gain(rig.stem_level(left))
            alone_right, _ = rig._level_match_gain(rig.stem_level(right))
        self.assertAlmostEqual(gains[left][0], gains[right][0])
        self.assertLess(alone_left, gains[left][0])
        self.assertLess(gains[right][0], alone_right)
        self.assertIn("same gain as GTR 1 R", gains[left][1])
        self.assertAlmostEqual(gains[solo][0], alone_right)
        self.assertEqual(gains[solo][1], "")


class AddTrackWordingTest(unittest.TestCase):
    def test_describe(self):
        track = AddTrack(action="add_track", name="Keys", input="5/6", pan=-0.5,
                         devices=[{"device": "Reverb", "preset": "Large Hall"}])
        self.assertEqual(track.describe(), "Add a track “Keys” on input 5/6, with Reverb (Large Hall), balance 50% left")


class SongMixCase(FakeLiveCase):
    """A set with two songs, a vocal, keys and a reverb, and the app keeping their mixes."""

    def setUp(self):
        super().setUp()
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        self.mixes = song_mixes.SongMixMemory(Path(self.home.name) / "song_mixes.json")
        self.app = App(self.live, Conversation(client_factory=lambda: None),
                       folders=FolderMemory(Path(self.home.name) / "folders.json"), mixes=self.mixes)
        self.now = [100.0]
        self.app.clock = lambda: self.now[0]
        self.live.call("create_return_track", name="Reverb")
        for name in ("Lead Vocal", "Keys"):
            self.live.call("create_audio_track", name=name)
        self.live.call("set_scene", scene_index=0, name="Living Hope")  # a new set starts with one empty song
        self.live.call("create_scene", name="Gratitude")

    def look(self):
        """A poll, after any put-back has settled."""
        self.now[0] += 10
        return self.app.state()

    def track(self, name):
        return next(t for t in self.live.snapshot(max_age=0)["tracks"] if t["name"] == name)

    def apply(self, *actions):
        parsed = Proposal(actions=list(actions)).actions
        return run_all(self.live, parsed, mixes=self.mixes, mix_control=self.app)


class SongMixTest(SongMixCase):
    """Faders, pan, mute and sends kept per song, put back when the song is picked."""

    def test_each_song_gets_its_own_faders_pan_mute_and_sends_back(self):
        self.app.pick_song_mix(0)
        self.live.call("set_volume", track_index=0, db=-6.0)
        self.live.call("set_pan", track_index=1, pan=-0.5)
        self.live.call("set_send", track_index=0, return_index=0, db=-12.0)
        self.look()  # saved to Living Hope
        self.app.pick_song_mix(1)
        self.live.call("set_volume", track_index=0, db=-1.0)
        self.live.call("set_mute", track_index=1, on=True)
        self.look()  # saved to Gratitude

        self.app.pick_song_mix(0)
        vocal, keys = self.track("Lead Vocal"), self.track("Keys")
        self.assertAlmostEqual(vocal["volume_db"], -6.0, places=1)
        self.assertAlmostEqual(vocal["sends"][0]["level_db"], -12.0, places=1)
        self.assertAlmostEqual(keys["pan_value"], -0.5, places=2)
        self.assertFalse(keys["mute"])
        self.app.pick_song_mix(1)
        self.assertAlmostEqual(self.track("Lead Vocal")["volume_db"], -1.0, places=1)
        self.assertTrue(self.track("Keys")["mute"])

    def test_survives_a_restart_and_a_moved_song(self):
        self.app.pick_song_mix(0)
        self.live.call("set_volume", track_index=1, db=-9.0)
        self.look()
        self.app.pick_song_mix(1)
        self.live.call("set_volume", track_index=1, db=0.0)
        self.look()
        self.live.call("move_scene", scene_index=0, to_index=1)  # Living Hope is now second

        again = App(self.live, Conversation(client_factory=lambda: None), mixes=song_mixes.SongMixMemory(self.mixes.path))
        self.assertEqual(again.mix_state(self.live.snapshot(max_age=0))["song"], "Gratitude")
        again.pick_song_mix(1)
        self.assertAlmostEqual(self.track("Keys")["volume_db"], -9.0, places=1)

    def test_a_change_while_putting_a_mix_back_is_not_saved_to_the_song(self):
        self.app.pick_song_mix(1)
        self.live.call("set_volume", track_index=0, db=-3.0)
        self.look()
        self.app.pick_song_mix(0)
        self.live.call("set_volume", track_index=0, db=-8.0)
        self.look()
        self.app.pick_song_mix(1)  # Gratitude's -3 goes back on
        self.live.call("set_volume", track_index=0, db=-20.0)  # read while still settling
        self.app.state()
        self.assertAlmostEqual(self.mixes.saved("Gratitude")["tracks"]["lead vocal"]["volume_db"], -3.0, places=1)
        self.look()
        self.assertAlmostEqual(self.mixes.saved("Gratitude")["tracks"]["lead vocal"]["volume_db"], -20.0, places=1)

    def test_no_song_picked_saves_nothing(self):
        self.live.call("set_volume", track_index=0, db=-4.0)
        self.look()
        self.assertIsNone(self.mixes.saved("Living Hope"))
        self.assertIn("Faders are shared by every song", self.app.notes())

    def test_starting_a_song_in_live_puts_its_mix_on(self):
        self.app.pick_song_mix(1)
        self.live.call("set_volume", track_index=0, db=-15.0)
        self.look()
        self.app.pick_song_mix(0)
        self.look()
        with tempfile.TemporaryDirectory() as folder:
            wav = Path(folder) / "vocal.wav"
            with wave.open(str(wav), "wb") as w:
                w.setnchannels(1), w.setsampwidth(2), w.setframerate(8000), w.writeframes(b"\0\0" * 800)
            self.live.call("import_audio", track_index=0, file_path=str(wav), scene_index=1)
        self.live.call("fire_scene", scene_index=1)  # pressed in Live, not in the app
        state = self.look()
        self.assertEqual(state["song_mix"]["song"], "Gratitude")
        self.assertAlmostEqual(self.track("Lead Vocal")["volume_db"], -15.0, places=1)

    def test_checkpoints_restore_and_undo(self):
        self.app.pick_song_mix(0)
        self.live.call("set_volume", track_index=0, db=-2.0)
        self.look()
        self.app.checkpoint_song_mix("After soundcheck")
        self.live.call("set_volume", track_index=0, db=-30.0)
        self.look()

        marks = self.app.mix_state()["checkpoints"]
        self.assertEqual([m["label"] for m in marks], ["After soundcheck"])
        self.app.restore_song_mix(marks[0]["id"])
        self.assertAlmostEqual(self.track("Lead Vocal")["volume_db"], -2.0, places=1)
        self.assertAlmostEqual(self.mixes.saved("Living Hope")["tracks"]["lead vocal"]["volume_db"], -2.0, places=1)
        undo = self.app.mix_state()["checkpoints"][0]
        self.assertEqual(undo["label"], "Before going back to After soundcheck")
        self.app.restore_song_mix(undo["id"])
        self.assertAlmostEqual(self.track("Lead Vocal")["volume_db"], -30.0, places=1)

    def test_renaming_a_song_keeps_its_mix(self):
        self.app.pick_song_mix(0)
        self.look()
        self.mixes.rename("Living Hope", "Living Hope (Acoustic)")
        self.assertIsNotNone(self.mixes.saved("living hope (acoustic)"))
        self.assertEqual(self.mixes.current, "Living Hope (Acoustic)")

    def test_unnamed_song_is_refused_in_a_sentence(self):
        self.live.call("create_scene")
        from app.server import UserError
        with self.assertRaisesRegex(UserError, "name"):
            self.app.pick_song_mix(2)

    def test_ai_change_for_another_song_saves_it_without_moving_the_faders(self):
        self.app.pick_song_mix(0)
        self.look()
        [result] = self.apply({"action": "set_volume", "track": "Keys", "db": -9, "song": "Gratitude"})
        self.assertTrue(result["ok"], result)
        self.assertIn("in Gratitude", result["text"])
        self.assertAlmostEqual(self.track("Keys")["volume_db"], 0.0, places=1)  # Living Hope is playing
        self.look()
        self.assertIn("Keys -9 dB", self.app.notes())
        self.app.pick_song_mix(1)
        self.assertAlmostEqual(self.track("Keys")["volume_db"], -9.0, places=1)

    def test_ai_change_for_the_song_on_the_mixer_happens_now(self):
        self.app.pick_song_mix(0)
        self.apply({"action": "set_mute", "track": "Keys", "on": True, "song": "Living Hope"},
                 {"action": "set_send", "track": "Lead Vocal", "to_return": "Reverb", "db": -10, "song": "Gratitude"})
        self.assertTrue(self.track("Keys")["mute"])
        self.look()
        self.assertTrue(self.mixes.saved("Living Hope")["tracks"]["keys"]["mute"])
        self.assertEqual(self.mixes.saved("Gratitude")["tracks"]["lead vocal"]["sends"]["Reverb"], -10.0)

    def test_ai_can_put_the_mixer_on_a_song(self):
        self.app.pick_song_mix(1)
        self.live.call("set_volume", track_index=0, db=-12.0)
        self.look()
        self.app.pick_song_mix(0)
        [result] = self.apply({"action": "pick_song_mix", "song": "Gratitude"})
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.mixes.current, "Gratitude")
        self.assertAlmostEqual(self.track("Lead Vocal")["volume_db"], -12.0, places=1)

    def test_by_db_moves_from_that_songs_own_level(self):
        self.app.pick_song_mix(1)
        self.live.call("set_volume", track_index=1, db=-6.0)  # Keys in Gratitude
        self.look()
        self.app.pick_song_mix(0)
        self.live.call("set_volume", track_index=1, db=-1.0)  # Keys in Living Hope
        self.look()
        results = self.apply({"action": "set_volume", "track": "Keys", "by_db": -3, "song": "Gratitude"},
                             {"action": "set_volume", "track": "Keys", "by_db": 2},
                             {"action": "set_send", "track": "Lead Vocal", "to_return": "Reverb", "by_db": 6})
        self.assertTrue(all(r["ok"] for r in results), results)
        self.assertAlmostEqual(self.mixes.saved("Gratitude")["tracks"]["keys"]["volume_db"], -9.0, places=1)
        self.assertAlmostEqual(self.track("Keys")["volume_db"], 1.0, places=1)
        reverb = next(x for x in self.track("Lead Vocal")["sends"] if x["return"] == "Reverb")
        self.assertAlmostEqual(reverb["level_db"], -64.0, places=1)  # from off

    def test_loose_names_find_the_one_track_or_song_meant(self):
        self.app.pick_song_mix(0)
        [result] = self.apply({"action": "set_mute", "track": "lead vox", "on": True, "song": "the gratitude song"})
        self.assertTrue(result["ok"], result)
        self.assertTrue(self.mixes.saved("Gratitude")["tracks"]["lead vocal"]["mute"])

    def test_assistant_saves_and_restores_checkpoints_by_name(self):
        self.app.pick_song_mix(0)
        self.live.call("set_volume", track_index=0, db=-2.0)
        self.look()
        [saved] = self.apply({"action": "save_checkpoint", "label": "After soundcheck"})
        self.assertTrue(saved["ok"], saved)
        self.assertIn('"After soundcheck"', self.app.notes())
        self.live.call("set_volume", track_index=0, db=-20.0)
        self.look()
        [back] = self.apply({"action": "restore_checkpoint", "label": "after soundcheck"})
        self.assertTrue(back["ok"], back)
        self.assertAlmostEqual(self.track("Lead Vocal")["volume_db"], -2.0, places=1)
        [missing] = self.apply({"action": "restore_checkpoint", "label": "Rehearsal"})
        self.assertIn("After soundcheck", missing["text"])

    def test_notes_spell_out_every_songs_mix(self):
        self.app.pick_song_mix(1)
        self.live.call("set_volume", track_index=1, db=-6.0)
        self.live.call("set_mute", track_index=0, on=True)
        self.look()
        self.app.pick_song_mix(0)
        self.live.call("set_volume", track_index=1, db=0.0)
        self.live.call("set_mute", track_index=0, on=False)
        self.look()
        notes = self.app.notes()
        self.assertIn("1. Living Hope — MIXER IS ON THIS SONG", notes)
        self.assertIn("mix: Lead Vocal 0 dB, Keys 0 dB", notes)
        self.assertIn("mix: Lead Vocal 0 dB MUTED, Keys -6 dB", notes)

    def test_only_differences_are_sent_and_new_tracks_are_left_alone(self):
        snap = self.live.snapshot(max_age=0)
        mix = song_mixes.mix_of(snap)
        self.assertEqual(song_mixes.commands(mix, snap), [])
        mix["tracks"]["lead vocal"]["volume_db"] = -5.0
        del mix["tracks"]["keys"]
        self.assertEqual(song_mixes.commands(mix, snap),
                         [("set_volume", {"track_index": 0, "is_return": False, "db": -5.0})])



class EqRulesTest(unittest.TestCase):
    """app/eq.py: Live's type names, and the problems a curve has by rule."""

    def band(self, n, kind, hz, db=0.0, q=0.71, on=True):
        return {"band": n, "on": on, "type": kind, "freq_hz": hz, "gain_db": db, "q": q}

    def test_live_type_names_map_to_one_vocabulary(self):
        self.assertEqual(eq.kind_of("Low Cut 48"), "low cut 48")
        self.assertEqual(eq.kind_of("High Pass"), "low cut")
        self.assertEqual(eq.kind_of("Low Pass 48"), "high cut 48")
        self.assertEqual(eq.kind_of("Bell"), "bell")
        self.assertEqual(eq.kind_of("High Shelf"), "high shelf")
        self.assertIsNone(eq.kind_of("Something New"))
        self.assertEqual(eq.type_index("bell", ["Low Cut 48", "Low Cut 12", "Low Shelf", "Bell"]), 3)

    def test_a_messed_up_vocal_gets_every_problem_named(self):
        bands = [self.band(1, "low cut", 450), self.band(2, "bell", 1000, 12, q=6),
                 self.band(3, "high cut", 4000), self.band(4, "low shelf", 200, 5)]
        found = " | ".join(eq.problems(bands, "Lead Vocal"))
        for words in ("below 450 Hz", "boosts 1 kHz by 12.0 dB", "narrow boost", "above 4 kHz", "low end below 200 Hz"):
            self.assertIn(words, found)

    def test_a_gentle_curve_has_no_problems(self):
        vocal = [self.band(1, "low cut", 100), self.band(2, "bell", 300, -3, q=1.2), self.band(3, "bell", 4000, 2)]
        self.assertEqual(eq.problems(vocal, "Lead Vocal"), [])
        self.assertEqual(eq.problems([self.band(1, "low cut", 35), self.band(2, "bell", 80, 3)], "Bass"), [])
        self.assertEqual(eq.problems(eq.flat(), "Lead Vocal"), ["no low cut on a vocal (rumble and mic pops come through)"])
        self.assertEqual(eq.describe(eq.flat()), "flat")


class EqTest(SongMixCase):
    """EQ Eight from the mixer and the assistant, kept per song like the faders."""

    def eq_of(self, name):
        return eq.bands_of(self.track(name)["eq"])

    def test_set_eq_adds_an_eq_eight_and_shapes_it(self):
        [result] = self.apply({"action": "set_eq", "track": "Lead Vocal", "bands": [
            {"band": 1, "type": "low cut", "freq_hz": 100},
            {"band": 2, "freq_hz": 300, "gain_db": -3, "q": 1.2}]})
        self.assertTrue(result["ok"], result)
        self.assertIn("Added EQ Eight", result["text"])
        self.assertEqual(self.track("Lead Vocal")["devices"], ["EQ Eight"])
        low, mud = self.eq_of("Lead Vocal")[:2]
        self.assertEqual((low["type"], low["freq_hz"]), ("low cut", 100.0))
        self.assertEqual((mud["type"], mud["freq_hz"], mud["gain_db"], mud["q"]), ("bell", 300.0, -3.0, 1.2))

    def test_eq_changes_with_the_song(self):
        self.live.call("load_device", track_index=0, device_name="EQ Eight")
        self.app.pick_song_mix(0)
        self.live.call("set_eq_band", track_index=0, band=2, freq_hz=250, gain_db=-4)  # as the page does
        self.look()
        self.app.pick_song_mix(1)
        self.live.call("set_eq_band", track_index=0, band=2, freq_hz=3000, gain_db=3)
        self.look()

        self.app.pick_song_mix(0)
        band = self.eq_of("Lead Vocal")[1]
        self.assertEqual((band["freq_hz"], band["gain_db"]), (250.0, -4.0))
        self.app.pick_song_mix(1)
        band = self.eq_of("Lead Vocal")[1]
        self.assertEqual((band["freq_hz"], band["gain_db"]), (3000.0, 3.0))
        self.assertIn("(EQ 2: bell 3 kHz +3.0 dB", self.app.notes())

    def test_eq_for_another_song_waits_for_it(self):
        self.live.call("load_device", track_index=1, device_name="EQ Eight")
        self.app.pick_song_mix(0)
        self.look()
        [result] = self.apply({"action": "set_eq", "track": "Keys", "song": "Gratitude",
                               "bands": [{"band": 4, "gain_db": 2.5}]})
        self.assertTrue(result["ok"], result)
        self.assertIn("in Gratitude", result["text"])
        self.assertEqual(self.eq_of("Keys")[3]["gain_db"], 0.0)  # Living Hope is on the mixer
        self.app.pick_song_mix(1)
        self.assertEqual(self.eq_of("Keys")[3]["gain_db"], 2.5)

    def test_a_messed_up_eq_is_flagged_and_a_fix_clears_it(self):
        self.live.call("load_device", track_index=0, device_name="EQ Eight")
        self.app.pick_song_mix(0)
        self.live.call("set_eq_band", track_index=0, band=2, freq_hz=800, gain_db=13, q=7)
        self.live.call("set_eq_band", track_index=0, band=8, on=True, freq_hz=3500)
        self.assertIn("EQ PROBLEMS", self.app.notes())
        [result] = self.apply({"action": "set_eq", "track": "Lead Vocal", "flat_first": True, "bands": [
            {"band": 1, "type": "low cut", "freq_hz": 100},
            {"band": 2, "freq_hz": 300, "gain_db": -2.5, "q": 1.2},
            {"band": 3, "freq_hz": 4000, "gain_db": 2}]})
        self.assertTrue(result["ok"], result)
        self.assertNotIn("EQ PROBLEMS", self.app.notes())
        self.assertFalse(self.eq_of("Lead Vocal")[7]["on"])  # the high cut went with the flat reset
        self.look()
        self.assertEqual(self.mixes.saved("Living Hope")["tracks"]["lead vocal"]["eq"][1]["gain_db"], -2.5)

    def test_flat_button_resets_through_the_server(self):
        self.live.call("load_device", track_index=0, device_name="EQ Eight")
        self.live.call("set_eq_band", track_index=0, band=3, gain_db=9)
        self.app.eq_flat("Lead Vocal")
        self.assertEqual(eq.describe(self.eq_of("Lead Vocal")), "flat")

    def test_eq_limits_are_tighter_than_the_device(self):
        with self.assertRaises(ValidationError):
            Proposal(actions=[{"action": "set_eq", "track": "Keys", "bands": [{"band": 2, "gain_db": 14}]}])
        with self.assertRaises(ValidationError):
            Proposal(actions=[{"action": "set_eq", "track": "Keys", "bands": [{"band": 9}]}])


if __name__ == "__main__":
    unittest.main()


class McpTest(StemFolderCase):
    """app/mcp.py over stdio JSON-RPC, against a real app server on the fake Live."""

    def setUp(self):
        super().setUp()
        self.server, self.fake = fake_live.serve(port=0, latency=0)
        self.live = LiveLink(port=self.server.server_address[1])
        self.live.call("create_audio_track", name="Keys")
        room = RoomMemory(Path(self._tmp.name) / "room.json")
        self.app = App(self.live, Conversation(client_factory=lambda: None, room=room))
        self.http = None
        self.hs = self.client()
        self._ids = iter(range(1, 1000))

    def client(self):
        """Over HTTP to a running web app."""
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.app))
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        return mcp.HolySound("http://%s:%d" % self.http.server_address)

    def tearDown(self):
        if self.http:
            self.http.shutdown()
            self.http.server_close()
        self.live.close()
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def rpc(self, *messages):
        lines = "".join(json.dumps(m) + "\n" for m in messages)
        out = io.StringIO()
        mcp.serve(io.StringIO(lines), out, self.hs)
        return [json.loads(line) for line in out.getvalue().splitlines()]

    def tool(self, name, **arguments):
        [reply] = self.rpc({"jsonrpc": "2.0", "id": next(self._ids), "method": "tools/call",
                            "params": {"name": name, "arguments": arguments}})
        result = reply["result"]
        return result["content"][0]["text"], result["isError"]

    def test_handshake_and_tool_list(self):
        init, tools = self.rpc(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        )
        self.assertEqual(init["result"]["protocolVersion"], "2025-03-26")
        self.assertIn("tools", init["result"]["capabilities"])
        names = [t["name"] for t in tools["result"]["tools"]]
        self.assertIn("propose_changes", names)
        propose = next(t for t in tools["result"]["tools"] if t["name"] == "propose_changes")
        self.assertIn("actions", propose["inputSchema"]["properties"])
        self.assertNotIn("$defs", json.dumps(propose["inputSchema"]))
        [unknown] = self.rpc({"jsonrpc": "2.0", "id": 3, "method": "resources/list"})
        self.assertEqual(unknown["error"]["code"], -32601)

    def test_an_outside_agent_proposes_and_applies(self):
        notes, failed = self.tool("read_session")
        self.assertFalse(failed)
        self.assertIn("Keys", notes)

        text, failed = self.tool("propose_changes", say="Keys down a little.",
                                 actions=[{"action": "set_volume", "track": "Keys", "db": -6}])
        self.assertFalse(failed, text)
        self.assertIn("(pending)", text)
        pid = next(p["id"] for p in self.app.chat.proposals.values())
        self.assertEqual(self.app.transcript()[-1]["text"], "Keys down a little.")  # the page shows it

        text, failed = self.tool("apply_changes", proposal_id=pid)
        self.assertFalse(failed, text)
        self.assertIn("(applied)", text)
        self.assertNotIn("FAILED", text)
        keys = next(t for t in self.live.snapshot()["tracks"] if t["name"] == "Keys")
        self.assertAlmostEqual(keys["volume_db"], -6.0, places=1)

        text, failed = self.tool("apply_changes", proposal_id=pid)
        self.assertTrue(failed)
        self.assertIn("already been dealt with", text)

    def test_bad_actions_come_back_as_sentences(self):
        text, failed = self.tool("propose_changes", actions=[{"action": "set_volume", "db": -6}])
        self.assertTrue(failed)
        self.assertIn("didn't validate", text)
        self.assertEqual(self.app.chat.proposals, {})

    def test_import_report_is_for_the_agent_not_the_assistant(self):
        text, failed = self.tool("import_folder", folder=str(self.folder))
        self.assertFalse(failed, text)
        self.assertIn('"Sunday Stems/Way Maker/Click.wav"', text)
        self.assertIn("Sunday Stems/Way Maker/Click.wav", self.app.files)  # import_part may use it
        self.assertEqual(self.app.chat.transcript, [])

    def test_browse_folders(self):
        text, failed = self.tool("browse_folders", path=str(self.folder))
        self.assertFalse(failed, text)
        self.assertIn("Way Maker", text)

    def test_remember_and_forget(self):
        text, _ = self.tool("remember", add="The drummer's in-ears are on output 3/4.")
        self.assertIn("output 3/4", text)
        fact_id = self.app.room.facts()[0]["id"]
        text, _ = self.tool("remember", forget=fact_id)
        self.assertEqual(text, "Nothing remembered yet.")

    def test_app_not_running_is_a_sentence(self):
        self.hs = mcp.HolySound("http://127.0.0.1:1")
        text, failed = self.tool("read_session")
        self.assertTrue(failed)
        self.assertIn("isn't running", text)


class McpHeadlessTest(McpTest):
    """The same tools with no web app: app/mcp.py runs the App and its API in-process."""

    def client(self):
        return mcp.LocalHolySound(self.app)

    def test_app_not_running_is_a_sentence(self):
        self.skipTest("headless has no web app to be missing")

    def test_export_writes_the_file(self):
        text, failed = self.tool("propose_changes", actions=[
            {"action": "add_track", "name": "Click", "kind": "audio"}])
        self.assertFalse(failed, text)
        pid = next(iter(self.app.chat.proposals))
        out = Path(self._tmp.name) / "Sunday.als"
        text, failed = self.tool("export_session_file", proposal_id=pid, path=str(out))
        self.assertFalse(failed, text)
        self.assertEqual(out.read_bytes()[:2], b"\x1f\x8b")

    def test_a_bug_is_a_sentence_not_a_crash(self):
        with mock.patch.object(self.app, "notes", side_effect=RuntimeError("boom")), \
                mock.patch("traceback.print_exc"):
            text, failed = self.tool("read_session")
        self.assertTrue(failed)
        self.assertIn("Something went wrong", text)


class HeadlessFollowTest(SongMixCase):
    """With no page polling, the headless MCP looks at Live itself so song mixes keep up."""

    def test_a_fader_move_is_saved_to_the_song_with_no_tool_call(self):
        self.app.pick_song_mix(0)  # Living Hope saved at 0 dB
        self.now[0] += 10
        self.live.call("set_volume", track_index=0, db=-6.0)
        before = self.mixes.saved("Living Hope")
        stop = threading.Event()
        follower = threading.Thread(target=mcp.follow_live, args=(self.app, stop, 0.01))
        follower.start()
        try:
            for _ in range(300):
                if self.mixes.saved("Living Hope") != before:
                    break
                threading.Event().wait(0.01)
        finally:
            stop.set()
            follower.join()

        self.live.call("set_volume", track_index=0, db=-20.0)
        self.app.pick_song_mix(1)
        self.app.pick_song_mix(0)
        self.assertAlmostEqual(self.track("Lead Vocal")["volume_db"], -6.0, places=1)


class CrewModel:
    """Stands in for the provider in expert mode: answers the lead and each specialist by role."""

    def __init__(self, lead_turns, specialists):
        self.lead_turns = list(lead_turns)  # brief_crew inputs, in order
        self.specialists = specialists  # title -> [hand_back inputs, in order of attempts]
        self.lock = threading.Lock()
        self.prompts = []

    def create(self, system, tools, messages, on_event=None):
        tool = tools[0]["name"]
        with self.lock:
            self.prompts.append((system, messages[-1]))
            if tool == "brief_crew":
                given = self.lead_turns.pop(0)
            else:
                title = next(t for t in self.specialists if f"You are the {t}" in system)
                given = self.specialists[title].pop(0)
        return reply(Block(type="tool_use", id=f"tu_{len(self.prompts)}", name=tool, input=given))


class ExpertModeTest(SongMixCase):
    def setUp(self):
        super().setUp()
        for name in ("Drums", "Click"):
            self.live.call("create_audio_track", name=name)
        self.app.pick_song_mix(0)

    def test_crew_owns_tracks_by_folder_then_part(self):
        self.assertEqual(expert.crew_for("Lead Vocal"), "vocals")
        self.assertEqual(expert.crew_for("Synth Bass"), "rhythm")
        self.assertEqual(expert.crew_for("Keys", "instruments"), "band")
        self.assertEqual(expert.crew_for("Click", "playback"), "playback")
        self.assertEqual(expert.crew_for("3-Audio", "other"), "playback")
        self.assertEqual(expert.crew_for("Pad", "vocals"), "vocals")  # moved there by hand
        owned = expert.roster(self.app.with_folders(self.live.snapshot(max_age=0)))
        self.assertEqual(owned, {"vocals": ["Lead Vocal"], "rhythm": ["Drums"], "band": ["Keys"],
                                 "playback": ["Click", "A-Reverb", "B-Delay", "Reverb"]})

    def test_run_briefs_applies_and_checks_without_apply(self):
        crew = CrewModel(
            lead_turns=[
                {"done": False, "summary": "The vocal is too loud and the drums are buried.",
                 "briefs": [{"crew": "vocals", "brief": "Lead Vocal down 3 dB."},
                            {"crew": "rhythm", "brief": "Drums down 6 dB."}]},
                {"done": True, "summary": "Sounds right now.", "briefs": []},
            ],
            specialists={
                "Vocals tech": [{"summary": "Took the vocal down.",
                                 "actions": [{"action": "set_volume", "track": "Lead Vocal", "db": -3}]}],
                "Rhythm tech": [
                    # Not its track: sent back, then it fixes its own.
                    {"summary": "x", "actions": [{"action": "set_volume", "track": "Lead Vocal", "db": -20}]},
                    {"summary": "Drums sit under the band now.",
                     "actions": [{"action": "set_volume", "track": "Drums", "db": -6}]},
                ],
            },
        )
        self.app.chat._client = crew

        result = self.app.expert("vocals on top")

        self.assertEqual(result, {"ok": True, "rounds": 1, "applied": 2, "summary": "Sounds right now."})
        self.assertAlmostEqual(self.track("Lead Vocal")["volume_db"], -3.0, places=1)
        self.assertAlmostEqual(self.track("Drums")["volume_db"], -6.0, places=1)
        self.assertFalse(self.app.chat.busy)
        said = [(e["agent"], e["text"]) for e in self.app.chat.transcript if e["role"] == "expert"]
        self.assertEqual([a for a, _ in said],
                         ["Lead engineer", "Lead engineer", "Vocals tech", "Rhythm tech", "Lead engineer",
                          "Lead engineer", "Lead engineer"])
        self.assertIn("vocals on top", said[0][1])
        sent_back = [m for s, m in crew.prompts if "Rhythm tech" in s and m["content"][0]["type"] == "tool_result"]
        self.assertIn("Not your tracks: Lead Vocal", sent_back[0]["content"][0]["content"])
        proposal = next(p for p in self.app.chat.proposals.values())
        self.assertEqual(proposal["status"], "applied")
        labels = [m["label"] for m in self.mixes.checkpoints("Living Hope")]
        self.assertEqual(labels, [expert.AFTER, expert.BEFORE])

    def test_needs_live_and_a_free_chat(self):
        self.app.chat.busy = True
        with self.assertRaises(app_server.UserError):
            self.app.expert()
