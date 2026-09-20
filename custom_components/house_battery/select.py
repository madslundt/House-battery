"""Direct local operating-mode control for FBP1200 devices."""

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import MODE_BATTERY, MODE_CHARGE, MODE_GRID
from .coordinator import Fbp1200Coordinator
from .entity import Fbp1200Entity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: Fbp1200Coordinator = entry.runtime_data
    if coordinator.is_direct_local:
        async_add_entities([FbpLocalOperatingModeSelect(coordinator)])


class FbpLocalOperatingModeSelect(Fbp1200Entity, SelectEntity):
    """A local-commanded mode; physical power remains the observed evidence."""

    _attr_name = "Operating mode"
    _attr_icon = "mdi:battery-sync"
    _attr_options = (MODE_BATTERY, MODE_GRID, MODE_CHARGE)

    def __init__(self, coordinator: Fbp1200Coordinator) -> None:
        super().__init__(coordinator, "operating_mode")

    @property
    def current_option(self) -> str | None:
        return self.coordinator.data.get("local_operating_mode")

    @property
    def available(self) -> bool:
        return bool(self.coordinator.data.get("local_connected"))

    @property
    def extra_state_attributes(self) -> dict[str, str]:
        return {
            "source": "local_commanded_state",
            "note": "The vendor app can change mode separately; use battery power sensors as physical evidence.",
        }

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_set_manual_mode(option)
