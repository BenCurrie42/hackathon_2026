"""Which AI model answers, and how we talk to it.

The conversation (app/assistant.py) keeps its history in Anthropic's content
block format. A provider takes that history and returns the next reply in the
same shape, so the conversation never knows who answered:

    provider.create(system, tools, messages, on_event) -> Reply(stop_reason, content)

Both stream. on_event(kind, data), if given, hears the reply as it's written:
("thinking", text), ("text", text) and ("tool", name) as a tool call starts.

Three providers:

- AnthropicProvider: the Anthropic SDK. Talks to Claude directly, or to an
  OpenCode Go model that speaks Anthropic's Messages format (Qwen, MiniMax).
- OpenAIChatProvider: OpenAI's Chat Completions format over plain urllib, for
  the OpenCode Go models that speak it (Kimi, GLM, DeepSeek, MiMo...).
- GlooProvider: the same Chat Completions format against Gloo AI Studio, with
  Gloo's routing (a model, auto routing or a model family), its optional
  `tradition`, and either an API key or OAuth client credentials.

OpenCode Go doesn't translate between formats, so each model has to be called
in its own. discover_models() asks OpenCode Go which models exist and
models.dev which format each one speaks.

Configuration comes from the environment (.env), see .env.example.
"""

from __future__ import annotations

import base64
import itertools
import json
import os
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

DEFAULT_ANTHROPIC_MODEL = "claude-opus-5-5"
DEFAULT_EFFORT = "medium"
OPENCODE_BASE_URL = "https://opencode.ai/zen/go/v1"
MODELS_DEV_URL = "https://models.dev/api.json"
PREFERRED_OPENCODE_MODEL = "kimi-k3"
MAX_TOKENS = 16000
REQUEST_TIMEOUT = 180  # seconds; a long proposal takes a while to write
DISCOVERY_TIMEOUT = 4

GLOO_BASE_URL = "https://platform.ai.gloo.com/ai/v2/guarded"
GLOO_TOKEN_URL = "https://platform.ai.gloo.com/oauth2/token"
GLOO_MODELS_URL = "https://platform.ai.gloo.com/platform/v2/models"  # public, no key
DEFAULT_GLOO_MODEL = "gloo-anthropic-claude-sonnet-5.5"
GLOO_FAMILIES = ("openai", "anthropic", "google", "open source")
GLOO_TRADITIONS = ("evangelical", "catholic", "mainline", "not_faith_specific")

CHAT, MESSAGES, RESPONSES = "chat", "messages", "responses"

# Used when OpenCode Go or models.dev can't be reached. Checked against both on 2026-10-05.
FALLBACK_MODELS = {
    "kimi-k3": CHAT,
    "kimi-k2.6": CHAT,
    "glm-5.3": CHAT,
    "glm-5.2": CHAT,
    "deepseek-v4-pro": CHAT,
    "deepseek-v4-flash": CHAT,
    "mimo-v2.6-pro": CHAT,
    "qwen3.8-max": MESSAGES,
    "qwen3.7-plus": MESSAGES,
    "minimax-m3": MESSAGES,
    "grok-4.5": RESPONSES,
    "gpt-5.6-luna": RESPONSES,
}

# models.dev says which SDK package each model needs; that is its format.
_NPM_FORMATS = {
    "@ai-sdk/openai-compatible": CHAT,
    "@ai-sdk/anthropic": MESSAGES,
    "@ai-sdk/openai": RESPONSES,
}

FORMAT_NAMES = {
    CHAT: "OpenAI Chat",
    MESSAGES: "Anthropic Messages",
    RESPONSES: "OpenAI Responses (not supported)",
}


# OpenCode Go asks clients to name themselves rather than send an HTTP library's name.
USER_AGENT = "holy-sound/0.1"


def session_headers(session_id):
    """OpenCode Go rejects requests without a stable per-conversation session id."""
    return {"x-opencode-session": session_id} if session_id else {}


class AssistantUnavailable(RuntimeError):
    """No API key, or the AI service can't be reached. Message is a sentence."""


class AssistantSetupError(AssistantUnavailable):
    """The key or model settings are wrong: the volunteer has to fix .env."""


