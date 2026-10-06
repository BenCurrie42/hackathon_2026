"""Providers: OpenAI Chat translation, error mapping, OpenCode Go model discovery.

No network: every HTTP call goes through a fake opener.
"""

import io
import json
import unittest
import urllib.error
from unittest import mock

from app import providers
from app.assistant import AssistantUnavailable, Conversation, _tools
from app.providers import (
    CHAT,
    MESSAGES,
    RESPONSES,
    AnthropicProvider,
    AssistantSetupError,
    OpenAIChatProvider,
    chat_messages,
    chat_reply,
    default_model,
    discover_models,
    models_dev_formats,
    provider_from_env,
)


class FakeResponse(io.BytesIO):
    pass


class FakeOpener:
    """Stands in for urllib.request.urlopen: canned bodies by URL, records requests."""

    def __init__(self, *bodies, routes=None):
        self.bodies = list(bodies)
        self.routes = routes or {}
        self.requests = []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        body = self.routes[request.full_url] if self.routes else self.bodies.pop(0)
        if isinstance(body, Exception):
            raise body
        return FakeResponse(body if isinstance(body, bytes) else json.dumps(body).encode())

    def sent(self, i=-1):
        return json.loads(self.requests[i].data)


def completion(content=None, tool_calls=None, finish="stop", **extra):
    message = {"role": "assistant", "content": content, **extra}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {"choices": [{"index": 0, "message": message, "finish_reason": finish}]}


def tool_call(call_id, name, args):
    return {"id": call_id, "type": "function",
            "function": {"name": name, "arguments": args if isinstance(args, str) else json.dumps(args)}}


def http_error(code, body):
    return urllib.error.HTTPError("https://x/chat/completions", code, "err", {},
                                  io.BytesIO(json.dumps(body).encode()))


def sse(*chunks):
    """A streamed Chat Completions body."""
    lines = [": keep-alive", ""] + [f"data: {json.dumps(c)}\n" for c in chunks] + ["data: [DONE]", ""]
    return "\n".join(lines).encode()


def delta(finish=None, **fields):
    return {"choices": [{"index": 0, "delta": fields, "finish_reason": finish}]}


def chat_provider(*bodies):
    opener = FakeOpener(*bodies)
    return OpenAIChatProvider("https://opencode.ai/zen/go/v1", "test-key", "kimi-k3", opener=opener), opener


class ChatTranslationTest(unittest.TestCase):
    def test_request_carries_system_tools_and_translated_history(self):
        provider, opener = chat_provider(completion("Hi."))
        history = [
            {"role": "user", "content": [{"type": "text", "text": "Add a vocal"}]},
            {"role": "assistant", "content": [
                {"type": "text", "text": "Here."},
                {"type": "tool_use", "id": "c1", "name": "propose_changes", "input": {"actions": []}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "c1", "is_error": True, "content": "Bad."},
                {"type": "text", "text": "<session>...</session>"},
            ]},
        ]
        provider.create("Be kind.", _tools(), history)
        request = opener.requests[0]
        self.assertEqual(request.full_url, "https://opencode.ai/zen/go/v1/chat/completions")
        self.assertEqual(request.get_header("Authorization"), "Bearer test-key")
        body = opener.sent()
        self.assertEqual(body["model"], "kimi-k3")
        self.assertEqual((body["tool_choice"], body["parallel_tool_calls"]), ("auto", False))
        self.assertEqual([t["function"]["name"] for t in body["tools"]], ["propose_changes", "remember"])
        self.assertEqual(body["tools"][0]["type"], "function")
        self.assertIn("actions", body["tools"][0]["function"]["parameters"]["properties"])
        self.assertEqual(body["messages"], [
            {"role": "system", "content": "Be kind."},
            {"role": "user", "content": "Add a vocal"},
            {"role": "assistant", "content": "Here.", "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "propose_changes", "arguments": '{"actions": []}'}},
            ]},
            {"role": "tool", "tool_call_id": "c1", "content": "Error: Bad."},
            {"role": "user", "content": "<session>...</session>"},
        ])

    def test_reasoning_and_unparsed_arguments_go_back_as_sent(self):
        out = chat_messages("s", [{"role": "assistant", "content": [
            {"type": "thinking", "thinking": "Hmm."},
            {"type": "tool_use", "id": "c1", "name": "remember", "input": "{oops"},
        ]}])
        self.assertEqual(out[1], {"role": "assistant", "content": None, "reasoning_content": "Hmm.",
                                  "tool_calls": [{"id": "c1", "type": "function",
                                                  "function": {"name": "remember", "arguments": "{oops"}}]})

    def test_reply_tool_calls_become_tool_use_blocks(self):
        reply = chat_reply(completion("Sure.", [tool_call("c1", "propose_changes", {"actions": []})],
                                      finish="tool_calls", reasoning_content="Think."))
        self.assertEqual(reply.stop_reason, "tool_use")
        self.assertEqual(reply.content, [
            {"type": "thinking", "thinking": "Think."},
            {"type": "text", "text": "Sure."},
            {"type": "tool_use", "id": "c1", "name": "propose_changes", "input": {"actions": []}},
        ])

    def test_finish_reasons(self):
        for finish, stop in [("stop", "end_turn"), ("length", "max_tokens"),
                             ("content_filter", "refusal"), (None, "end_turn")]:
            self.assertEqual(chat_reply(completion("x", finish=finish)).stop_reason, stop)
        # Some models finish with "stop" even after calling a tool.
        self.assertEqual(chat_reply(completion(None, [tool_call("c", "remember", {})])).stop_reason, "tool_use")

    def test_invalid_json_arguments_are_kept_as_text(self):
        reply = chat_reply(completion(None, [tool_call("c1", "remember", "{not json")], "tool_calls"))
        self.assertEqual(reply.content[0]["input"], "{not json")


