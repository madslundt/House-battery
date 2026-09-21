"""Guarded Home Assistant adapter for proven local battery controls."""

from __future__ import annotations

import logging
import math
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
_SOC_CONTROL_KEYS = (CONF_MIN_SOC_CONTROL, CONF_MAX_SOC_CONTROL)
_UNAVAILABLE_STATES = {"unknown", "unavailable", "none", ""}


def soc_control_problems(
    hass: HomeAssistant,
    config: dict[str, Any],
    *,
    absolute_min_soc: float | None = None,
    reserve_soc: float | None = None,
    maximum_soc: float | None = None,
) -> list[str]:
    """Return blockers for the native SOC controls required for automation."""
    problems: list[str] = []
    requested_values = ((absolute_min_soc, reserve_soc), (maximum_soc,))
    for key, label, requests in zip(
        _SOC_CONTROL_KEYS,
        ("minimum", "maximum"),
        requested_values,
        strict=True,
    ):
        entity_id = config.get(key)
        if not entity_id:
            problems.append(f"{label} SOC control is not configured")
            continue
        state = hass.states.get(entity_id)
        if state is None or state.state.lower() in _UNAVAILABLE_STATES:
            problems.append(f"{label} SOC control is unavailable")
            continue
        try:
            value = float(state.state)
            minimum = float(state.attributes["min"])
            maximum = float(state.attributes["max"])
        except (KeyError, TypeError, ValueError):
            problems.append(f"{label} SOC control has no valid native bounds")
            continue
        if not all(math.isfinite(item) for item in (value, minimum, maximum)) or (
            minimum > maximum or not minimum <= value <= maximum
        ):
            problems.append(f"{label} SOC control reports invalid native bounds")
        else:
            for requested in requests:
                if requested is not None and not minimum <= requested <= maximum:
                    problems.append(
                        f"{label} SOC control cannot accept configured {requested:g}% "
                        f"within native bounds [{minimum:g}, {maximum:g}]"
                    )
    return problems


