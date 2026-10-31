"""Safe extension and evidence tracking for external electricity-price forecasts."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from math import ceil, isfinite
from typing import Any

from .models import PriceSlot
from .price import normalize_price_rows_with_report

_MAX_SAMPLES = 1_000
_MAX_FORECAST_HORIZON = timedelta(days=3)
FORECAST_EVIDENCE_MAX_AGE = timedelta(days=7)


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
    slots = tuple(slot for slot in report.slots if slot.end > now)
    if not slots:
        return ForecastAssessment("expired")
    return ForecastAssessment("available", slots)


def _key(slot: PriceSlot) -> str:
    return f"{slot.start.isoformat()}|{slot.end.isoformat()}"


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
                uncertainty_dkk_per_kwh=max(0.0, uncertainty_dkk_per_kwh),
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


@dataclass(slots=True)
class ForecastAccuracy:
    """Persisted, one-shot comparisons between issued forecasts and actual prices."""

    outstanding: dict[str, dict[str, str | float]] = field(default_factory=dict)
    errors_dkk_per_kwh: list[float] = field(default_factory=list)
    within_uncertainty: list[bool] = field(default_factory=list)
    # Keep interval identities after removing the pending prediction, so a
    # provider's later reports cannot score the same published tariff again.
    scored_intervals: dict[str, str] = field(default_factory=dict)
    # Untimestamped historic aggregates remain display-only. Adaptive choices
    # require dated, auditable comparisons made from genuinely issued forecasts.
    error_samples: list[dict[str, str | float]] = field(default_factory=list)

    def prune(self, now: datetime) -> None:
        """Drop forecasts that can no longer be compared with recent prices."""
        retained = {}
        for key, forecast in self.outstanding.items():
            try:
                start = datetime.fromisoformat(str(forecast["start"]))
                end = datetime.fromisoformat(str(forecast["end"]))
                if (
                    start.tzinfo is not None
                    and end.tzinfo is not None
                    and start < end
                    and now - FORECAST_EVIDENCE_MAX_AGE < end
                    and start <= now + _MAX_FORECAST_HORIZON
                ):
                    retained[key] = forecast
            except (KeyError, TypeError, ValueError):
                continue
        self.outstanding = dict(list(retained.items())[-_MAX_SAMPLES:])
        scored = {}
        for key, end_value in self.scored_intervals.items():
            try:
                end = datetime.fromisoformat(end_value)
                if (
                    end.tzinfo is not None
                    and now - FORECAST_EVIDENCE_MAX_AGE < end
                ):
                    scored[key] = end_value
            except (TypeError, ValueError):
                continue
        self.scored_intervals = dict(list(scored.items())[-_MAX_SAMPLES:])
        self.error_samples = [
            sample for sample in self.error_samples
            if (end := _aware(sample.get("end"))) is not None
            and now - FORECAST_EVIDENCE_MAX_AGE < end <= now + _MAX_FORECAST_HORIZON
        ][-_MAX_SAMPLES:]

    def record_forecasts(self, slots: list[PriceSlot], *, now: datetime | None = None) -> None:
        """Keep first predictions; callers must exclude already-known intervals."""
        for slot in slots:
            if slot.start.tzinfo is None or slot.end.tzinfo is None or slot.start >= slot.end or _finite(slot.price) is None:
                continue
            if now is not None and (now.tzinfo is None or slot.start < now):
                continue
            if _key(slot) in self.scored_intervals:
                continue
            self.outstanding.setdefault(
                _key(slot),
                {
                    "start": slot.start.isoformat(),
                    "end": slot.end.isoformat(),
                    "price": slot.price,
                    **({"issued_at": now.isoformat()} if now is not None else {}),
                },
            )
        self.outstanding = dict(list(self.outstanding.items())[-_MAX_SAMPLES:])

    def score_actual_prices(
        self, actual: list[PriceSlot], *, uncertainty_dkk_per_kwh: float,
        now: datetime | None = None,
    ) -> int:
        """Score forecast intervals once their actual price becomes known."""
        matched = 0
        for key, forecast in list(self.outstanding.items()):
            try:
                start = _aware(forecast["start"])
                end = _aware(forecast["end"])
                predicted_price = _finite(forecast["price"])
                if start is None or end is None or start >= end or predicted_price is None:
                    raise ValueError("Invalid issued prediction")
            except (KeyError, TypeError, ValueError):
                self.outstanding.pop(key)
                continue
            cover = _actual_cover(start, end, actual)
            if not cover:
                continue
            actual_price = sum((b - a).total_seconds() * p for a, b, p in cover) / (end - start).total_seconds()
            self.outstanding.pop(key)
            self.scored_intervals[key] = end.isoformat()
            error = predicted_price - actual_price
            self.errors_dkk_per_kwh.append(error)
            self.within_uncertainty.append(
                abs(error) <= max(0.0, uncertainty_dkk_per_kwh)
            )
            issued = _aware(forecast.get("issued_at"))
            if now is not None and now.tzinfo is not None and issued is not None and issued <= min(start, now):
                self.error_samples.extend({
                    "start": a.isoformat(), "end": b.isoformat(),
                    "error_dkk_per_kwh": predicted_price - price, "scored_at": now.isoformat(),
                    "issued_at": issued.isoformat(),
                } for a, b, price in cover)
            matched += 1
        self.errors_dkk_per_kwh = self.errors_dkk_per_kwh[-_MAX_SAMPLES:]
        self.within_uncertainty = self.within_uncertainty[-_MAX_SAMPLES:]
        self.scored_intervals = dict(list(self.scored_intervals.items())[-_MAX_SAMPLES:])
        self.error_samples = self.error_samples[-_MAX_SAMPLES:]
        return matched

    @property
    def samples(self) -> int:
        return len(self.errors_dkk_per_kwh)

    @property
    def mean_absolute_error_dkk_per_kwh(self) -> float | None:
        if not self.errors_dkk_per_kwh:
            return None
        return round(
            sum(abs(error) for error in self.errors_dkk_per_kwh) / self.samples, 6
        )

    @property
    def mean_bias_dkk_per_kwh(self) -> float | None:
        if not self.errors_dkk_per_kwh:
            return None
        return round(sum(self.errors_dkk_per_kwh) / self.samples, 6)

    @property
    def within_uncertainty_pct(self) -> float | None:
        if not self.within_uncertainty:
            return None
        return round(100 * sum(self.within_uncertainty) / len(self.within_uncertainty), 1)

    @property
    def quality(self) -> str:
        mae = self.mean_absolute_error_dkk_per_kwh
        if mae is None:
            return "unknown"
        if mae <= 0.10:
            return "excellent"
        if mae <= 0.25:
            return "good"
        if mae <= 0.50:
            return "fair"
        return "poor"

    def as_dict(self) -> dict[str, Any]:
        return {
            "outstanding": self.outstanding,
            "errors_dkk_per_kwh": self.errors_dkk_per_kwh,
            "within_uncertainty": self.within_uncertainty,
            "scored_intervals": self.scored_intervals,
            "error_samples": self.error_samples,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ForecastAccuracy:
        data = data if isinstance(data, dict) else {}
        outstanding: dict[str, dict[str, str | float]] = {}
        pending = data.get("outstanding")
        for key, value in (pending.items() if isinstance(pending, dict) else []):
            if isinstance(value, dict):
                try:
                    prediction = {
                        "start": str(value["start"]),
                        "end": str(value["end"]),
                        "price": float(value["price"]),
                    }
                    if (issued := _aware(value.get("issued_at"))) is not None:
                        prediction["issued_at"] = issued.isoformat()
                    outstanding[str(key)] = prediction
                except (KeyError, TypeError, ValueError):
                    continue
            elif isinstance(value, (int, float)):
                try:
                    start, end = str(key).split("|", maxsplit=1)
                    outstanding[str(key)] = {
                        "start": start,
                        "end": end,
                        "price": float(value),
                    }
                except ValueError:
                    continue
        errors, within = data.get("errors_dkk_per_kwh"), data.get("within_uncertainty")
        scored, samples = data.get("scored_intervals"), data.get("error_samples")
        restored = cls(
            outstanding={k: v for k, v in list(outstanding.items())[-_MAX_SAMPLES:]
                         if _finite(v["price"]) is not None},
            errors_dkk_per_kwh=[v for value in (errors if isinstance(errors, list) else [])
                                if (v := _finite(value)) is not None][-_MAX_SAMPLES:],
            within_uncertainty=[v for v in (within if isinstance(within, list) else [])
                                if isinstance(v, bool)][-_MAX_SAMPLES:],
            scored_intervals={str(k): end.isoformat()
                              for k, value in (scored.items() if isinstance(scored, dict) else [])
                              if (end := _aware(value)) is not None},
            error_samples=[sample for value in (samples if isinstance(samples, list) else [])
                           if (sample := _error_sample(value)) is not None][-_MAX_SAMPLES:],
        )
        restored.scored_intervals = dict(list(restored.scored_intervals.items())[-_MAX_SAMPLES:])
        return restored


def _finite(value: Any) -> float | None:
    try:
        result = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return result if isfinite(result) and not isinstance(value, bool) else None


def _aware(value: Any) -> datetime | None:
    try:
        result = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    return result if result.tzinfo is not None else None


def _error_sample(value: Any) -> dict[str, str | float] | None:
    if not isinstance(value, dict):
        return None
    start, end, scored, issued = (_aware(value.get(k)) for k in ("start", "end", "scored_at", "issued_at"))
    error = _finite(value.get("error_dkk_per_kwh"))
    if start is None or end is None or scored is None or issued is None or start >= end or error is None or issued > min(start, scored):
        return None
    return {"start": start.isoformat(), "end": end.isoformat(),
            "scored_at": scored.isoformat(), "issued_at": issued.isoformat(), "error_dkk_per_kwh": error}


def _actual_cover(start: datetime, end: datetime, actual: list[PriceSlot]) -> list[tuple[datetime, datetime, float]]:
    """Require an exact non-overlapping confirmed-price cover of the forecast."""
    cursor, cover = start, []
    valid = [s for s in actual if s.start.tzinfo is not None and s.end.tzinfo is not None]
    for slot in sorted(valid, key=lambda item: item.start):
        if slot.source != "known" or slot.end <= start or slot.start >= end:
            continue
        left, right = max(start, slot.start), min(end, slot.end)
        if left != cursor or right <= left or _finite(slot.price) is None:
            return []
        cover.append((left, right, slot.price))
        cursor = right
    return cover if cursor == end else []


def _fresh_errors(accuracy: ForecastAccuracy, now: datetime) -> list[dict[str, Any]]:
    cutoff = now - FORECAST_EVIDENCE_MAX_AGE
    return [sample for value in accuracy.error_samples
            if (sample := _error_sample(value)) is not None
            and cutoff < _aware(sample["end"]) <= now
            and cutoff < _aware(sample["scored_at"]) <= now]


def _hourly_errors(accuracy: ForecastAccuracy, now: datetime) -> dict[int, list[tuple[float, float]]]:
    """Common UTC hours prevent cadence changes or overlapping rows inflating evidence."""
    cutoff = now - FORECAST_EVIDENCE_MAX_AGE
    buckets: dict[int, list[tuple[float, float, float]]] = {}
    samples = sorted(_fresh_errors(accuracy, now), key=lambda s: (
        _aware(s["issued_at"]).timestamp(), -abs(s["error_dkk_per_kwh"]),
        _aware(s["scored_at"]).timestamp()))
    pending = []
    for value in accuracy.outstanding.values():
        start, end, issued = (_aware(value.get(k)) for k in ("start", "end", "issued_at"))
        if start is not None and end is not None and issued is not None and issued <= start < end:
            pending.append((start, end, issued))
    for sample in samples:
        start, end = (_aware(sample[k]) for k in ("start", "end"))
        if any(issued <= _aware(sample["issued_at"]) and a < end and b > start for a, b, issued in pending):
            continue
        left = max(start, cutoff).timestamp()
        right = end.timestamp()
        for hour in range(int(left // 3600), ceil(right / 3600)):
            buckets.setdefault(hour, []).append((max(left, hour * 3600),
                min(right, (hour + 1) * 3600), abs(sample["error_dkk_per_kwh"])))
    result = {}
    for hour, pieces in buckets.items():
        boundaries = sorted({v for a, b, _ in pieces for v in (a, b)})
        weighted = []
        for left, right in pairwise(boundaries):
            covered = next((error for a, b, error in pieces if a <= left and b >= right), None)
            if covered is not None:
                weighted.append((covered, right - left))
        if sum(seconds for _, seconds in weighted) >= 3600 - 1e-6:
            result[hour] = weighted
    return result


def forecast_adaptation(
    available_sources: list[str], accuracies: dict[str, ForecastAccuracy], *,
    now: datetime, configured_uncertainty: float, enabled: bool,
) -> dict[str, Any]:
    """Raise the user's uncertainty floor and reprioritize only comparable fresh evidence."""
    if now.tzinfo is None:
        raise ValueError("Forecast adaptation requires an aware timestamp")
    order = list(dict.fromkeys(available_sources))
    floor = max(0.0, _finite(configured_uncertainty) or 0.0)
    histories = {source: _hourly_errors(accuracies.get(source, ForecastAccuracy()), now)
                 for source in order}
    summaries = {}
    for source, history in histories.items():
        values = sorted(value for pieces in history.values() for value in pieces)
        seconds = sum(weight for _, weight in values)
        running, p90 = 0.0, 0.0
        for error, weight in values:
            running += weight
            p90 = error
            if running >= seconds * .9:
                break
        days = len({datetime.fromtimestamp(h * 3600, UTC).date() for h in history})
        ready = enabled and len(history) >= 24 and days >= 3
        summaries[source] = {
            "effective_uncertainty_dkk_per_kwh": max(floor, p90) if ready else floor,
            "adaptive_ready": ready, "samples": len(_fresh_errors(accuracies.get(source, ForecastAccuracy()), now)),
            "covered_hours": len(history), "covered_days": days,
            "p90_absolute_error_dkk_per_kwh": p90 if seconds else None,
            "reason": "fresh_error_evidence" if ready else "disabled" if not enabled else "insufficient_fresh_coverage",
        }
    common = set.intersection(*(set(h) for h in histories.values())) if histories else set()
    common_days = len({datetime.fromtimestamp(h * 3600, UTC).date() for h in common})
    priority_ready = enabled and len(order) > 1 and len(common) >= 24 and common_days >= 3
    if priority_ready:
        maes = {source: sum(error * weight for h in common for error, weight in history[h])
                / (len(common) * 3600) for source, history in histories.items()}
        for source, mae in maes.items():
            summaries[source]["common_period_mae_dkk_per_kwh"] = mae
        # Insertion sorting preserves configured order for ties and changes
        # smaller than 10%; a zero-error incumbent cannot be displaced.
        for index in range(1, len(order)):
            while index and maes[order[index - 1]] > 0 and maes[order[index]] <= maes[order[index - 1]] * .9:
                order[index - 1], order[index] = order[index], order[index - 1]
                index -= 1
    return {"source_order": order, "sources": summaries,
            "priority_adaptive_ready": priority_ready, "common_hours": len(common),
            "common_days": common_days}
