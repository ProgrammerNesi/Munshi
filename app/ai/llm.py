"""Thin Ollama HTTP client for the local Gemma model.

generate_json(prompt, schema): JSON-mode extraction with the "format"
schema option. chat(messages, tools=None): plain/agent chat for later phases.
Every call returns (result, seconds). MUNSHI_MOCK_AI=1 returns canned outputs.
"""

from __future__ import annotations

import copy
import json
import os
import time

import httpx

from app.ai import mock_enabled

TIMEOUT = 60  # seconds per request; one retry on transport errors
NUM_PREDICT_CAP = 4096  # never ask for more tokens than this

# Mock mode payload (tests/UI). Override with set_mock_json().
_MOCK_JSON: dict = {"lines": [], "notes": ""}
_MOCK_CHAT = "Theek hai, order note kar liya."


class LLMUnavailable(Exception):
    """Ollama unreachable, model missing, or HTTP error."""


class LLMBadOutput(Exception):
    """Model replied, but no valid JSON could be found in it."""


def set_mock_json(payload: dict) -> None:
    """Set the canned generate_json output (tests, UI prototyping)."""
    global _MOCK_JSON
    _MOCK_JSON = copy.deepcopy(payload)


def _config() -> dict:
    """Model options from env (read per call so tests can override)."""
    return {
        "model": os.environ.get("MUNSHI_LLM_MODEL", "gemma4:e4b"),
        "num_ctx": int(os.environ.get("MUNSHI_NUM_CTX", "8192")),
        "keep_alive": os.environ.get("MUNSHI_KEEP_ALIVE", "10m"),
        "host": os.environ.get("MUNSHI_LLM_HOST", "http://localhost:11434").rstrip("/"),
    }


def _post(path: str, payload: dict) -> dict:
    """POST to Ollama with one retry. Transport/HTTP trouble -> LLMUnavailable."""
    cfg = _config()
    last: Exception | None = None
    for _ in range(2):  # first try + one retry
        try:
            with httpx.Client(timeout=TIMEOUT) as client:
                resp = client.post(f"{cfg['host']}{path}", json=payload)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 404:
                raise LLMUnavailable(
                    f"Ollama has no such route/model ({resp.status_code})."
                    f" Try: ollama pull {cfg['model']}")
            raise LLMUnavailable(f"Ollama HTTP {resp.status_code}: "
                                 f"{resp.text[:200]}")
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            last = e
    raise LLMUnavailable(
        f"Cannot reach Ollama at {cfg['host']}. Is it running? (ollama serve)."
        f" Last error: {last}") from last


def _extract_json(text: str) -> dict:
    """Parse the final JSON object, ignoring any reasoning/thinking text."""
    text = text.strip()
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    # Reasoning models wrap the answer: take first '{' .. last '}'.
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 < end:
        try:
            obj = json.loads(text[start:end + 1])
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
    raise LLMBadOutput(f"No JSON object found in model output: {text[:200]!r}")


def generate_json(prompt: str, schema: dict,
                  num_predict: int = 1024) -> tuple[dict, float]:
    """Ask the model for JSON matching `schema`. Returns (object, seconds)."""
    if mock_enabled():
        return copy.deepcopy(_MOCK_JSON), 0.0
    cfg = _config()
    started = time.perf_counter()
    body = _post("/api/generate", {
        "model": cfg["model"], "prompt": prompt, "format": schema,
        "stream": False, "keep_alive": cfg["keep_alive"],
        "options": {"temperature": 0, "num_ctx": cfg["num_ctx"],
                    "num_predict": min(num_predict, NUM_PREDICT_CAP)},
    })
    obj = _extract_json(body.get("response", ""))
    return obj, time.perf_counter() - started


def chat(messages: list[dict], tools: list[dict] | None = None) -> tuple[str, float]:
    """Plain chat (tools reserved for the agent loop). Returns (text, seconds)."""
    if mock_enabled():
        return _MOCK_CHAT, 0.0
    msg, seconds = chat_with_tools(messages, tools)
    return msg["content"], seconds


def chat_with_tools(messages: list[dict],
                    tools: list[dict] | None = None) -> tuple[dict, float]:
    """chat() with native Ollama tool calling (used by the agent loop).

    Returns (message, seconds) where message is
    {"content": str, "tool_calls": [{"name": str, "arguments": dict}]}.
    Malformed tool arguments raise LLMBadOutput; transport trouble raises
    LLMUnavailable, exactly like the rest of this module.
    """
    if mock_enabled():
        return {"content": _MOCK_CHAT, "tool_calls": []}, 0.0
    cfg = _config()
    started = time.perf_counter()
    payload: dict = {"model": cfg["model"], "messages": messages,
                     "stream": False, "keep_alive": cfg["keep_alive"],
                     "options": {"temperature": 0, "num_ctx": cfg["num_ctx"]}}
    if tools:
        payload["tools"] = tools
    body = _post("/api/chat", payload)
    raw = body.get("message", {})
    calls = []
    for tc in raw.get("tool_calls", []) or []:
        fn = tc.get("function", {}) if isinstance(tc, dict) else {}
        args = fn.get("arguments", {})
        if isinstance(args, str):  # some models serialise arguments
            try:
                args = json.loads(args)
            except json.JSONDecodeError as e:
                raise LLMBadOutput(
                    f"Tool arguments are not JSON: {args[:120]!r}") from e
        if not isinstance(args, dict):
            raise LLMBadOutput(f"Tool arguments are not an object: {args!r}")
        calls.append({"name": fn.get("name", ""), "arguments": args})
    return {"content": raw.get("content") or "", "tool_calls": calls}, \
        time.perf_counter() - started