class LocalControlAdapter:
    """Map planner actions onto the reference integration's verified entities."""

    def __init__(
        self,
        hass: HomeAssistant,
        config: Callable[[], dict[str, Any]],
        runtime: Callable[[], RuntimeState],
        save: Callable[[], Awaitable[None]],
        direct_client: Callable[[], Any | None] | None = None,
        direct_mode_changed: Callable[[str], None] | None = None,
    ) -> None:
        self._hass = hass
        self._config = config
        self._runtime = runtime
        self._save = save
        self._direct_client = direct_client
        self._direct_mode_changed = direct_mode_changed

    async def _async_direct_command(
        self, action: Action, now: datetime, target_soc: float | None
    ) -> tuple[bool, str] | None:
        """Use the built-in TCP adapter when this is a direct-local entry."""
        client = self._direct_client() if self._direct_client else None
        if client is None:
            return None
        runtime = self._runtime()
        limits_warning: str | None = None
        try:
            minimum = round(self._minimum_soc_for(action))
            maximum = round(
                target_soc if target_soc is not None else runtime.settings["target_soc"]
            )
            await client.async_set_limits(minimum, maximum)
        except Exception as exc:
            if action is not Action.SAFE:
                _LOGGER.exception("Local TCP SOC-limit command failed")
                runtime.execution_enabled = False
                await self._save()
                return False, f"local TCP command failed: {exc}"
            limits_warning = f"SOC limits not confirmed: {exc}"
            _LOGGER.warning("%s; continuing with requested safe mode", limits_warning)
        mode = _ACTION_TO_MODE[action]
        try:
            if action is Action.BATTERY or action is Action.SAFE:
                await client.async_set_self_consumption()
            elif action is Action.CHARGE:
                await client.async_set_mode(
                    "Charge",
                    round(runtime.settings["charge_power_w"]),
                    min_soc=minimum,
                    max_soc=maximum,
                )
            else:
                await client.async_set_mode("Idle", 0, min_soc=minimum, max_soc=maximum)
        except Exception as exc:
            _LOGGER.exception("Local TCP mode command failed")
            runtime.execution_enabled = False
            await self._save()
            return False, f"local TCP command failed: {exc}"
        if self._direct_mode_changed:
            self._direct_mode_changed(mode)
        if runtime.last_action != action.value:
            runtime.last_action = action.value
            runtime.last_action_at = now.isoformat()
            runtime.transitions.append(now.isoformat())
        await self._save()
        result = "local TCP command acknowledged; SOC limits read back"
        return True, result if limits_warning is None else f"{result}; {limits_warning}"

    async def _set_number(
        self, key: str, value: float, *, required: bool = False
    ) -> None:
        entity_id = self._config().get(key)
        if not entity_id:
            if required:
                raise ValueError(f"{key} is not configured")
            return
        current = self._hass.states.get(entity_id)
        if current is None:
            raise ValueError(f"{entity_id} is unavailable")
        try:
            minimum = float(current.attributes["min"])
            maximum = float(current.attributes["max"])
            step = abs(float(current.attributes.get("step", 0)))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"{entity_id} has no valid native bounds") from exc
        if not all(math.isfinite(item) for item in (value, minimum, maximum)) or (
            minimum > maximum
        ):
            raise ValueError(f"{entity_id} has invalid requested or native bounds")
        if not minimum <= value <= maximum:
            raise ValueError(
                f"{value:g} for {entity_id} is outside native bounds "
                f"[{minimum:g}, {maximum:g}]"
            )
        try:
            if abs(float(current.state) - value) <= _readback_tolerance(step):
                return
        except (TypeError, ValueError):
            pass
        await self._hass.services.async_call(
            "number",
            "set_value",
            {"entity_id": entity_id, "value": value},
            blocking=True,
        )
        await self._hass.async_block_till_done()
        readback = self._hass.states.get(entity_id)
        try:
            confirmed = float(readback.state) if readback else None
        except (TypeError, ValueError):
            confirmed = None
        if confirmed is None or abs(confirmed - value) > _readback_tolerance(step):
            raise ValueError(
                f"SOC limit read-back did not confirm {entity_id}={value:g}"
            )

    def _minimum_soc_for(self, action: Action) -> float:
        """Return the native lower SOC bound required by an operating action.

        The optimizer models ``reserve_soc`` as unavailable for arbitrage.  A
        Self-Gen command must therefore raise the inverter's native minimum to
        that reserve; otherwise a plan which is energy-neutral at reserve can
        still physically drain down to the emergency minimum.
        """
        runtime = self._runtime()
        if action is Action.SAFE:
            return runtime.settings["absolute_min_soc"]
        return runtime.settings["reserve_soc"]

    async def _apply_limits(self, action: Action, target_soc: float | None) -> None:
        runtime = self._runtime()
        await self._set_number(
            CONF_CHARGE_POWER_CONTROL, runtime.settings["charge_power_w"]
        )
        await self._set_number(
            CONF_DISCHARGE_POWER_CONTROL, runtime.settings["discharge_power_w"]
        )
        await self._set_number(
            CONF_MIN_SOC_CONTROL, self._minimum_soc_for(action), required=True
        )
        await self._set_number(
            CONF_MAX_SOC_CONTROL,
            target_soc if target_soc is not None else runtime.settings["target_soc"],
            required=True,
        )

    async def async_command(
        self, action: Action, now: datetime, *, target_soc: float | None = None
    ) -> tuple[bool, str]:
        """Apply limits, issue one mode change, and require immediate read-back."""
        direct_result = await self._async_direct_command(action, now, target_soc)
        if direct_result is not None:
            return direct_result
        mode = _ACTION_TO_MODE[action]
        mode_entity = self._config()[CONF_OPERATING_MODE]
        runtime = self._runtime()
        limits_warning: str | None = None
        try:
            await self._apply_limits(action, target_soc)
        except Exception as exc:  # The safety command must remain available.
            if action is not Action.SAFE:
                _LOGGER.exception("Battery SOC limit command failed")
                runtime.execution_enabled = False
                await self._save()
                return False, f"command failed: {exc}"
            limits_warning = f"SOC limits not confirmed: {exc}"
            _LOGGER.warning("%s; continuing with requested safe mode", limits_warning)
        try:
            current = self._hass.states.get(mode_entity)
            if current and current.state == mode:
                detail = (
                    "limits confirmed" if limits_warning is None else limits_warning
                )
                return True, f"{detail}; already in requested mode"
            await self._hass.services.async_call(
                "select",
                "select_option",
                {"entity_id": mode_entity, "option": mode},
                blocking=True,
            )
        except Exception as exc:  # Home Assistant service failures vary by adapter
            _LOGGER.exception("Battery command failed")
            runtime.execution_enabled = False
            await self._save()
            return False, f"command failed: {exc}"
        await self._hass.async_block_till_done()
        readback = self._hass.states.get(mode_entity)
        if readback is None or readback.state != mode:
            runtime.execution_enabled = False
            await self._save()
            return False, f"mode read-back did not confirm {mode}"
        if runtime.last_action != action.value:
            runtime.last_action = action.value
            runtime.last_action_at = now.isoformat()
            runtime.transitions.append(now.isoformat())
        await self._save()
        detail = "command confirmed by Operating Mode entity"
        return True, detail if limits_warning is None else f"{detail}; {limits_warning}"


def _readback_tolerance(step: float) -> float:
    """Avoid false rejects from decimal formatting while never accepting one step off."""
    return min(0.01, step / 100) if step else 0.001
