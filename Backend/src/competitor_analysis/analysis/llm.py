"""
Provider-agnostic chat client for the analysis layer.

One neutral conversation format, translated per provider:

    {"role": "user", "content": "..."}
    {"role": "assistant", "content": "...", "tool_calls": [{"id", "name", "args"}]}
    {"role": "tool", "tool_call_id": "...", "name": "...", "content": "<json>"}

and one neutral tool spec: {"name", "description", "parameters": JSON Schema}.

Provider selection (env):
    CHAT_LLM_PROVIDER   databricks | anthropic | gemini   (default: the first
                        one configured, in that order)
    CHAT_LLM_MODEL      overrides the provider's default model

Databricks: Model Serving endpoints speak the OpenAI chat-completions
protocol at {DATABRICKS_HOST}/serving-endpoints, so the `openai` SDK is the
client; the "model" is the serving endpoint's name (DATABRICKS_MODEL).
Auth is a service principal via OAuth machine-to-machine: DATABRICKS_CLIENT_ID
and DATABRICKS_CLIENT_SECRET are exchanged at {host}/oidc/v1/token for a
short-lived access token, cached and refreshed before it expires.
"""
import json
import os
import random
import threading
import time
import uuid
from dataclasses import dataclass, field

import httpx

DEFAULT_MODELS = {"databricks": "databricks-claude-sonnet-4-5", "anthropic": "claude-sonnet-5-5",
                  # flash-lite, not flash: in testing, flash spent ~3 min "thinking" on the first
                  # tool step and then hit 503 high-demand errors; flash-lite answered in ~18s.
                  "gemini": "gemini-flash-lite-latest"}
MAX_TOKENS = 4096


class LLMUnavailable(RuntimeError):
    """No provider is configured (missing key/SDK) - shown to the user as-is."""


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict


@dataclass
class Turn:
    text: str
    tool_calls: list = field(default_factory=list)
    # Provider-native assistant content, replayed verbatim on the next call
    # within the same request (Gemini needs its thought signatures back).
    raw: object = None


def _retry(fn, attempts=4):
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 - provider SDKs raise their own types
            status = getattr(e, "status_code", None) or getattr(e, "code", None)
            transient = status in (429, 500, 502, 503, 504, 529) or "overloaded" in str(e).lower() \
                or "RESOURCE_EXHAUSTED" in str(e) or "UNAVAILABLE" in str(e)
            if not transient or i == attempts - 1:
                raise
            time.sleep(min(30, 2 ** i + random.random()))


# ---------------------------------------------------------------------------
# Databricks (OpenAI-compatible Model Serving)
# ---------------------------------------------------------------------------

def openai_messages(system, messages):
    """Neutral conversation -> OpenAI chat-completions messages."""
    out = [{"role": "system", "content": system}]
    for m in messages:
        if m["role"] == "user":
            out.append({"role": "user", "content": m["content"]})
        elif m["role"] == "assistant":
            msg = {"role": "assistant", "content": m.get("content") or None}
            if m.get("tool_calls"):
                msg["tool_calls"] = [{"id": c["id"], "type": "function",
                                      "function": {"name": c["name"], "arguments": json.dumps(c["args"])}}
                                     for c in m["tool_calls"]]
            out.append(msg)
        elif m["role"] == "tool":
            out.append({"role": "tool", "tool_call_id": m["tool_call_id"], "content": m["content"]})
    return out


def accumulate_stream(chunks, on_text=None):
    """Fold streamed chat-completion chunks into a Turn. Tool-call arguments
    arrive as string fragments keyed by index, so they are concatenated
    before parsing."""
    text, calls = [], {}
    for chunk in chunks:
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta
        if delta is None:
            continue
        if delta.content:
            text.append(delta.content)
            if on_text:
                on_text(delta.content)
        for tc in delta.tool_calls or []:
            slot = calls.setdefault(tc.index if tc.index is not None else len(calls),
                                    {"id": None, "name": "", "args": ""})
            if tc.id:
                slot["id"] = tc.id
            if tc.function is not None:
                slot["name"] += tc.function.name or ""
                slot["args"] += tc.function.arguments or ""
    out = []
    for i in sorted(calls):
        c = calls[i]
        try:
            args = json.loads(c["args"]) if c["args"].strip() else {}
        except json.JSONDecodeError:
            args = {}
        out.append(ToolCall(c["id"] or f"call_{uuid.uuid4().hex[:10]}", c["name"], args))
    return Turn("".join(text), out)


