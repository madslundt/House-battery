"""Sizing recommendations expose evidence without changing battery limits."""

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

from house_battery.const import DEFAULT_SETTINGS, SETTING_LIMITS
from house_battery.number import FbpSettingNumber
from house_battery.number import async_setup_entry as setup_numbers
from house_battery.sensor import FbpBatterySizingSensor
from house_battery.sensor import async_setup_entry as setup_sensors

SIZING_SETTINGS = {
    "sizing_expansion_capacity_kwh": (1.958, .1, 100, .001, "kWh"),
    "sizing_expansion_cost_dkk": (3000, 0, 100000, 100, "DKK"),
    "sizing_payback_target_years": (10, 1, 30, 1, "years"),
}


def _coordinator(*, direct_local: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        entry=SimpleNamespace(entry_id="sizing-test"),
        config={}, device_info={}, is_direct_local=direct_local,
        runtime=SimpleNamespace(settings=dict(DEFAULT_SETTINGS)),
        data={}, async_set_setting=AsyncMock(),
    )


@pytest.mark.parametrize("state", [
    "learning", "bigger_battery_better", "size_is_fine", "battery_too_big",
])
def test_sizing_sensor_preserves_reason_evidence_and_capacity_cases(state) -> None:
    coordinator = _coordinator()
    payload = {
        "state": state,
        "reason": "Compare qualified recent days at equal starting and ending SOC",
        "evidence": {"complete_days": 28, "weekend_days": 8},
        "capacity_cases": [{"capacity_kwh": 1.958, "annualized_net_savings_dkk": 250}],
        "marginal_annual_savings_dkk": 125,
        "payback_years": 24,
    }
    coordinator.data["battery_sizing"] = payload
    original_settings = dict(coordinator.runtime.settings)
    sensor = FbpBatterySizingSensor(coordinator)

    assert sensor.name == "Battery sizing"
    assert sensor.native_value == state
    assert sensor.device_class is SensorDeviceClass.ENUM
    assert sensor.entity_category is EntityCategory.DIAGNOSTIC
    assert sensor.extra_state_attributes == {key: value for key, value in payload.items() if key != "state"}
    assert coordinator.runtime.settings == original_settings
    coordinator.async_set_setting.assert_not_called()


@pytest.mark.parametrize("direct_local", [False, True])
def test_sizing_sensor_is_visible_and_starts_learning_without_evidence(direct_local) -> None:
    sensor = FbpBatterySizingSensor(_coordinator(direct_local=direct_local))
    assert sensor.entity_registry_enabled_default
    assert sensor.native_value == "learning"
    assert sensor.extra_state_attributes == {}


@pytest.mark.parametrize("payload", [None, [], {"state": "profitable"}])
def test_invalid_sizing_payload_does_not_invent_a_recommendation(payload) -> None:
    coordinator = _coordinator()
    coordinator.data["battery_sizing"] = payload
    sensor = FbpBatterySizingSensor(coordinator)
    assert sensor.native_value is None
    assert "state" not in sensor.extra_state_attributes


@pytest.mark.parametrize("key", SIZING_SETTINGS)
def test_sizing_number_defaults_limits_and_units_are_optional_configuration(key) -> None:
    coordinator = _coordinator()
    default, minimum, maximum, step, unit = SIZING_SETTINGS[key]
    number = FbpSettingNumber(coordinator, key)

    assert DEFAULT_SETTINGS[key] == default
    assert SETTING_LIMITS[key] == (minimum, maximum, step, unit)
    assert number.native_value == default
    assert number.native_min_value == minimum
    assert number.native_max_value == maximum
    assert number.native_step == step
    assert number.native_unit_of_measurement == unit
    assert number.entity_category is EntityCategory.CONFIG
    assert not number.entity_registry_enabled_default


def test_free_expansion_cost_is_accepted_without_changing_installed_capacity() -> None:
    coordinator = _coordinator()
    number = FbpSettingNumber(coordinator, "sizing_expansion_cost_dkk")
    asyncio.run(number.async_set_native_value(0))
    coordinator.async_set_setting.assert_awaited_once_with("sizing_expansion_cost_dkk", 0)
    assert coordinator.runtime.settings["capacity_kwh"] == DEFAULT_SETTINGS["capacity_kwh"]


@pytest.mark.parametrize(("key", "value"), [
    ("sizing_expansion_capacity_kwh", 0), ("sizing_expansion_capacity_kwh", 101),
    ("sizing_expansion_cost_dkk", -1), ("sizing_expansion_cost_dkk", 100001),
    ("sizing_payback_target_years", 0), ("sizing_payback_target_years", 31),
])
def test_sizing_numbers_reject_invalid_input(key, value) -> None:
    coordinator = _coordinator()
    number = FbpSettingNumber(coordinator, key)
    with pytest.raises(ValueError):
        asyncio.run(number.async_set_native_value(value))
    coordinator.async_set_setting.assert_not_called()


@pytest.mark.parametrize("direct_local", [False, True])
def test_platforms_register_sizing_sensor_and_all_three_numbers(monkeypatch, direct_local) -> None:
    coordinator = _coordinator(direct_local=direct_local)
    entry = SimpleNamespace(entry_id="sizing-test", runtime_data=coordinator)
    registry = SimpleNamespace(async_get_entity_id=lambda *args: None)
    monkeypatch.setattr("house_battery.sensor.er.async_get", lambda hass: registry)
    monkeypatch.setattr("house_battery.number.er.async_get", lambda hass: registry)
    sensors, numbers = [], []

    asyncio.run(setup_sensors(SimpleNamespace(), entry, sensors.extend))
    asyncio.run(setup_numbers(SimpleNamespace(), entry, numbers.extend))

    assert any(isinstance(entity, FbpBatterySizingSensor) for entity in sensors)
    assert SIZING_SETTINGS.keys() <= {entity.key for entity in numbers}
