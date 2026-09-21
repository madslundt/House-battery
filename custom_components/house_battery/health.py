"""Freshness and safety checks for bound physical battery entities."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from homeassistant.core import HomeAssistant

from .const import (
    CONF_BATTERY_CHARGE_POWER,
    CONF_BATTERY_DISCHARGE_POWER,
    CONF_FAULT,
    CONF_GRID_AVAILABLE,
    CONF_GRID_IMPORT_POWER,
    CONF_LOAD_POWER,
    CONF_ONLINE,
    CONF_OPERATING_MODE,
    CONF_SOC,
    TELEMETRY_STALE_AFTER,
)
from .policy import parse_grid_available

_BAD_STATES = {"unknown", "unavailable", "none", ""}
_FAULT_CLEAR = {"0", "off", "ok", "normal", "none", "false", "clear"}
_ONLINE = {"1", "on", "online", "connected", "true"}
_REQUIRED = (
    CONF_SOC,
    CONF_LOAD_POWER,
    CONF_GRID_IMPORT_POWER,
    CONF_GRID_AVAILABLE,
    CONF_OPERATING_MODE,
    CONF_BATTERY_CHARGE_POWER,
    CONF_BATTERY_DISCHARGE_POWER,
)
_PHYSICAL = (*_REQUIRED, CONF_FAULT, CONF_ONLINE)


def get_health_problems(
    hass: HomeAssistant, config: dict[str, Any], now: datetime
) -> list[str]:
    """Return safety blockers for required, physical local telemetry."""

    def state(key: str):
        return hass.states.get(config[key]) if config.get(key) else None

    required = _REQUIRED
    if config.get("host"):
        # Battery SOC, charge/discharge power, and operating mode come from
        # the built-in TCP adapter. Its direct telemetry also lets the
        # coordinator derive load from a grid-import meter.
        required = (CONF_GRID_IMPORT_POWER, CONF_GRID_AVAILABLE)
    problems = [
        f"{key} unavailable"
        for key in required
        if (value := state(key)) is None or value.state.lower() in _BAD_STATES
    ]
    grid = state(CONF_GRID_AVAILABLE)
    if parse_grid_available(grid.state if grid else None) is None:
        problems.append("grid availability is unknown")
    for key in _PHYSICAL:
        if (entity_id := config.get(key)) and (value := hass.states.get(entity_id)):
            reported = getattr(value, "last_reported", None) or value.last_updated
            if now - reported.astimezone(UTC) > TELEMETRY_STALE_AFTER:
                problems.append(f"{entity_id} stale")
    if (fault := state(CONF_FAULT)) and fault.state.lower() not in _FAULT_CLEAR:
        problems.append(f"battery fault: {fault.state}")
    if (online := state(CONF_ONLINE)) and online.state.lower() not in _ONLINE:
        problems.append(f"battery offline: {online.state}")
    return problems
