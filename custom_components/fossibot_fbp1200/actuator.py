"""Guarded Home Assistant adapter for proven local FBP1200 controls."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from homeassistant.core import HomeAssistant

from .const import (
    CONF_CHARGE_POWER_CONTROL,
    CONF_DISCHARGE_POWER_CONTROL,
    CONF_MAX_SOC_CONTROL,
    CONF_MIN_SOC_CONTROL,
    CONF_OPERATING_MODE,
    MODE_BATTERY,
    MODE_CHARGE,
    MODE_GRID,
    MODE_SAFE,
)
from .models import Action
from .runtime import RuntimeState

_LOGGER = logging.getLogger(__name__)
_ACTION_TO_MODE = {
    Action.CHARGE: MODE_CHARGE,
    Action.GRID: MODE_GRID,
    Action.BATTERY: MODE_BATTERY,
    Action.SAFE: MODE_SAFE,
}


class LocalControlAdapter:
    """Map planner actions onto the reference integration's verified entities."""

    def __init__(
        self,
        hass: HomeAssistant,
        config: Callable[[], dict[str, Any]],
        runtime: Callable[[], RuntimeState],
        save: Callable[[], Awaitable[None]],
    ) -> None:
        self._hass = hass
        self._config = config
        self._runtime = runtime
        self._save = save

    async def _set_number(self, key: str, value: float) -> None:
        entity_id = self._config().get(key)
        if not entity_id:
            return
        current = self._hass.states.get(entity_id)
        try:
            if current and abs(float(current.state) - value) < 0.5:
                return
        except (TypeError, ValueError):
            pass
        await self._hass.services.async_call(
            "number",
            "set_value",
            {"entity_id": entity_id, "value": value},
            blocking=True,
        )

    async def async_command(
        self, action: Action, now: datetime, *, target_soc: float | None = None
    ) -> tuple[bool, str]:
        """Apply limits, issue one mode change, and require immediate read-back."""
        mode = _ACTION_TO_MODE[action]
        mode_entity = self._config()[CONF_OPERATING_MODE]
        runtime = self._runtime()
        try:
            await self._set_number(
                CONF_CHARGE_POWER_CONTROL, runtime.settings["charge_power_w"]
            )
            await self._set_number(
                CONF_DISCHARGE_POWER_CONTROL, runtime.settings["discharge_power_w"]
            )
            await self._set_number(
                CONF_MIN_SOC_CONTROL, runtime.settings["absolute_min_soc"]
            )
            await self._set_number(
                CONF_MAX_SOC_CONTROL, target_soc or runtime.settings["target_soc"]
            )
            current = self._hass.states.get(mode_entity)
            if current and current.state == mode:
                return True, "limits confirmed; already in requested mode"
            await self._hass.services.async_call(
                "select",
                "select_option",
                {"entity_id": mode_entity, "option": mode},
                blocking=True,
            )
        except Exception as exc:  # Home Assistant service failures vary by adapter
            _LOGGER.exception("FBP1200 command failed")
            runtime.execution_enabled = False
            await self._save()
            return False, f"command failed: {exc}"
        readback = self._hass.states.get(mode_entity)
        if readback is None or readback.state != mode:
            runtime.execution_enabled = False
            await self._save()
            return False, f"mode read-back did not confirm {mode}"
        runtime.last_action = action.value
        runtime.last_action_at = now.isoformat()
        runtime.transitions.append(now.isoformat())
        await self._save()
        return True, "command confirmed by Operating Mode entity"
