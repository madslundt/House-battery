"""Tests for persistent optimizer runtime state."""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from homeassistant.core import HomeAssistant

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components"))

from house_battery.accounting import EnergyLedger, IntervalAccumulator
from house_battery.actual_history import ActualDailyHistory
from house_battery.coordinator import Fbp1200Coordinator
from house_battery.dailyplan import DailyPlan
from house_battery.forecast import ForecastAccuracy
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


def test_runtime_persists_opportunistic_charging_opt_in() -> None:
    runtime = RuntimeState(opportunistic_charging_enabled=True)

    restored = RuntimeState.from_dict(runtime.as_dict())

    assert restored.opportunistic_charging_enabled


def test_pending_safe_retry_survives_restart_with_automatic_control_disabled() -> None:
    runtime = RuntimeState(execution_enabled=False, pending_safe=True)

    restored = RuntimeState.from_dict(runtime.as_dict())

    assert restored.pending_safe
    assert not restored.execution_enabled
    assert not RuntimeState.from_dict({}).pending_safe


def test_recent_action_changes_remain_diagnostics_only() -> None:
    first = datetime(2026, 9, 21, 16, tzinfo=UTC)
    runtime = RuntimeState(
        transitions=[
            (first + timedelta(minutes=minute)).isoformat() for minute in range(4)
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
            "battery_learner": {
                "capacity_samples_wh": [100.0],
                "efficiency_samples": [],
            },
            "ledger": {"total_discharge_kwh": 3.5, "total_export_kwh": 0.1},
            "flow_model_started_at": "2026-09-22T00:19:00+00:00",
        }
    )
    assert runtime.battery_learner.capacity_samples_wh == [100.0]
    assert runtime.ledger.total_discharge_kwh == 3.5
    assert runtime.ledger.total_export_kwh == 0.1
    assert runtime.flow_model_started_at == "2026-09-22T00:19:00+00:00"
    # The marker is re-emitted so a second load is treated as v2, not legacy.
    assert runtime.as_dict()["schema_version"] == 3
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


def test_runtime_prunes_by_age_after_offline_gap_and_keeps_lifetime_totals() -> None:
    now = datetime(2026, 10, 22, 10, tzinfo=UTC)
    ledger = EnergyLedger(total_discharge_kwh=12, total_discharge_value_dkk=20)
    ledger.close(IntervalAccumulator(now - timedelta(days=70)), 0)
    ledger.close(IntervalAccumulator(now - timedelta(days=2)), 0)
    runtime = RuntimeState(
        ledger=ledger,
        decisions=[
            {"timestamp": (now - timedelta(days=8)).isoformat()},
            {"timestamp": now.isoformat()},
            {"timestamp": "invalid"},
            {"timestamp": "2026-10-22T09:00:00"},
        ],
        scheduled_loads=[
            {"end": (now - timedelta(hours=1)).isoformat()},
            {"end": (now + timedelta(hours=1)).isoformat()},
        ],
        forecast_accuracies={"kept": ForecastAccuracy(), "removed": ForecastAccuracy()},
    )
    runtime.prune(now, forecast_sources={"kept"})

    assert len(runtime.ledger.intervals) == 1
    assert runtime.ledger.total_discharge_kwh == 12
    assert runtime.ledger.total_discharge_value_dkk == 20
    assert runtime.decisions == [{"timestamp": now.isoformat()}]
    assert len(runtime.scheduled_loads) == 1
    assert set(runtime.forecast_accuracies) == {"kept"}
    # A long outage must expire all detail even if no new ledger interval closed.
    runtime.prune(now + timedelta(days=90))
    assert not runtime.ledger.intervals
    assert runtime.ledger.total_discharge_kwh == 12


def test_runtime_history_retention_uses_local_calendar_date() -> None:
    runtime = RuntimeState(
        actual_history=ActualDailyHistory(
            days={
                "2026-10-14": [],
                "2026-10-15": [],
                "2026-10-22": [],
            }
        )
    )
    runtime.load_learner.bucket_timezone = "Europe/Copenhagen"
    # Copenhagen is already October 22 while UTC is still October 21.
    runtime.prune(datetime(2026, 10, 21, 22, 30, tzinfo=UTC))
    assert set(runtime.actual_history.days) == {"2026-10-15", "2026-10-22"}


def test_runtime_expires_yesterdays_plan_but_preserves_current_local_day() -> None:
    runtime = RuntimeState(daily_plan=_daily_plan())
    runtime.load_learner.bucket_timezone = "Europe/Copenhagen"
    runtime.prune(datetime(2026, 9, 20, 21, 59, tzinfo=UTC))
    assert runtime.daily_plan.date == "2026-09-20"
    runtime.prune(datetime(2026, 9, 20, 22, 0, tzinfo=UTC))
    assert runtime.daily_plan.date == ""


def test_pending_forecasts_expire_without_erasing_bounded_accuracy_scores() -> None:
    now = datetime(2026, 10, 22, 10, tzinfo=UTC)
    accuracy = ForecastAccuracy(errors_dkk_per_kwh=[0.2], within_uncertainty=[True])
    for name, offset in (("old", -8), ("recent", -1), ("future", 4)):
        start = now + timedelta(days=offset)
        accuracy.outstanding[name] = {
            "start": start.isoformat(),
            "end": (start + timedelta(minutes=15)).isoformat(),
            "price": 1.5,
        }
    accuracy.prune(now)
    assert set(accuracy.outstanding) == {"recent"}
    assert accuracy.mean_absolute_error_dkk_per_kwh == 0.2


def test_coordinator_retires_removed_feeds_but_keeps_configured_unavailable_feeds() -> (
    None
):
    async def check() -> None:
        now = datetime(2026, 10, 22, 10, tzinfo=UTC)
        entry = SimpleNamespace(
            entry_id="retention-test",
            title="Battery",
            options={},
            async_on_unload=lambda callback: None,
            data={
                "price_entities": ["sensor.attached_forecast"],
                "price_forecast_entities": ["sensor.forecast"],
            },
        )
        coordinator = Fbp1200Coordinator(HomeAssistant("/tmp"), entry)
        coordinator.runtime.forecast_accuracies = {
            source: ForecastAccuracy(errors_dkk_per_kwh=[0.2])
            for source in (
                "sensor.attached_forecast",
                "sensor.forecast",
                "sensor.removed",
            )
        }
        # No source currently exists in the state machine (for example startup).
        coordinator._price_slots(now)
        assert set(coordinator.runtime.forecast_accuracies) == {
            "sensor.attached_forecast",
            "sensor.forecast",
        }
        assert coordinator.runtime.forecast_accuracies["sensor.forecast"].samples == 1
        entry.data = {"price_entities": [], "price_forecast_entities": []}
        coordinator._price_slots(now)
        assert coordinator.runtime.forecast_accuracies == {}

    asyncio.run(check())
