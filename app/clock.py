"""Demo-aware clock. All app time goes through now().

now() = real UTC + an in-memory offset. skip() advances the offset but only
in demo mode (MUNSHI_DEMO=1); every portal shows the offset as a badge.
Tests use skip()/reset() as a fake clock. Never import datetime here
elsewhere in app/ — import this module instead.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

_OFFSET = timedelta(0)


def now() -> datetime:
    """Current time: real UTC plus any demo offset. Always tz-aware."""
    return datetime.now(timezone.utc) + _OFFSET


def offset_minutes() -> int:
    """Whole minutes the demo clock is ahead (0 in normal mode)."""
    return int(_OFFSET.total_seconds() // 60)


def skip(minutes: int) -> int:
    """Advance the demo clock. Refused outside MUNSHI_DEMO=1."""
    global _OFFSET
    if os.environ.get("MUNSHI_DEMO", "0") != "1":
        raise ValueError("Clock skip is demo-mode only (MUNSHI_DEMO=1).")
    if minutes < 0:
        raise ValueError("Cannot skip backwards.")
    _OFFSET += timedelta(minutes=minutes)
    return offset_minutes()


def reset() -> None:
    """Zero the offset (tests, fresh demos)."""
    global _OFFSET
    _OFFSET = timedelta(0)