class ChatStreamTest(unittest.TestCase):
    def test_streamed_reply_is_reassembled_and_heard_as_it_arrives(self):
        provider, opener = chat_provider(sse(
            delta(role="assistant", reasoning_content="They want "),
            delta(reasoning_content="a vocal."),
            delta(content="Here it is."),
            delta(tool_calls=[{"index": 0, "id": "c1", "type": "function",
                               "function": {"name": "propose_changes", "arguments": '{"acti'}}]),
            delta(tool_calls=[{"index": 0, "function": {"arguments": 'ons": []}'}}]),
            delta(finish="tool_calls"),
            {"choices": [], "usage": {"prompt_tokens": 900, "completion_tokens": 45}},
        ))
        heard = []
        out = provider.create("sys", [], [{"role": "user", "content": "hi"}],
                              on_event=lambda kind, data: heard.append((kind, data)))
        self.assertTrue(opener.sent()["stream"])
        self.assertEqual(opener.sent()["stream_options"], {"include_usage": True})
        self.assertEqual(out.usage, {"input": 900, "output": 45})
        self.assertEqual(out.stop_reason, "tool_use")
        self.assertEqual(out.content, [
            {"type": "thinking", "thinking": "They want a vocal."},
            {"type": "text", "text": "Here it is."},
            {"type": "tool_use", "id": "c1", "name": "propose_changes", "input": {"actions": []}},
        ])
        self.assertEqual(heard, [
            ("thinking", "They want "), ("thinking", "a vocal."),
            ("text", "Here it is."), ("tool", "propose_changes"),
        ])

    def test_error_part_way_through_is_a_sentence(self):
        provider, _ = chat_provider(sse(delta(content="Hel"), {"error": {"message": "overloaded"}}))
        with self.assertRaises(AssistantUnavailable) as ctx:
            provider.create("sys", [], [])
        self.assertIn("overloaded", str(ctx.exception))


class ChatErrorTest(unittest.TestCase):
    def check(self, error, expected, setup=False):
        provider, _ = chat_provider(error)
        with self.assertRaises(AssistantUnavailable) as ctx:
            provider.create("s", [], [])
        self.assertIn(expected, str(ctx.exception))
        self.assertEqual(isinstance(ctx.exception, AssistantSetupError), setup)

    def test_bad_key_names_the_variable(self):
        self.check(http_error(401, {"type": "error", "error": {"type": "authentication_error",
                                                               "message": "Invalid key"}}),
                   "OPENCODE_API_KEY", setup=True)
        self.check(http_error(403, {}), "OPENCODE_API_KEY", setup=True)

    def test_busy_unreachable_and_rejected(self):
        self.check(http_error(429, {}), "busy")
        self.check(http_error(502, {}), "answered 502")
        self.check(urllib.error.URLError("no route"), "internet connection")
        self.check(TimeoutError(), "internet connection")
        # Anthropic-shaped and OpenAI-shaped error bodies both give their message.
        self.check(http_error(400, {"type": "error", "error": {"message": "Model not found."}}),
                   "(400): Model not found.")
        self.check(http_error(400, {"error": {"message": "bad tools"}}), "bad tools")


