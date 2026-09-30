"""Holy Sound web app, end to end against the fake Live and a scripted Claude.

    uv run python -m unittest discover tests

Needs neither Ableton nor an API key. unittest rather than pytest because
pytest isn't an approved dependency yet (CLAUDE.md).
"""

import copy
import http.client
import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

from app import fake_live
from app.actions import AddTrack, Proposal, run_all, to_rigspec
from app.assistant import AssistantUnavailable, Conversation, session_notes
from app.live import LiveLink, LiveUnavailable
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
        self.assertIn("1. Lead Vocal | audio | in: Ext. In 1 | out: Master", notes)
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
        status, _ = self.request("GET", "/../spec.py")
        self.assertEqual(status, 404)


class AddTrackWordingTest(unittest.TestCase):
    def test_describe(self):
        track = AddTrack(action="add_track", name="Keys", input="5/6", pan=-0.5,
                         devices=[{"device": "Reverb", "preset": "Large Hall"}])
        self.assertEqual(track.describe(), "Add an audio track “Keys” on input 5/6, with Reverb (Large Hall), panned 25L")


if __name__ == "__main__":
    unittest.main()
