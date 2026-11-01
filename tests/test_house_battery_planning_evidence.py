"""Coordinator evidence capture, persistence and executable held-slot checks."""

import asyncio
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components"))

from house_battery.local_tcp import FbpLocalSnapshot
from house_battery.models import Action, PlannerSettings, PriceSlot
from house_battery.planning_evidence import (
    observe_actual_evidence,
    record_plan_evidence,
)
from house_battery.runtime import RuntimeState

BASE = datetime(2026, 11, 1, tzinfo=UTC)


def test_new_evidence_survives_restart_without_promoting_old_smoothing_errors():
    runtime = RuntimeState()
    observe_actual_evidence(runtime, BASE, load_w=400, price=2, scope="load-a")
    settings = PlannerSettings(2000, 20, 90, 800, 800, .85, .35, .75, .05)
    record_plan_evidence(runtime, BASE, slots=[PriceSlot(BASE, BASE + timedelta(hours=1), 2, 400)],
                         settings=settings, soc=50, current_action=Action.GRID, scope="load-a",
                         forecast_load=lambda start, end: (100, 0))
    restored = RuntimeState.from_dict(runtime.as_dict())
    assert restored.load_forecast_evidence.summary(BASE)["pending"] == 4
    assert restored.replay_history.summary()["snapshot_count"] == 1
    assert restored.load_forecast_evidence.summary(BASE)["mae_w"] is None
    exported = restored.export("battery", {})
    assert exported["replay"]["records"][0]["soc"] == 50
    assert exported["load_forecast_evidence"]["pending"][0]["total_wh"] == 100
    assert RuntimeState.from_dict({"schema_version": 3}).replay_history.summary()["snapshot_count"] == 0


def test_actuals_continue_without_baseline_learning_and_missing_prices_stay_unpriced():
    runtime = RuntimeState()
    for minute in range(16):
        observe_actual_evidence(runtime, BASE + timedelta(minutes=minute),
                                load_w=400, price=None, scope="load-a")
    assert runtime.replay_history.summary()["actual_count"] == 0
    assert runtime.load_learner.observations == 0
    assert runtime.load_forecast_evidence._actual_start == BASE + timedelta(minutes=15)


def test_optional_capture_errors_do_not_prevent_normal_battery_control(monkeypatch):
    import house_battery.coordinator as coordinator_module
    from test_house_battery_cutoff_gate import coordinator

    def broken(*args, **kwargs):
        raise TypeError("bad optional analytics")

    async def scenario():
        coord, _client, hass, _ = await coordinator()
        status = await coord._async_update_data()
        assert status["executable_slot"]["action"] == "battery"
        assert hass.states.get("switch.grid_input").state == "off"
        assert status["planning_evidence_error"] == "TypeError: bad optional analytics"

    monkeypatch.setattr(coordinator_module, "observe_actual_evidence", broken)
    monkeypatch.setattr(coordinator_module, "record_plan_evidence", broken)
    asyncio.run(scenario())


@pytest.mark.parametrize("load", [25, 50, 1000])
def test_infeasible_direct_discharge_does_not_survive_in_held_published_slot(monkeypatch, load):
    import house_battery.coordinator as coordinator_module
    from test_house_battery_cutoff_gate import coordinator

    now = BASE + timedelta(minutes=1)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz) if tz else now.replace(tzinfo=None)

    async def scenario():
        coord, client, hass, _ = await coordinator(now=now)

        async def snapshot():
            return FbpLocalSnapshot(50, load, 0, 0, 200,
                                    {"Storage_list": [{"OffGridLoadPower": load}]})

        client.async_snapshot = snapshot
        coord.runtime.load_learner.recent_w.extend([400] * 8)
        coord.runtime.load_learner.recent_at = now.isoformat()
        status = await coord._async_update_data()
        assert status["executable_slot"]["action"] != "battery"
        assert hass.states.get("switch.grid_input").state == "on"
        assert not any(call[:2] == ("mode", "Discharge") for call in client.calls)

    monkeypatch.setattr(coordinator_module, "datetime", Clock)
    asyncio.run(scenario())