class ChatConversationTest(unittest.TestCase):
    def test_every_tool_call_is_answered(self):
        vocal = {"actions": [{"action": "add_track", "name": "Lead Vocal", "input": "1"}]}
        provider, opener = chat_provider(
            completion("Here you go.", [
                tool_call("c1", "propose_changes", vocal),
                tool_call("c2", "propose_changes", {"actions": [{"action": "set_tempo", "bpm": 70}]}),
                tool_call("c3", "remember", {"facts": ["Sarah sings on input 1."]}),
            ], "tool_calls"),
            completion("Done."),
        )
        chat = Conversation(client_factory=lambda: provider)
        chat.send("Set up Sarah's vocal", "notes")
        pid = chat.transcript[-1]["proposal_id"]
        self.assertEqual([a.action for a in chat.proposal(pid)["actions"]], ["add_track"])

        chat.record_outcome(pid, "applied", [{"ok": True, "text": "Added Lead Vocal."}])
        chat.send("Thanks", "notes")
        tools = [m for m in opener.sent()["messages"] if m["role"] == "tool"]
        self.assertEqual({m["tool_call_id"] for m in tools}, {"c1", "c2", "c3"})
        by_id = {m["tool_call_id"]: m["content"] for m in tools}
        self.assertIn("applied", by_id["c1"])
        self.assertIn("only one propose_changes", by_id["c2"])
        self.assertIn("Memory isn't available", by_id["c3"])

    def test_unparsed_arguments_are_sent_back_to_fix(self):
        provider, opener = chat_provider(
            completion(None, [tool_call("c1", "propose_changes", "{bad")], "tool_calls"),
            completion(None, [tool_call("c2", "propose_changes",
                                        {"actions": [{"action": "set_tempo", "bpm": 70}]})], "tool_calls"),
        )
        chat = Conversation(client_factory=lambda: provider)
        chat.send("Tempo 70", "notes")
        tool = [m for m in opener.sent()["messages"] if m["role"] == "tool"][0]
        self.assertIn("wasn't valid JSON", tool["content"])
        self.assertIn("proposal_id", chat.transcript[-1])

    def test_bad_key_sets_setup_error(self):
        provider, _ = chat_provider(http_error(401, {}))
        chat = Conversation(client_factory=lambda: provider)
        with self.assertRaises(AssistantUnavailable):
            chat.send("Hi", "notes")
        self.assertIn("OPENCODE_API_KEY", chat.setup_error)
        self.assertEqual(chat.messages, [])


class ToolSchemaTest(unittest.TestCase):
    def test_variants_use_anyof_and_pin_their_action(self):
        schema = _tools()[0]["input_schema"]
        self.assertNotIn("oneOf", json.dumps(schema))
        variants = schema["properties"]["actions"]["items"]["anyOf"]
        self.assertGreater(len(variants), 10)
        for v in variants:
            action = v["properties"]["action"]
            self.assertEqual(action["enum"], [action["const"]])
            self.assertIn("action", v["required"])


MODELS = {"object": "list", "data": [{"id": "glm-5.3"}, {"id": "kimi-k3"}, {"id": "qwen3.8-max"},
                                     {"id": "grok-4.5"}, {"id": "minimax-m9"}, {"id": "new-model"}]}
CATALOG = {"opencode-go": {"npm": "@ai-sdk/openai-compatible", "models": {
    "glm-5.3": {}, "kimi-k3": {"provider": None},
    "qwen3.8-max": {"provider": {"npm": "@ai-sdk/anthropic"}},
    "grok-4.5": {"provider": {"npm": "@ai-sdk/openai"}},
}}}
ROUTES = {"https://opencode.ai/zen/go/v1/models": MODELS, providers.MODELS_DEV_URL: CATALOG}


class DiscoveryTest(unittest.TestCase):
    def setUp(self):
        providers._discovered = None
        self.addCleanup(setattr, providers, "_discovered", None)

    def test_formats_from_models_dev_and_names(self):
        models, live = discover_models(opener=FakeOpener(routes=ROUTES))
        self.assertTrue(live)
        self.assertEqual(models, {"glm-5.3": CHAT, "kimi-k3": CHAT, "qwen3.8-max": MESSAGES,
                                  "grok-4.5": RESPONSES, "minimax-m9": MESSAGES, "new-model": CHAT})
        self.assertEqual(models_dev_formats(CATALOG)["qwen3.8-max"], MESSAGES)

    def test_unreachable_falls_back_to_built_in_list(self):
        models, live = discover_models(opener=FakeOpener(urllib.error.URLError("offline")))
        self.assertFalse(live)
        self.assertEqual(models["kimi-k3"], CHAT)
        self.assertEqual(models["qwen3.7-plus"], MESSAGES)

    def test_models_dev_down_still_lists_models(self):
        routes = dict(ROUTES, **{providers.MODELS_DEV_URL: urllib.error.URLError("down")})
        models, live = discover_models(opener=FakeOpener(routes=routes))
        self.assertTrue(live)
        self.assertEqual((models["qwen3.8-max"], models["grok-4.5"]), (MESSAGES, RESPONSES))

    def test_default_model(self):
        self.assertEqual(default_model({"glm-5.3": CHAT, "kimi-k3": CHAT}), "kimi-k3")
        self.assertEqual(default_model({"qwen3.8-max": MESSAGES, "glm-5.3": CHAT}), "glm-5.3")

    def test_list_models_text_marks_the_model_in_use(self):
        text = providers.list_models_text({"OPENCODE_API_KEY": "k", "HOLYSOUND_MODEL": "glm-5.3"},
                                          opener=FakeOpener(routes=ROUTES))
        self.assertIn("glm-5.3      OpenAI Chat  <- in use", text)
        self.assertIn("qwen3.8-max  Anthropic Messages", text)
        self.assertIn("not supported", text)


