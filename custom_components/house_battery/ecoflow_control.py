"""EF BLE control strategy for EcoFlow battery entries."""

from __future__ import annotations

import logging
import math
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any, Protocol

from homeassistant.core import HomeAssistant

from .const import (
    CONF_CHARGE_POWER_CONTROL,
    CONF_DEVICE_TYPE,
    CONF_ECOFLOW_AC_PORT_SWITCH,
    CONF_GRID_ISOLATION_SWITCH,
    CONF_MAX_SOC_CONTROL,
    CONF_MIN_SOC_CONTROL,
    DEVICE_ECOFLOW,
)
from .control_errors import CommandSuperseded
from .ecoflow_capabilities import (
    ecoflow_charge_power_range_w,
    ecoflow_soc_control_range,
)
from .models import Action
from .runtime import RuntimeState

_LOGGER = logging.getLogger(__name__)


class _ControlPort(Protocol):
    """Internal operations the EcoFlow strategy shares with the actuator."""

    _hass: HomeAssistant
    _config: Callable[[], dict[str, Any]]
    _runtime: Callable[[], RuntimeState]

    def _check_command_current(self, generation: int | None) -> None: ...

    def _minimum_soc_for(
        self, action: Action, *, grid_available: bool | None = None
    ) -> float: ...

    async def _set_number(
        self, key: str, value: float, *, required: bool = False
    ) -> None: ...

    async def _set_switch(self, entity_id: str, enabled: bool) -> None: ...

    async def async_set_grid_isolation(
        self, enabled: bool, *, generation: int | None = None
    ) -> bool: ...

    _save: Callable[[], Awaitable[None]]


