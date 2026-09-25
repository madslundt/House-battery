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


def test_recent_action_changes_remain_diagnostics_only() -> None:
    first = datetime(2026, 9, 21, 16, tzinfo=UTC)
    runtime = RuntimeState(
        transitions=[
            (first + timedelta(minutes=minute)).isoformat()
            for minute in range(4)
        ]
    )

    assert runtime.transitions_used(first + timedelta(minutes=5)) == 4
    assert len(runtime.transitions) == 4


def test_schema_v2_payload_is_round_tripped_untouched() -> None:
    """A v2 payload preserves battery learning, ledger totals and the stamp."""
    runtime = RuntimeState.from_dict(
        {
            "schema_version": 2,
            "settings": {"reserve_soc": 20.0},
            "battery_learner": {"capacity_samples_wh": [100.0], "efficiency_samples": []},
            "ledger": {"total_discharge_kwh": 3.5, "total_export_kwh": 0.1},
            "flow_model_started_at": "2026-09-22T00:19:00+00:00",
        }
    )
    assert runtime.battery_learner.capacity_samples_wh == [100.0]
    assert runtime.ledger.total_discharge_kwh == 3.5
    assert runtime.ledger.total_export_kwh == 0.1
    assert runtime.flow_model_started_at == "2026-09-22T00:19:00+00:00"
    # The marker is re-emitted so a second load is treated as v2, not legacy.
    assert runtime.as_dict()["schema_version"] == 2
    assert runtime.as_dict()["flow_model_started_at"] == "2026-09-22T00:19:00+00:00"


def test_legacy_payload_resets_batterylearning_and_ledger_only() -> None:
    """A pre-v2 payload (no schema_version) keeps everything but discards the
    battery-learning and flow-accounting evidence built on bad telemetry."""
    runtime = RuntimeState.from_dict(
        {
            "settings": {"reserve_soc": 25.0},
            "scheduled_loads": [{"entity": "sensor.pump", "power_w": 400}],
            "override_action": "battery",
            "forecast_accuracies": {
                "sensor.forecast": {
                    "outstanding": {},
                    "errors_dkk_per_kwh": [],
                    "within_uncertainty": [],
                }
            },
            "load_learner": {"recent_w": [300.0]},
            "battery_learner": {"capacity_samples_wh": [999.0]},
            "ledger": {"total_discharge_kwh": 12.0, "total_export_kwh": 7.0},
        }
    )
    # Reset evidence.
    assert runtime.battery_learner.capacity_samples_wh == []
    assert runtime.ledger.total_discharge_kwh == 0.0
    assert runtime.ledger.total_export_kwh == 0.0
    # Preserved evidence and preferences.
    assert runtime.settings["reserve_soc"] == 25.0
    assert runtime.scheduled_loads == [{"entity": "sensor.pump", "power_w": 400}]
    assert runtime.override_action == "battery"
    assert list(runtime.load_learner.recent_w) == [300.0]
    assert "sensor.forecast" in runtime.forecast_accuracies
    # The upgrade stamps the moment the new model began.
    assert runtime.flow_model_started_at is not None
