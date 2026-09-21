"""Tests for persistent optimizer runtime state."""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components"))

from house_battery.runtime import RuntimeState


def test_collapses_legacy_rapid_transition_burst() -> None:
    """Repeated pre-fix commands must not keep direct control locked out."""
    first = datetime(2026, 9, 21, 16, tzinfo=UTC)
    runtime = RuntimeState(
        transitions=[
            (first + timedelta(minutes=minute)).isoformat()
            for minute in range(4)
        ]
    )

    assert runtime.collapse_rapid_transition_burst(
        first + timedelta(minutes=5), maximum_transitions=4
    )
    assert runtime.transitions == [first.isoformat()]
