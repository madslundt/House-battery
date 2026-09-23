"""Tests for persistent optimizer runtime state."""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components"))

from house_battery.dailyplan import DailyPlan
from house_battery.models import Action, PlannedSlot
from house_battery.runtime import RuntimeState


def _daily_plan() -> DailyPlan:
    return DailyPlan(
        date="2026-09-20",
        slots=(
            PlannedSlot(
                start=datetime(2026, 9, 20, 0, tzinfo=UTC),
                end=datetime(2026, 9, 20, 6, tzinfo=UTC),
                action=Action.GRID,
                price=0.1,
                expected_load_wh=100,
                grid_import_wh=100,
                battery_charge_wh=0,
                battery_discharge_wh=0,
                soc_start=100.0,
                soc_end=100.0,
                interval_cost_dkk=1.0,
                baseline_cost_dkk=1.0,
                reason="grid",
            ),
        ),
        created_at=datetime(2026, 9, 20, 5, tzinfo=UTC),
    )


def test_runtime_persists_the_daily_plan_across_a_restart() -> None:
    """Requirement #12: the persisted daily plan survives serialization so a
    restart at 16:00 must not cause the plan to start at 16:00."""
    runtime = RuntimeState.from_dict({"daily_plan": _daily_plan().as_dict()})
    assert runtime.daily_plan.date == "2026-09-20"
    assert runtime.daily_plan.slots[0].action is Action.GRID
    # Round-trips cleanly through the persistence layer.
    assert runtime.as_dict()["daily_plan"]["date"] == "2026-09-20"


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
