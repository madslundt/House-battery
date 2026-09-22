"""Tests for the simplified coordinator (extra-storage removed).

Extra-storage target and spread knobs were removed (2025-09) because they
created wasteful discharge→recharge cycles.  The coordinator now uses a
single plan with target_soc as the hard charge ceiling.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components"))

from house_battery.models import Action, Plan, PlannedSlot


NOW = datetime(2026, 9, 21, 10, tzinfo=UTC)


def _make_plan(slots) -> Plan:
    return Plan(
        created_at=NOW,
        slots=slots,
        expected_cost_dkk=sum(s.interval_cost_dkk for s in slots),
        baseline_cost_dkk=sum(s.baseline_cost_dkk for s in slots),
        expected_savings_dkk=0,
        battery_throughput_kwh=0,
        terminal_price_dkk_per_kwh=1.0,
        reason="test",
    )


def test_plan_today_dict_filters_out_tomorrow() -> None:
    """today_dict should only include slots for today, not tomorrow."""
    today_slot = PlannedSlot(
        start=NOW,
        end=NOW + timedelta(hours=1),
        action=Action.GRID,
        price=1.0,
        expected_load_wh=100,
        grid_import_wh=100,
        battery_charge_wh=0,
        battery_discharge_wh=0,
        soc_start=50,
        soc_end=50,
        interval_cost_dkk=0.1,
        baseline_cost_dkk=0.1,
        reason="today",
    )
    tomorrow_slot = PlannedSlot(
        start=NOW + timedelta(days=1),
        end=NOW + timedelta(days=1, hours=1),
        action=Action.GRID,
        price=1.0,
        expected_load_wh=100,
        grid_import_wh=100,
        battery_charge_wh=0,
        battery_discharge_wh=0,
        soc_start=50,
        soc_end=50,
        interval_cost_dkk=0.1,
        baseline_cost_dkk=0.1,
        reason="tomorrow",
    )
    full_plan = _make_plan((today_slot, tomorrow_slot))
    today_data = full_plan.today_dict(NOW)
    assert len(today_data["slots"]) == 1
    assert today_data["slots"][0]["reason"] == "today"


def test_plan_today_dict_returns_all_when_all_today() -> None:
    """When all slots are for today, today_dict returns all of them."""
    slots = tuple(
        PlannedSlot(
            start=NOW + timedelta(hours=i),
            end=NOW + timedelta(hours=i + 1),
            action=Action.GRID,
            price=1.0,
            expected_load_wh=100,
            grid_import_wh=100,
            battery_charge_wh=0,
            battery_discharge_wh=0,
            soc_start=50,
            soc_end=50,
            interval_cost_dkk=0.1,
            baseline_cost_dkk=0.1,
            reason=f"hour_{i}",
        )
        for i in range(10)
    )
    full_plan = _make_plan(slots)
    today_data = full_plan.today_dict(NOW)
    assert len(today_data["slots"]) == 10
