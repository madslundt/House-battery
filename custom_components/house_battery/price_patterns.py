"""Evidence-gated weekly price patterns learned from confirmed import tariffs.

This is a fallback forecast, not a calendar rule: weekends need to earn their
own profile from independent observations. All prices retain the configured
source's DKK/kWh basis, including its tariffs and taxes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from math import isfinite
from statistics import median
from typing import Any
from zoneinfo import ZoneInfo

from .models import PriceSlot
from .pattern_models import (
    BASELINE,
    CONTROL_MODEL,
    LEAD_BANDS,
    MODEL_NAMES,
    PatternEvidence,
    build_profiles,
    lead_band,
    quantile,
)

_RETENTION = timedelta(days=90)
_FRESHNESS = timedelta(days=3)
_HORIZON = timedelta(days=3)
_MIN_DATES = 4
_MIN_UNCERTAINTY = 0.25
_HOUR = timedelta(hours=1)
_MAX_PARTS = 120


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Price patterns require timezone-aware timestamps")
    return value.astimezone(UTC)


def _hour(value: datetime) -> datetime:
    return value.replace(minute=0, second=0, microsecond=0)


@dataclass(slots=True)
class PricePatternLearner:
    """Bounded elapsed-hour evidence with a robust local weekday/hour profile."""

    source: str | None = None
    time_zone: str = "UTC"
    # UTC-hour -> (time-weighted actual price, first out-of-sample error).
    samples: dict[str, tuple[float, float | None]] = field(default_factory=dict)
    # Non-overlapping coverage is integrated before an hour can qualify. Keeping
    # boundaries makes repeated quarter-hour rows and overlapping feeds harmless.
    pending: dict[str, list[tuple[float, float, float]]] = field(default_factory=dict)
    evidence: PatternEvidence = field(default_factory=PatternEvidence)

    def configure(self, source: str, time_zone: str) -> None:
        """Discard incompatible histories when source or local timezone changes."""
        ZoneInfo(time_zone)  # Validate at the boundary before mutating state.
        if self.source != source or self.time_zone != time_zone:
            self.samples.clear()
            self.pending.clear()
            self.evidence = PatternEvidence()
        self.source, self.time_zone = source, time_zone

    def prune(self, now: datetime) -> None:
        """Keep at most ninety days of elapsed hourly evidence."""
        now = _aware(now)
        cutoff = now - _RETENTION
        for collection in (self.samples, self.pending):
            for key in list(collection):
                start = datetime.fromisoformat(key)
                future = start + _HOUR > now if collection is self.samples else start > now
                if start < cutoff or future:
                    collection.pop(key)
        self.evidence.prune(now)

    def _profile(
        self, before: datetime,
    ) -> dict[tuple[int, int], list[float]]:
        by_date: dict[tuple[int, int], dict[str, list[float]]] = {}
        zone = ZoneInfo(self.time_zone)
        for key, (price, _) in self.samples.items():
            start = datetime.fromisoformat(key)
            if start + _HOUR > before or start < before - _RETENTION:
                continue
            local = start.astimezone(zone)
            bucket = by_date.setdefault((local.weekday(), local.hour), {})
            bucket.setdefault(local.date().isoformat(), []).append(price)
        # DST's repeated hour contributes one independent local date, not two.
        return {
            key: [sum(values) / len(values) for values in dates.values()]
            for key, dates in by_date.items()
        }

    def _prediction(
        self, when: datetime, profile: dict[tuple[int, int], list[float]],
    ) -> tuple[float, list[float]] | None:
        local = when.astimezone(ZoneInfo(self.time_zone))
        values = profile.get((local.weekday(), local.hour), [])
        if len(values) < _MIN_DATES:
            return None
        return median(values), values

    def observe(self, slots: list[PriceSlot], now: datetime) -> bool:
        """Learn elapsed confirmed prices once, with duration-weighted coverage.

        Future published prices and all forecasts are excluded. A minimum 90%
        observed hour prevents sparse quarter-hours being treated as full hours.
        Conflicting overlaps withhold the hour rather than blending two feeds.
        """
        now = _aware(now)
        previous = (len(self.samples), len(self.pending))
        self.prune(now)
        changed = previous != (len(self.samples), len(self.pending))
        cutoff = now - _RETENTION
        for slot in slots:
            if slot.source != "known" or not isfinite(slot.price):
                continue
            left = _aware(slot.tariff_start or slot.start)
            right = _aware(slot.end)
            if right > now or right <= left or right <= cutoff:
                continue
            left = max(left, cutoff)
            while left < right:
                start = _hour(left)
                end = min(start + _HOUR, right)
                key = start.isoformat()
                if key not in self.samples:
                    parts = self.pending.setdefault(key, [])
                    part = ((left - start).total_seconds(),
                            (end - start).total_seconds(), float(slot.price))
                    if part not in parts and len(parts) < _MAX_PARTS:
                        parts.append(part)
                        changed = True
                left = end
        # Commit in chronological order: retrospective imports must never score
        # a model trained on a later week against an earlier actual price.
        for key in sorted(list(self.pending)):
            start = datetime.fromisoformat(key)
            if start + _HOUR > now:
                continue
            parts = sorted(self.pending[key])
            cursor, weighted, seconds = 0.0, 0.0, 0.0
            conflict = False
            for index, (left, right, price) in enumerate(parts):
                if any(a < right and b > left and abs(other - price) > 1e-9
                       for a, b, other in parts[:index]):
                    conflict = True
                    break
                covered_left = max(left, cursor)
                duration = max(0.0, right - covered_left)
                weighted += duration * price
                seconds += duration
                cursor = max(cursor, right)
            if conflict or seconds < 0.9 * 3600:
                continue
            actual = weighted / seconds
            self.evidence.score(start, actual)
            prediction = self._prediction(start, self._profile(start))
            error = abs(actual - prediction[0]) if prediction else None
            self.samples[key] = (actual, error)
            self.pending.pop(key)
            changed = True
        return changed

    def shadow_issue(self, slots: list[PriceSlot], now: datetime) -> bool:
        """Issue causal challenger forecasts even while external forecasts win.

        Only confirmed-prefix coverage is used. Predictions remain immutable
        per target and lead band, so repeated minute polls are not new evidence.
        No forecast becomes a control input until it beats the flat baseline.
        """
        now = _aware(now)
        known = sorted((slot for slot in slots if slot.source == "known" and slot.end > now),
                       key=lambda slot: slot.start)
        if not known or known[0].start > now:
            return False
        cursor = now
        for slot in known:
            if slot.start > cursor or slot.end <= slot.start:
                return False
            cursor = max(cursor, slot.end)
        latest = max((datetime.fromisoformat(key) + _HOUR for key in self.samples), default=None)
        if latest is None or now - latest > _FRESHNESS:
            return False
        profiles = build_profiles(self.samples, now, self.time_zone)
        cursor = _hour(cursor) + (_HOUR if cursor != _hour(cursor) else timedelta())
        changed = False
        while cursor + _HOUR <= now + _HORIZON:
            predictions = {model: result[0] for model in MODEL_NAMES
                           if (result := profiles.predict(model, cursor)) is not None}
            if BASELINE in predictions:
                changed |= self.evidence.record(now, cursor, predictions)
            cursor += _HOUR
        return changed

    def extend(
        self, slots: list[PriceSlot], now: datetime, uncertainty_floor: float,
        *, horizon_end: datetime | None = None,
    ) -> list[PriceSlot]:
        """Append qualified pattern forecasts after a contiguous known prefix.

        Existing external forecasts win. Missing current tariffs or interior
        gaps are never repaired. Statistical uncertainty is added to the same
        planner guard rails used by external forecasts.
        """
        now = _aware(now)
        if not isfinite(uncertainty_floor) or uncertainty_floor < 0:
            raise ValueError("Price-pattern uncertainty must be finite and nonnegative")
        result = list(slots)
        current = sorted((slot for slot in slots if slot.end > now), key=lambda s: s.start)
        if not current or not any(
            slot.source == "known" and slot.start <= now < slot.end for slot in current
        ):
            return result
        cursor = now
        for slot in current:
            if slot.start > cursor or slot.end <= slot.start:
                return result
            cursor = max(cursor, slot.end)
        latest = max((datetime.fromisoformat(key) + _HOUR for key in self.samples),
                     default=None)
        if latest is None or now - latest > _FRESHNESS:
            return result
        self.shadow_issue(slots, now)
        profiles = build_profiles(self.samples, now, self.time_zone)
        # Published prices and selected external forecasts can reveal a new
        # regime before any actual hour has elapsed. Carry their disagreement
        # with the old weekly profile into fallback uncertainty; never alter
        # the authoritative prefix or pretend its level shift is known to last.
        prefix_residuals: list[float] = []
        for slot in current:
            position = max(slot.start, now)
            while position < slot.end:
                prediction = profiles.predict(CONTROL_MODEL, position)
                if prediction is not None:
                    prefix_residuals.append(abs(slot.price - prediction[0]))
                position = min(_hour(position) + _HOUR, slot.end)
        prefix_error = quantile(prefix_residuals, .9) if prefix_residuals else 0.0
        selected = {band: self.evidence.select(band, now, self.time_zone) for band in LEAD_BANDS}
        allowances = {band: self.evidence.error_allowance(band, model, now)
                      for band, model in selected.items() if model is not None}
        horizon_end = min(_aware(horizon_end), now + _HORIZON) if horizon_end else now + _HORIZON
        while cursor < horizon_end:
            band = lead_band(now, cursor)
            model = selected[band]
            prediction = profiles.predict(model, cursor) if model is not None else None
            if prediction is None or profiles.predict(BASELINE, cursor) is None:
                break
            price, values = prediction
            # Prediction dispersion, not standard error of the mean: more
            # weeks must not make volatile market prices look deterministic.
            deviation = quantile([abs(value - price) for value in values], .9)
            validation_error = allowances[band]
            uncertainty = max(uncertainty_floor, _MIN_UNCERTAINTY,
                              deviation, validation_error, prefix_error)
            end = min(_hour(cursor) + _HOUR, horizon_end)
            result.append(PriceSlot(cursor, end, price, source="forecast",
                                    uncertainty_dkk_per_kwh=uncertainty))
            cursor = end
        return result

    def summary(self, now: datetime) -> dict[str, Any]:
        """Describe readiness, measured uncertainty and matched-hour weekend effect."""
        now = _aware(now)
        profile = self._profile(now)
        qualified = {key: median(values) for key, values in profile.items()
                     if len(values) >= _MIN_DATES}
        latest = max((datetime.fromisoformat(key) + _HOUR for key in self.samples),
                     default=None)
        errors = [error for _, error in self.samples.values() if error is not None]
        selected = {band: self.evidence.select(band, now, self.time_zone) for band in LEAD_BANDS}
        differences = []
        for hour in range(24):
            if all((day, hour) in qualified for day in range(7)):
                weekdays = sum(qualified[day, hour] for day in range(5)) / 5
                weekends = sum(qualified[day, hour] for day in (5, 6)) / 2
                differences.append(weekdays - weekends)
        return {
            "status": "stale" if latest and now - latest > _FRESHNESS else
                      "ready" if len(qualified) == 168 and any(selected.values()) else "learning",
            "source": "learned_weekly_pattern",
            "training_source": self.source,
            "time_zone": self.time_zone,
            "observed_hours": len(self.samples),
            "qualified_weekday_hours": len(qualified),
            "required_dates_per_bucket": _MIN_DATES,
            "history_ready": len(qualified) == 168,
            "selected_models": selected,
            "control_model": CONTROL_MODEL,
            "shadow_models": [model for model in MODEL_NAMES if model != CONTROL_MODEL],
            "control_policy": "fixed_validated_model_with_issued_skill_gate",
            "model_selection": {band: self.evidence.comparison(band, now, self.time_zone)
                                for band in LEAD_BANDS},
            "issued_validation_samples": len(self.evidence.scores),
            "issued_validation_basis": "first_issued_hourly_targets_by_elapsed_lead_band",
            "last_observation": latest.isoformat() if latest else None,
            "validation_samples": len(errors),
            "validation_mae_dkk_per_kwh": sum(errors) / len(errors) if errors else None,
            "validation_mae_basis": "legacy_chronological_one_step_weekday_90d_hourly",
            "weekend_comparison_hours": len(differences),
            "weekend_discount_dkk_per_kwh": (
                sum(differences) / len(differences) if differences else None
            ),
        }

    def as_dict(self) -> dict[str, Any]:
        """Serialize bounded evidence without forecasts becoming training data."""
        return {"source": self.source, "time_zone": self.time_zone,
                "samples": {key: list(value) for key, value in self.samples.items()},
                "model_evidence": self.evidence.as_dict(),
                "pending": {key: [list(part) for part in value]
                            for key, value in self.pending.items()}}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PricePatternLearner:
        """Restore valid evidence while ignoring malformed stored rows."""
        if not isinstance(data, dict):
            return cls()
        learner = cls()
        learner.evidence = PatternEvidence.from_dict(data.get("model_evidence"))
        try:
            zone = str(data.get("time_zone", "UTC"))
            ZoneInfo(zone)
            learner.time_zone = zone
            learner.source = data.get("source") if isinstance(data.get("source"), str) else None
        except (ValueError, KeyError):
            return cls()
        for collection in ("samples", "pending"):
            rows = data.get(collection, {})
            if not isinstance(rows, dict):
                continue
            for key, value in list(rows.items())[-2162:]:
                try:
                    start = _aware(datetime.fromisoformat(key))
                    if start != _hour(start):
                        continue
                    if collection == "samples":
                        price, error = value
                        price = float(price)
                        error = float(error) if error is not None else None
                        if isfinite(price) and (error is None or isfinite(error) and error >= 0):
                            learner.samples[start.isoformat()] = (price, error)
                    else:
                        parts = []
                        for part in value[:_MAX_PARTS]:
                            left, right, price = map(float, part)
                            if 0 <= left < right <= 3600 and isfinite(price):
                                parts.append((left, right, price))
                        if parts:
                            learner.pending[start.isoformat()] = parts
                except (ValueError, TypeError, OverflowError):
                    continue
        return learner