@dataclass
class Reply:
    """A model's reply in Anthropic's shape: content is a list of block dicts."""

    stop_reason: str
    content: list
    usage: dict | None = None  # {"input": tokens, "output": tokens}, when the service says


def token_usage(reply):
    """(input, output) tokens one reply cost, from either provider; zeros if not reported.

    Input counts cached prompt tokens too: they were sent, just billed cheaper.
    """
    usage = getattr(reply, "usage", None)
    if usage is None:
        return 0, 0
    if isinstance(usage, dict):
        return usage.get("input") or 0, usage.get("output") or 0
    cached = (getattr(usage, "cache_read_input_tokens", 0) or 0) + \
        (getattr(usage, "cache_creation_input_tokens", 0) or 0)
    return (getattr(usage, "input_tokens", 0) or 0) + cached, getattr(usage, "output_tokens", 0) or 0


def not_set_up(key_var):
    return (f"The AI isn't set up yet. Put {key_var}=... in a .env file next to "
            "README.md and restart Holy Sound.")


def bad_key(key_var):
    return f"The AI key isn't valid. Check {key_var} in the .env file and restart Holy Sound."


BUSY = "The AI service is busy right now. Try again in a minute."
UNREACHABLE = "Couldn't reach the AI service. Check this computer's internet connection."


# -- Anthropic Messages -------------------------------------------------------


class AnthropicProvider:
    """Anthropic's Messages API through the anthropic SDK.

    gateway=False is Claude itself, with every feature Holy Sound uses there
    (prompt caching, effort, the safety fallback). gateway=True is another
    service speaking the same format, so only the plain request goes out.
    """

    def __init__(self, client, model=DEFAULT_ANTHROPIC_MODEL, effort=DEFAULT_EFFORT,
                 key_var="ANTHROPIC_API_KEY", gateway=False):
        self.client = client
        self.model = model
        self.effort = effort
        self.key_var = key_var
        self.gateway = gateway
        self.session_id = None  # set per conversation; OpenCode Go requires it

    def create(self, system, tools, messages, on_event=None):
        import anthropic

        try:
            if self.gateway:
                stream = self.client.messages.stream(
                    model=self.model,
                    max_tokens=MAX_TOKENS,
                    system=system,
                    tools=tools,
                    tool_choice={"type": "auto"},
                    messages=messages,
                    extra_headers=session_headers(self.session_id),
                )
            else:
                stream = self.client.beta.messages.stream(
                    model=self.model,
                    max_tokens=MAX_TOKENS,
                    system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                    tools=tools,
                    tool_choice={"type": "auto", "disable_parallel_tool_use": True},
                    messages=messages,
                    # Thinking is always on; "summarized" lets the page show it.
                    thinking={"type": "adaptive", "display": "summarized"},
                    output_config={"effort": self.effort},
                    # If a safety classifier declines, retry on Anthropic's recommended model.
                    betas=["server-side-fallback-2026-07-01"],
                    fallbacks="default",
                )
            with stream as events:
                for event in events:
                    if on_event is not None:
                        _forward(event, on_event)
                return events.get_final_message()
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as e:
            raise AssistantSetupError(bad_key(self.key_var)) from e
        except TypeError as e:
            # The SDK only notices a missing key when it makes the first request.
            if "authentication" not in str(e):
                raise
            raise AssistantSetupError(not_set_up(self.key_var)) from e
        except anthropic.RateLimitError as e:
            raise AssistantUnavailable(BUSY) from e
        except anthropic.APIConnectionError as e:
            raise AssistantUnavailable(UNREACHABLE) from e
        except anthropic.APIStatusError as e:
            raise AssistantUnavailable(f"The AI service had a problem ({e.status_code}). Try again.") from e
        except anthropic.APIError as e:  # an error event part-way through the stream
            raise AssistantUnavailable("The AI service stopped part-way through. Try again.") from e


def _forward(event, on_event):
    """Pass on the parts of an Anthropic stream event the page shows."""
    if event.type == "content_block_start" and event.content_block.type == "tool_use":
        on_event("tool", event.content_block.name)
    elif event.type == "content_block_delta":
        if event.delta.type == "thinking_delta":
            on_event("thinking", event.delta.thinking)
        elif event.delta.type == "text_delta":
            on_event("text", event.delta.text)


