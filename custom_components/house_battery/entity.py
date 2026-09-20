"""Shared entity base for the FBP1200 optimizer."""

from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import Fbp1200Coordinator


class Fbp1200Entity(CoordinatorEntity[Fbp1200Coordinator]):
    """Attach optimizer entities to one Home Assistant device."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: Fbp1200Coordinator, key: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{key}"
        self._attr_device_info = coordinator.device_info
