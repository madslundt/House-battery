"""Editable battery and economic policy inputs."""

from __future__ import annotations

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import SETTING_LIMITS
from .coordinator import Fbp1200Coordinator
from .entity import Fbp1200Entity

SETTING_NAMES = {
    "capacity_kwh": ("Nominal battery capacity", "mdi:battery-high"),
    "absolute_min_soc": ("Absolute emergency SOC", "mdi:battery-alert"),
    "reserve_soc": ("Arbitrage reserve SOC", "mdi:battery-lock"),
    "target_soc": ("Maximum charge SOC", "mdi:battery-charging-90"),
    "opportunistic_target_soc": (
        "Extra-storage charge SOC",
        "mdi:battery-charging-100",
    ),
    "charge_power_w": ("Maximum charge power", "mdi:battery-charging"),
    "discharge_power_w": ("Maximum discharge power", "mdi:battery-arrow-down"),
    "round_trip_efficiency": ("Fallback round-trip efficiency", "mdi:percent-circle"),
    "degradation_cost_dkk_per_kwh": (
        "Battery degradation cost",
        "mdi:battery-heart-variant",
    ),
    "minimum_profit_dkk_per_kwh": ("Minimum required profit", "mdi:cash-lock"),
    "extra_storage_spread_dkk_per_kwh": (
        "Extra-storage price spread",
        "mdi:chart-timeline-variant-shimmer",
    ),
    "extra_storage_cheap_window_minutes": (
        "Extra-storage cheap-window maximum duration",
        "mdi:timer-sand",
    ),
    "switching_penalty_dkk": ("Mode switching penalty", "mdi:swap-horizontal-bold"),
    "minimum_mode_minutes": ("Minimum mode duration", "mdi:timer-lock"),
    "maximum_transitions_per_day": ("Maximum daily mode transitions", "mdi:counter"),
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
    entities: list[NumberEntity] = [
        FbpSettingNumber(coordinator, key) for key in SETTING_NAMES
    ]
    if coordinator.is_direct_local:
        entities.extend(
            [
                FbpNativeSocNumber(coordinator, "minimum"),
                FbpNativeSocNumber(coordinator, "maximum"),
            ]
        )
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
            <= candidate["opportunistic_target_soc"]
            <= 100
        ):
            raise ValueError(
                "Absolute emergency SOC ≤ arbitrage reserve < normal target ≤ extra-storage target is required"
            )
        await self.coordinator.async_set_setting(self.key, value)


class FbpNativeSocNumber(Fbp1200Entity, NumberEntity):
    """Directly expose the battery's two allowlisted native SOC registers."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_device_class = NumberDeviceClass.BATTERY
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator: Fbp1200Coordinator, key: str) -> None:
        super().__init__(coordinator, f"native_{key}_soc")
        self.key = key
        self._attr_name = (
            "Native minimum SOC" if key == "minimum" else "Native maximum SOC"
        )
        self._attr_icon = (
            "mdi:battery-lock" if key == "minimum" else "mdi:battery-charging-100"
        )

    @property
    def native_value(self) -> float | None:
        data_key = "native_min_soc" if self.key == "minimum" else "native_max_soc"
        return self.coordinator.data.get(data_key)

    @property
    def available(self) -> bool:
        data_key = "native_min_soc" if self.key == "minimum" else "native_max_soc"
        return self.coordinator.data.get(data_key) is not None

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_set_native_soc_limit(self.key, value)
