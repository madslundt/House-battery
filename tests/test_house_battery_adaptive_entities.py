"""Public controls explain adaptive decisions without replacing manual settings."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.helpers.entity import EntityCategory

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components"))

from house_battery.entity import enabled_by_default
from house_battery.sensor import (
    SENSORS,
    FbpAdaptationSensor,
    FbpValueSensor,
)
from house_battery.sensor import (
    async_setup_entry as setup_sensors,
)
from house_battery.switch import (
    FbpAdaptivePlanningSwitch,
)
from house_battery.switch import (
    async_setup_entry as setup_switches,
)


def _coordinator() -> SimpleNamespace:
    return SimpleNamespace(
        entry=SimpleNamespace(entry_id="adaptive-test"),
        config={}, device_info={}, is_direct_local=False,
        runtime=SimpleNamespace(
            adaptive_enabled=False,
            settings={"minimum_profit_dkk_per_kwh": .5},
        ),
        data={"adaptation": {
            "enabled": False,
            "state": "disabled",
            "reason": "Adaptive planning is disabled",
            "configured_minimum_profit_dkk_per_kwh": .5,
            "effective_minimum_profit_dkk_per_kwh": .5,
            "automatic_changes": [],
            "recommendations": [],
            "forecast_adaptation": {},
        }},
        async_set_adaptive_enabled=AsyncMock(),
    )


def test_adaptive_switch_exposes_status_and_retained_manual_margin() -> None:
    coordinator = _coordinator()
    switch = FbpAdaptivePlanningSwitch(coordinator)

    assert not switch.is_on
    assert switch.entity_category is EntityCategory.CONFIG
    assert switch.entity_registry_enabled_default
    attrs = switch.extra_state_attributes
    assert attrs["status"] == "disabled"
    assert attrs["manual_settings_retained"]
    assert attrs["configured_minimum_profit_dkk_per_kwh"] == .5
    assert attrs["effective_minimum_profit_dkk_per_kwh"] == .5
    assert "forecast_uncertainty_dkk_per_kwh" in attrs["what_can_change"]

    coordinator.runtime.adaptive_enabled = True
    coordinator.data["adaptation"].update(
        state="active", effective_minimum_profit_dkk_per_kwh=0,
        reason="Qualified efficiency and recent tariff evidence are available",
    )
    assert switch.is_on
    assert switch.extra_state_attributes["effective_minimum_profit_dkk_per_kwh"] == 0
    assert coordinator.runtime.settings["minimum_profit_dkk_per_kwh"] == .5


def test_adaptive_switch_delegates_both_changes_without_editing_settings() -> None:
    coordinator = _coordinator()
    switch = FbpAdaptivePlanningSwitch(coordinator)

    asyncio.run(switch.async_turn_on())
    asyncio.run(switch.async_turn_off())

    assert [call.args for call in coordinator.async_set_adaptive_enabled.await_args_list] == [
        (True,), (False,),
    ]
    assert coordinator.runtime.settings == {"minimum_profit_dkk_per_kwh": .5}


@pytest.mark.parametrize("state", ["disabled", "learning", "active"])
def test_adaptation_sensor_retains_actionable_recommendations_and_evidence(state) -> None:
    coordinator = _coordinator()
    recommendation = {
        "id": "meter_quality", "severity": "warning", "status": "recommended",
        "message": "Review the active-power source",
        "evidence": {"rejected_windows": 2},
    }
    adaptation = coordinator.data["adaptation"]
    adaptation.update(
        state=state, recommendations=[recommendation],
        automatic_changes=[{"setting": "minimum_profit_dkk_per_kwh", "value": 0}],
        forecast_adaptation={"source": "sensor.total_price_forecast", "samples": 20},
    )
    sensor = FbpAdaptationSensor(coordinator)

    assert sensor.native_value == state
    assert sensor.entity_category is EntityCategory.DIAGNOSTIC
    assert sensor.device_class is SensorDeviceClass.ENUM
    assert sensor.entity_registry_enabled_default
    assert sensor.extra_state_attributes["recommendations"] == [recommendation]
    assert sensor.extra_state_attributes["automatic_changes"] == adaptation["automatic_changes"]
    assert sensor.extra_state_attributes["forecast_adaptation"]["samples"] == 20


def test_missing_adaptation_payload_does_not_invent_active_status() -> None:
    coordinator = _coordinator()
    coordinator.data = {}
    sensor = FbpAdaptationSensor(coordinator)
    assert sensor.native_value == "disabled"
    assert sensor.extra_state_attributes == {}


@pytest.mark.parametrize("direct_local", [False, True])
def test_adaptation_controls_are_visible_without_advanced_sensor_bindings(direct_local) -> None:
    assert enabled_by_default("adaptive_planning", {}, direct_local=direct_local)
    assert enabled_by_default("adaptation_status", {}, direct_local=direct_local)


@pytest.mark.parametrize(("key", "value"), [
    ("planned_bill_savings_dkk", 1.5),
    ("planned_wear_cost_dkk", .2),
    ("planned_switching_cost_dkk", .1),
])
def test_plan_cost_components_are_optional_numeric_monetary_sensors(key, value) -> None:
    coordinator = _coordinator()
    coordinator.data[key] = value
    description = next(item for item in SENSORS if item.key == key)
    sensor = FbpValueSensor(coordinator, description)

    assert sensor.native_value == value
    assert sensor.native_unit_of_measurement == "DKK"
    assert sensor.device_class is SensorDeviceClass.MONETARY
    assert not sensor.entity_registry_enabled_default


def test_platform_setup_registers_adaptive_switch_and_status(monkeypatch) -> None:
    coordinator = _coordinator()
    entry = SimpleNamespace(entry_id="adaptive-test", runtime_data=coordinator)
    registry = SimpleNamespace(async_get_entity_id=lambda *args: None)
    monkeypatch.setattr("house_battery.sensor.er.async_get", lambda hass: registry)
    switches, sensors = [], []

    asyncio.run(setup_switches(SimpleNamespace(), entry, switches.extend))
    asyncio.run(setup_sensors(SimpleNamespace(), entry, sensors.extend))

    assert any(isinstance(entity, FbpAdaptivePlanningSwitch) for entity in switches)
    assert any(isinstance(entity, FbpAdaptationSensor) for entity in sensors)
