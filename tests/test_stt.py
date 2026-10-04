"""STT tests: mock mode, backend fallback, chunk splitting (no models)."""

import sys
import wave
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.ai import stt  # noqa: E402
from app.ai.audio import split_chunks  # noqa: E402
from app.ai.stt import AudioBackendUnavailable, STTResult  # noqa: E402


@pytest.fixture
def real_env(monkeypatch):
    """Ambient env hygiene: mock off, backend chosen per test."""
    monkeypatch.delenv("MUNSHI_MOCK_AI", raising=False)
    monkeypatch.delenv("MUNSHI_STT_BACKEND", raising=False)
    monkeypatch.delenv("MUNSHI_STT_LANG", raising=False)
    return monkeypatch


def _wav_stub(monkeypatch):
    """Skip ffmpeg: pretend conversion/split already happened."""
    monkeypatch.setattr(stt, "to_wav16k", lambda src, dst=None: Path(str(src)))
    monkeypatch.setattr(stt, "split_chunks", lambda wav, n=28: [Path(str(wav))])


# -- mock mode -----------------------------------------------------------------

def test_mock_returns_canned_text_by_filename(monkeypatch):
    monkeypatch.setenv("MUNSHI_MOCK_AI", "1")
    res = stt.transcribe("order1.webm")
    assert isinstance(res, STTResult)
    assert (res.backend, res.seconds) == ("mock", 0.0)
    assert "cheeni" in res.text


def test_mock_unknown_filename_gets_default(monkeypatch):
    monkeypatch.setenv("MUNSHI_MOCK_AI", "1")
    assert stt.transcribe("whatever.ogg").text == stt.MOCK_TEXTS["default"]


# -- fallback -------------------------------------------------------------------

def test_fallback_mlx_to_gemma(real_env):
    _wav_stub(real_env)
    real_env.setenv("MUNSHI_STT_BACKEND", "mlx_whisper")

    def _boom(wav, terms, lang):
        raise AudioBackendUnavailable("mlx down")
    real_env.setattr(stt, "_transcribe_mlx", _boom)
    real_env.setattr(stt, "_transcribe_gemma",
                     lambda wav, terms, lang: ("gemma sun raha hai", "hi"))
    res = stt.transcribe("note.webm")
    assert res.backend == "gemma_audio" and "gemma" in res.text


def test_fallback_gemma_to_mlx(real_env):
    _wav_stub(real_env)
    real_env.setenv("MUNSHI_STT_BACKEND", "gemma_audio")
    real_env.setattr(stt, "_transcribe_gemma",
                     lambda wav, terms, lang: (_ for _ in ()).throw(
                         AudioBackendUnavailable("ollama down")))
    real_env.setattr(stt, "_transcribe_mlx",
                     lambda wav, terms, lang: ("mlx text", "hi"))
    res = stt.transcribe("note.webm")
    assert res.backend == "mlx_whisper"


def test_both_backends_down_raises(real_env):
    _wav_stub(real_env)

    def _boom(wav, terms, lang):
        raise AudioBackendUnavailable("down")
    real_env.setattr(stt, "_transcribe_mlx", _boom)
    real_env.setattr(stt, "_transcribe_gemma", _boom)
    with pytest.raises(AudioBackendUnavailable):
        stt.transcribe("note.webm")


def test_unknown_backend_name_rejected(real_env):
    _wav_stub(real_env)
    real_env.setenv("MUNSHI_STT_BACKEND", "whisper_cpp")
    with pytest.raises(AudioBackendUnavailable):
        stt.transcribe("note.webm")


# -- chunk splitting (stdlib wave only, no ffmpeg) -------------------------------

def _silent_wav(path: Path, seconds: float) -> Path:
    frames = int(16000 * seconds)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00" * frames * 2)
    return path


def test_long_clip_splits_into_bounded_chunks(tmp_path):
    wav = _silent_wav(tmp_path / "long.wav", 65)
    chunks = split_chunks(wav, 28)
    assert len(chunks) == 3  # 28 + 28 + 9
    from app.ai.audio import wav_duration
    assert all(wav_duration(c) <= 28 for c in chunks)


def test_short_clip_returns_itself(tmp_path):
    wav = _silent_wav(tmp_path / "short.wav", 5)
    assert split_chunks(wav, 28) == [wav]
