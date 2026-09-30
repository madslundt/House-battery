"""Editable battery and economic policy inputs."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, SETTING_LIMITS
from .coordinator import Fbp1200Coordinator
from .entity import Fbp1200Entity

SETTING_NAMES = {
    "capacity_kwh": ("Nominal battery capacity", "mdi:battery-high"),
    "absolute_min_soc": ("Absolute emergency SOC", "mdi:battery-alert"),
    "reserve_soc": ("Arbitrage reserve SOC", "mdi:battery-lock"),
    "target_soc": ("Maximum charge SOC", "mdi:battery-charging-90"),
    "charge_power_w": ("Maximum AC charge power", "mdi:battery-charging"),
    "discharge_power_w": ("Maximum discharge power", "mdi:battery-arrow-down"),
    "round_trip_efficiency": ("Fallback round-trip efficiency", "mdi:percent-circle"),
    "degradation_cost_dkk_per_kwh": (
        "Battery degradation cost",
        "mdi:battery-heart-variant",
    ),
    "minimum_profit_dkk_per_kwh": ("Minimum required profit", "mdi:cash-lock"),
    "cycle_life": ("Cycle-life reference", "mdi:sync"),
    "forecast_uncertainty_dkk_per_kwh": (
        "External price forecast uncertainty",
        "mdi:chart-bell-curve-cumulative",
    ),
}


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: Fbp1200Coordinator = entry.runtime_data
    registry = er.async_get(hass)
    obsolete_settings = {
        "opportunistic_target_soc",
        "low_soc_charge_threshold",
        "low_soc_charge_premium_dkk_per_kwh",
        "high_soc_discharge_threshold",
        "high_soc_discharge_discount_dkk_per_kwh",
        "switching_penalty_dkk",
    }
    if coordinator.is_direct_local:
        obsolete_settings.add("cycle_life")
    for key in obsolete_settings:
        unique_id = f"{entry.entry_id}_setting_{key}"
        entity_id = registry.async_get_entity_id("number", DOMAIN, unique_id)
        if entity_id:
            registry.async_remove(entity_id)

    entities: list[NumberEntity] = [
        FbpSettingNumber(coordinator, key)
        for key in SETTING_NAMES
        if key != "cycle_life" or not coordinator.is_direct_local
    ]
    async_add_entities(entities)


class FbpSettingNumber(Fbp1200Entity, NumberEntity):
    """A persistent, auditable input to the planner."""

    _attr_mode = NumberMode.BOX
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: Fbp1200Coordinator, key: str) -> None:
        super().__init__(coordinator, f"setting_{key}")
        self.key = key
        name, icon = SETTING_NAMES[key]
        minimum, maximum, step, unit = SETTING_LIMITS[key]
        self._attr_name = name
        self._attr_icon = icon
        self._attr_native_min_value = minimum
        self._attr_native_max_value = maximum
        self._attr_native_step = step
        self._attr_native_unit_of_measurement = unit
        if key == "forecast_uncertainty_dkk_per_kwh":
            self._attr_entity_registry_enabled_default = False

    @property
    def native_value(self) -> float:
        return self.coordinator.runtime.settings[self.key]

    async def async_set_native_value(self, value: float) -> None:
        minimum, maximum, _, _ = SETTING_LIMITS[self.key]
        if not minimum <= value <= maximum:
            raise ValueError(f"{self.key} must be between {minimum} and {maximum}")
        candidate = dict(self.coordinator.runtime.settings)
        candidate[self.key] = value
        if not (
            0
            <= candidate["absolute_min_soc"]
            <= candidate["reserve_soc"]
            < candidate["target_soc"]
            <= 100
        ):
            raise ValueError(
                "Absolute emergency SOC ≤ arbitrage reserve < maximum charge SOC ≤ 100% is required"
            )
        await self.coordinator.async_set_setting(self.key, value)
