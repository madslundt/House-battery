"""Compact entity defaults, upgrade behavior, and Recorder storage policy."""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from homeassistant.components.recorder.db_schema import StateAttributes
from homeassistant.core import Event, HomeAssistant
from homeassistant.helpers.entity import EntityPlatformState

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components"))

from house_battery import async_migrate_entry
from house_battery.const import (
    CONF_ACCOUNTING_LOAD_POWER,
    CONF_GRID_IMPORT_POWER,
    CONF_PRICE_FORECAST_ENTITIES,
)
from house_battery.entity import enabled_by_default
from house_battery.number import FbpSettingNumber
from house_battery.select import FbpOperatingModeSelect
from house_battery.sensor import SENSORS, FbpPlanSensor, FbpValueSensor
from house_battery.switch import FbpAutomaticControlSwitch, FbpExternalForecastSwitch


def _entity_coordinator(config: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        entry=SimpleNamespace(entry_id="entry-1"),
        config=config or {},
        is_direct_local=True,
        data={},
        device_info={},
    )


def test_compact_catalogue_keeps_daily_operation_and_learning_visible() -> None:
    coordinator = _entity_coordinator()
    values = {item.key: FbpValueSensor(coordinator, item) for item in SENSORS}
    for key in (
        "soc",
        "load_power_w",
        "battery_power_w",
        "load_learning_confidence_pct",
        "today_charge_kwh",
        "today_discharge_kwh",
        "lifetime_charge_kwh",
        "lifetime_discharge_kwh",
        "today_battery_net_savings_dkk",
    ):
        assert values[key].entity_registry_enabled_default, key
    for key in (
        "battery_charge_power_w",
        "battery_output_power_w",
        "effective_target_soc",
        "today_charge_cost_dkk",
        "week_battery_net_savings_dkk",
        "learned_capacity_kwh",
        "grid_import_power_w",
        "today_net_savings_dkk",
    ):
        assert not values[key].entity_registry_enabled_default, key


def test_compact_controls_keep_safety_and_daily_limits_but_make_tuning_opt_in() -> None:
    coordinator = _entity_coordinator()
    assert FbpAutomaticControlSwitch(coordinator).entity_registry_enabled_default
    assert not FbpOperatingModeSelect(coordinator).entity_registry_enabled_default
    for key in (
        "reserve_soc",
        "target_soc",
        "charge_power_w",
        "discharge_power_w",
        "minimum_profit_dkk_per_kwh",
    ):
        assert FbpSettingNumber(coordinator, key).entity_registry_enabled_default
    for key in (
        "capacity_kwh",
        "absolute_min_soc",
        "round_trip_efficiency",
        "degradation_cost_dkk_per_kwh",
        "cycle_life",
    ):
        assert not FbpSettingNumber(coordinator, key).entity_registry_enabled_default


def test_default_entities_follow_configured_meter_and_forecast_capabilities() -> None:
    config = {CONF_GRID_IMPORT_POWER: "sensor.grid"}
    assert enabled_by_default("grid_export_power_w", config, direct_local=True)
    assert enabled_by_default("export_safety_fault", config, direct_local=True)
    assert not enabled_by_default("today_net_savings_dkk", config, direct_local=True)
    config[CONF_ACCOUNTING_LOAD_POWER] = "sensor.house_load"
    assert enabled_by_default("today_net_savings_dkk", config, direct_local=True)
    assert not FbpExternalForecastSwitch(
        _entity_coordinator(config)
    ).entity_registry_enabled_default
    config[CONF_PRICE_FORECAST_ENTITIES] = ["sensor.forecast"]
    assert FbpExternalForecastSwitch(
        _entity_coordinator(config)
    ).entity_registry_enabled_default


