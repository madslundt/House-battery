"""Backup-cutoff evidence does not block normal economic battery operation."""

from __future__ import annotations

import asyncio
import sys
from dataclasses import replace
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
        self.soc = 50

    async def async_snapshot(self):
        if self.unavailable:
            raise LocalProtocolError("battery temporarily unreachable")
        return FbpLocalSnapshot(self.soc, 250, 0, 0, 200, {
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


async def coordinator(*, override="auto", startup=False, now=None):
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
    now = now or datetime.now(UTC)
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
def test_missing_or_malformed_verification_is_not_reported_as_verified(stored):
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
def test_unverified_cutoff_does_not_block_actuator_commands(action):
    async def scenario():
        coord, client, hass, timeline = await coordinator()
        success, result = await coord.actuator.async_command(
            action, datetime.now(UTC), load_w=250,
        )
        assert success, result
        if action is Action.BATTERY:
            assert hass.states.get("switch.grid_input").state == "off"
            assert client.calls == [("limits", 20, 90), ("mode", "Discharge", 200)]
        elif action is Action.CHARGE:
            assert client.calls[-1][:2] == ("mode", "Charge")
        else:
            assert client.calls == [("grid_idle", 20, 90)]
        if action is not Action.BATTERY:
            assert hass.states.get("switch.grid_input").state == "on"
            assert "turn_off" not in timeline
        assert coord.runtime.execution_enabled
        assert not coord.runtime.backup_cutoff_verified

    asyncio.run(scenario())


@pytest.mark.parametrize("override", ["auto", "battery"])
@pytest.mark.parametrize("startup", [False, True])
def test_unverified_cutoff_preserves_override_and_executable_discharge(override, startup):
    async def scenario():
        coord, client, hass, timeline = await coordinator(override=override, startup=startup)
        status = await coord._async_update_data()
        if startup:
            assert hass.states.get("switch.grid_input").state == "on"
            assert "turn_off" not in timeline
            assert client.calls == [("grid_idle", 20, 90)]
        else:
            assert hass.states.get("switch.grid_input").state == "off"
            assert client.calls == [("limits", 20, 90), ("mode", "Discharge", 200)]
        assert coord.runtime.override_action == override
        assert status["backup_cutoff_verified"] is False
        assert status["battery_discharge_blocked"] is False
        assert status["battery_discharge_blocked_reason"] is None
        assert status["execution_enabled"]
        assert status["executable_slot"]["action"] == "battery"

    asyncio.run(scenario())


@pytest.mark.parametrize("native", [False, True])
def test_unverified_manual_battery_request_is_accepted(native):
    async def scenario():
        coord, client, hass, _ = await coordinator()
        if native:
            await coord.async_set_manual_mode(MODE_BATTERY)
            assert client.calls == [("self_consumption",)]
            assert hass.states.get("switch.grid_input").state == "off"
        else:
            await coord.async_set_override_action("battery")
            assert coord.runtime.override_action == "battery"
            await coord._async_update_data()
            assert client.calls == [("limits", 20, 90), ("mode", "Discharge", 200)]
        assert not coord.runtime.backup_cutoff_verified

    asyncio.run(scenario())


@pytest.mark.parametrize("mode", [MODE_GRID, MODE_CHARGE])
def test_unverified_native_grid_and_charge_controls_remain_available(mode):
    async def scenario():
        coord, client, _, timeline = await coordinator()
        await coord.async_set_manual_mode(mode)
        assert client.calls[-1][:2] == ("mode", mode)
        assert "turn_off" not in timeline

    asyncio.run(scenario())


def test_interrupted_telemetry_restores_grid_and_resumes_authorized_discharge():
    async def scenario():
        coord, client, hass, timeline = await coordinator(override="battery")
        client.unavailable = True
        recovering = await coord._async_update_data()
        assert recovering["system_state"] == "RECOVERING"
        assert hass.states.get("switch.grid_input").state == "on"
        assert not client.calls
        client.unavailable = False
        resumed = await coord._async_update_data()
        assert resumed["battery_discharge_blocked"] is False
        assert timeline[-1] == "turn_off"
        assert client.calls == [("limits", 20, 90), ("mode", "Discharge", 200)]
        assert not coord.runtime.backup_cutoff_verified

    asyncio.run(scenario())


def test_failed_grid_restore_still_stops_output_and_reports_failure(monkeypatch):
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
            Action.CHARGE, datetime.now(UTC), load_w=250,
        )
        assert not success
        assert "did not confirm on" in result
        assert client.calls == [("grid_idle", 20, 90)]
        assert timeline == ["turn_on"] * 3
        assert not coord.runtime.backup_cutoff_verified
        assert coord.runtime.execution_enabled

    asyncio.run(scenario())


def test_rearming_control_preserves_cutoff_evidence_and_allows_battery_override():
    async def scenario():
        coord, _, _, _ = await coordinator()
        await coord._async_update_data()
        await coord.async_set_execution_enabled(False)
        await coord.async_set_execution_enabled(True)
        assert coord.runtime.execution_enabled
        assert not coord.runtime.backup_cutoff_verified
        assert coord.store.runtime.as_dict()["backup_cutoff_verified"] is False
        await coord.async_set_override_action("battery")
        assert coord.runtime.override_action == "battery"
        assert not coord.battery_discharge_blocked

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
        assert not status["battery_discharge_blocked"]
        assert not status["backup_cutoff_verified"]

    asyncio.run(scenario())


def test_unverified_cutoff_allows_charging_for_future_expensive_load(monkeypatch):
    now = datetime(2026, 10, 24, 12, 5, tzinfo=UTC)

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz) if tz else now.replace(tzinfo=None)

    monkeypatch.setattr("house_battery.coordinator.datetime", FrozenDatetime)

    async def scenario():
        coord, client, hass, _ = await coordinator(now=now)
        client.soc = 19
        coord.runtime.settings["reserve_soc"] = 10
        coord.runtime.daily_plan = DailyPlan(date=now.date().isoformat())
        start = now.replace(minute=now.minute // 15 * 15, second=0, microsecond=0)
        hass.states.async_set("sensor.prices", ".3", {"prices": [
            {"start": (start + timedelta(minutes=15 * index)).isoformat(),
             "end": (start + timedelta(minutes=15 * (index + 1))).isoformat(),
             "price": price}
            for index, price in enumerate([.3] * 12 + [4.] * 24)
        ]})
        status = await coord._async_update_data()
        assert status["system_state"] == "ACTIVE"
        assert not status["battery_discharge_blocked"]
        assert not status["backup_cutoff_verified"]
        charging = [slot for slot in coord.plan.slots if slot.action is Action.CHARGE]
        assert charging
        assert any(slot.price == pytest.approx(.3) for slot in charging)
        assert any(
            slot.action is Action.BATTERY and slot.price == pytest.approx(4.)
            for slot in coord.plan.slots
        )

    asyncio.run(scenario())


def test_unverified_discharge_still_exits_at_reserve_soc():
    async def scenario():
        coord, client, hass, timeline = await coordinator(override="battery")
        client.soc = 20
        status = await coord._async_update_data()
        assert hass.states.get("switch.grid_input").state == "on"
        assert "turn_off" not in timeline
        assert client.calls == [("grid_idle", 20, 90)]
        assert "reserve SOC protection" in status["command_result"]
        assert not coord.runtime.backup_cutoff_verified

    asyncio.run(scenario())


@pytest.mark.parametrize("override", ["auto", "battery"])
@pytest.mark.parametrize("reserve_soc,soc", [(20, 19), (20, 20), (10, 0), (10, 10)])
def test_reserve_soc_cuts_published_discharge_plan_immediately(
    monkeypatch, override, reserve_soc, soc,
):
    """A physical reserve exit must also end the published battery block."""
    import house_battery.coordinator as coordinator_module

    now = datetime(2026, 10, 29, 19, 5, tzinfo=UTC)
    clock = [now]

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            current = clock[0]
            return current.astimezone(tz) if tz else current.replace(tzinfo=None)

    async def scenario():
        coord, client, hass, _ = await coordinator(override=override, now=now)
        published = coord.runtime.daily_plan.slot_at(now)
        coord.runtime.settings["reserve_soc"] = reserve_soc
        client.soc = soc
        if soc == 0:
            # Empty telemetry is accepted after a confirming frame and must
            # report no active battery output; exercise the real validator.
            snapshot = replace(await client.async_snapshot(), discharge_power_w=0)

            async def empty_snapshot():
                return snapshot

            monkeypatch.setattr(client, "async_snapshot", empty_snapshot)
            coord._local_telemetry_validator.validate(snapshot, now - timedelta(minutes=1))

        status = await coord._async_update_data()

        assert hass.states.get("switch.grid_input").state == "on"
        assert client.calls == [("grid_idle", reserve_soc, 90)]
        assert status["executable_slot"]["action"] == "grid"
        assert coord.runtime.daily_plan.slot_at(now).action is Action.GRID
        stopped = [slot for slot in coord.runtime.daily_plan.slots
                   if slot.start == published.start and slot.action is Action.BATTERY]
        assert len(stopped) == 1
        assert stopped[0].end == now
        assert coord.runtime.decisions[-1]["action"] == "grid"

        clock[0] += timedelta(seconds=30)
        resumed = await coord._async_update_data()

        assert hass.states.get("switch.grid_input").state == "on"
        assert client.calls == [("grid_idle", reserve_soc, 90)] * 2
        assert resumed["executable_slot"]["action"] == "grid"
        assert coord.runtime.daily_plan.slot_at(clock[0]).action is Action.GRID
        assert [slot for slot in coord.runtime.daily_plan.slots
                if slot.start == published.start and slot.action is Action.BATTERY] == stopped
        assert coord.runtime.decisions[-1]["action"] == "grid"

    monkeypatch.setattr(coordinator_module, "datetime", FrozenDatetime)
    asyncio.run(scenario())
