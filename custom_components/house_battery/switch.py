"""Explicit runtime authorization for automatic local control."""

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import Fbp1200Coordinator
from .entity import Fbp1200Entity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: Fbp1200Coordinator = entry.runtime_data
    async_add_entities(
        [
            FbpAutomaticControlSwitch(coordinator),
            FbpExternalForecastSwitch(coordinator),
            FbpOpportunisticChargingSwitch(coordinator),
        ]
    )


class FbpAutomaticControlSwitch(Fbp1200Entity, SwitchEntity):
    """Opt in to writes after the separate commissioning gate is complete."""

    _attr_name = "Automatic control"
    _attr_icon = "mdi:battery-sync-outline"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: Fbp1200Coordinator) -> None:
        super().__init__(coordinator, "automatic_control")

    @property
    def is_on(self) -> bool:
        return self.coordinator.runtime.execution_enabled

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        return {
            "commissioned": self.coordinator.data.get("commissioned", False),
            "safety": "Writes only through the configured local Operating Mode entity",
        }

    async def async_turn_on(self, **kwargs: object) -> None:
        try:
            await self.coordinator.async_set_execution_enabled(True)
        except ValueError as exc:
            raise HomeAssistantError(str(exc)) from exc

    async def async_turn_off(self, **kwargs: object) -> None:
        await self.coordinator.async_set_execution_enabled(False)


class FbpExternalForecastSwitch(Fbp1200Entity, SwitchEntity):
    """Opt in to using forecast intervals after the known price horizon."""

    _attr_name = "Use external price forecast"
    _attr_icon = "mdi:chart-timeline-variant"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: Fbp1200Coordinator) -> None:
        super().__init__(coordinator, "external_price_forecast")

    @property
    def available(self) -> bool:
        return bool(self.coordinator._forecast_entities())

    @property
    def is_on(self) -> bool:
        return self.coordinator.runtime.forecast_enabled

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        return {
            "source": self.coordinator.data.get("price_forecast_source"),
            "available_slots": self.coordinator.data.get(
                "price_forecast_available_slots", 0
            ),
            "used_slots": self.coordinator.data.get("price_forecast_used_slots", 0),
            "status": self.coordinator.data.get("price_forecast_status"),
            "safety": "Known prices always win; only a contiguous extension is used.",
        }

    async def async_turn_on(self, **kwargs: object) -> None:
        try:
            await self.coordinator.async_set_forecast_enabled(True)
        except ValueError as exc:
            raise HomeAssistantError(str(exc)) from exc

    async def async_turn_off(self, **kwargs: object) -> None:
        await self.coordinator.async_set_forecast_enabled(False)


class FbpOpportunisticChargingSwitch(Fbp1200Entity, SwitchEntity):
    """Allow a higher charge target only for a proven profitable cycle."""

    _attr_name = "Allow opportunistic full charge"
    _attr_icon = "mdi:battery-charging-100"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: Fbp1200Coordinator) -> None:
        super().__init__(coordinator, "opportunistic_full_charge")

    @property
    def is_on(self) -> bool:
        return self.coordinator.runtime.opportunistic_charging_enabled

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        return {
            "active": self.coordinator.data.get("extra_storage_active", False),
            "normal_target_soc": self.coordinator.runtime.settings["target_soc"],
            "opportunistic_target_soc": self.coordinator.runtime.settings[
                "opportunistic_target_soc"
            ],
            "effective_target_soc": self.coordinator.data.get("effective_target_soc"),
            "incremental_savings_dkk": self.coordinator.data.get(
                "opportunistic_incremental_savings_dkk"
            ),
            "reason": self.coordinator.data.get("extra_storage_reason"),
        }

    async def async_turn_on(self, **kwargs: object) -> None:
        await self.coordinator.async_set_opportunistic_charging_enabled(True)

    async def async_turn_off(self, **kwargs: object) -> None:
        await self.coordinator.async_set_opportunistic_charging_enabled(False)
