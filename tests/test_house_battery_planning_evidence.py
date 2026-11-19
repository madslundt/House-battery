"""Coordinator evidence capture, persistence and executable held-slot checks."""

import asyncio
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.core import HomeAssistant

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components"))

from house_battery import async_setup
from house_battery.dailyplan import DailyPlan
from house_battery.local_tcp import FbpLocalSnapshot
from house_battery.models import Action, PlannerSettings, PriceSlot
from house_battery.planning_evidence import (
    observe_actual_evidence,
    record_plan_evidence,
)
from house_battery.replay_model import evaluate_policies
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


def test_export_service_supports_compact_replay_and_preserves_expanded_default(tmp_path):
    async def scenario():
        hass = HomeAssistant(str(tmp_path))
        runtime = RuntimeState()
        settings = PlannerSettings(2000, 20, 90, 800, 800, .85, .35, .75, .05)
        record_plan_evidence(
            runtime, BASE, slots=[PriceSlot(BASE, BASE + timedelta(hours=1), 2, 400)],
            settings=settings, soc=50, current_action=Action.GRID, scope="load-a",
            forecast_load=lambda start, end: (100, 0),
        )

        def export_data(*, compact_replay=False):
            return runtime.export("battery", {}, compact_replay=compact_replay)

        hass.data["house_battery"] = {"entry": SimpleNamespace(export_data=export_data)}
        assert await async_setup(hass, {})
        expanded = await hass.services.async_call(
            "house_battery", "export_data", {}, blocking=True, return_response=True,
        )
        compact = await hass.services.async_call(
            "house_battery", "export_data", {"compact_replay": True},
            blocking=True, return_response=True,
        )
        assert expanded["replay"]["records"][0]["soc"] == 50
        assert compact["replay"]["encoding"] == "zlib-base64-chunks"
        restored = RuntimeState.from_dict({"replay_history": compact["replay"]})
        assert restored.replay_history.as_dict() == runtime.replay_history.as_dict()

    asyncio.run(scenario())


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


async def hourly_coordinator(monkeypatch):
    import house_battery.coordinator as coordinator_module
    from test_house_battery_cutoff_gate import coordinator

    start = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    clock = [start]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0].astimezone(tz) if tz else clock[0].replace(tzinfo=None)

    monkeypatch.setattr(coordinator_module, "datetime", Clock)
    coord, _, hass, _ = await coordinator(now=start)
    coord.runtime.daily_plan = DailyPlan(date=start.date().isoformat())
    coord._optimize_with_storage_policy = Mock(wraps=coord._optimize_with_storage_policy)
    hass.states.async_set("sensor.prices", "4", {"prices": [
        {"start": (start + timedelta(hours=index)).isoformat(),
         "end": (start + timedelta(hours=index + 1)).isoformat(),
         "price": price}
        for index, price in enumerate([4, 1])
    ]})
    return coord, hass, clock, start


def test_hourly_replay_covers_a_complete_hour_with_one_issued_plan(monkeypatch):
    async def scenario():
        coord, _, clock, start = await hourly_coordinator(monkeypatch)
        for minute in range(61):
            clock[0] = start + timedelta(minutes=minute)
            status = await coord._async_update_data()
            if 0 < minute < 60:
                assert status["economic_plan_reused"] is True
                assert coord.runtime.replay_history.summary()["snapshot_count"] == 1

        assert coord._optimize_with_storage_policy.call_count == 2
        history = coord.runtime.replay_history
        assert history.summary()["actual_count"] == 4
        first = history.as_dict()["records"][0]
        assert first["valid_until"] == clock[0].isoformat()
        assert not first.get("interrupted")
        result = evaluate_policies(history, settlement_price_dkk_per_kwh=1.5)
        assert result["evaluated_hours"] == pytest.approx(1)
        assert result["skipped_hours"] == 0
        assert result["segments"] == 1

    asyncio.run(scenario())


def test_hourly_replay_ends_at_grid_outage_and_resumes_with_a_new_plan(monkeypatch):
    async def scenario():
        coord, hass, clock, start = await hourly_coordinator(monkeypatch)
        outage_at = start + timedelta(minutes=20)
        resumed_at = start + timedelta(minutes=35)
        for minute in range(61):
            clock[0] = start + timedelta(minutes=minute)
            if clock[0] == outage_at:
                hass.states.async_set("binary_sensor.grid", "off")
            elif clock[0] == resumed_at:
                hass.states.async_set("binary_sensor.grid", "on")
            status = await coord._async_update_data()
            if outage_at <= clock[0] < resumed_at:
                assert status["system_state"] == "OUTAGE"

        history = coord.runtime.replay_history
        first, resumed, _ = history.as_dict()["records"]
        assert first["valid_until"] == outage_at.isoformat()
        assert first["interrupted"] is True
        assert resumed["issued_at"] == resumed_at.isoformat()
        result = evaluate_policies(history, settlement_price_dkk_per_kwh=1.5)
        assert result["evaluated_hours"] == pytest.approx(.75)
        assert result["skipped_hours"] == pytest.approx(.25)
        assert result["segments"] == 2

    asyncio.run(scenario())


@pytest.mark.parametrize("interruption", ["override", "failed_command", "grid_fallback"])
def test_hourly_replay_closes_on_interruption_during_a_healthy_tariff(monkeypatch, interruption):
    async def scenario():
        coord, hass, clock, start = await hourly_coordinator(monkeypatch)
        await coord._async_update_data()
        clock[0] = start + timedelta(minutes=20)
        if interruption == "override":
            await coord.async_set_override_action("grid")
        elif interruption == "failed_command":
            coord.actuator.async_command = AsyncMock(return_value=(False, "command failed"))
        else:
            hass.states.async_set("switch.grid_input", "on")
            coord.actuator.async_set_grid_isolation = AsyncMock(return_value=False)
        await coord._async_update_data()
        record, = coord.runtime.replay_history.as_dict()["records"]
        assert record["valid_until"] == clock[0].isoformat()

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