class EcoFlowControlAdapter:
    """Map planner actions to EF BLE numbers and switches with read-back."""

    def __init__(self, control: _ControlPort) -> None:
        self._control = control

    async def async_command(
        self,
        action: Action,
        now: datetime,
        target_soc: float | None,
        grid_available: bool | None,
        generation: int | None,
    ) -> tuple[bool, str] | None:
        """Apply an EcoFlow action, or return ``None`` for another device."""
        control = self._control
        config = control._config()
        if config.get(CONF_DEVICE_TYPE) != DEVICE_ECOFLOW:
            return None
        if not config.get(CONF_GRID_ISOLATION_SWITCH):
            return False, "EcoFlow automatic control requires a grid isolation switch"
        if action is Action.CHARGE:
            charge_entity = config.get(CONF_CHARGE_POWER_CONTROL)
            charge_state = control._hass.states.get(charge_entity) if charge_entity else None
            if charge_state is None:
                return await self._freeze_after_charge_control_loss(
                    now, grid_available, generation
                )
            try:
                ecoflow_charge_power_range_w(charge_state)
            except ValueError:
                return await self._freeze_after_charge_control_loss(
                    now, grid_available, generation
                )

        runtime = control._runtime()
        battery_transfer = action is Action.BATTERY or (
            action is Action.SAFE and grid_available is False
        )
        isolation_confirmed = True
        try:
            control._check_command_current(generation)
            soc_entity = config.get("soc_entity")
            soc_state = control._hass.states.get(soc_entity) if soc_entity else None
            if soc_state is None:
                raise ValueError("EcoFlow SOC is unavailable")
            soc = float(soc_state.state)
            if not math.isfinite(soc) or not 0 <= soc <= 100:
                raise ValueError("EcoFlow SOC is invalid")

            grid_limits: tuple[float, float] | None = None
            if action is Action.GRID or (
                action is Action.SAFE and grid_available is not False
            ):
                try:
                    grid_limits = self._grid_hold_limits(soc)
                except ValueError as exc:
                    # Check the unit's native SOC ranges before reconnecting
                    # grid. Otherwise an unsupported hold request can connect
                    # mains while the previous limits still permit charging.
                    return False, f"EcoFlow cannot hold SOC in Grid mode: {exc}"

            if not battery_transfer:
                isolation_confirmed = await control.async_set_grid_isolation(True)
                if not isolation_confirmed and action is not Action.SAFE:
                    return False, "grid isolation switch did not confirm on"

            await control._set_switch(config[CONF_ECOFLOW_AC_PORT_SWITCH], True)
            control._check_command_current(generation)

            if action is Action.BATTERY:
                minimum = self._soc_limit(
                    CONF_MIN_SOC_CONTROL,
                    control._minimum_soc_for(action, grid_available=grid_available),
                    round_up=True,
                )
                maximum = self._soc_limit(
                    CONF_MAX_SOC_CONTROL,
                    target_soc if target_soc is not None else runtime.settings["target_soc"],
                    round_up=False,
                )
            elif action is Action.CHARGE:
                maximum = self._soc_limit(
                    CONF_MAX_SOC_CONTROL,
                    target_soc if target_soc is not None else runtime.settings["target_soc"],
                    round_up=False,
                )
                minimum = self._soc_limit(
                    CONF_MIN_SOC_CONTROL, min(soc, maximum), round_up=False
                )
            elif action is Action.SAFE and grid_available is False:
                # Keep backup output available down to the emergency floor.
                if soc <= runtime.settings["absolute_min_soc"]:
                    minimum = maximum = self._common_freeze_limit(
                        soc, round_up=True
                    )
                else:
                    minimum = self._soc_limit(
                        CONF_MIN_SOC_CONTROL,
                        runtime.settings["absolute_min_soc"],
                        round_up=True,
                    )
                    maximum = self._soc_limit(
                        CONF_MAX_SOC_CONTROL, soc, round_up=False
                    )
                    if minimum > maximum:
                        minimum = maximum = self._common_freeze_limit(soc)
            else:
                # Use a shared threshold when the controls overlap. Some
                # EcoFlow units instead expose a charge ceiling no lower than
                # 50% and a discharge floor no higher than 30%; in that case
                # keep those native limits and require SOC to be at/above the
                # charge ceiling before connecting grid.
                minimum, maximum = (
                    grid_limits
                    if grid_limits is not None
                    else self._grid_hold_limits(soc)
                )

            await control._set_number(CONF_MIN_SOC_CONTROL, minimum, required=True)
            control._check_command_current(generation)
            await control._set_number(CONF_MAX_SOC_CONTROL, maximum, required=True)
            control._check_command_current(generation)

            if action is Action.CHARGE:
                charge_control = control._hass.states.get(
                    config[CONF_CHARGE_POWER_CONTROL]
                )
                if charge_control is None:
                    raise ValueError("EcoFlow AC Charging Speed is unavailable")
                try:
                    charge_min = float(charge_control.attributes["min"])
                    charge_max = float(charge_control.attributes["max"])
                    charge_step = float(charge_control.attributes["step"])
                except (KeyError, TypeError, ValueError) as exc:
                    raise ValueError(
                        "EcoFlow AC Charging Speed has no valid native bounds"
                    ) from exc
                unit = str(
                    charge_control.attributes.get("unit_of_measurement", "")
                ).strip()
                to_watts = {"W": 1.0, "kW": 1000.0}.get(unit)
                if to_watts is None:
                    raise ValueError("EcoFlow AC Charging Speed must use W or kW")
                if not all(
                    math.isfinite(value)
                    for value in (charge_min, charge_max, charge_step)
                ) or charge_min < 0 or charge_max <= charge_min or charge_step <= 0:
                    raise ValueError("EcoFlow AC Charging Speed has invalid native bounds")
                charge_power_w = min(
                    max(runtime.settings["charge_power_w"], charge_min * to_watts),
                    charge_max * to_watts,
                )
                charge_native = charge_power_w / to_watts
                charge_native = min(
                    max(
                        charge_min
                        + round((charge_native - charge_min) / charge_step)
                        * charge_step,
                        charge_min,
                    ),
                    charge_max,
                )
                await control._set_number(
                    CONF_CHARGE_POWER_CONTROL,
                    charge_native,
                    required=True,
                )
            control._check_command_current(generation)

            if battery_transfer:
                # Confirm the SOC cutoff before transferring the load.
                if not await control.async_set_grid_isolation(
                    False, generation=generation
                ):
                    return False, (
                        "grid isolation switch did not confirm off; "
                        "discharge inhibited"
                    )
        except CommandSuperseded:
            raise
        except Exception as exc:
            _LOGGER.exception("EcoFlow entity command failed")
            if action is Action.CHARGE:
                frozen, freeze_detail = await self.async_command(
                    Action.SAFE,
                    now,
                    target_soc=None,
                    grid_available=grid_available,
                    generation=generation,
                )
                if frozen:
                    return False, f"EcoFlow charge failed; SOC limits frozen safely: {exc}"
                return False, f"EcoFlow charge failed; safety freeze failed: {freeze_detail}"
            if battery_transfer:
                await control.async_set_grid_isolation(True)
            return False, f"EcoFlow command failed: {exc}"

        if runtime.last_action != action.value:
            runtime.last_action = action.value
            runtime.last_action_at = now.isoformat()
            runtime.transitions.append(now.isoformat())
        await control._save()
        if not isolation_confirmed:
            return False, "grid isolation switch did not confirm on; SOC limits frozen"
        return True, "EcoFlow AC port and SOC limits confirmed"

    def _soc_limit(self, key: str, value: float, *, round_up: bool) -> float:
        """Snap a SOC command to the selected EF BLE number's native step."""
        entity_id = self._control._config().get(key)
        state = self._control._hass.states.get(entity_id) if entity_id else None
        if state is None:
            raise ValueError(f"{key} is unavailable")
        minimum, maximum, step = ecoflow_soc_control_range(state)
        increments = (min(max(value, minimum), maximum) - minimum) / step
        rounded = math.ceil(increments - 1e-9) if round_up else math.floor(increments + 1e-9)
        return min(maximum, max(minimum, minimum + rounded * step))

    def _common_freeze_limit(self, soc: float, *, round_up: bool = False) -> float:
        """Find a shared native SOC value on the safe side of live SOC."""
        config = self._control._config()
        min_entity = config.get(CONF_MIN_SOC_CONTROL)
        max_entity = config.get(CONF_MAX_SOC_CONTROL)
        min_state = self._control._hass.states.get(min_entity) if min_entity else None
        max_state = self._control._hass.states.get(max_entity) if max_entity else None
        if min_state is None or max_state is None:
            raise ValueError("EcoFlow SOC controls are unavailable")
        min_low, min_high, min_step = ecoflow_soc_control_range(min_state)
        max_low, max_high, max_step = ecoflow_soc_control_range(max_state)
        if round_up:
            low = max(soc, min_low, max_low)
            high = min(min_high, max_high)
        else:
            low = max(min_low, max_low)
            high = min(soc, min_high, max_high)
        if high < low:
            raise ValueError("EcoFlow SOC controls have no shared safe value for current SOC")
        index = (
            math.ceil((low - min_low) / min_step - 1e-9)
            if round_up
            else math.floor((high - min_low) / min_step + 1e-9)
        )
        remaining = (
            math.floor((high - min_low) / min_step + 1e-9) - index + 1
            if round_up
            else index + 1
        )
        attempts = min(max(0, remaining), 100_001)
        for _ in range(attempts):
            candidate = min_low + index * min_step
            max_index = (candidate - max_low) / max_step
            if low - 1e-9 <= candidate <= high + 1e-9 and abs(
                max_index - round(max_index)
            ) <= 1e-6:
                return candidate
            index += 1 if round_up else -1
        raise ValueError("EcoFlow SOC controls have incompatible native steps")

    def _grid_hold_limits(self, soc: float) -> tuple[float, float]:
        """Return safe native limits for grid pass-through without SOC transfer.

        Equal limits are preferred when the EF BLE controls share an allowed
        SOC value. Some models have disjoint ranges, such as a minimum 50%
        charge ceiling and a maximum 30% discharge floor. Those can hold on
        grid only once live SOC is already at/above the charge ceiling; below
        it, connecting AC would charge the battery, so fail before switching.
        """
        try:
            limit = self._common_freeze_limit(soc)
        except ValueError:
            control = self._control
            config = control._config()
            discharge_state = control._hass.states.get(
                config.get(CONF_MIN_SOC_CONTROL)
            )
            charge_state = control._hass.states.get(
                config.get(CONF_MAX_SOC_CONTROL)
            )
            if discharge_state is None or charge_state is None:
                raise ValueError("EcoFlow SOC controls are unavailable")
            discharge_low, discharge_high, _ = ecoflow_soc_control_range(
                discharge_state
            )
            charge_low, charge_high, _ = ecoflow_soc_control_range(charge_state)
            if discharge_high > charge_low:
                raise ValueError(
                    "EcoFlow SOC controls have no safe grid pass-through band"
                )
            if soc + 1e-9 < charge_low:
                raise ValueError(
                    "minimum charge ceiling is "
                    f"{charge_low:g}% but live SOC is {soc:g}%; "
                    "grid connection would charge the battery"
                )
            minimum = self._soc_limit(
                CONF_MIN_SOC_CONTROL, discharge_high, round_up=False
            )
            maximum = self._soc_limit(
                CONF_MAX_SOC_CONTROL, charge_low, round_up=True
            )
            if not discharge_low <= minimum <= maximum <= charge_high:
                raise ValueError("EcoFlow SOC controls have no safe grid pass-through band")
            return minimum, maximum
        return limit, limit

    async def _freeze_after_charge_control_loss(
        self,
        now: datetime,
        grid_available: bool | None,
        generation: int | None,
    ) -> tuple[bool, str]:
        """Stop a held EcoFlow charge when its writable speed control is lost."""
        frozen, detail = await self.async_command(
            Action.SAFE,
            now,
            target_soc=None,
            grid_available=grid_available,
            generation=generation,
        )
        if frozen:
            return False, "EcoFlow AC Charging Speed is unavailable; charge frozen safely"
        return False, f"EcoFlow AC Charging Speed is unavailable; safety freeze failed: {detail}"