# -- OpenAI Chat Completions --------------------------------------------------


class OpenAIChatProvider:
    """OpenAI's Chat Completions format, stdlib only.

    opener(request, timeout) returns something with .read(), like
    urllib.request.urlopen; tests pass a fake one.
    """

    def __init__(self, base_url, api_key, model, key_var="OPENCODE_API_KEY", opener=None):
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.api_key = api_key
        self.model = model
        self.key_var = key_var
        self.opener = opener or urllib.request.urlopen
        self.session_id = None  # set per conversation; OpenCode Go requires it

    def body(self, system, tools, messages):
        return {
            "model": self.model,
            "max_tokens": MAX_TOKENS,
            "messages": chat_messages(system, messages),
            "tools": [chat_tool(t) for t in tools],
            "tool_choice": "auto",
            "parallel_tool_calls": False,
            "stream": True,
            "stream_options": {"include_usage": True},
        }

    def headers(self):
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream, application/json",
            "User-Agent": USER_AGENT,
            **session_headers(self.session_id),
        }

    def create(self, system, tools, messages, on_event=None):
        try:
            request = urllib.request.Request(
                self.url,
                data=json.dumps(self.body(system, tools, messages)).encode(),
                headers=self.headers(),
                method="POST",
            )
            with self.opener(request, timeout=REQUEST_TIMEOUT) as response:
                data = read_chat(response, on_event)
        except urllib.error.HTTPError as e:
            raise self._http_error(e) from e
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as e:
            raise AssistantUnavailable(UNREACHABLE) from e
        except ValueError as e:  # not JSON
            raise AssistantUnavailable("The AI service sent back something unreadable. Try again.") from e
        return chat_reply(data)

    def _http_error(self, error):
        message = _error_message(error)
        if error.code in (401, 403):
            return AssistantSetupError(bad_key(self.key_var))
        if error.code == 429:
            return AssistantUnavailable(BUSY)
        if error.code >= 500:
            return AssistantUnavailable(
                f"Couldn't reach the AI model right now (the service answered {error.code}). "
                "Try again in a minute.")
        detail = f": {message}" if message else ""
        return AssistantUnavailable(f"The AI service turned that request down ({error.code}){detail}.")


class GlooProvider(OpenAIChatProvider):
    """Gloo AI Studio's Completions V2: OpenAI Chat format plus Gloo's routing.

    model is a Gloo model id, "auto" (Gloo picks per request) or a model family
    ("anthropic", "openai", "google", "open source"). credentials is an API key,
    or a GlooToken for OAuth client credentials.
    """

    def __init__(self, credentials, model, tradition=None, base_url=GLOO_BASE_URL, opener=None):
        token = credentials if isinstance(credentials, GlooToken) else None
        super().__init__(base_url, None if token else credentials, model,
                         key_var=token.key_var if token else "GLOO_API_KEY", opener=opener)
        self.token = token
        self.tradition = tradition

    def body(self, system, tools, messages):
        body = super().body(system, tools, messages)
        del body["parallel_tool_calls"]  # not in Gloo's request schema
        routing = self.model.strip().lower()
        if routing == "auto":
            del body["model"]
            body["auto_routing"] = True
        elif routing in GLOO_FAMILIES:
            del body["model"]
            body["model_family"] = routing
        if self.tradition:
            body["tradition"] = self.tradition
        if self.session_id:
            body["prompt_cache_key"] = self.session_id
        return body

    def headers(self):
        key = self.token.get(self.opener) if self.token else self.api_key
        return {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream, application/json",
            "User-Agent": USER_AGENT,
        }


