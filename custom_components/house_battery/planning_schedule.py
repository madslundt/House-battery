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
    market_inputs: tuple[object, ...] | None = None,
    configured_settings: dict[str, float] | None = None,
    model_eligibility: tuple[object, ...] = (),
) -> tuple[object, ...]:
    """Identify a market plan without minute-level load and SOC fluctuations.

    The caller supplies future intervals, retaining ``tariff_start`` on the
    partially elapsed current interval. Expiring that interval therefore changes
    the key even when the next tariff has exactly the same price. Revisions to
    future prices or their uncertainty are significant, as are operator settings
    and changes in immediate physical eligibility.

    When raw provider inputs and manual settings are supplied, continuously
    learned price corrections, uncertainty, capacity and efficiency do not
    change the key. Those refinements apply at the next tariff or real input
    event, rather than creating a new economic plan on a telemetry poll. Discrete
    qualification changes remain significant so expired evidence is withdrawn
    immediately rather than held until the next tariff.

    Expected load and ordinary SOC drift are deliberately absent. Fresh safety
    checks and physical command limits must still run on every telemetry poll;
    a reused economic plan never authorizes bypassing those checks.
    """
    if not isfinite(soc) or not 0 <= soc <= 100:
        raise ValueError("Planning SOC must be finite and between 0 and 100")
    slots = tuple(slots)
    market = tuple(sorted(
        (
            _utc(slot.tariff_start or slot.start), _utc(slot.end), slot.price,
            slot.source, slot.uncertainty_dkk_per_kwh,
        )
        for slot in slots
    ))
    if market_inputs is not None:
        # Only the current source interval identifies the scheduling boundary;
        # a model's rolling extension must not move it between telemetry polls.
        interval = min(((_utc(slot.tariff_start or slot.start), _utc(slot.end))
                        for slot in slots), default=None)
        market = (interval, market_inputs)
    policy = settings if configured_settings is None else (
        tuple(sorted(configured_settings.items())), settings.control_capabilities,
        settings.partial_charge_targets, settings.discharge_enabled,
        settings.charge_enabled,
    )
    schedules = tuple(sorted(
        dumps(item, sort_keys=True, separators=(",", ":"), default=str)
        for item in scheduled_loads
    ))
    return (
        market, revision, policy, forecast_enabled, opportunistic_enabled,
        schedules, override_action, current_discharge_enabled,
        soc < settings.reserve_soc, soc <= settings.reserve_soc,
        soc >= settings.target_soc, soc >= 100,
        model_eligibility,
    )


def planning_change_reason(previous: tuple | None, current: tuple) -> str:
    """Explain cache invalidation for keys built from raw provider inputs."""
    if previous is None:
        return "startup_or_recovery"
    if previous[0][0] != current[0][0]:
        return "tariff_boundary"
    if previous[0] != current[0]:
        return "price_or_forecast_update"
    if previous[2:7] != current[2:7]:
        return "settings_or_schedule_changed"
    if previous[12] != current[12]:
        return "model_qualification_changed"
    if previous[7:12] != current[7:12]:
        return "physical_eligibility_changed"
    if previous[1] != current[1]:
        return "input_event"
    return "expired_or_replaced_plan"


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
