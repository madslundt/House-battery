"""Regression coverage for coordinator safety and planning boundaries."""

from __future__ import annotations

import asyncio
import sys
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components"))

from house_battery.const import (
    CONF_BATTERY_CHARGE_POWER,
    CONF_BATTERY_DISCHARGE_POWER,
    CONF_GRID_IMPORT_POWER,
    CONF_LOAD_POWER,
    CONF_MAX_SOC_CONTROL,
    CONF_OPERATING_MODE,
    CONF_SOC,
)
from house_battery.local_tcp import LocalProtocolError
from house_battery.models import Action, PriceSlot
from test_house_battery_cutoff_gate import coordinator


@pytest.mark.parametrize("operation", ["disable", "force_safe"])
def test_failed_safe_is_retried_without_rearming_control(operation):
    async def scenario():
        coord, client, hass, _ = await coordinator()
        original = client.async_set_grid_idle
        attempts = 0

        async def fail_once(minimum, maximum):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise LocalProtocolError("temporary stop-command failure")
            await original(minimum, maximum)

        client.async_set_grid_idle = fail_once
        if operation == "disable":
            await coord.async_set_execution_enabled(False)
        else:
            await coord.async_force_safe()

        assert not coord.runtime.execution_enabled
        assert coord.runtime.pending_safe
        assert attempts == 1
        result = await coord._async_update_data()
        assert attempts == 2
        assert not coord.runtime.pending_safe
        assert not result["execution_enabled"]
        assert hass.states.get("switch.grid_input").state == "on"
        assert all(call[:2] != ("mode", "Discharge") for call in client.calls)

    asyncio.run(scenario())


@pytest.mark.parametrize("reading", [None, "unavailable", "unknown", "nan", "inf"])
def test_unavailable_configured_meter_inhibits_discharge(reading):
    async def scenario():
        coord, client, hass, _ = await coordinator()
        coord.entry.data[CONF_GRID_IMPORT_POWER] = "sensor.ct_meter"
        if reading is not None:
            hass.states.async_set("sensor.ct_meter", reading)
        status = await coord._async_update_data()
        assert not status["healthy"]
        assert status["export_detected"] is None
        assert any("grid_import_power_entity" in p for p in status["health_problems"])
        assert all(call[:2] != ("mode", "Discharge") for call in client.calls)

    asyncio.run(scenario())


def test_manual_charge_uses_configured_ceiling(monkeypatch):
    import house_battery.coordinator as module

    now = datetime(2026, 10, 29, 19, tzinfo=UTC)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz) if tz else now.replace(tzinfo=None)

    async def scenario():
        coord, client, hass, _ = await coordinator(override="charge", now=now)
        client.soc = 90
        coord.runtime.opportunistic_charging_enabled = True
        hass.states.async_set("sensor.prices", ".1", {"prices": [
            {"start": (now + timedelta(hours=i)).isoformat(),
             "end": (now + timedelta(hours=i + 1)).isoformat(), "price": price}
            for i, price in enumerate([.1, 5, 5, 5])
        ]})
        status = await coord._async_update_data()
        assert ("limits", 20, 90) in client.calls
        assert not any(call[0] == "limits" and call[2] > 90 for call in client.calls)
        assert status["effective_target_soc"] == 90

    monkeypatch.setattr(module, "datetime", Clock)
    asyncio.run(scenario())


def test_planning_does_not_block_home_assistant_loop():
    async def scenario():
        coord, _, hass, _ = await coordinator()
        original = coord._optimize_with_storage_policy
        release = threading.Event()
        thread_ids = []

        def slow_plan(*args, **kwargs):
            thread_ids.append(threading.get_ident())
            assert release.wait(.5), "Home Assistant's timer was blocked by planning"
            return original(*args, **kwargs)

        coord._optimize_with_storage_policy = slow_plan
        timer = hass.loop.call_later(.01, release.set)
        try:
            await coord._async_update_data()
        finally:
            timer.cancel()
        assert thread_ids and thread_ids[0] != threading.get_ident()

    asyncio.run(scenario())