class DatabricksOAuth:
    """OAuth M2M token for a Databricks service principal. One instance per
    (host, client_id), shared across requests; refreshes a minute before the
    token's expiry (they live ~1 hour)."""

    _instances = {}
    _instances_lock = threading.Lock()

    def __init__(self, host, client_id, client_secret):
        self.url = f"{host}/oidc/v1/token"
        self.auth = (client_id, client_secret)
        self.token, self.expires_at = None, 0.0
        self._lock = threading.Lock()

    @classmethod
    def shared(cls, host, client_id, client_secret):
        with cls._instances_lock:
            inst = cls._instances.get((host, client_id))
            if inst is None or inst.auth[1] != client_secret:
                inst = cls._instances[(host, client_id)] = cls(host, client_id, client_secret)
            return inst

    def get(self):
        with self._lock:
            if self.token is None or time.time() >= self.expires_at - 60:
                resp = httpx.post(self.url, auth=self.auth, timeout=30,
                                  data={"grant_type": "client_credentials", "scope": "all-apis"})
                if resp.status_code != 200:
                    raise LLMUnavailable(f"Databricks OAuth token request failed ({resp.status_code}): "
                                         f"{resp.text[:200]}")
                body = resp.json()
                self.token = body["access_token"]
                self.expires_at = time.time() + float(body.get("expires_in", 3600))
            return self.token


def databricks_host():
    host = (os.getenv("DATABRICKS_HOST") or "").strip().rstrip("/")
    if host and not host.startswith("http"):
        host = "https://" + host
    return host


class DatabricksClient:
    name = "databricks"

    def __init__(self, model=None):
        try:
            from openai import OpenAI
        except ImportError as e:
            raise LLMUnavailable("The 'openai' package is not installed (needed for Databricks).") from e
        host = databricks_host()
        client_id, secret = os.getenv("DATABRICKS_CLIENT_ID"), os.getenv("DATABRICKS_CLIENT_SECRET")
        if not (host and client_id and secret):
            raise LLMUnavailable("DATABRICKS_HOST, DATABRICKS_CLIENT_ID and DATABRICKS_CLIENT_SECRET must be set.")
        self.oauth = DatabricksOAuth.shared(host, client_id, secret)
        # The real bearer token is supplied per request (it rotates hourly).
        self.client = OpenAI(api_key="oauth", base_url=f"{host}/serving-endpoints", timeout=120, max_retries=0)
        self.model = model or os.getenv("DATABRICKS_MODEL") or DEFAULT_MODELS["databricks"]

    def complete(self, system, messages, tools, on_text=None):
        kwargs = dict(model=self.model, messages=openai_messages(system, messages), max_tokens=MAX_TOKENS,
                      temperature=0.2)
        if tools:
            kwargs["tools"] = [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                                 "parameters": t["parameters"]}} for t in tools]

        def run():
            client = self.client.with_options(api_key=self.oauth.get())
            return accumulate_stream(client.chat.completions.create(stream=True, **kwargs), on_text)

        return _retry(run)


# ---------------------------------------------------------------------------
# Anthropic
# ---------------------------------------------------------------------------

class AnthropicClient:
    name = "anthropic"

    def __init__(self, model=None):
        try:
            import anthropic
        except ImportError as e:
            raise LLMUnavailable("The 'anthropic' package is not installed.") from e
        if not os.getenv("ANTHROPIC_API_KEY"):
            raise LLMUnavailable("ANTHROPIC_API_KEY is not set.")
        self.client = anthropic.Anthropic()
        self.model = model or DEFAULT_MODELS["anthropic"]

    def _messages(self, messages):
        out = []
        for m in messages:
            if m["role"] == "user":
                out.append({"role": "user", "content": m["content"]})
            elif m["role"] == "assistant":
                if m.get("raw") is not None:
                    out.append({"role": "assistant", "content": m["raw"]})
                    continue
                blocks = [{"type": "text", "text": m["content"]}] if m.get("content") else []
                blocks += [{"type": "tool_use", "id": c["id"], "name": c["name"], "input": c["args"]}
                           for c in m.get("tool_calls", [])]
                out.append({"role": "assistant", "content": blocks})
            elif m["role"] == "tool":
                block = {"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": m["content"]}
                # Consecutive tool results belong in one user turn.
                if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
                    out[-1]["content"].append(block)
                else:
                    out.append({"role": "user", "content": [block]})
        return out

    def complete(self, system, messages, tools, on_text=None):
        kwargs = dict(model=self.model, max_tokens=MAX_TOKENS,
                      system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                      messages=self._messages(messages))
        if tools:
            kwargs["tools"] = [{"name": t["name"], "description": t["description"],
                                "input_schema": t["parameters"]} for t in tools]

        def run():
            with self.client.messages.stream(**kwargs) as stream:
                for text in stream.text_stream:
                    if on_text:
                        on_text(text)
                return stream.get_final_message()

        msg = _retry(run)
        text = "".join(b.text for b in msg.content if b.type == "text")
        calls = [ToolCall(b.id, b.name, dict(b.input or {})) for b in msg.content if b.type == "tool_use"]
        raw = [b.model_dump(exclude_none=True) for b in msg.content]
        return Turn(text, calls, raw)


