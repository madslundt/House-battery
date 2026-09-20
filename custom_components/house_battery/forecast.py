"""Safe extension and evidence tracking for external electricity-price forecasts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .models import PriceSlot

_MAX_SAMPLES = 1_000


def _key(slot: PriceSlot) -> str:
    return f"{slot.start.isoformat()}|{slot.end.isoformat()}"


def extend_known_horizon(
    known: list[PriceSlot],
    forecast: list[PriceSlot],
    *,
    uncertainty_dkk_per_kwh: float,
) -> list[PriceSlot]:
    """Append only contiguous forecast slots after the known-price horizon.

    A forecast can never replace a known price or bridge a missing known-price
    interval. This prevents an unavailable actual price feed from silently
    becoming a control input.
    """
    result = sorted(known, key=lambda item: item.start)
    if not result:
        return result
    expected_start = result[-1].end
    for slot in sorted(forecast, key=lambda item: item.start):
        if slot.start < expected_start:
            continue
        if slot.start != expected_start:
            break
        result.append(
            PriceSlot(
                slot.start,
                slot.end,
                slot.price,
                slot.expected_load_wh,
                slot.expected_pv_wh,
                source="forecast",
                uncertainty_dkk_per_kwh=max(0.0, uncertainty_dkk_per_kwh),
            )
        )
        expected_start = slot.end
    return result


@dataclass(slots=True)
class ForecastAccuracy:
    """Persisted, one-shot comparisons between issued forecasts and actual prices."""

    outstanding: dict[str, float] = field(default_factory=dict)
    errors_dkk_per_kwh: list[float] = field(default_factory=list)
    within_uncertainty: list[bool] = field(default_factory=list)

    def record_forecasts(self, slots: list[PriceSlot]) -> None:
        """Keep the first forecast seen for each interval for an honest score."""
        for slot in slots:
            self.outstanding.setdefault(_key(slot), slot.price)
        self.outstanding = dict(list(self.outstanding.items())[-_MAX_SAMPLES:])

    def score_actual_prices(
        self, actual: list[PriceSlot], *, uncertainty_dkk_per_kwh: float
    ) -> int:
        """Score forecast intervals once their actual price becomes known."""
        matched = 0
        for slot in actual:
            key = _key(slot)
            forecast = self.outstanding.pop(key, None)
            if forecast is None:
                continue
            error = forecast - slot.price
            self.errors_dkk_per_kwh.append(error)
            self.within_uncertainty.append(
                abs(error) <= max(0.0, uncertainty_dkk_per_kwh)
            )
            matched += 1
        self.errors_dkk_per_kwh = self.errors_dkk_per_kwh[-_MAX_SAMPLES:]
        self.within_uncertainty = self.within_uncertainty[-_MAX_SAMPLES:]
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
        return round(100 * sum(self.within_uncertainty) / self.samples, 1)

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
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ForecastAccuracy:
        return cls(
            outstanding={
                str(key): float(value)
                for key, value in data.get("outstanding", {}).items()
            },
            errors_dkk_per_kwh=[
                float(value) for value in data.get("errors_dkk_per_kwh", [])
            ][-_MAX_SAMPLES:],
            within_uncertainty=[
                bool(value) for value in data.get("within_uncertainty", [])
            ][-_MAX_SAMPLES:],
        )