class GlooToken:
    """An OAuth access token from Gloo's client credentials, fetched again before it expires."""

    key_var = "GLOO_CLIENT_ID and GLOO_CLIENT_SECRET"

    def __init__(self, client_id, client_secret, url=GLOO_TOKEN_URL, clock=None):
        self.client_id = client_id
        self.client_secret = client_secret
        self.url = url
        self.clock = clock or time.monotonic
        self._token, self._expires_at = None, 0.0

    def get(self, opener):
        if self._token and self.clock() < self._expires_at - 60:
            return self._token
        basic = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        request = urllib.request.Request(
            self.url,
            data=b"grant_type=client_credentials&scope=api/access",
            headers={"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded",
                     "User-Agent": USER_AGENT},
            method="POST",
        )
        try:
            with opener(request, timeout=DISCOVERY_TIMEOUT * 4) as response:
                data = json.loads(response.read())
            self._token = data["access_token"]
        except urllib.error.HTTPError as e:
            if e.code in (400, 401, 403):
                raise AssistantSetupError(bad_key(self.key_var)) from e
            raise AssistantUnavailable(UNREACHABLE) from e
        except (OSError, ValueError, KeyError, TypeError) as e:
            raise AssistantUnavailable(UNREACHABLE) from e
        self._expires_at = self.clock() + float(data.get("expires_in") or 3600)
        return self._token


def _error_message(error):
    """The message in an error body, OpenAI-shaped or Anthropic-shaped."""
    try:
        data = json.loads(error.read() or b"{}")
    except (ValueError, OSError):
        return ""
    inner = data.get("error") if isinstance(data, dict) else None
    if isinstance(inner, dict):
        return str(inner.get("message") or "").strip().rstrip(".")
    if isinstance(inner, str):
        return inner.strip().rstrip(".")
    return str(data.get("message") or "").strip().rstrip(".") if isinstance(data, dict) else ""


def chat_tool(tool):
    """An Anthropic tool definition as an OpenAI function tool."""
    return {"type": "function", "function": {
        "name": tool["name"],
        "description": tool.get("description", ""),
        "parameters": tool["input_schema"],
    }}


def chat_messages(system, messages):
    """Anthropic-shaped history as Chat Completions messages."""
    out = [{"role": "system", "content": system}]
    for message in messages:
        content = message["content"]
        if isinstance(content, str):
            out.append({"role": message["role"], "content": content})
            continue
        if message["role"] == "assistant":
            out.append(_chat_assistant(content))
            continue
        texts = []
        for block in content:
            if block.get("type") == "tool_result":
                text = _result_text(block.get("content"))
                if block.get("is_error"):
                    text = "Error: " + text
                out.append({"role": "tool", "tool_call_id": block["tool_use_id"], "content": text})
            elif block.get("type") == "text":
                texts.append(block["text"])
        if texts:
            out.append({"role": "user", "content": "\n\n".join(texts)})
    return out


def _chat_assistant(blocks):
    texts, calls, reasoning = [], [], []
    for block in blocks:
        kind = block.get("type")
        if kind == "text":
            texts.append(block["text"])
        elif kind == "thinking":
            reasoning.append(block.get("thinking", ""))
        elif kind == "tool_use":
            args = block["input"]
            calls.append({"id": block["id"], "type": "function", "function": {
                "name": block["name"],
                # Input that wasn't JSON is kept as the string the model sent.
                "arguments": args if isinstance(args, str) else json.dumps(args),
            }})
    message = {"role": "assistant", "content": "\n\n".join(texts) if texts or not calls else None}
    if calls:
        message["tool_calls"] = calls
    if reasoning:
        # Thinking models (Kimi, DeepSeek) want their reasoning back alongside tool calls.
        message["reasoning_content"] = "\n\n".join(reasoning)
    return message


def _result_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict))
    return ""


_FINISH_REASONS = {
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "stop": "end_turn",
    "length": "max_tokens",
    "content_filter": "refusal",
}


def chat_reply(data):
    """A Chat Completions response as an Anthropic-shaped Reply."""
    try:
        choice = data["choices"][0]
        message = choice.get("message") or {}
    except (KeyError, IndexError, TypeError) as e:
        raise AssistantUnavailable("The AI service sent back an empty answer. Try again.") from e
    content = []
    if message.get("reasoning_content"):
        content.append({"type": "thinking", "thinking": message["reasoning_content"]})
    if message.get("content"):
        content.append({"type": "text", "text": message["content"]})
    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        raw = function.get("arguments") or "{}"
        try:
            args = json.loads(raw) if isinstance(raw, str) else raw
        except ValueError:
            args = raw  # the conversation answers it with an error and asks again
        content.append({"type": "tool_use", "id": call.get("id") or f"call_{len(content)}",
                        "name": function.get("name", ""), "input": args})
    stop = _FINISH_REASONS.get(choice.get("finish_reason"), "end_turn")
    if any(b["type"] == "tool_use" for b in content):
        stop = "tool_use"  # some models say "stop" even when they called a tool
    usage = data.get("usage") or {}
    tokens = {"input": usage.get("prompt_tokens") or 0, "output": usage.get("completion_tokens") or 0}
    return Reply(stop_reason=stop, content=content, usage=tokens if usage else None)


