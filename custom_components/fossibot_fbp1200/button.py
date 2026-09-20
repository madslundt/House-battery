"""Immediate safe-mode control."""

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import Fbp1200Coordinator
from .entity import Fbp1200Entity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities([FbpForceSafeButton(entry.runtime_data)])


class FbpForceSafeButton(Fbp1200Entity, ButtonEntity):
    """Disable optimization and restore the adapter's native safe mode."""

    _attr_name = "Force safe mode"
    _attr_icon = "mdi:shield-alert"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: Fbp1200Coordinator) -> None:
        super().__init__(coordinator, "force_safe_mode")

    async def async_press(self) -> None:
        await self.coordinator.async_force_safe()
