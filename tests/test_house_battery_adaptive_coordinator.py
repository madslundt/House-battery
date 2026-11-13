"""Adaptive policy is gated by real measurements and survives HA reloads."""

from __future__ import annotations

import asyncio
import sys
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components"))

from house_battery.const import CONF_EFFICIENCY_GRID_POWER
from house_battery.forecast import ForecastAccuracy
from house_battery.runtime import RuntimeState
from test_house_battery_cutoff_gate import coordinator
from test_house_battery_efficiency_coordinator import QualifiedMeasurement


@pytest.mark.parametrize("stored", [None, False, "true", "false", 1, {}])
def test_adaptation_requires_explicit_persisted_authorization(stored):
    state = RuntimeState.from_dict({"adaptive_enabled": stored})
    assert not state.adaptive_enabled
    assert not RuntimeState.from_dict(state.as_dict()).adaptive_enabled


def test_adaptive_opt_in_and_bounded_audit_survive_restart():
    now = datetime.now(UTC)
    state = RuntimeState(adaptive_enabled=True, adaptive_history=[
        {"timestamp": now.isoformat(), "state": "active"},
        {"timestamp": (now - timedelta(days=31)).isoformat(), "state": "learning"},
    ])
    restored = RuntimeState.from_dict(state.as_dict())
    restored.prune(now)
    assert restored.adaptive_enabled
    assert restored.adaptive_history == [state.adaptive_history[0]]
    assert restored.export("Battery", {})["adaptive_enabled"]


def test_opt_in_preserves_selected_meter_and_manual_settings_while_learning():
    async def scenario():
        coord, client, _, _ = await coordinator()
        meter = "sensor.watts_live_11d1045220005594_positive_active_power"
        coord.entry.options[CONF_EFFICIENCY_GRID_POWER] = meter
        coord.runtime.settings.update(round_trip_efficiency=80, minimum_profit_dkk_per_kwh=.5)
        coord.runtime.measured_efficiency = QualifiedMeasurement(qualified=False)
        manual = dict(coord.runtime.settings)
        commands = list(client.calls)
        await coord.async_set_adaptive_enabled(True)
        settings = coord._settings()
        assert settings.round_trip_efficiency == .8
        assert settings.minimum_profit_dkk_per_kwh == .5
        assert coord._adaptation["state"] == "learning"
        assert coord.entry.options[CONF_EFFICIENCY_GRID_POWER] == meter
        assert coord.runtime.settings == manual
        assert coord.store.runtime.adaptive_enabled
        assert client.calls == commands

    asyncio.run(scenario())


def test_qualified_measurements_activate_and_source_reset_restores_manual_policy():
    async def scenario():
        coord, _, _, _ = await coordinator()
        coord.entry.options[CONF_EFFICIENCY_GRID_POWER] = "sensor.real_power"
        coord.runtime.adaptive_enabled = True
        coord.runtime.settings.update(round_trip_efficiency=80, minimum_profit_dkk_per_kwh=.5)
        measurement = QualifiedMeasurement()
        original_summary = measurement.summary

        def measured_summary(now):
            summary = original_summary(now)
            summary["efficiency_pct"] = 74
            summary["planning_efficiency_pct"] = 74 if measurement.qualified else None
            return summary

        measurement.summary = measured_summary
        coord.runtime.measured_efficiency = measurement
        settings = coord._settings()
        assert settings.round_trip_efficiency == .74
        assert settings.minimum_profit_dkk_per_kwh == 0
        assert settings.low_soc_charge_premium_dkk_per_kwh == 0
        assert settings.high_soc_discharge_discount_dkk_per_kwh == 0
        assert settings.degradation_cost_dkk_per_kwh == coord.runtime.settings["degradation_cost_dkk_per_kwh"]
        assert coord._adaptation["state"] == "active"
        assert coord.runtime.settings["minimum_profit_dkk_per_kwh"] == .5
        assert coord.runtime.settings["round_trip_efficiency"] == 80
        history_count = len(coord.runtime.adaptive_history)
        coord._settings()
        assert len(coord.runtime.adaptive_history) == history_count

        coord.entry.options[CONF_EFFICIENCY_GRID_POWER] = "sensor.other_real_power"
        reset = coord._settings()
        assert reset.round_trip_efficiency == .8
        assert reset.minimum_profit_dkk_per_kwh == .5
        assert coord._adaptation["state"] == "learning"
        assert len(coord.runtime.adaptive_history) == history_count + 1

    asyncio.run(scenario())