def read_chat(response, on_event=None):
    """A Chat Completions response as one completion dict, streamed or not.

    Some services ignore "stream": true and answer with plain JSON; take either.
    """
    first = response.readline()
    while first and not first.strip():
        first = response.readline()
    if first.lstrip().startswith(b"{"):
        return json.loads(first + response.read())
    return chat_stream(itertools.chain([first], response), on_event)


def chat_stream(lines, on_event=None):
    """Server-sent Chat Completions chunks, put back together as one completion."""
    emit = on_event or (lambda kind, data: None)
    texts, reasoning, calls, finish, usage = [], [], {}, None, None
    for raw in lines:
        line = raw.decode("utf-8", "replace").strip()
        if not line.startswith("data:"):
            continue  # comments, event names, keep-alives
        payload = line[5:].strip()
        if payload == "[DONE]":
            break
        chunk = json.loads(payload)
        if chunk.get("error"):
            message = chunk["error"].get("message") if isinstance(chunk["error"], dict) else chunk["error"]
            raise AssistantUnavailable(f"The AI service stopped part-way through: {message}. Try again.")
        usage = chunk.get("usage") or usage  # the last chunk, when asked for
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            if delta.get("reasoning_content"):
                reasoning.append(delta["reasoning_content"])
                emit("thinking", delta["reasoning_content"])
            if delta.get("content"):
                texts.append(delta["content"])
                emit("text", delta["content"])
            for part in delta.get("tool_calls") or []:
                call = calls.setdefault(part.get("index", len(calls)), {"id": None, "name": "", "arguments": ""})
                function = part.get("function") or {}
                if part.get("id"):
                    call["id"] = part["id"]
                if function.get("name"):
                    if not call["name"]:
                        emit("tool", function["name"])
                    call["name"] += function["name"]
                call["arguments"] += function.get("arguments") or ""
            finish = choice.get("finish_reason") or finish
    message = {"role": "assistant", "content": "".join(texts) or None}
    if reasoning:
        message["reasoning_content"] = "".join(reasoning)
    if calls:
        message["tool_calls"] = [
            {"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": c["arguments"]}}
            for _i, c in sorted(calls.items())
        ]
    out = {"choices": [{"index": 0, "message": message, "finish_reason": finish}]}
    if usage:
        out["usage"] = usage
    return out


# -- model discovery -----------------------------------------------------------

_discovered = None


def discover_models(base_url=OPENCODE_BASE_URL, opener=None, refresh=False):
    """{model id: format} for every model OpenCode Go offers, plus whether the
    list is live (True) or the built-in fallback (False). Cached per process."""
    global _discovered
    if _discovered is not None and not refresh:
        return _discovered
    opener = opener or urllib.request.urlopen
    try:
        listed = _get_json(base_url.rstrip("/") + "/models", opener)
        ids = [m["id"] for m in listed["data"] if m.get("id")]
    except (OSError, ValueError, KeyError, TypeError):
        _discovered = (dict(FALLBACK_MODELS), False)
        return _discovered
    try:
        formats = models_dev_formats(_get_json(MODELS_DEV_URL, opener))
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        formats = {}
    _discovered = ({i: formats.get(i) or guess_format(i) for i in ids}, True)
    return _discovered


_gloo_catalog = None


def gloo_models(opener=None, refresh=False):
    """({model id: catalog entry} for Gloo's chat models, live?). Cached per process."""
    global _gloo_catalog
    if _gloo_catalog is not None and not refresh:
        return _gloo_catalog
    try:
        listed = _get_json(GLOO_MODELS_URL, opener or urllib.request.urlopen)["data"]
        models = {m["id"]: m for m in listed if m.get("id") and m.get("supports_streaming")}
        _gloo_catalog = (models, True)
    except (OSError, ValueError, KeyError, TypeError):
        _gloo_catalog = ({}, False)
    return _gloo_catalog


