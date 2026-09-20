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
    async_add_entities([FbpAutomaticControlSwitch(entry.runtime_data)])


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
