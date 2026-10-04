"""LLM client tests: mock mode, thinking-text parsing, offline error."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.ai import llm  # noqa: E402


def test_mock_generate_json_and_chat(monkeypatch):
    monkeypatch.setenv("MUNSHI_MOCK_AI", "1")
    llm.set_mock_json({"lines": [{"raw": "x"}], "notes": ""})
    obj, seconds = llm.generate_json("anything", {})
    assert obj["lines"] == [{"raw": "x"}] and seconds == 0.0
    text, seconds = llm.chat([{"role": "user", "content": "hi"}])
    assert isinstance(text, str) and seconds == 0.0


def test_thinking_text_ignored_final_json_parsed():
    out = ('<think>user wants an order, items are cheeni...</think>\n'
           'Here is the answer: {"lines": [], "notes": "kal"} trailing words')
    assert llm._extract_json(out) == {"lines": [], "notes": "kal"}
    assert llm._extract_json('{"a": 1}') == {"a": 1}


def test_garbage_output_raises_bad_output():
    with pytest.raises(llm.LLMBadOutput):
        llm._extract_json("namaste, koi JSON nahi hai yahan")
    with pytest.raises(llm.LLMBadOutput):
        llm._extract_json('["not", "an object"]')


def test_unreachable_ollama_raises_unavailable(monkeypatch):
    monkeypatch.delenv("MUNSHI_MOCK_AI", raising=False)
    monkeypatch.setenv("MUNSHI_LLM_HOST", "http://localhost:9")  # discard port
    with pytest.raises(llm.LLMUnavailable):
        llm.generate_json("hi", {"type": "object"})
