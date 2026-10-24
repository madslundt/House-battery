"""Discharge requires independent physical backup-cutoff verification."""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from homeassistant.core import HomeAssistant, callback

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components"))

from house_battery.const import MODE_BATTERY, MODE_CHARGE, MODE_GRID
from house_battery.coordinator import Fbp1200Coordinator
from house_battery.dailyplan import DailyPlan
from house_battery.local_tcp import FbpLocalSnapshot, LocalProtocolError
from house_battery.models import Action, PlannedSlot
from house_battery.runtime import RuntimeState


class Store:
    def __init__(self, runtime):
        self.runtime = runtime

    async def load(self):
        return self.runtime

    async def save(self, runtime):
        self.runtime = runtime


class Client:
    def __init__(self):
        self.calls = []
        self.unavailable = False

    async def async_snapshot(self):
        if self.unavailable:
            raise LocalProtocolError("battery temporarily unreachable")
        return FbpLocalSnapshot(50, 250, 0, 0, 200, {
            "Storage_list": [{"OffGridLoadPower": 250}],
        })

    async def async_read_controls(self):
        return {
            "3000": "1", "3030": "1", "3023": "20", "3024": "90",
            "3003": "1,00:00,23:59,200,0,6,5,0,0,90,20",
        }

    async def async_set_limits(self, minimum, maximum):
        self.calls.append(("limits", minimum, maximum))

    async def async_set_mode(self, mode, power, **kwargs):
        self.calls.append(("mode", mode, power))

    async def async_set_grid_idle(self, minimum, maximum):
        self.calls.append(("grid_idle", minimum, maximum))

    async def async_set_self_consumption(self):
        self.calls.append(("self_consumption",))