class ProviderChoiceTest(unittest.TestCase):
    def setUp(self):
        providers._discovered = None
        self.addCleanup(setattr, providers, "_discovered", None)
        self.opener = FakeOpener(routes=ROUTES)

    def make(self, **env):
        return provider_from_env(env, opener=self.opener)

    def test_defaults(self):
        self.assertEqual(providers.chosen_provider_name({}), "anthropic")
        self.assertEqual(providers.chosen_provider_name({"OPENCODE_API_KEY": "k"}), "opencode-go")
        self.assertEqual(providers.chosen_provider_name({"OPENCODE_API_KEY": "k", "ANTHROPIC_API_KEY": "a"}),
                         "anthropic")
        self.assertEqual(providers.chosen_provider_name(
            {"HOLYSOUND_PROVIDER": "OpenCode-Go", "ANTHROPIC_API_KEY": "a"}), "opencode-go")

    def test_anthropic_reads_model_and_effort(self):
        p = self.make(ANTHROPIC_API_KEY="a", HOLYSOUND_MODEL="claude-x", HOLYSOUND_EFFORT="high")
        self.assertIsInstance(p, AnthropicProvider)
        self.assertEqual((p.model, p.effort, p.gateway), ("claude-x", "high", False))

    def test_opencode_routes_by_format(self):
        chat = self.make(OPENCODE_API_KEY="k")
        self.assertIsInstance(chat, OpenAIChatProvider)
        self.assertEqual(chat.model, "kimi-k3")
        messages = self.make(OPENCODE_API_KEY="k", HOLYSOUND_MODEL="qwen3.8-max")
        self.assertIsInstance(messages, AnthropicProvider)
        self.assertTrue(messages.gateway)
        self.assertEqual(str(messages.client.base_url).rstrip("/"), "https://opencode.ai/zen/go")

    def test_opencode_setup_errors_are_sentences(self):
        with self.assertRaises(AssistantSetupError) as ctx:
            self.make(HOLYSOUND_PROVIDER="opencode-go")
        self.assertIn("OPENCODE_API_KEY", str(ctx.exception))
        with self.assertRaises(AssistantSetupError) as ctx:
            self.make(OPENCODE_API_KEY="k", HOLYSOUND_MODEL="grok-4.5")
        self.assertIn("Responses format", str(ctx.exception))
        with self.assertRaises(AssistantSetupError) as ctx:
            self.make(OPENCODE_API_KEY="k", HOLYSOUND_MODEL="nope-1")
        self.assertIn("--list-models", str(ctx.exception))
        with self.assertRaises(AssistantSetupError):
            self.make(HOLYSOUND_PROVIDER="openai")

    def test_gateway_request_drops_claude_only_options(self):
        create = mock.MagicMock()
        client = mock.Mock()
        client.messages.stream = create
        AnthropicProvider(client, model="qwen3.8-max", gateway=True).create("sys", [], [])
        kwargs = create.call_args.kwargs
        self.assertEqual(kwargs["system"], "sys")
        self.assertEqual(kwargs["tool_choice"], {"type": "auto"})
        for key in ("betas", "fallbacks", "output_config", "thinking"):
            self.assertNotIn(key, kwargs)
        client.beta.messages.stream.assert_not_called()

    def test_opencode_requests_carry_session_and_user_agent(self):
        # OpenCode Go answers 400 MissingSessionID without x-opencode-session.
        provider, opener = chat_provider(completion("Hi."))
        provider.session_id = "conv-1"
        provider.create("sys", [], [{"role": "user", "content": "hi"}])
        request = opener.requests[0]
        self.assertEqual(request.get_header("X-opencode-session"), "conv-1")
        self.assertEqual(request.get_header("User-agent"), "holy-sound/0.1")

        create = mock.MagicMock()
        client = mock.Mock()
        client.messages.stream = create
        gateway = AnthropicProvider(client, model="qwen3.8-flash", gateway=True)
        gateway.session_id = "conv-2"
        gateway.create("sys", [], [])
        self.assertEqual(create.call_args.kwargs["extra_headers"], {"x-opencode-session": "conv-2"})


if __name__ == "__main__":
    unittest.main()
