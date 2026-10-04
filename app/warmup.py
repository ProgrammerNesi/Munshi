"""Model pre-warm at startup: STT then LLM, sequentially, in the background.

Transcribes 1s of silence with the configured STT backend (loads Whisper),
then makes one tiny LLM call (loads Gemma; keep_alive comes from env, as do
all llm.py calls). Statuses surface on the role-picker page as
ready / warming / failed. Never raises; never blocks server startup.
"""

from __future__ import annotations

import tempfile
import wave
from pathlib import Path

STATUS: dict[str, str] = {"stt": "warming", "llm": "warming"}
DETAIL: dict[str, str] = {"stt": "", "llm": ""}


def get_status() -> dict[str, str]:
    """Current warmup state for /api/health and the home page."""
    return dict(STATUS)


def _silence_wav() -> Path:
    """1s of 16kHz mono silence (same shape STT expects)."""
    fd, name = tempfile.mkstemp(prefix="munshi_warm_", suffix=".wav")
    import os

    os.close(fd)
    with wave.open(name, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00" * 16000 * 2)
    return Path(name)


async def run() -> None:
    """Warm STT, then LLM. Sequential: one model load at a time."""
    import asyncio

    from app.ai import llm
    from app.ai import stt

    wav = _silence_wav()
    try:
        # to_thread: transcribe/chat block; the event loop must stay free.
        await asyncio.to_thread(stt.transcribe, wav)
        STATUS["stt"] = "ready"
    except Exception as e:  # noqa: BLE001 — any failure is a status, not a crash
        STATUS["stt"] = "failed"
        DETAIL["stt"] = str(e)[:160]
    finally:
        wav.unlink(missing_ok=True)
    try:
        await asyncio.to_thread(
            llm.chat, [{"role": "user", "content": "Reply with: ok"}])
        STATUS["llm"] = "ready"
    except Exception as e:  # noqa: BLE001 — same deal
        STATUS["llm"] = "failed"
        DETAIL["llm"] = str(e)[:160]