async def coordinator(*, override="auto", startup=False):
    hass = HomeAssistant("/tmp")
    hass.config.time_zone = "UTC"
    timeline = []

    @callback
    def switch(call):
        timeline.append(call.service)
        hass.states.async_set(
            call.data["entity_id"], "on" if call.service == "turn_on" else "off"
        )

    hass.services.async_register("switch", "turn_on", switch)
    hass.services.async_register("switch", "turn_off", switch)
    hass.states.async_set("switch.grid_input", "off")
    hass.states.async_set("binary_sensor.grid", "on")
    now = datetime.now(UTC)
    start = now.replace(minute=now.minute // 15 * 15, second=0, microsecond=0)
    hass.states.async_set("sensor.prices", "4", {"prices": [
        {"start": (start + timedelta(minutes=15 * index)).isoformat(),
         "end": (start + timedelta(minutes=15 * (index + 1))).isoformat(),
         "price": price}
        for index, price in enumerate([4, 4, 1, 1])
    ]})
    entry = SimpleNamespace(
        entry_id="cutoff-gate", title="Battery", options={},
        async_on_unload=lambda callback: None,
        data={"host": "192.0.2.1", "commissioned": True,
              "grid_available_entity": "binary_sensor.grid",
              "grid_isolation_switch_entity": "switch.grid_input",
              "price_entities": ["sensor.prices"]},
    )
    coord = Fbp1200Coordinator(hass, entry)
    client = Client()
    coord.local_client = client
    published = PlannedSlot(
        start=start, end=start + timedelta(minutes=15), action=Action.BATTERY,
        price=4, expected_load_wh=62.5, grid_import_wh=0,
        battery_charge_wh=0, battery_discharge_wh=62.5,
        soc_start=70, soc_end=60, interval_cost_dkk=0,
        baseline_cost_dkk=.25, reason="previously published discharge",
    )
    coord.store = Store(RuntimeState.from_dict({
        "schema_version": 3, "execution_enabled": True,
        "override_action": override,
        "daily_plan": DailyPlan(date=now.date().isoformat(), slots=(published,)).as_dict(),
    }))
    await coord.async_initialize()
    coord._startup_guard_passed = not startup

    async def no_refresh():
        pass

    coord.async_request_refresh = no_refresh
    return coord, client, hass, timeline


@pytest.mark.parametrize("stored", [None, False, "true", "false", 1, {}])
def test_missing_or_malformed_verification_cannot_unlock_discharge(stored):
    data = {"execution_enabled": True, "override_action": "battery"}
    if stored is not None:
        data["backup_cutoff_verified"] = stored
    runtime = RuntimeState.from_dict(data)
    assert runtime.backup_cutoff_verified is False
    assert RuntimeState.from_dict(runtime.as_dict()).backup_cutoff_verified is False


def test_explicit_verification_round_trips_without_being_inferred():
    assert RuntimeState().backup_cutoff_verified is False
    verified = RuntimeState(backup_cutoff_verified=True)
    assert RuntimeState.from_dict(verified.as_dict()).backup_cutoff_verified is True


@pytest.mark.parametrize("action", [Action.BATTERY, Action.CHARGE, Action.GRID])
def test_actuator_locks_only_discharge_before_any_grid_isolation(action):
    async def scenario():
        coord, client, hass, timeline = await coordinator()
        success, result = await coord.actuator.async_command(
            action, datetime.now(UTC), load_w=250,
        )
        assert success, result
        assert hass.states.get("switch.grid_input").state == "on"
        assert "turn_off" not in timeline
        if action is Action.CHARGE:
            assert client.calls[-1][:2] == ("mode", "Charge")
        else:
            assert client.calls == [("grid_idle", 20, 90)]
        if action is Action.BATTERY:
            assert "physically verified" in result
        assert coord.runtime.execution_enabled

    asyncio.run(scenario())


@pytest.mark.parametrize("override", ["auto", "battery"])
@pytest.mark.parametrize("startup", [False, True])
def test_persisted_override_and_held_plan_cannot_bypass_cutoff_lock(override, startup):
    async def scenario():
        coord, client, hass, timeline = await coordinator(override=override, startup=startup)
        status = await coord._async_update_data()
        assert hass.states.get("switch.grid_input").state == "on"
        assert "turn_off" not in timeline
        assert client.calls == [("grid_idle", 20, 90)]
        assert status["backup_cutoff_verified"] is False
        assert status["battery_discharge_blocked"] is True
        assert "physically verified" in status["battery_discharge_blocked_reason"]
        assert status["execution_enabled"]
        assert all(slot["action"] != "battery" for slot in status["plan"]["slots"])
        assert status["executable_slot"]["action"] != "battery"
        assert coord.plan.terminal_value_dkk == 0
        historical_discharge = [
            slot for slot in coord.runtime.daily_plan.slots if slot.action is Action.BATTERY
        ]
        assert len(historical_discharge) == 1
        assert historical_discharge[0].end == datetime.fromisoformat(status["last_refresh"])

    asyncio.run(scenario())


@pytest.mark.parametrize("native", [False, True])
def test_unverified_manual_battery_request_is_rejected_before_writes(native):
    async def scenario():
        coord, client, _, timeline = await coordinator()
        with pytest.raises(ValueError, match="physically verified"):
            if native:
                await coord.async_set_manual_mode(MODE_BATTERY)
            else:
                await coord.async_set_override_action("battery")
        assert not client.calls
        assert not timeline
        assert coord.runtime.override_action == "auto"

    asyncio.run(scenario())


@pytest.mark.parametrize("mode", [MODE_GRID, MODE_CHARGE])
def test_unverified_native_grid_and_charge_controls_remain_available(mode):
    async def scenario():
        coord, client, _, timeline = await coordinator()
        await coord.async_set_manual_mode(mode)
        assert client.calls[-1][:2] == ("mode", mode)
        assert "turn_off" not in timeline

    asyncio.run(scenario())


def test_interrupted_telemetry_restores_grid_without_unlocking_on_recovery():
    async def scenario():
        coord, client, hass, timeline = await coordinator(override="battery")
        client.unavailable = True
        recovering = await coord._async_update_data()
        assert recovering["system_state"] == "RECOVERING"
        assert hass.states.get("switch.grid_input").state == "on"
        assert not client.calls
        client.unavailable = False
        resumed = await coord._async_update_data()
        assert resumed["battery_discharge_blocked"]
        assert "turn_off" not in timeline
        assert client.calls == [("grid_idle", 20, 90)]
        assert not coord.runtime.backup_cutoff_verified

    asyncio.run(scenario())


def test_failed_grid_restore_remains_locked_and_reports_failure(monkeypatch):
    monkeypatch.setattr("house_battery.actuator._STATE_CONFIRM_TIMEOUT_SECONDS", .01)
    monkeypatch.setattr("house_battery.actuator._STATE_CONFIRM_POLL_SECONDS", .001)
    monkeypatch.setattr("house_battery.actuator._GRID_SWITCH_RETRY_DELAY_SECONDS", 0)

    async def scenario():
        coord, client, hass, timeline = await coordinator()

        @callback
        def unavailable_switch(call):
            timeline.append(call.service)

        hass.services.async_register("switch", "turn_on", unavailable_switch)
        success, result = await coord.actuator.async_command(
            Action.BATTERY, datetime.now(UTC), load_w=250,
        )
        assert not success
        assert "did not confirm on" in result
        assert client.calls == [("grid_idle", 20, 90)]
        assert timeline == ["turn_on"] * 3
        assert not coord.runtime.backup_cutoff_verified
        assert coord.runtime.execution_enabled

    asyncio.run(scenario())


def test_rearming_automatic_control_never_unlocks_backup_cutoff():
    async def scenario():
        coord, _, _, _ = await coordinator()
        await coord._async_update_data()
        await coord.async_set_execution_enabled(False)
        await coord.async_set_execution_enabled(True)
        assert coord.runtime.execution_enabled
        assert not coord.runtime.backup_cutoff_verified
        assert coord.store.runtime.as_dict()["backup_cutoff_verified"] is False
        with pytest.raises(ValueError, match="physically verified"):
            await coord.async_set_override_action("battery")

    asyncio.run(scenario())


@pytest.mark.parametrize("grid_state", ["on", "unknown"])
def test_first_startup_stops_previous_discharge_even_without_price_plan(grid_state):
    async def scenario():
        coord, client, hass, timeline = await coordinator(startup=True)
        hass.states.async_set("binary_sensor.grid", grid_state)
        hass.states.async_remove("sensor.prices")
        status = await coord._async_update_data()
        assert client.calls == [("grid_idle", 20, 90)]
        assert hass.states.get("switch.grid_input").state == "on"
        assert "turn_off" not in timeline
        assert status["battery_discharge_blocked"]

    asyncio.run(scenario())
