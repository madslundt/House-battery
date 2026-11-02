"""Causal, source-specific bias correction for issued external forecasts.

Calendar detail cannot be established from seven days of residual history.
Separate lead bands therefore compete against the unmodified source using
independent covered hours and genuinely later issued validation predictions.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from math import isfinite
from statistics import mean, median
from typing import Any

from .forecast import ForecastAccuracy
from .models import PriceSlot

_HOUR = timedelta(hours=1)
_RETENTION = timedelta(days=7)
_BANDS = ("0_24h", "24_48h", "48_72h")
_MIN_HOURS = 24
_MIN_BIAS = .05
_MAX_CORRECTION = .75
_SHRINKAGE = .8


@dataclass(frozen=True, slots=True)
class _Residual:
    start: datetime
    issued_at: datetime
    scored_at: datetime
    error: float


def _stamp(value: Any) -> datetime | None:
    try:
        stamp = datetime.fromisoformat(str(value))
        return stamp.astimezone(UTC) if stamp.tzinfo is not None else None
    except (ValueError, TypeError, OverflowError):
        return None


def _band(hours: float) -> str | None:
    return _BANDS[min(2, int(hours // 24))] if 0 <= hours <= 72 else None


def _days(rows: list[_Residual]) -> int:
    """One isolated quarter at midnight must not qualify a separate day."""
    counts = Counter(row.start.date() for row in rows)
    return sum(count >= 6 for count in counts.values())


def _quantile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[int((len(ordered) - 1) * fraction)] if ordered else 0.0


def _hours(accuracy: ForecastAccuracy, now: datetime) -> dict[str, list[_Residual]]:
    """Deduplicate interval pieces; require ninety percent of each target hour."""
    pieces: dict[tuple[str, datetime], set[tuple]] = defaultdict(set)
    for sample in accuracy.error_samples:
        if not isinstance(sample, dict):
            continue
        start, end, issued, scored = (
            _stamp(sample.get(key)) for key in ("start", "end", "issued_at", "scored_at")
        )
        try:
            error = float(sample["error_dkk_per_kwh"])
        except (KeyError, ValueError, TypeError, OverflowError):
            continue
        if (None in (start, end, issued, scored) or not isfinite(error)
                or not issued <= min(start, scored) or scored > now
                or not timedelta() < end - start <= timedelta(days=1)
                or end <= now - _RETENTION or start > now + timedelta(days=3)):
            continue
        left = max(start, now - _RETENTION)
        while left < end:
            hour = left.replace(minute=0, second=0, microsecond=0)
            right = min(hour + _HOUR, end)
            band = _band((left - issued).total_seconds() / 3600)
            if band is not None:
                pieces[band, hour].add((
                    (left - hour).total_seconds(), (right - hour).total_seconds(),
                    error, issued, scored,
                ))
            left = right
    result = {band: [] for band in _BANDS}
    for (band, hour), parts in pieces.items():
        boundaries = sorted({value for a, b, *_ in parts for value in (a, b)})
        weighted, seconds, conflicting = 0.0, 0.0, False
        for left, right in pairwise(boundaries):
            covering = [part for part in parts if part[0] <= left and part[1] >= right]
            if not covering:
                continue
            errors = [part[2] for part in covering]
            if max(errors) - min(errors) > 1e-9:
                conflicting = True
                break
            weighted += errors[0] * (right - left)
            seconds += right - left
        if not conflicting and seconds >= .9 * 3600:
            result[band].append(_Residual(
                hour, min(part[3] for part in parts), max(part[4] for part in parts),
                weighted / seconds,
            ))
    return {band: sorted(rows, key=lambda row: (row.issued_at, row.start))
            for band, rows in result.items()}


def _fit(rows: list[_Residual]) -> float | None:
    if len(rows) < _MIN_HOURS or _days(rows) < 3:
        return None
    center = median(row.error for row in rows)
    if abs(center) < _MIN_BIAS:
        return None
    if mean(row.error * center > 0 for row in rows) < .75:
        return None
    return max(-_MAX_CORRECTION, min(_MAX_CORRECTION, center * _SHRINKAGE))


def _policy(rows: list[_Residual], now: datetime) -> tuple[float | None, float, dict[str, Any]]:
    diagnostic: dict[str, Any] = {
        "covered_hours": len(rows), "covered_days": _days(rows),
        "validation_hours": 0, "validation_days": 0,
        "reason": "insufficient_independent_history", "correction_dkk_per_kwh": 0.0,
    }
    current = _fit(rows)
    if current is None:
        if len(rows) >= _MIN_HOURS and _days(rows) >= 3:
            diagnostic["reason"] = "no_consistent_bias"
        return None, 0.0, diagnostic
    latest = max(row.scored_at for row in rows)
    if now - latest > timedelta(hours=36):
        diagnostic["reason"] = "stale_scored_history"
        return None, 0.0, diagnostic
    validated: list[tuple[_Residual, float]] = []
    for row in rows:
        # A forecast issued at t can use only outcomes published/scored by t.
        # Chronological target ordering alone is insufficient for multi-day leads.
        training = [old for old in rows if old.scored_at <= row.issued_at
                    and old.start != row.start]
        correction = _fit(training)
        if correction is not None:
            validated.append((row, correction))
    diagnostic.update({
        "validation_hours": len(validated),
        "validation_days": _days([row for row, _ in validated]),
        "reason": "insufficient_issued_validation",
    })
    if len(validated) < _MIN_HOURS or diagnostic["validation_days"] < 2:
        return None, 0.0, diagnostic
    raw = [abs(row.error) for row, _ in validated]
    corrected = [abs(row.error - correction) for row, correction in validated]
    raw_mae, corrected_mae = mean(raw), mean(corrected)
    raw_p90, corrected_p90 = _quantile(raw, .9), _quantile(corrected, .9)
    diagnostic.update({
        "raw_mae_dkk_per_kwh": round(raw_mae, 6),
        "calibrated_mae_dkk_per_kwh": round(corrected_mae, 6),
        "raw_p90_absolute_error_dkk_per_kwh": round(raw_p90, 6),
        "calibrated_p90_absolute_error_dkk_per_kwh": round(corrected_p90, 6),
    })
    recent = [row for row in rows if row.scored_at >= latest - timedelta(hours=24)]
    if (len(recent) < 6 or mean(row.error * current > 0 for row in recent) < .75
            or mean(abs(row.error - current) for row in recent)
            > mean(abs(row.error) for row in recent) + .01):
        diagnostic["reason"] = "recent_regime_changed"
        return None, 0.0, diagnostic
    if (corrected_mae > raw_mae * .9 or raw_mae - corrected_mae < .02
            or corrected_p90 > raw_p90 + .01 or max(corrected) > max(raw) + .05):
        diagnostic["reason"] = "no_validated_improvement"
        return None, 0.0, diagnostic
    if abs(current - median(correction for _, correction in validated)) > .15:
        diagnostic["reason"] = "correction_changed_since_validation"
        return None, 0.0, diagnostic
    # Preserve at least the observed residual prediction interval. This is
    # dispersion, not a standard error shrinking with correlated quarters.
    uncertainty = max(corrected_p90, _quantile([abs(row.error - current) for row in recent], .9))
    diagnostic.update({"reason": "validated_bias_correction",
                       "correction_dkk_per_kwh": round(current, 6),
                       "residual_uncertainty_dkk_per_kwh": round(uncertainty, 6)})
    return current, uncertainty, diagnostic


def calibrate_forecast(
    slots: list[PriceSlot], accuracy: ForecastAccuracy, *, now: datetime,
    enabled: bool, uncertainty_floor: float,
) -> tuple[list[PriceSlot], dict[str, Any]]:
    """Correct only qualified external forecast prices from this source.

    Call after raw forecast evidence is recorded, with forecast source labels
    and uncertainty already attached. The source's original predictions remain
    the independent evidence benchmark. Existing source/manual buffers never
    shrink; neither forecast authorization nor known tariffs are changed.
    """
    if now.tzinfo is None:
        raise ValueError("Forecast calibration requires an aware timestamp")
    if not isfinite(uncertainty_floor) or uncertainty_floor < 0:
        raise ValueError("Forecast calibration uncertainty must be finite and nonnegative")
    now = now.astimezone(UTC)
    diagnostic: dict[str, Any] = {
        "status": "disabled" if not enabled else "learning",
        "method": "source_specific_prequential_lead_band_bias",
        "lead_bands": {}, "corrected_slots": 0,
        "point_forecasts": [],
        "uncertainty_policy": "contains_original_source_interval",
        "calendar_patterns": "insufficient_retention_for_independent_weekday_patterns",
    }
    if not enabled:
        return list(slots), diagnostic
    histories = _hours(accuracy, now)
    policies = {}
    for band, rows in histories.items():
        correction, uncertainty, details = _policy(rows, now)
        diagnostic["lead_bands"][band] = details
        if correction is not None:
            policies[band] = (correction, uncertainty)
    result = []
    for slot in slots:
        band = _band((slot.start - now).total_seconds() / 3600)
        if (slot.source != "forecast" or band not in policies or not isfinite(slot.price)
                or not isfinite(slot.uncertainty_dkk_per_kwh)):
            result.append(slot)
            continue
        correction, residual_uncertainty = policies[band]
        adjusted = slot.price - correction
        if not -20 <= adjusted <= 100:
            result.append(slot)
            continue
        guarded_uncertainty = max(
            slot.uncertainty_dkk_per_kwh + abs(correction),
            uncertainty_floor, residual_uncertainty,
        )
        result.append(replace(
            slot, price=adjusted,
            uncertainty_dkk_per_kwh=guarded_uncertainty,
        ))
        # Until archived external forecasts establish incremental dispatch
        # benefit, retain the entire original source interval around the new
        # point estimate. A learned mean shift can remove optimistic trades,
        # but cannot create a better charge/discharge bound by itself.
        if len(diagnostic["point_forecasts"]) < 8:
            diagnostic["point_forecasts"].append({
                "start": slot.start.isoformat(), "end": slot.end.isoformat(),
                "raw_price_dkk_per_kwh": slot.price,
                "calibrated_price_dkk_per_kwh": adjusted,
                "uncertainty_dkk_per_kwh": guarded_uncertainty,
            })
        diagnostic["corrected_slots"] += 1
    if diagnostic["corrected_slots"]:
        diagnostic["status"] = "active"
    return result, diagnostic
