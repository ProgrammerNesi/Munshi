"""Shared helpers for the local AI modules (STT, LLM, extraction)."""

from __future__ import annotations

import os


def mock_enabled() -> bool:
    """True when MUNSHI_MOCK_AI=1: canned outputs, no models touched."""
    return os.environ.get("MUNSHI_MOCK_AI") == "1"