def test_force_safe_remains_final_when_a_refresh_is_in_flight():
    async def scenario():
        coord, client, _, _ = await coordinator()
        entered = asyncio.Event()
        release = asyncio.Event()
        original = client.async_set_limits

        async def pause_limits(minimum, maximum):
            await original(minimum, maximum)
            entered.set()
            await release.wait()

        client.async_set_limits = pause_limits
        update = asyncio.create_task(coord._async_update_data())
        await entered.wait()
        stop = asyncio.create_task(coord.async_force_safe())
        await asyncio.sleep(0)
        assert not coord.runtime.execution_enabled
        release.set()
        status, _ = await asyncio.gather(update, stop)
        assert status["system_state"] != "ACTIVE"
        assert not status["execution_enabled"]
        assert not any(call[:2] == ("mode", "Discharge") for call in client.calls)
        assert client.calls[-1] == ("grid_idle", 20, 90)
        assert not coord.runtime.pending_safe

    asyncio.run(scenario())


def test_setting_change_during_planning_discards_old_economic_command():
    async def scenario():
        coord, client, hass, _ = await coordinator()
        started = asyncio.Event()
        release = threading.Event()
        original = coord._optimize_with_storage_policy

        def pause_plan(*args, **kwargs):
            hass.loop.call_soon_threadsafe(started.set)
            assert release.wait(1)
            return original(*args, **kwargs)

        coord._optimize_with_storage_policy = pause_plan
        update = asyncio.create_task(coord._async_update_data())
        await started.wait()
        await coord.async_set_setting("reserve_soc", 60)
        release.set()
        status = await update
        assert status["system_state"] != "ACTIVE"
        assert not any(call[:2] == ("mode", "Discharge") for call in client.calls)
        assert client.calls[-1] == ("grid_idle", 60, 90)

    asyncio.run(scenario())


def test_stale_numeric_inputs_do_not_create_observed_evidence(monkeypatch):
    import house_battery.coordinator as module

    clock = [datetime.now(UTC) + timedelta(minutes=10)]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            current = clock[0]
            return current.astimezone(tz) if tz else current.replace(tzinfo=None)

    async def scenario():
        coord, _, hass, _ = await coordinator()
        coord.local_client = None
        for key, entity, value in [
            (CONF_SOC, "sensor.soc", "60"),
            (CONF_LOAD_POWER, "sensor.load", "1000"),
            (CONF_GRID_IMPORT_POWER, "sensor.grid", "0"),
            (CONF_BATTERY_CHARGE_POWER, "sensor.charge", "0"),
            (CONF_BATTERY_DISCHARGE_POWER, "sensor.discharge", "1000"),
            (CONF_OPERATING_MODE, "select.mode", "Discharge"),
        ]:
            coord.entry.data[key] = entity
            hass.states.async_set(entity, value)

        async def safe_command(*args, **kwargs):
            return True, "safe stop confirmed"

        coord.actuator.async_command = safe_command
        for _ in range(16):
            status = await coord._async_update_data()
            assert not status["healthy"]
            assert status["soc"] is None
            assert coord.power_flow is None
            clock[0] += timedelta(minutes=1)
        assert coord.runtime.ledger.total_discharge_kwh == 0
        assert coord.runtime.ledger.total_net_savings_dkk == 0
        assert not coord.runtime.load_learner.as_dict()["buckets"]

    monkeypatch.setattr(module, "datetime", Clock)
    asyncio.run(scenario())


@pytest.mark.parametrize("interruption", ["unload", "outage", "export"])
def test_planning_cannot_issue_discharge_after_lifecycle_or_grid_change(interruption):
    async def scenario():
        coord, client, hass, _ = await coordinator()
        if interruption == "export":
            coord.entry.data[CONF_GRID_IMPORT_POWER] = "sensor.ct_meter"
            hass.states.async_set("sensor.ct_meter", "0")
        started = asyncio.Event()
        release = threading.Event()
        original = coord._optimize_with_storage_policy

        def pause_plan(*args, **kwargs):
            hass.loop.call_soon_threadsafe(started.set)
            assert release.wait(1)
            return original(*args, **kwargs)

        coord._optimize_with_storage_policy = pause_plan
        update = asyncio.create_task(coord._async_update_data())
        await started.wait()
        if interruption == "unload":
            unload = asyncio.create_task(coord.async_prepare_unload())
            await asyncio.sleep(0)
            assert not unload.done()
        elif interruption == "outage":
            hass.states.async_set("binary_sensor.grid", "off")
        else:
            hass.states.async_set("sensor.ct_meter", "-200")
        release.set()
        await update
        if interruption == "unload":
            await unload
        assert not any(call[:2] == ("mode", "Discharge") for call in client.calls)
        assert hass.states.get("switch.grid_input").state == "on"
        if interruption == "export":
            assert coord.runtime.export_safety_fault_latched

    asyncio.run(scenario())


