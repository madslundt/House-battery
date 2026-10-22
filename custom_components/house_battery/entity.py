"""Shared entity base for the battery optimizer."""

from typing import Any

from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_ACCOUNTING_LOAD_POWER,
    CONF_GRID_IMPORT_POWER,
    CONF_PRICE_FORECAST_ENTITIES,
    CONF_PRICE_FORECAST_ENTITY,
)
from .coordinator import Fbp1200Coordinator

# Keep everyday operation, safety, consumption learning and Energy dashboard
# totals visible. Other entities remain available for explicit opt-in.
DEFAULT_ENTITY_KEYS = frozenset(
    {
        "system_state",
        "battery_mode",
        "soc",
        "load_power_w",
        "battery_power_w",
        "current_price_dkk_per_kwh",
        "expected_savings_dkk",
        "operation_plan",
        "current_plan_slot",
        "actual_battery_history",
        "planned_load_power",
        "load_learning_confidence_pct",
        "today_charge_kwh",
        "today_discharge_kwh",
        "today_battery_net_savings_dkk",
        "lifetime_battery_net_savings_dkk",
        "lifetime_charge_kwh",
        "lifetime_discharge_kwh",
        "optimizer_problem",
        "grid_available",
        "export_safety_fault",
        "grid_import_power_w",
        "grid_export_power_w",
        "automatic_control",
        "opportunistic_full_charge",
        "today_net_savings_dkk",
        "lifetime_net_savings_dkk",
        "external_price_forecast",
        "force_safe_mode",
        "setting_reserve_soc",
        "setting_target_soc",
        "setting_charge_power_w",
        "setting_discharge_power_w",
        "setting_minimum_profit_dkk_per_kwh",
    }
)


def enabled_by_default(key: str, config: dict[str, Any], *, direct_local: bool) -> bool:
    """Enable only useful entities supported by the configured inputs."""
    if key not in DEFAULT_ENTITY_KEYS:
        return False
    if key in {"grid_import_power_w", "grid_export_power_w", "export_safety_fault"}:
        return bool(config.get(CONF_GRID_IMPORT_POWER))
    if key == "external_price_forecast":
        return bool(
            config.get(
                CONF_PRICE_FORECAST_ENTITIES, config.get(CONF_PRICE_FORECAST_ENTITY)
            )
        )
    if key in {"today_net_savings_dkk", "lifetime_net_savings_dkk"}:
        return not direct_local or bool(
            config.get(CONF_GRID_IMPORT_POWER)
            and config.get(CONF_ACCOUNTING_LOAD_POWER)
        )
    return True


class Fbp1200Entity(CoordinatorEntity[Fbp1200Coordinator]):
    """Attach optimizer entities to one Home Assistant device."""

    _attr_has_entity_name = True
    # Live explanatory attributes already have a bounded local evidence store.
    # Keep them available to dashboards without duplicating them every refresh
    # in Recorder. Numeric states and their statistics remain recordable.
    _unrecorded_attributes = frozenset({"*"})

    def __init__(self, coordinator: Fbp1200Coordinator, key: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{key}"
        self._attr_device_info = coordinator.device_info
        self._attr_entity_registry_enabled_default = enabled_by_default(
            key,
            coordinator.config,
            direct_local=getattr(coordinator, "is_direct_local", False),
        )
