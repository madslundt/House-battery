"""Price-triggered planning keeps independent telemetry and safety checks live."""

from __future__ import annotations

import asyncio
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components"))

import house_battery.coordinator as coordinator_module
from house_battery.const import CONF_EFFICIENCY_GRID_POWER
from house_battery.models import Action, PriceSlot
from test_house_battery_cutoff_gate import coordinator
from test_house_battery_efficiency_coordinator import QualifiedMeasurement


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
        assert polled["economic_plan_last_trigger"] == "startup_or_recovery"

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
        assert boundary["economic_plan_last_trigger"] == "tariff_boundary"

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
        assert revised["economic_plan_last_trigger"] == "price_or_forecast_update"

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


def test_learned_efficiency_refinement_waits_for_tariff_boundary(monkeypatch):
    async def scenario():
        coord, _, _, _, clock, optimizer = await setup(monkeypatch)
        original_settings = coord._settings
        efficiency = [.8]
        monkeypatch.setattr(coord, "_settings", lambda: replace(
            original_settings(), round_trip_efficiency=efficiency[0],
        ))
        initial = await coord._async_update_data()
        boundary = coord.plan.slots[0].end
        efficiency[0] = .81
        clock[0] += timedelta(minutes=1)

        refreshed = await coord._async_update_data()

        assert initial["system_state"] == refreshed["system_state"] == "ACTIVE"
        assert optimizer.call_count == 1
        assert refreshed["economic_plan_reused"] is True
        assert coord._economic_decision.settings.round_trip_efficiency == .8
        clock[0] = boundary
        await coord._async_update_data()
        assert optimizer.call_count == 2
        assert coord._economic_decision.settings.round_trip_efficiency == .81

    asyncio.run(scenario())


def test_withdrawn_adaptive_qualification_restores_manual_policy_immediately(monkeypatch):
    async def scenario():
        coord, _, hass, _, clock, optimizer = await setup(monkeypatch)
        coord.entry.options[CONF_EFFICIENCY_GRID_POWER] = "sensor.real_power"
        hass.states.async_set("sensor.real_power", "320", {"unit_of_measurement": "W"})
        coord.runtime.adaptive_enabled = True
        measurement = QualifiedMeasurement()
        coord.runtime.measured_efficiency = measurement
        await coord._async_update_data()
        assert coord._economic_decision.settings.minimum_profit_dkk_per_kwh == 0
        measurement.qualified = False
        clock[0] += timedelta(minutes=1)

        revised = await coord._async_update_data()

        assert optimizer.call_count == 2
        assert revised["economic_plan_reused"] is False
        assert coord._economic_decision.settings.minimum_profit_dkk_per_kwh == coord.runtime.settings["minimum_profit_dkk_per_kwh"]
        assert revised["economic_plan_last_trigger"] == "model_qualification_changed"

    asyncio.run(scenario())


def test_withdrawn_calibration_qualification_removes_correction_immediately(monkeypatch):
    async def scenario():
        coord, _, hass, _, clock, optimizer = await setup(monkeypatch)
        coord.entry.data["price_forecast_entities"] = ["sensor.forecast"]
        coord.runtime.forecast_enabled = True
        last = datetime.fromisoformat(hass.states.get("sensor.prices").attributes["prices"][-1]["end"])
        rows = [{"start": last.isoformat(), "end": (last + timedelta(hours=1)).isoformat(),
                 "price": 2}]
        hass.states.async_set("sensor.forecast", "ready", {"prices": rows})
        qualified = [True]

        def calibration(slots, *args, **kwargs):
            return ([replace(slot, price=slot.price - .4) for slot in slots] if qualified[0] else slots), {
                "lead_bands": {"0_24h": {"reason": "validated_bias_correction"
                                         if qualified[0] else "recent_regime_changed"}},
            }

        monkeypatch.setattr(coordinator_module, "calibrate_forecast", calibration)
        await coord._async_update_data()
        assert coord.plan.slots[-1].price == 1.6
        qualified[0] = False
        clock[0] += timedelta(minutes=1)

        revised = await coord._async_update_data()

        assert optimizer.call_count == 2
        assert revised["economic_plan_last_trigger"] == "model_qualification_changed"
        assert coord.plan.slots[-1].price == 2

    asyncio.run(scenario())


