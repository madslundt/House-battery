"""Editable battery and economic policy inputs."""

from __future__ import annotations

import math

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_DEVICE_TYPE,
    CONF_ECOFLOW_CAPACITY_KWH,
    CONF_ECOFLOW_DISCHARGE_POWER_W,
    CONF_MAX_SOC_CONTROL,
    CONF_MIN_SOC_CONTROL,
    DEVICE_ECOFLOW,
    DOMAIN,
    SETTING_LIMITS,
)
from .coordinator import Fbp1200Coordinator
from .ecoflow_capabilities import (
    ecoflow_charge_power_range_w,
    ecoflow_soc_control_range,
)
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
        "Price forecast uncertainty",
        "mdi:chart-bell-curve-cumulative",
    ),
    "sizing_expansion_capacity_kwh": (
        "Sizing expansion capacity",
        "mdi:battery-plus-outline",
    ),
    "sizing_expansion_cost_dkk": ("Sizing expansion cost", "mdi:cash-plus"),
    "sizing_payback_target_years": ("Sizing payback target", "mdi:calendar-clock"),
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
        if _setting_limits(coordinator, key) is None:
            self._attr_entity_registry_enabled_default = False
        self._fallback_limits = SETTING_LIMITS[key]
        self._attr_name = name
        self._attr_icon = icon
        if key == "forecast_uncertainty_dkk_per_kwh":
            self._attr_entity_registry_enabled_default = False

    @property
    def _limits(self) -> tuple[float, float, float, str | None]:
        return _setting_limits(self.coordinator, self.key) or self._fallback_limits

    @property
    def native_min_value(self) -> float:
        return self._limits[0]

    @property
    def native_max_value(self) -> float:
        return self._limits[1]

    @property
    def native_step(self) -> float:
        return self._limits[2]

    @property
    def native_unit_of_measurement(self) -> str | None:
        return self._limits[3]

    @property
    def native_value(self) -> float:
        return self.coordinator.runtime.settings[self.key]

    @property
    def available(self) -> bool:
        return super().available and _setting_limits(self.coordinator, self.key) is not None

    async def async_set_native_value(self, value: float) -> None:
        limits = _setting_limits(self.coordinator, self.key)
        if limits is None:
            raise ValueError(f"{self.key} is unavailable for this EcoFlow device")
        minimum, maximum, step, _ = limits
        if not minimum <= value <= maximum:
            raise ValueError(f"{self.key} must be between {minimum} and {maximum}")
        if self.coordinator.config.get(CONF_DEVICE_TYPE) == DEVICE_ECOFLOW:
            increments = (value - minimum) / step
            if abs(increments - round(increments)) > 1e-6:
                raise ValueError(f"{self.key} must follow the device step of {step}")
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


def _setting_limits(
    coordinator: Fbp1200Coordinator, key: str
) -> tuple[float, float, float, str | None] | None:
    """Apply EcoFlow device ratings and native SOC ranges to editable fields."""
    minimum, maximum, step, unit = SETTING_LIMITS[key]
    if coordinator.config.get(CONF_DEVICE_TYPE) != DEVICE_ECOFLOW:
        return minimum, maximum, step, unit
    if key == "charge_power_w":
        entity_id = coordinator.config.get("charge_power_control_entity")
        state = coordinator.hass.states.get(entity_id) if entity_id else None
        if state is None:
            return None
        try:
            power_min, power_max, power_step = ecoflow_charge_power_range_w(state)
        except ValueError:
            return None
        return power_min, power_max, power_step, "W"
    if key == "capacity_kwh":
        rated_capacity = coordinator.config.get(CONF_ECOFLOW_CAPACITY_KWH)
        if isinstance(rated_capacity, (int, float)):
            maximum = min(100.0, float(rated_capacity))
    elif key == "discharge_power_w":
        rated_power = coordinator.config.get(CONF_ECOFLOW_DISCHARGE_POWER_W)
        if isinstance(rated_power, (int, float)):
            maximum = min(12000.0, float(rated_power))
    elif key in {"absolute_min_soc", "reserve_soc", "target_soc"}:
        binding = CONF_MAX_SOC_CONTROL if key == "target_soc" else CONF_MIN_SOC_CONTROL
        entity_id = coordinator.config.get(binding)
        state = coordinator.hass.states.get(entity_id) if entity_id else None
        if state is None:
            return None
        try:
            native_min, native_max, native_step = ecoflow_soc_control_range(state)
        except ValueError:
            return None
        minimum = max(minimum, native_min)
        maximum = min(maximum, native_max)
        minimum = native_min + math.ceil(
            (minimum - native_min) / native_step - 1e-9
        ) * native_step
        maximum = native_min + math.floor(
            (maximum - native_min) / native_step + 1e-9
        ) * native_step
        step = native_step
    if minimum > maximum:
        return None
    return minimum, maximum, step, unit