def gloo_models_text(env, opener=None):
    models, live = gloo_models(opener)
    chosen = env.get("HOLYSOUND_MODEL") or DEFAULT_GLOO_MODEL
    if not live:
        return "Couldn't reach Gloo AI Studio's model list. Check this computer's internet connection."
    usable = {i: m for i, m in models.items() if m.get("supports_tools") and not m.get("is_deprecated")}
    width = max(len(i) for i in usable)
    lines = ["Gloo AI Studio models that can drive Holy Sound (tool calling):"]
    for model_id, m in usable.items():
        mark = "  <- in use" if model_id == chosen else ""
        lines.append(f"  {model_id.ljust(width)}  {m.get('family') or ''}{mark}")
    lines.append("")
    lines.append("Pick one with HOLYSOUND_MODEL=... in .env, or HOLYSOUND_MODEL=auto to let Gloo choose "
                 "per message, or a family: anthropic, openai, google, open source.")
    return "\n".join(lines)


def _get_json(url, opener):
    request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "holy-sound"})
    with opener(request, timeout=DISCOVERY_TIMEOUT) as response:
        return json.loads(response.read())


def models_dev_formats(catalog):
    """{model id: format} from models.dev's api.json, provider "opencode-go"."""
    go = catalog["opencode-go"]
    default = _NPM_FORMATS.get(go.get("npm"), CHAT)
    out = {}
    for model_id, model in go.get("models", {}).items():
        npm = (model.get("provider") or {}).get("npm")
        out[model_id] = _NPM_FORMATS.get(npm, default)
    return out


def guess_format(model_id):
    """For a model models.dev doesn't know yet, by family name."""
    if model_id in FALLBACK_MODELS:
        return FALLBACK_MODELS[model_id]
    if model_id.startswith(("grok-", "muse-spark")) or (model_id.startswith("gpt-") and "luna" in model_id):
        return RESPONSES
    if model_id.startswith("minimax-") or model_id.startswith(("qwen3.7-plus", "qwen3.8-")):
        return MESSAGES
    return CHAT


def default_model(models):
    """kimi-k3 if offered, else the first model that speaks Chat Completions."""
    if PREFERRED_OPENCODE_MODEL in models:
        return PREFERRED_OPENCODE_MODEL
    return next((m for m, f in models.items() if f == CHAT), PREFERRED_OPENCODE_MODEL)


def list_models_text(env=None, opener=None):
    """What `python -m app --list-models` prints."""
    env = os.environ if env is None else env
    if chosen_provider_name(env) == "gloo":
        return gloo_models_text(env, opener)
    base_url = env.get("HOLYSOUND_BASE_URL") or OPENCODE_BASE_URL
    models, live = discover_models(base_url, opener)
    using_go = chosen_provider_name(env) in ("opencode-go", "opencode")
    chosen = (env.get("HOLYSOUND_MODEL") if using_go else None) or default_model(models)
    lines = ["OpenCode Go models:" if live else
             "Couldn't reach OpenCode Go, so this is the built-in list (it may be out of date):"]
    width = max(len(m) for m in models)
    for model_id, fmt in models.items():
        mark = ("  <- in use" if using_go else "  <- default") if model_id == chosen else ""
        lines.append(f"  {model_id.ljust(width)}  {FORMAT_NAMES[fmt]}{mark}")
    lines.append("")
    lines.append("Pick one with HOLYSOUND_MODEL=... in .env. Responses-only models won't work.")
    if not using_go:
        lines.append("Holy Sound is using Anthropic right now; set HOLYSOUND_PROVIDER=opencode-go to switch.")
    return "\n".join(lines)


# -- choosing a provider ---------------------------------------------------------


def chosen_provider_name(env=None):
    env = os.environ if env is None else env
    choice = (env.get("HOLYSOUND_PROVIDER") or "").strip().lower()
    if choice:
        return choice
    if env.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    if env.get("OPENCODE_API_KEY"):
        return "opencode-go"
    if env.get("GLOO_API_KEY") or env.get("GLOO_CLIENT_ID"):
        return "gloo"
    return "anthropic"


