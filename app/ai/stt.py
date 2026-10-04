"""Speech-to-text with two local backends and one fallback.

Backend from MUNSHI_STT_BACKEND: "mlx_whisper" (default), "gemma_audio", "mock".
Long clips are split into 28s chunks, transcribed separately and joined.
If the chosen backend fails, we try the other real backend once and report
whichever one actually produced the text. MUNSHI_MOCK_AI=1 forces mock mode.
"""

from __future__ import annotations

import inspect
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from app.ai import mock_enabled
from app.ai.audio import split_chunks, to_wav16k

log = logging.getLogger(__name__)

CHUNK_SECONDS = 28
OLLAMA_TIMEOUT = 180  # audio uploads/transcription can be slow

# Mock mode: canned transcript per filename stem (tests + UI work offline).
MOCK_TEXTS = {
    "order1": "bhaiya do kilo cheeni aur paanch kilo atta bhej dena",
    "order2": "आधा किलो हल्दी और दो लीटर सरसों तेल",
    "default": "do kilo cheeni aur paanch kilo atta",
}


class AudioBackendUnavailable(Exception):
    """Both real STT backends failed (or the only one tried did)."""


@dataclass
class STTResult:
    """One transcription: text, detected/requested language, winning backend."""

    text: str
    language: str
    backend: str
    seconds: float


def _mock_result(audio_path: str | Path) -> STTResult:
    """Canned text keyed by filename stem; unknown files get the default."""
    stem = Path(audio_path).stem
    return STTResult(
        text=MOCK_TEXTS.get(stem, MOCK_TEXTS["default"]),
        language="hi",
        backend="mock",
        seconds=0.0,
    )


def _transcribe_mlx(wav: Path, catalog_terms: list[str] | None,
                    language: str) -> tuple[str, str]:
    """mlx-whisper backend. Import is lazy: Apple-Silicon-only dep."""
    try:
        import mlx_whisper
    except ImportError:
        raise AudioBackendUnavailable(
            "mlx_whisper is not installed. Install it with:"
            " pip install mlx-whisper (Apple Silicon only)"
        )
    model = os.environ.get(
        "MUNSHI_STT_MODEL", "mlx-community/whisper-large-v3-turbo")
    kwargs: dict = {}
    if language and language != "auto":
        kwargs["language"] = language
    if catalog_terms:
        # Vocabulary hint; older versions lack the flag, so check first.
        try:
            supported = "initial_prompt" in inspect.signature(
                mlx_whisper.transcribe).parameters
        except (TypeError, ValueError):
            supported = False
        if supported:
            kwargs["initial_prompt"] = ", ".join(catalog_terms)
        else:
            log.warning("mlx_whisper has no initial_prompt support; skipping hint")
    try:
        out = mlx_whisper.transcribe(str(wav), path_or_hf_repo=model, **kwargs)
    except Exception as e:
        raise AudioBackendUnavailable(f"mlx_whisper failed: {e}") from e
    text = out.get("text", "") if isinstance(out, dict) else str(out)
    return text.strip(), out.get("language", language) if isinstance(out, dict) else language


def _ollama_host() -> str:
    """Ollama base URL (localhost default; override only for dev)."""
    return os.environ.get("MUNSHI_LLM_HOST", "http://localhost:11434").rstrip("/")


def _transcribe_gemma(wav: Path, catalog_terms: list[str] | None,
                      language: str) -> tuple[str, str]:
    """Gemma audio via Ollama: transcriptions route, else chat-with-audio."""
    model = os.environ.get("MUNSHI_LLM_MODEL", "gemma4:e4b")
    host = _ollama_host()
    raw = wav.read_bytes()
    try:
        with httpx.Client(timeout=OLLAMA_TIMEOUT) as client:
            resp = client.post(
                f"{host}/v1/audio/transcriptions",
                files={"file": (wav.name, raw, "audio/wav")},
                data={"model": model},
            )
        if resp.status_code == 200:
            return resp.json().get("text", "").strip(), language
        if resp.status_code not in (400, 404):
            raise AudioBackendUnavailable(
                f"gemma_audio transcriptions route failed:"
                f" HTTP {resp.status_code}")
        # Route missing (older Ollama): chat with the WAV as input audio.
        import base64

        prompt = ("Transcribe this audio exactly as spoken. Keep Hindi words"
                  " in Devanagari and English words in Latin script.")
        with httpx.Client(timeout=OLLAMA_TIMEOUT) as client:
            chat = client.post(
                f"{host}/api/chat",
                json={"model": model, "stream": False,
                      "messages": [{"role": "user", "content": prompt,
                                    "images": [base64.b64encode(raw).decode()]}]},
            )
        if chat.status_code != 200:
            raise AudioBackendUnavailable(
                f"gemma_audio chat fallback failed: HTTP {chat.status_code}")
        return chat.json()["message"]["content"].strip(), language
    except httpx.ConnectError as e:
        raise AudioBackendUnavailable(
            f"Cannot reach Ollama at {host}. Is it running? (ollama serve)") from e


_BACKENDS = {"mlx_whisper": _transcribe_mlx, "gemma_audio": _transcribe_gemma}


def transcribe(audio_path: str | Path,
               catalog_terms: list[str] | None = None) -> STTResult:
    """Voice note -> STTResult. Falls back once; reports the backend that ran."""
    if mock_enabled() or os.environ.get("MUNSHI_STT_BACKEND") == "mock":
        return _mock_result(audio_path)

    language = os.environ.get("MUNSHI_STT_LANG", "auto")
    primary = os.environ.get("MUNSHI_STT_BACKEND", "mlx_whisper")
    if primary not in _BACKENDS:
        raise AudioBackendUnavailable(
            f"Unknown MUNSHI_STT_BACKEND={primary!r} (mlx_whisper|gemma_audio|mock)")
    order = [primary] + [b for b in ("mlx_whisper", "gemma_audio") if b != primary]

    started = time.perf_counter()
    wav = to_wav16k(audio_path)  # may raise AudioError (ffmpeg/file) — no fallback
    chunks = split_chunks(wav, CHUNK_SECONDS)
    last_error: Exception | None = None
    for name in order:
        # globals() lookup (not the _BACKENDS dict): monkeypatch-friendly.
        backend_fn = globals()["_transcribe_mlx" if name == "mlx_whisper"
                               else "_transcribe_gemma"]
        try:
            texts, langs = [], []
            for chunk in chunks:
                text, lang = backend_fn(chunk, catalog_terms, language)
                texts.append(text)
                langs.append(lang)
            return STTResult(text=" ".join(t for t in texts if t).strip(),
                             language=langs[0] if langs else language,
                             backend=name,
                             seconds=time.perf_counter() - started)
        except AudioBackendUnavailable as e:
            last_error = e
            log.warning("STT backend %s failed, trying fallback: %s", name, e)
    raise AudioBackendUnavailable(
        f"No STT backend worked (tried {order}). Last error: {last_error}")
