"""Smoke test for the local model stack. Prints an exact fix per failure.

Checks: ffmpeg binary, Ollama reachable, MUNSHI_LLM_MODEL tag pulled,
mlx_whisper importable, one-line JSON generation with timing.
With an audio file arg, runs STT on every available backend side by side.

Usage:
    python scripts/smoke_models.py [voice_note.webm]
"""

from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

MODEL = os.environ.get("MUNSHI_LLM_MODEL", "gemma4:e4b")
HOST = os.environ.get("MUNSHI_LLM_HOST", "http://localhost:11434").rstrip("/")

failures = 0


def check(label: str, ok: bool, detail: str = "", fix: str = "") -> None:
    """One PASS/FAIL line; on failure print the exact command that fixes it."""
    global failures
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        failures += 1
        if fix:
            print(f"       FIX: {fix}")


def main() -> int:
    print(f"Ollama host: {HOST} | LLM model: {MODEL}")

    # 1. ffmpeg binary.
    ff = shutil.which("ffmpeg")
    check("ffmpeg binary", bool(ff), ff or "not on PATH",
          "brew install ffmpeg")

    # 2. Ollama reachable + model tag pulled.
    tags: list = []
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.get(f"{HOST}/api/tags")
        resp.raise_for_status()
        tags = [t["name"] for t in resp.json().get("models", [])]
        pulled = MODEL in tags or any(t.startswith(MODEL + ":") for t in tags)
        check("Ollama reachable", True, f"{len(tags)} tags locally")
        check(f"model tag pulled ({MODEL})", pulled,
              "found" if pulled else f"have: {', '.join(tags) or 'none'}",
              f"ollama pull {MODEL}")
    except (httpx.ConnectError, httpx.TimeoutException, httpx.HTTPError) as e:
        check("Ollama reachable", False, str(e)[:100],
              "start it with: ollama serve (then: ollama pull " + MODEL + ")")

    # 3. mlx_whisper import (Apple Silicon only; skipped elsewhere).
    try:
        import mlx_whisper  # noqa: F401
        check("mlx_whisper imports", True, "STT default backend ready")
        mlx_ok = True
    except ImportError as e:
        mlx_ok = False
        check("mlx_whisper imports", False, str(e)[:100],
              "pip install mlx-whisper (Apple Silicon only)")

    # 4. One-line JSON generation test with timing.
    from app.ai import llm

    try:
        started = time.perf_counter()
        obj, seconds = llm.generate_json(
            'Reply with exactly this JSON and nothing else: {"ok": true}',
            {"type": "object", "properties": {"ok": {"type": "boolean"}},
             "required": ["ok"]},
        )
        check("LLM JSON test", obj.get("ok") is True,
              f"{seconds:.1f}s, got {obj}",
              f"ollama pull {MODEL} (or update Ollama if the route 404s)")
    except (llm.LLMUnavailable, llm.LLMBadOutput) as e:
        check("LLM JSON test", False, str(e)[:150],
              f"ollama pull {MODEL} (or update Ollama if the route 404s)")

    # 5. Optional: STT every backend side by side.
    if len(sys.argv) > 1:
        from app.ai import stt
        from app.ai.audio import AudioError, to_wav16k

        src = sys.argv[1]
        try:
            wav = to_wav16k(src)
            print(f"converted: {src} -> {wav}")
            for name in ("mlx_whisper", "gemma_audio", "mock"):
                if name == "mlx_whisper" and not mlx_ok:
                    print(f"[SKIP] mlx_whisper (not installed)")
                    continue
                os.environ["MUNSHI_STT_BACKEND"] = name
                try:
                    res = stt.transcribe(wav)
                    print(f"[{name}] {res.seconds:.1f}s ({res.backend}): {res.text}")
                except Exception as e:
                    print(f"[{name}] FAILED: {e}")
        except AudioError as e:
            check("audio converts", False, str(e), "brew install ffmpeg")

    print("smoke: all checks passed" if failures == 0
          else f"smoke: {failures} check(s) failed — fixes above")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