def test_upgrade_disables_untouched_advanced_entities_once_and_preserves_customization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = datetime(2026, 9, 20, tzinfo=UTC)

    def row(key: str, **changes: object) -> SimpleNamespace:
        values = {
            "platform": "house_battery",
            "unique_id": f"entry-1_{key}",
            "entity_id": f"sensor.{key}",
            "disabled_by": None,
            "created_at": created,
            # HA initializes the timestamps in separate calls.
            "modified_at": created + timedelta(microseconds=50),
            "name": None,
            "icon": None,
            "area_id": None,
            "hidden_by": None,
            "labels": set(),
        }
        values.update(changes)
        return SimpleNamespace(**values)

    rows = [
        row("soc"),
        row("battery_charge_power_w"),
        row("setting_capacity_kwh", entity_id="number.capacity"),
        row("grid_export_power_w"),
        row("operating_mode_override", modified_at=created + timedelta(days=1)),
        row("today_charge_cost_dkk", name="My charge cost"),
        row("power_source", disabled_by="user"),
        row("unrelated", platform="other"),
    ]
    updated = []
    registry = SimpleNamespace(
        async_update_entity=lambda entity_id, **kw: updated.append((entity_id, kw))
    )
    monkeypatch.setattr("house_battery.er.async_get", lambda hass: registry)
    monkeypatch.setattr(
        "house_battery.er.async_entries_for_config_entry", lambda *a: rows
    )
    entry = SimpleNamespace(
        entry_id="entry-1",
        version=1,
        minor_version=1,
        data={"host": "battery.local"},
        options={},
    )

    def update_entry(entry, **values):
        entry.minor_version = values["minor_version"]

    hass = SimpleNamespace(
        config_entries=SimpleNamespace(async_update_entry=update_entry)
    )
    assert asyncio.run(async_migrate_entry(hass, entry))
    assert {entity_id for entity_id, _ in updated} == {
        "sensor.battery_charge_power_w",
        "number.capacity",
        "sensor.grid_export_power_w",
    }
    assert all(kw["disabled_by"] == "integration" for _, kw in updated)
    assert entry.minor_version == 2
    updated.clear()
    assert asyncio.run(async_migrate_entry(hass, entry))
    assert updated == []


def test_recorder_omits_live_plan_payload_but_keeps_numeric_statistics_metadata() -> (
    None
):
    async def check() -> None:
        hass = HomeAssistant("/tmp")
        coordinator = _entity_coordinator()
        coordinator.data = {
            "plan_created_at": "2026-10-22T10:00:00+02:00",
            "daily_plan": {"blocks": [{"action": "charge"}]},
            "lifetime_charge_kwh": 12.5,
            "today": {"battery_net_savings_dkk": -1.25},
            "accounting_period_starts": {"today": "2026-10-22T00:00:00+02:00"},
        }
        coordinator.last_update_success = True
        descriptions = {item.key: item for item in SENSORS}
        sensors = [
            FbpPlanSensor(coordinator),
            FbpValueSensor(coordinator, descriptions["lifetime_charge_kwh"]),
            FbpValueSensor(coordinator, descriptions["today_battery_net_savings_dkk"]),
        ]
        for sensor, entity_id in zip(
            sensors, ("sensor.plan", "sensor.charge_total", "sensor.net_return")
        ):
            sensor.hass = hass
            sensor.entity_id = entity_id
            sensor.platform = SimpleNamespace(platform_name="sensor", config_entry=None)
            sensor._platform_state = EntityPlatformState.ADDED
            await sensor.async_internal_added_to_hass()
            sensor.async_write_ha_state()
            state = hass.states.get(entity_id)
            event = Event("state_changed", {"new_state": state})
            recorded = json.loads(
                StateAttributes.shared_attrs_bytes_from_event(event, None)
            )
            if entity_id == "sensor.plan":
                assert state.attributes["blocks"] == [{"action": "charge"}]
                assert "blocks" not in recorded
                assert "created_at" not in recorded
            elif entity_id == "sensor.charge_total":
                assert state.state == "12.5"
                assert recorded["unit_of_measurement"] == "kWh"
                assert recorded["state_class"] == "total_increasing"
                assert recorded["device_class"] == "energy"
            else:
                assert state.state == "-1.25"
                assert recorded["last_reset"] == "2026-10-22T00:00:00+02:00"
                assert recorded["state_class"] == "total"

    asyncio.run(check())
