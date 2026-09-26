"""Manual operating override for the battery storage controller.

There are two distinct controls here:

* A configured native **Battery Operating Mode** select is owned by the device
  integration. Direct-local FBP1200 entries instead write verified TCP control
  slots directly.
* This **Operation mode** select is the integration's own policy control with
  ``auto / charge / battery / grid``.  ``auto`` follows the optimizer plan; a
  forced mode commands the physical actuator every refresh.
"""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    OVERRIDE_AUTO,
    OVERRIDE_OPTIONS,
)
from .coordinator import Fbp1200Coordinator
from .entity import Fbp1200Entity

_OPERATION_NAME = "Operation mode"
_OPERATION_ICON = "mdi:battery-clock"


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: Fbp1200Coordinator = entry.runtime_data
    async_add_entities([FbpOperatingModeSelect(coordinator)])


class FbpOperatingModeSelect(Fbp1200Entity, SelectEntity):
    """Force the commanded battery action to test each physical function.

    ``auto`` (default) follows the optimizer plan. A forced mode commands that
    action every refresh regardless of price by setting the native Battery
    Operating Mode:

    - ``charge`` raises the native ceiling to the maximum charge SOC and never
      charges above it.
    - ``battery`` lowers the native floor to the arbitrage reserve SOC and never
      discharges below it.
    - ``grid`` holds the inverter idle.

    The override only changes the commanded action; it never bypasses the
    configured SOC ceiling/floor.  It is the integration's own control and is
    separate from the native ``Battery Operating Mode`` hardware select.
    """

    _attr_name = _OPERATION_NAME
    _attr_icon = _OPERATION_ICON
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: Fbp1200Coordinator) -> None:
        super().__init__(coordinator, "operating_mode_override")
        self._attr_options = list(OVERRIDE_OPTIONS)

    @property
    def current_option(self) -> str:
        return self.coordinator.runtime.override_action

    async def async_select_option(self, option: str) -> None:
        try:
            await self.coordinator.async_set_override_action(option)
        except ValueError as exc:
            raise HomeAssistantError(str(exc)) from exc

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        return {
            "mode": self.coordinator.runtime.override_action,
            "auto_follows_plan": self.coordinator.runtime.override_action
            == OVERRIDE_AUTO,
            "note": (
                "auto follows the plan; forced modes command the native "
                "Battery Operating Mode every refresh without bypassing the "
                "SOC ceiling/floor."
            ),
        }
