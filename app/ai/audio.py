"""Audio prep: any voice-note format -> 16kHz mono WAV, split long clips.

Uses the ffmpeg binary via subprocess (no python audio deps). Everything
downstream (mlx-whisper, Gemma audio) wants 16kHz mono WAV.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import wave
from pathlib import Path

SAMPLE_RATE = 16000


class AudioError(Exception):
    """ffmpeg missing, unreadable input, or split failure."""


def ffmpeg_path() -> str:
    """ffmpeg binary location, or a clear install hint."""
    found = shutil.which("ffmpeg")
    if not found:
        raise AudioError("ffmpeg not found. Install it with: brew install ffmpeg")
    return found


def wav_duration(wav: str | Path) -> float:
    """Length of a WAV file in seconds (stdlib wave module)."""
    with wave.open(str(wav), "rb") as w:
        return w.getnframes() / float(w.getframerate())


def to_wav16k(src: str | Path, dst: str | Path | None = None) -> Path:
    """Convert anything ffmpeg reads to 16kHz mono WAV. Returns wav path."""
    src = Path(src)
    if not src.is_file():
        raise AudioError(f"Audio file not found: {src}")
    out = Path(dst) if dst else Path(
        tempfile.mkstemp(prefix="munshi_", suffix=".wav")[1]
    )
    try:
        proc = subprocess.run(
            [ffmpeg_path(), "-y", "-v", "error", "-i", str(src),
             "-ac", "1", "-ar", str(SAMPLE_RATE), "-c:a", "pcm_s16le",
             str(out)],
            capture_output=True, text=True, timeout=300,
        )
    except FileNotFoundError:
        raise AudioError("ffmpeg not found. Install it with: brew install ffmpeg")
    if proc.returncode != 0 or not out.is_file():
        raise AudioError(f"ffmpeg could not read {src}: {proc.stderr.strip()[-300:]}")
    return out


def split_chunks(wav: str | Path, max_seconds: float = 28) -> list[Path]:
    """Split a WAV into <= max_seconds pieces. Short clips return [wav]."""
    wav = Path(wav)
    with wave.open(str(wav), "rb") as w:
        params, rate, frames = w.getparams(), w.getframerate(), w.getnframes()
    chunk_frames = int(rate * max_seconds)
    if frames <= chunk_frames:
        return [wav]
    with wave.open(str(wav), "rb") as w:
        raw = w.readframes(frames)
    width = params.sampwidth
    out_dir = Path(tempfile.mkdtemp(prefix="munshi_chunks_"))
    chunks = []
    for i in range(0, frames, chunk_frames):
        piece = out_dir / f"chunk{i // chunk_frames:03d}.wav"
        with wave.open(str(piece), "wb") as w:
            w.setparams(params)
            w.writeframes(raw[i * width:(i + chunk_frames) * width])
        chunks.append(piece)
    return chunks