def test_turning_adaptation_off_restores_manual_margin_and_preserves_control():
    async def scenario():
        coord, client, _, _ = await coordinator()
        coord.entry.options[CONF_EFFICIENCY_GRID_POWER] = "sensor.real_power"
        coord.runtime.measured_efficiency = QualifiedMeasurement()
        coord.runtime.adaptive_enabled = True
        manual = dict(coord.runtime.settings)
        assert coord._settings().minimum_profit_dkk_per_kwh == 0
        commands = list(client.calls)
        await coord.async_set_adaptive_enabled(False)
        assert coord._settings().minimum_profit_dkk_per_kwh == manual["minimum_profit_dkk_per_kwh"]
        assert coord._adaptation["state"] == "disabled"
        assert coord.runtime.execution_enabled
        assert coord.runtime.settings == manual
        assert client.calls == commands

    asyncio.run(scenario())


def test_update_publishes_learning_explanation_and_reconciled_cash_costs():
    async def scenario():
        coord, _, hass, _ = await coordinator(override="grid")
        coord.entry.options[CONF_EFFICIENCY_GRID_POWER] = "sensor.real_power"
        hass.states.async_set("sensor.real_power", "320", {"unit_of_measurement": "W"})
        coord.runtime.measured_efficiency = QualifiedMeasurement(qualified=False)
        coord.runtime.adaptive_enabled = True
        status = await coord._async_update_data()
        assert status["adaptation"]["state"] == "learning"
        assert status["adaptation"]["measurement_entity"] == "sensor.real_power"
        assert status["adaptation"]["recommendations"]
        assert status["planning_efficiency_source"] == "configured_fallback"
        assert status["planned_net_savings_dkk"] == pytest.approx(
            status["planned_bill_savings_dkk"] - status["planned_wear_cost_dkk"]
            - status["planned_switching_cost_dkk"]
        )
        assert status["planned_net_savings_dkk"] == pytest.approx(status["realized_savings_dkk"])

    asyncio.run(scenario())


def test_paused_refresh_persists_the_adaptation_audit_it_publishes():
    async def scenario():
        coord, _, hass, _ = await coordinator(startup=True)
        hass.states.async_set("binary_sensor.grid", "off")
        saved = []

        async def save(runtime):
            saved.append(deepcopy(runtime.as_dict()))

        coord.store.save = save
        status = await coord._async_update_data()
        assert status["system_state"] in {"BOOTSTRAP", "OUTAGE"}
        assert saved[-1]["adaptive_history"] == coord.runtime.adaptive_history
        assert status["adaptation"]["recent_changes"] == saved[-1]["adaptive_history"][-10:]
        assert not coord._adaptation_dirty

    asyncio.run(scenario())


@pytest.mark.parametrize("enabled", [False, True])
def test_forecast_tuning_reaches_real_planner_slots_without_replacing_known_prices(enabled):
    async def scenario():
        now = datetime.now(UTC)
        coord, _, hass, _ = await coordinator(now=now)
        coord._load_power = lambda: 250
        coord.runtime.adaptive_enabled = enabled
        coord.runtime.forecast_enabled = True
        coord.entry.options["price_forecast_entities"] = ["sensor.forecast_a", "sensor.forecast_b"]
        known_end = datetime.fromisoformat(hass.states.get("sensor.prices").attributes["prices"][-1]["end"])
        predictions = [{
            "start": known_end.isoformat(),
            "end": (known_end + timedelta(hours=1)).isoformat(), "price": 3,
        }]
        for source, error in [("sensor.forecast_a", .8), ("sensor.forecast_b", .6)]:
            hass.states.async_set(source, "3", {"prices": predictions})
            samples = []
            for day in [1, 2, 3]:
                for hour in range(12):
                    start = (now - timedelta(days=day)).replace(hour=hour, minute=0, second=0, microsecond=0)
                    end = start + timedelta(hours=1)
                    samples.append({
                        "start": start.isoformat(), "end": end.isoformat(),
                        "scored_at": end.isoformat(), "error_dkk_per_kwh": error,
                        "issued_at": (start - timedelta(hours=12)).isoformat(),
                    })
            coord.runtime.forecast_accuracies[source] = ForecastAccuracy(
                error_samples=samples,
                lead_error_samples={
                    "0_24h": [{**sample, "error_dkk_per_kwh": .45} for sample in samples],
                },
            )

        slots = coord._price_slots(now)
        assert slots[0].source == "known"
        assert slots[0].price == 4
        forecasts = [slot for slot in slots if slot.source == "forecast"]
        assert forecasts
        expected_source = "sensor.forecast_b" if enabled else "sensor.forecast_a"
        assert coord._forecast_planning_source == expected_source
        assert coord._forecast_sources[expected_source]["used_slots"] == len(forecasts)
        expected_uncertainty = .45 if enabled else coord.runtime.settings["forecast_uncertainty_dkk_per_kwh"]
        assert forecasts[0].uncertainty_dkk_per_kwh == pytest.approx(expected_uncertainty)
        for accuracy in coord.runtime.forecast_accuracies.values():
            assert all(datetime.fromisoformat(v["start"]) >= known_end for v in accuracy.outstanding.values())

    asyncio.run(scenario())
