"""Optimizer health binary sensor."""

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import Fbp1200Coordinator
from .entity import Fbp1200Entity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: Fbp1200Coordinator = entry.runtime_data
    async_add_entities(
        [FbpHealthBinarySensor(coordinator), FbpGridAvailableBinarySensor(coordinator)]
    )


class FbpHealthBinarySensor(Fbp1200Entity, BinarySensorEntity):
    _attr_name = "Optimizer problem"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, coordinator: Fbp1200Coordinator) -> None:
        super().__init__(coordinator, "optimizer_problem")

    @property
    def is_on(self) -> bool:
        return not self.coordinator.data.get("healthy", False)

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        return {
            "problems": self.coordinator.data.get("health_problems", []),
            "reason": self.coordinator.data.get("reason"),
        }


class FbpGridAvailableBinarySensor(Fbp1200Entity, BinarySensorEntity):
    """Expose the independently bound on-grid signal, never import power."""

    _attr_name = "Grid available"
    _attr_device_class = BinarySensorDeviceClass.POWER
    _attr_icon = "mdi:transmission-tower"

    def __init__(self, coordinator: Fbp1200Coordinator) -> None:
        super().__init__(coordinator, "grid_available")

    @property
    def is_on(self) -> bool | None:
        return self.coordinator.data.get("grid_available")

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        return {
            "source": self.coordinator.config.get("grid_available_entity"),
            "note": "This is physical on-grid availability, not whether the optimizer currently uses grid power.",
        }