@pytest.mark.parametrize("held_save", [1, 2])
def test_safe_request_waiting_on_storage_cannot_write_after_unload(held_save):
    async def scenario():
        coord, client, _, _ = await coordinator()
        entered = asyncio.Event()
        release = asyncio.Event()
        original = coord.store.save
        calls = 0

        async def pause_save(runtime):
            nonlocal calls
            calls += 1
            if calls == held_save:
                entered.set()
                await release.wait()
            await original(runtime)

        coord.store.save = pause_save
        stop = asyncio.create_task(coord.async_force_safe())
        await entered.wait()
        await coord.async_prepare_unload()
        completed_commands = list(client.calls)
        release.set()
        await stop
        assert completed_commands == [("grid_idle", 20, 90)]
        assert client.calls == completed_commands

    asyncio.run(scenario())


def test_legacy_soc_reaching_reserve_during_planning_cuts_discharge():
    async def scenario():
        coord, _, hass, _ = await coordinator()
        coord.local_client = None
        for key, entity, value in [
            (CONF_SOC, "sensor.soc", "50"),
            (CONF_LOAD_POWER, "sensor.load", "250"),
            (CONF_GRID_IMPORT_POWER, "sensor.grid", "0"),
            (CONF_BATTERY_CHARGE_POWER, "sensor.charge", "0"),
            (CONF_BATTERY_DISCHARGE_POWER, "sensor.discharge", "200"),
            (CONF_OPERATING_MODE, "select.mode", "Discharge"),
        ]:
            coord.entry.data[key] = entity
            hass.states.async_set(entity, value)
        commands = []

        async def capture_command(action, *args, **kwargs):
            commands.append(action)
            return True, "command acknowledged"

        coord.actuator.async_command = capture_command
        started = asyncio.Event()
        release = threading.Event()
        original = coord._optimize_with_storage_policy

        def pause_plan(*args, **kwargs):
            hass.loop.call_soon_threadsafe(started.set)
            assert release.wait(1)
            return original(*args, **kwargs)

        coord._optimize_with_storage_policy = pause_plan
        update = asyncio.create_task(coord._async_update_data())
        await started.wait()
        hass.states.async_set("sensor.soc", "20")
        release.set()
        status = await update
        assert commands == [Action.GRID]
        assert status["executable_slot"]["action"] == "grid"
        assert coord.runtime.daily_plan.slot_at(datetime.now(UTC)).action is Action.GRID

    asyncio.run(scenario())


@pytest.mark.parametrize("native_minimum,expected_target", [(0, 30), (50, 50)])
def test_partial_charge_respects_legacy_native_bounds_and_step(native_minimum, expected_target):
    async def scenario():
        coord, _, hass, _ = await coordinator()
        coord.local_client = None
        coord.entry.data[CONF_MAX_SOC_CONTROL] = "number.native_max_soc"
        hass.states.async_set("number.native_max_soc", "90", {
            "min": native_minimum, "max": 100, "step": 10,
        })
        now = datetime.now(UTC)
        slots = [PriceSlot(
            now + timedelta(hours=i), now + timedelta(hours=i + 1), price,
            expected_load_wh=load,
        ) for i, (price, load) in enumerate([(.1, 0), (5, 100), (0, 0)])]
        decision = coord._optimize_with_storage_policy(
            slots, now=now, soc=20, settings=coord._settings(),
            current_action=Action.GRID,
        )
        assert decision.plan.slots[0].action is Action.CHARGE
        assert decision.plan.slots[0].charge_target_soc == expected_target
        assert all(
            slot.charge_target_soc >= native_minimum
            and (slot.charge_target_soc - native_minimum) % 10 == 0
            for slot in decision.plan.slots if slot.action is Action.CHARGE
        )

    asyncio.run(scenario())