def test_withdrawn_price_pattern_qualification_removes_tail_immediately(monkeypatch):
    async def scenario():
        coord, _, _, _, clock, optimizer = await setup(monkeypatch)
        coord.runtime.forecast_enabled = True
        learner_type = type(coord.runtime.price_patterns)
        summary = learner_type.summary
        qualified = [True]

        def extension(self, slots, now, uncertainty_floor, **kwargs):
            if not qualified[0]:
                return list(slots)
            end = max(slot.end for slot in slots)
            return [*slots, PriceSlot(end, end + timedelta(hours=1), 3,
                                     source="forecast", uncertainty_dkk_per_kwh=uncertainty_floor)]

        def evidence(self, now):
            result = summary(self, now)
            result["selected_models"] = {"0_24h": "validated_pattern" if qualified[0] else None}
            return result

        monkeypatch.setattr(learner_type, "extend", extension)
        monkeypatch.setattr(learner_type, "summary", evidence)
        await coord._async_update_data()
        assert coord._pattern_used_slots == 1
        initial_calls = optimizer.call_count
        qualified[0] = False
        clock[0] += timedelta(minutes=1)

        revised = await coord._async_update_data()

        assert optimizer.call_count == initial_calls + 1
        assert revised["economic_plan_last_trigger"] == "model_qualification_changed"
        assert coord._pattern_used_slots == 0

    asyncio.run(scenario())


def test_learned_forecast_uncertainty_waits_but_provider_revision_replans(monkeypatch):
    async def scenario():
        coord, _, hass, _, clock, optimizer = await setup(monkeypatch)
        coord.entry.data["price_forecast_entities"] = ["sensor.forecast"]
        coord.runtime.forecast_enabled = True
        last = datetime.fromisoformat(hass.states.get("sensor.prices").attributes["prices"][-1]["end"])
        rows = [{"start": last.isoformat(), "end": (last + timedelta(hours=1)).isoformat(),
                 "price": 2}]
        hass.states.async_set("sensor.forecast", "ready", {"prices": rows})
        original_adaptation = coordinator_module.forecast_adaptation
        uncertainty = [.35]

        def adaptation(*args, **kwargs):
            result = original_adaptation(*args, **kwargs)
            result["sources"]["sensor.forecast"]["effective_uncertainty_dkk_per_kwh"] = uncertainty[0]
            return result

        monkeypatch.setattr(coordinator_module, "forecast_adaptation", adaptation)
        await coord._async_update_data()
        uncertainty[0] = .4
        clock[0] += timedelta(minutes=1)

        refreshed = await coord._async_update_data()

        assert optimizer.call_count == 1
        assert refreshed["economic_plan_reused"] is True
        rows[0] = {**rows[0], "price": 3}
        hass.states.async_set("sensor.forecast", "ready", {"prices": rows})
        clock[0] += timedelta(minutes=1)
        revised = await coord._async_update_data()
        assert optimizer.call_count == 2
        assert revised["economic_plan_reused"] is False
        assert coord.plan.slots[-1].price_uncertainty_dkk_per_kwh == .4

    asyncio.run(scenario())


@pytest.mark.parametrize("attribute,value", [("unit_of_measurement", "DKK/kWh"), ("currency", "EUR")])
def test_forecast_basis_revision_replans_without_a_registered_listener(monkeypatch, attribute, value):
    async def scenario():
        coord, _, hass, _, clock, optimizer = await setup(monkeypatch)
        coord.entry.data["price_forecast_entities"] = ["sensor.forecast"]
        coord.runtime.forecast_enabled = True
        last = datetime.fromisoformat(hass.states.get("sensor.prices").attributes["prices"][-1]["end"])
        rows = [{"start": last.isoformat(), "end": (last + timedelta(hours=1)).isoformat(),
                 "price": 2}]
        hass.states.async_set("sensor.forecast", "ready", {"prices": rows})
        await coord._async_update_data()
        hass.states.async_set("sensor.forecast", "ready", {"prices": rows, attribute: value})
        clock[0] += timedelta(minutes=1)

        revised = await coord._async_update_data()

        assert optimizer.call_count == 2
        assert revised["economic_plan_last_trigger"] == "price_or_forecast_update"

    asyncio.run(scenario())


@pytest.mark.parametrize("soc,charge_w,expected", [
    (39, 0, Action.GRID), (38, 0, Action.CHARGE), (39, 500, Action.CHARGE),
])
def test_idle_at_native_charge_ceiling_starts_planning_as_stopped(monkeypatch, soc, charge_w, expected):
    async def scenario():
        coord, client, _, _, _, optimizer = await setup(monkeypatch)
        client.soc = soc
        controls = await client.async_read_controls()
        client.async_read_controls = AsyncMock(return_value={**controls, "3024": "39"})
        measured = client.async_snapshot

        async def snapshot():
            return replace(await measured(), charge_power_w=charge_w)

        client.async_snapshot = snapshot
        monkeypatch.setattr(coord, "_observed_action", lambda: Action.CHARGE)
        await coord._async_update_data()

        assert optimizer.call_args.kwargs["current_action"] is expected

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
        assert changed["economic_plan_last_trigger"] == "settings_or_schedule_changed"

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
        assert stopped["economic_plan_last_trigger"] == "physical_eligibility_changed"
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
