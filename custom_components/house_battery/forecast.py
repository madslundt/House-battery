"""Safe extension and evidence tracking for external electricity-price forecasts."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from .forecast_accuracy import (
    FORECAST_EVIDENCE_MAX_AGE,
    ForecastAccuracy,
    forecast_adaptation,
    forecast_lead_band,
)
from .models import PriceSlot
from .price import normalize_price_rows_with_report

__all__ = [
    "FORECAST_EVIDENCE_MAX_AGE",
    "ForecastAccuracy",
    "ForecastAssessment",
    "assess_external_forecast",
    "detect_extreme_price_movement",
    "extend_known_horizon",
    "forecast_adaptation",
    "forecast_lead_band",
]

_MAX_FORECAST_HORIZON = timedelta(days=3)


@dataclass(frozen=True, slots=True)
class ForecastAssessment:
    """Validation result for an optional external forecast source."""

    status: str
    slots: tuple[PriceSlot, ...] = ()

    @property
    def usable(self) -> bool:
        return self.status == "available"


def assess_external_forecast(
    rows: list[Any],
    *,
    now: datetime,
    reported_at: datetime | None,
    maximum_age: timedelta,
    source_platform: str | None = None,
) -> ForecastAssessment:
    """Fail closed for an optional forecast while leaving known prices untouched."""
    if reported_at is None or reported_at.tzinfo is None:
        return ForecastAssessment("unavailable")
    if now - reported_at > maximum_age:
        return ForecastAssessment("stale")
    report = normalize_price_rows_with_report(rows)
    if report.source_rows == 0:
        return ForecastAssessment("empty")
    if report.invalid_rows:
        return ForecastAssessment("invalid")
    slots = report.slots
    if source_platform == "stromligning":
        slots = _repair_stromligning_forecast_ends(slots)
    slots = tuple(slot for slot in slots if slot.end > now)
    if not slots:
        return ForecastAssessment("expired")
    return ForecastAssessment("available", slots)


def _repair_stromligning_forecast_ends(slots: tuple[PriceSlot, ...]) -> tuple[PriceSlot, ...]:
    """Repair hourly forecasts labeled with the configured quarter-hour duration."""
    hour = timedelta(hours=1)
    quarter = timedelta(minutes=15)

    def has_quarter_end(slot: PriceSlot) -> bool:
        start = slot.start.astimezone(UTC)
        return (
            start == start.replace(minute=0, second=0, microsecond=0)
            and slot.end - slot.start == quarter
        )

    if not all(has_quarter_end(slot) for slot in slots):
        return slots

    repaired = []
    for index, slot in enumerate(slots):
        previous = slots[index - 1] if index else None
        following = slots[index + 1] if index + 1 < len(slots) else None
        hour_end = slot.start + hour
        follows_hour = (
            previous is not None and slot.start - previous.start == hour
        )
        precedes_hour = (
            following is not None and following.start == hour_end
        )
        if (
            (following is None or following.start >= hour_end)
            and (precedes_hour or follows_hour)
        ):
            slot = replace(slot, end=hour_end)
        repaired.append(slot)
    return tuple(repaired)


def extend_known_horizon(
    known: list[PriceSlot],
    forecast: list[PriceSlot],
    *,
    now: datetime,
    uncertainty_dkk_per_kwh: float,
) -> list[PriceSlot]:
    """Append contiguous forecast slots, capped three days ahead of ``now``.

    A forecast can never replace a known price or bridge a missing known-price
    interval. This prevents an unavailable actual price feed from silently
    becoming a control input.

    Known prices are authoritative. Forecasts are only appended when known
    prices truly end — they are never used to overwrite or replace confirmed
    tariff data. Every appended interval remains explicitly marked as
    ``forecast`` and carries the configured uncertainty allowance. If the
    three-day boundary falls inside a forecast interval, that final interval is
    clipped to the boundary.
    """
    result = sorted(known, key=lambda item: item.start)
    if not result:
        return result
    expected_start = result[-1].end
    horizon_end = now + _MAX_FORECAST_HORIZON
    if horizon_end <= expected_start:
        return result
    for slot in sorted(forecast, key=lambda item: item.start):
        if slot.end <= slot.start:
            continue
        if slot.start < expected_start:
            continue
        if slot.start != expected_start:
            break
        end = min(slot.end, horizon_end)
        if end <= slot.start:
            break
        fraction = (end - slot.start).total_seconds() / (
            slot.end - slot.start
        ).total_seconds()
        result.append(
            PriceSlot(
                slot.start,
                end,
                slot.price,
                slot.expected_load_wh * fraction,
                source="forecast",
                uncertainty_dkk_per_kwh=max(
                    0.0, uncertainty_dkk_per_kwh, slot.uncertainty_dkk_per_kwh,
                ),
            )
        )
        expected_start = end
        if end < slot.end:
            break
    return result


def detect_extreme_price_movement(
    slots: list[PriceSlot],
    *,
    threshold_factor: float = 2.0,
) -> dict[str, float | bool]:
    """Detect when prices deviate sharply from the recent average.

    Returns a summary that can be used for UI indicators (e.g. "prices going
    crazy") without feeding uncertain forecast data into the optimizer.

    Uses the most recent known prices as the baseline and compares against
    future prices (known or forecast).  Only flags when the future price
    exceeds *threshold_factor* times the baseline average.
    """
    if not slots:
        return {"is_extreme": False, "baseline": 0.0, "max_future": 0.0}

    known = [s for s in slots if s.source == "known"]
    future = [s for s in slots if s.end > slots[0].start]

    if not known:
        return {"is_extreme": False, "baseline": 0.0, "max_future": 0.0}

    # Baseline: average of the most recent 24 known-price slots
    recent = known[-min(24, len(known)) :]
    baseline = sum(s.price for s in recent) / len(recent)

    # Max future price (including forecast)
    max_future = max((s.price for s in future), default=baseline)

    # Flag when future price is significantly above baseline
    is_extreme = max_future > baseline * threshold_factor if baseline > 0 else False

    return {
        "is_extreme": is_extreme,
        "baseline": round(baseline, 4),
        "max_future": round(max_future, 4),
        "ratio": round(max_future / baseline, 3) if baseline > 0 else 0.0,
    }
