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
        [
            FbpHealthBinarySensor(coordinator),
            FbpGridAvailableBinarySensor(coordinator),
            FbpExportDetectedBinarySensor(coordinator),
            FbpExportSafetyFaultBinarySensor(coordinator),
        ]
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


class FbpExportDetectedBinarySensor(Fbp1200Entity, BinarySensorEntity):
    """Flag any real grid export as measured by the meter.

    Zero export is enforced by the Self-Gen/Zero Export firmware mode, but this
    sensor is the independent detector: it confirms the guarantee holds. It is
    ``on" whenever the meter reports export above the noise floor, regardless of
    whether the optimizer currently writes to the inverter."""

    _attr_name = "Export detected"
    _attr_device_class = BinarySensorDeviceClass.POWER
    _attr_icon = "mdi:export"

    def __init__(self, coordinator: Fbp1200Coordinator) -> None:
        super().__init__(coordinator, "export_detected")

    @property
    def is_on(self) -> bool | None:
        return self.coordinator.data.get("export_detected")

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        return {
            "power_w": self.coordinator.data.get("export_power_w"),
            "note": "Any grid export is a fault; zero export is a firmware guarantee of the Self-Gen mode.",
        }


class FbpExportSafetyFaultBinarySensor(Fbp1200Entity, BinarySensorEntity):
    """Latch while the optimizer is enabled and the meter reports export.

    Once latched, the coordinator disables further writes so an export cannot
    continue through the next planning cycle. It stays set until an operator
    resets execution (or the entry reloads), matching the fail-closed intent."""

    _attr_name = "Export safety fault"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_icon = "mdi:alert-octagon"

    def __init__(self, coordinator: Fbp1200Coordinator) -> None:
        super().__init__(coordinator, "export_safety_fault")

    @property
    def is_on(self) -> bool | None:
        return self.coordinator.data.get("export_safety_fault")

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        return {
            "source": "grid meter",
            "reset": "Disable then re-enable optimizer control to clear the latch.",
        }
