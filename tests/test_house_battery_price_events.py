"""Price-triggered planning keeps independent telemetry and safety checks live."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock

import pytest

from test_house_battery_cutoff_gate import coordinator

import house_battery.coordinator as coordinator_module
from house_battery.models import PriceSlot


async def setup(monkeypatch):
    # Keep HA's real source reports within the freshness window while advancing
    # the coordinator deterministically across quarter-hour boundaries.
    actual = datetime.now(UTC)
    start = actual.replace(minute=actual.minute // 15 * 15, second=0, microsecond=0)
    clock = [start + timedelta(minutes=1)]

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0].astimezone(tz) if tz else clock[0].replace(tzinfo=None)

    monkeypatch.setattr(coordinator_module, "datetime", FrozenDateTime)
    coord, client, hass, timeline = await coordinator(now=clock[0])
    optimizer = Mock(wraps=coord._optimize_with_storage_policy)
    coord._optimize_with_storage_policy = optimizer
    client.load_w = 250
    snapshot = client.async_snapshot

    async def measured_snapshot():
        observed = await snapshot()
        return replace(observed, load_power_w=client.load_w, raw={
            "Storage_list": [{"OffGridLoadPower": client.load_w}],
        })

    client.async_snapshot = measured_snapshot
    return coord, client, hass, timeline, clock, optimizer


def test_minute_poll_reuses_market_plan_but_reads_fresh_soc_and_load(monkeypatch):
    async def scenario():
        coord, client, _, _, clock, optimizer = await setup(monkeypatch)
        initial = await coord._async_update_data()
        created = coord.plan.created_at
        client.soc = 49.8
        client.load_w = 260
        clock[0] += timedelta(minutes=1)

        polled = await coord._async_update_data()

        assert initial["system_state"] == polled["system_state"] == "ACTIVE"
        assert optimizer.call_count == 1
        assert polled["economic_plan_reused"] is True
        assert coord.plan.created_at == created
        assert polled["soc"] == 49.8
        assert polled["load_power_w"] == 260

    asyncio.run(scenario())


def test_adjacent_equal_price_quarters_still_replan_at_tariff_boundary(monkeypatch):
    async def scenario():
        coord, _, _, _, clock, optimizer = await setup(monkeypatch)
        await coord._async_update_data()
        first = coord.plan.slots[0]
        clock[0] = first.end

        boundary = await coord._async_update_data()

        assert optimizer.call_count == 2
        assert boundary["economic_plan_reused"] is False
        assert coord.plan.slots[0].price == first.price
        assert coord.plan.slots[0].start == first.end

    asyncio.run(scenario())


def test_new_future_price_replans_inside_current_tariff(monkeypatch):
    async def scenario():
        coord, _, hass, _, clock, optimizer = await setup(monkeypatch)
        await coord._async_update_data()
        state = hass.states.get("sensor.prices")
        rows = [dict(row) for row in state.attributes["prices"]]
        rows[-1]["price"] = 9
        hass.states.async_set("sensor.prices", state.state, {"prices": rows})
        clock[0] += timedelta(minutes=1)

        revised = await coord._async_update_data()

        assert optimizer.call_count == 2
        assert revised["economic_plan_reused"] is False
        assert coord.plan.slots[-1].price == 9

    asyncio.run(scenario())


def test_forecast_revision_replans_and_rolling_horizon_does_not(monkeypatch):
    async def scenario():
        coord, _, hass, _, clock, optimizer = await setup(monkeypatch)
        coord.entry.data["price_forecast_entities"] = ["sensor.forecast"]
        coord.runtime.forecast_enabled = True
        last = datetime.fromisoformat(hass.states.get("sensor.prices").attributes["prices"][-1]["end"])
        rows = [
            {"start": (last + timedelta(hours=i)).isoformat(),
             "end": (last + timedelta(hours=i + 1)).isoformat(), "price": 2}
            for i in range(80)
        ]
        hass.states.async_set("sensor.forecast", "ready", {"prices": rows})
        await coord._async_update_data()
        assert coord._forecast_used_slot_count > 0
        clock[0] += timedelta(minutes=1)
        unchanged = await coord._async_update_data()
        assert optimizer.call_count == 1
        assert unchanged["economic_plan_reused"] is True

        rows[0] = {**rows[0], "price": 5}
        hass.states.async_set("sensor.forecast", "ready", {"prices": rows})
        clock[0] += timedelta(minutes=1)
        revised = await coord._async_update_data()
        assert optimizer.call_count == 2
        assert revised["economic_plan_reused"] is False

    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["setting", "scheduled_load", "override"])
def test_operator_change_replans_inside_current_tariff(monkeypatch, change):
    async def scenario():
        coord, _, _, _, clock, optimizer = await setup(monkeypatch)
        await coord._async_update_data()
        clock[0] += timedelta(minutes=1)
        if change == "setting":
            await coord.async_set_setting("minimum_profit_dkk_per_kwh", .5)
        elif change == "scheduled_load":
            await coord.async_add_scheduled_load(
                clock[0] + timedelta(minutes=1), clock[0] + timedelta(minutes=5),
                100, "appliance",
            )
        else:
            await coord.async_set_override_action("grid")

        changed = await coord._async_update_data()
        assert optimizer.call_count == 2
        assert changed["economic_plan_reused"] is False

    asyncio.run(scenario())


def test_reserve_reached_between_tariffs_stops_discharge(monkeypatch):
    async def scenario():
        coord, client, hass, _, clock, optimizer = await setup(monkeypatch)
        client.soc = 20.2
        await coord._async_update_data()
        clock[0] += timedelta(minutes=1)
        client.soc = 20

        stopped = await coord._async_update_data()

        assert optimizer.call_count == 2
        assert stopped["economic_plan_reused"] is False
        assert stopped["executable_slot"]["action"] != "battery"
        assert hass.states.get("switch.grid_input").state == "on"
        assert stopped["execution_enabled"] is True

    asyncio.run(scenario())


def test_load_becoming_infeasible_stops_held_discharge_between_tariffs(monkeypatch):
    async def scenario():
        coord, client, hass, _, clock, optimizer = await setup(monkeypatch)
        initial = await coord._async_update_data()
        assert initial["executable_slot"]["action"] == "battery"
        clock[0] += timedelta(minutes=1)
        client.load_w = 1500

        stopped = await coord._async_update_data()

        assert optimizer.call_count == 2
        assert stopped["executable_slot"]["action"] != "battery"
        assert hass.states.get("switch.grid_input").state == "on"
        assert stopped["execution_enabled"] is True

    asyncio.run(scenario())


def test_outage_invalidates_plan_and_recovery_replans_inside_same_tariff(monkeypatch):
    async def scenario():
        coord, _, hass, _, clock, optimizer = await setup(monkeypatch)
        await coord._async_update_data()
        clock[0] += timedelta(minutes=1)
        hass.states.async_set("binary_sensor.grid", "off")

        outage = await coord._async_update_data()
        assert outage["system_state"] == "OUTAGE"
        assert optimizer.call_count == 1
        assert coord._economic_decision is None
        clock[0] += timedelta(minutes=1)
        hass.states.async_set("binary_sensor.grid", "on")

        restored = await coord._async_update_data()
        assert restored["system_state"] == "ACTIVE"
        assert optimizer.call_count == 2
        assert restored["economic_plan_reused"] is False

    asyncio.run(scenario())


def test_price_listener_refreshes_for_publication_and_ignores_unrelated_attributes(monkeypatch):
    async def scenario():
        coord, _, hass, _, _, _ = await setup(monkeypatch)
        unload = []
        coord.entry.async_on_unload = unload.append
        coord.async_request_refresh = AsyncMock()
        coord.async_start_price_tracking()
        state = hass.states.get("sensor.prices")
        hass.states.async_set("sensor.prices", state.state,
                              {**state.attributes, "friendly_name": "Electricity"})
        await hass.async_block_till_done()
        coord.async_request_refresh.assert_not_awaited()

        rows = [dict(row) for row in state.attributes["prices"]]
        rows[-1]["price"] = 8
        hass.states.async_set("sensor.prices", state.state, {"prices": rows})
        await hass.async_block_till_done()
        coord.async_request_refresh.assert_awaited_once()
        for cancel in unload:
            cancel()

    asyncio.run(scenario())


@pytest.mark.parametrize("minutes", [15, 60])
def test_exact_boundary_timer_follows_provider_cadence_and_cancels_on_unload(monkeypatch, minutes):
    async def scenario():
        coord, _, _, _, clock, _ = await setup(monkeypatch)
        cancel = Mock()
        tracker = Mock(return_value=cancel)
        monkeypatch.setattr(coordinator_module, "async_track_point_in_utc_time", tracker)
        coord._last_price_slots = [PriceSlot(clock[0], clock[0] + timedelta(minutes=minutes), 2)]
        coord.async_request_refresh = AsyncMock()
        coord.async_start_price_tracking()
        _, refresh, boundary = tracker.call_args.args
        assert boundary == clock[0] + timedelta(minutes=minutes)
        revision = coord._planning_revision
        refresh(boundary)
        await asyncio.sleep(0)
        coord.async_request_refresh.assert_awaited_once()
        assert coord._planning_revision == revision + 1

        coord._schedule_price_refresh(coord._last_price_slots, clock[0])
        coord.runtime.execution_enabled = False
        await coord.async_prepare_unload()
        cancel.assert_called_once()
        assert coord._price_timer_at is None

    asyncio.run(scenario())


def test_scheduled_load_boundary_wakes_planner_before_next_tariff(monkeypatch):
    async def scenario():
        coord, _, _, _, clock, _ = await setup(monkeypatch)
        tracker = Mock(return_value=Mock())
        monkeypatch.setattr(coordinator_module, "async_track_point_in_utc_time", tracker)
        coord._last_price_slots = [PriceSlot(clock[0], clock[0] + timedelta(hours=1), 2)]
        starts = clock[0] + timedelta(minutes=3)
        coord.runtime.scheduled_loads = [{
            "start": starts.isoformat(), "end": (starts + timedelta(minutes=2)).isoformat(),
            "additional_w": 100,
        }]
        coord.async_start_price_tracking()
        assert tracker.call_args.args[2] == starts
        coord._cancel_price_timer()

    asyncio.run(scenario())