def provider_from_env(env=None, opener=None):
    """The provider .env asks for. Raises AssistantUnavailable with a sentence."""
    env = os.environ if env is None else env
    name = chosen_provider_name(env)
    if name == "anthropic":
        return _anthropic_provider(env)
    if name in ("opencode-go", "opencode"):
        return _opencode_provider(env, opener)
    if name in ("gloo", "gloo-ai"):
        return _gloo_provider(env, opener)
    raise AssistantSetupError(
        f"HOLYSOUND_PROVIDER in .env should be anthropic, opencode-go or gloo, not \"{name}\".")


def _anthropic_provider(env):
    import anthropic

    try:
        client = anthropic.Anthropic(api_key=env.get("ANTHROPIC_API_KEY") or None)
    except anthropic.AnthropicError as e:
        raise AssistantSetupError(not_set_up("ANTHROPIC_API_KEY")) from e
    return AnthropicProvider(
        client,
        model=env.get("HOLYSOUND_MODEL") or DEFAULT_ANTHROPIC_MODEL,
        effort=env.get("HOLYSOUND_EFFORT") or DEFAULT_EFFORT,
    )


def _opencode_provider(env, opener=None):
    key = env.get("OPENCODE_API_KEY")
    if not key:
        raise AssistantSetupError(not_set_up("OPENCODE_API_KEY"))
    base_url = (env.get("HOLYSOUND_BASE_URL") or OPENCODE_BASE_URL).rstrip("/")
    models, live = discover_models(base_url, opener)
    model = env.get("HOLYSOUND_MODEL") or default_model(models)
    if live and model not in models:
        raise AssistantSetupError(
            f"OpenCode Go doesn't have a model called \"{model}\". Run "
            "`uv run python -m app --list-models` to see the ones it has.")
    fmt = models.get(model) or guess_format(model)
    if fmt == RESPONSES:
        raise AssistantSetupError(
            f"{model} only works with OpenAI's Responses format, which Holy Sound doesn't support. "
            "Pick another model with HOLYSOUND_MODEL in .env (`uv run python -m app --list-models` "
            "shows them).")
    if fmt == MESSAGES:
        import anthropic

        # The SDK adds /v1/messages itself.
        root = base_url[:-3] if base_url.endswith("/v1") else base_url
        client = anthropic.Anthropic(base_url=root, api_key=key, default_headers={"User-Agent": USER_AGENT})
        return AnthropicProvider(client, model=model, key_var="OPENCODE_API_KEY", gateway=True)
    return OpenAIChatProvider(base_url, key, model, opener=opener)


def _gloo_provider(env, opener=None):
    key = env.get("GLOO_API_KEY")
    client_id, secret = env.get("GLOO_CLIENT_ID"), env.get("GLOO_CLIENT_SECRET")
    if key:
        credentials = key
    elif client_id and secret:
        credentials = GlooToken(client_id, secret)
    else:
        raise AssistantSetupError(not_set_up("GLOO_API_KEY"))
    model = env.get("HOLYSOUND_MODEL") or DEFAULT_GLOO_MODEL
    if model.strip().lower() not in ("auto", *GLOO_FAMILIES):
        models, live = gloo_models(opener)
        if live and model not in models:
            raise AssistantSetupError(
                f"Gloo AI Studio doesn't have a model called \"{model}\". Run "
                "`uv run python -m app --list-models` to see the ones it has.")
        if live and not models[model].get("supports_tools"):
            raise AssistantSetupError(
                f"{model} can't call tools, which Holy Sound needs to make changes. Pick another with "
                "HOLYSOUND_MODEL in .env (`uv run python -m app --list-models` shows them).")
    tradition = (env.get("GLOO_TRADITION") or "").strip().lower() or None
    if tradition and tradition not in GLOO_TRADITIONS:
        raise AssistantSetupError(
            f"GLOO_TRADITION in .env should be one of {', '.join(GLOO_TRADITIONS)}, not \"{tradition}\".")
    return GlooProvider(credentials, model, tradition=tradition,
                        base_url=(env.get("HOLYSOUND_BASE_URL") or GLOO_BASE_URL), opener=opener)