# ---------------------------------------------------------------------------
# Gemini
# ---------------------------------------------------------------------------

class GeminiClient:
    name = "gemini"

    def __init__(self, model=None):
        try:
            from google import genai
            from google.genai import types
        except ImportError as e:
            raise LLMUnavailable("The 'google-genai' package is not installed.") from e
        if not os.getenv("GEMINI_API_KEY"):
            raise LLMUnavailable("GEMINI_API_KEY is not set.")
        self.types = types
        self.client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
        self.model = model or DEFAULT_MODELS["gemini"]

    def _contents(self, messages):
        T = self.types
        out = []
        for m in messages:
            if m["role"] == "user":
                out.append(T.Content(role="user", parts=[T.Part(text=m["content"])]))
            elif m["role"] == "assistant":
                if m.get("raw") is not None:
                    out.append(m["raw"])
                    continue
                parts = [T.Part(text=m["content"])] if m.get("content") else []
                parts += [T.Part(function_call=T.FunctionCall(name=c["name"], args=c["args"]))
                          for c in m.get("tool_calls", [])]
                out.append(T.Content(role="model", parts=parts))
            elif m["role"] == "tool":
                part = T.Part(function_response=T.FunctionResponse(
                    name=m["name"], response={"result": json.loads(m["content"])}))
                if out and out[-1].role == "user" and out[-1].parts and out[-1].parts[0].function_response:
                    out[-1].parts.append(part)
                else:
                    out.append(T.Content(role="user", parts=[part]))
        return out

    def complete(self, system, messages, tools, on_text=None):
        T = self.types
        config = T.GenerateContentConfig(
            system_instruction=system, temperature=0.2, max_output_tokens=MAX_TOKENS,
            tools=[T.Tool(function_declarations=[
                T.FunctionDeclaration(name=t["name"], description=t["description"],
                                      parameters_json_schema=t["parameters"]) for t in tools])] if tools else None,
            automatic_function_calling=T.AutomaticFunctionCallingConfig(disable=True))
        contents = self._contents(messages)

        def run():
            text, parts = [], []
            for chunk in self.client.models.generate_content_stream(model=self.model, contents=contents,
                                                                     config=config):
                cand = (chunk.candidates or [None])[0]
                for part in (cand.content.parts if cand and cand.content and cand.content.parts else []):
                    parts.append(part)
                    if part.text and not getattr(part, "thought", False):
                        text.append(part.text)
                        if on_text:
                            on_text(part.text)
            return text, parts

        text, parts = _retry(run)
        calls = [ToolCall(f"call_{uuid.uuid4().hex[:10]}", p.function_call.name, dict(p.function_call.args or {}))
                 for p in parts if p.function_call]
        return Turn("".join(text), calls, T.Content(role="model", parts=parts))


# ---------------------------------------------------------------------------

def provider_name():
    p = (os.getenv("CHAT_LLM_PROVIDER") or "").strip().lower()
    if p:
        return p
    if os.getenv("DATABRICKS_HOST") and os.getenv("DATABRICKS_CLIENT_ID") and os.getenv("DATABRICKS_CLIENT_SECRET"):
        return "databricks"
    return "anthropic" if os.getenv("ANTHROPIC_API_KEY") else "gemini"


def get_client(provider=None, model=None):
    provider = (provider or provider_name()).lower()
    model = model or os.getenv("CHAT_LLM_MODEL") or None
    if provider == "databricks":
        return DatabricksClient(model)
    if provider == "anthropic":
        return AnthropicClient(model)
    if provider == "gemini":
        return GeminiClient(model)
    raise LLMUnavailable(f"Unknown CHAT_LLM_PROVIDER {provider!r} (use 'databricks', 'anthropic' or 'gemini').")
