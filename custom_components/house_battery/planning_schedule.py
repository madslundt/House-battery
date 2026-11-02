"""Stable economic-plan inputs, independent of the telemetry polling cadence."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from json import dumps
from math import isfinite
from typing import Any

from .models import PlannerSettings, PriceSlot


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Planning timestamps must be timezone-aware")
    return value.astimezone(UTC)


def planning_key(
    slots: Iterable[PriceSlot],
    *,
    revision: int,
    settings: PlannerSettings,
    forecast_enabled: bool,
    opportunistic_enabled: bool,
    scheduled_loads: list[dict[str, Any]],
    soc: float,
    current_discharge_enabled: bool,
    override_action: str,
) -> tuple[object, ...]:
    """Identify a market plan without minute-level load and SOC fluctuations.

    The caller supplies future intervals, retaining ``tariff_start`` on the
    partially elapsed current interval. Expiring that interval therefore changes
    the key even when the next tariff has exactly the same price. Revisions to
    future prices or their uncertainty are significant, as are operator settings
    and changes in immediate physical eligibility.

    Expected load and ordinary SOC drift are deliberately absent. Fresh safety
    checks and physical command limits must still run on every telemetry poll;
    a reused economic plan never authorizes bypassing those checks.
    """
    if not isfinite(soc) or not 0 <= soc <= 100:
        raise ValueError("Planning SOC must be finite and between 0 and 100")
    market = tuple(sorted(
        (
            _utc(slot.tariff_start or slot.start), _utc(slot.end), slot.price,
            slot.source, slot.uncertainty_dkk_per_kwh,
        )
        for slot in slots
    ))
    schedules = tuple(sorted(
        dumps(item, sort_keys=True, separators=(",", ":"), default=str)
        for item in scheduled_loads
    ))
    return (
        market, revision, settings, forecast_enabled, opportunistic_enabled,
        schedules, override_action, current_discharge_enabled,
        soc < settings.reserve_soc, soc <= settings.reserve_soc,
        soc >= settings.target_soc, soc >= 100,
    )


def earliest_future_boundary(
    slots: Iterable[PriceSlot], now: datetime,
) -> datetime | None:
    """Return the next actual tariff boundary in UTC, including a future start.

    This supports quarter-hour, hourly and mixed-cadence feeds without assuming
    a fixed wake-up interval. Comparing UTC instants also preserves the repeated
    hour when local daylight-saving time ends.
    """
    current = _utc(now)
    boundaries = (
        boundary
        for slot in slots
        for boundary in (_utc(slot.tariff_start or slot.start), _utc(slot.end))
        if boundary > current
    )
    return min(boundaries, default=None)
