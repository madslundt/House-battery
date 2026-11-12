"""Read and validate capability bounds exposed by EF BLE entities."""

from __future__ import annotations

import math
from typing import Any

_POWER_TO_W = {"W": 1.0, "kW": 1000.0}


def ecoflow_charge_power_limit_w(state: Any) -> float:
    """Return the AC charging number's native maximum in watts."""
    _, maximum, _ = ecoflow_charge_power_range_w(state)
    return maximum


def ecoflow_charge_power_range_w(state: Any) -> tuple[float, float, float]:
    """Return the AC charging number's native range normalized to watts."""
    unit, minimum, maximum, step = _number_range(state)
    factor = _POWER_TO_W.get(unit)
    if factor is None:
        raise ValueError("AC Charging Speed must use W or kW")
    if minimum < 0 or maximum <= minimum or step <= 0:
        raise ValueError("AC Charging Speed has invalid native limits")
    maximum = minimum + math.floor((maximum - minimum) / step + 1e-9) * step
    if maximum <= minimum:
        raise ValueError("AC Charging Speed has no valid native step")
    return minimum * factor, maximum * factor, step * factor


def ecoflow_soc_control_range(state: Any) -> tuple[float, float, float]:
    """Return validated native percent bounds for an EF BLE SOC number."""
    unit, minimum, maximum, step = _number_range(state)
    if unit != "%":
        raise ValueError("SOC controls must use percent")
    if minimum < 0 or maximum > 100 or maximum <= minimum or step <= 0:
        raise ValueError("SOC controls have invalid native limits")
    maximum = minimum + math.floor((maximum - minimum) / step + 1e-9) * step
    if maximum <= minimum:
        raise ValueError("SOC controls have no valid native step")
    return minimum, maximum, step


def _number_range(state: Any) -> tuple[str, float, float, float]:
    attributes = state.attributes
    unit = str(attributes.get("unit_of_measurement", "")).strip()
    try:
        current = float(state.state)
        minimum = float(attributes["min"])
        maximum = float(attributes["max"])
        step = float(attributes["step"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("number entity is unavailable or has no valid native range") from exc
    if not all(math.isfinite(value) for value in (current, minimum, maximum, step)):
        raise ValueError("number entity has invalid native limits")
    return unit, minimum, maximum, step
