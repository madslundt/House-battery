"""High-level, non-detailed guidance over the price forecast horizon.

The real optimizer (:mod:`planner`) only ever plans the *known* price horizon
with a dynamic program.  Beyond that horizon the only signal available is an
external price *forecast*, which carries uncertainty and must never be treated
as confirmed pricing.  This module turns that forecast into a coarse, human
guideline: identify where prices are likely to dip (charge the battery) and
where they are likely to spike (use the battery), relative to a baseline.

It deliberately does **not** estimate prices or emit a per-interval plan.  It
only flags *major* deviations from a baseline, grouped into contiguous
charge/discharge windows, so an operator can glance at something like
"charge overnight, use the battery Thursday evening" without being asked to
trust a precise number.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from statistics import median
from typing import Any

from .models import Action, PriceSlot

# A dip must sit at least this fraction *below* the baseline to count as a
# charging window; a spike must sit at least this fraction *above* it.  The
# bounds are intentionally wide so only *major* deviations qualify and
# quarter-hour noise never produces a spurious window.
DIP_FACTOR = 0.80
HIGH_FACTOR = 1.30
# A dip/spike run must last at least this long to be reported, so brief blips
# are ignored.  One hour is a sensible floor for either charging or discharging.
MIN_BLOCK_HOURS = 1.0


@dataclass(frozen=True, slots=True)
class ForecastBlock:
    """A contiguous forecast window carrying a single charge/discharge hint."""

    start: datetime
    end: datetime
    recommendation: Action
    reason: str
    # Representative (median) price across the window — a label only, never an
    # input to any optimizer.
    price_dkk_per_kwh: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "recommendation": self.recommendation.value,
            "reason": self.reason,
            "price_dkk_per_kwh": round(self.price_dkk_per_kwh, 4),
        }


@dataclass(frozen=True, slots=True)
class ForecastPlan:
    """The coarse forecast-horizon guideline, separate from the real plan."""

    created_at: datetime
    blocks: tuple[ForecastBlock, ...] = field(default_factory=tuple)
    baseline_price_dkk_per_kwh: float = 0.0
    horizon_start: datetime | None = None
    horizon_end: datetime | None = None

    @property
    def has_blocks(self) -> bool:
        return bool(self.blocks)

    @property
    def next_recommendation(self) -> ForecastBlock | None:
        return self.blocks[0] if self.blocks else None

    def summary(self) -> str:
        """Return a one-line, glanceable description of the whole horizon."""
        if not self.blocks:
            return "no major forecast swings detected"
        return " · ".join(_describe_block(block) for block in self.blocks)

    def as_dict(self) -> dict[str, Any]:
        return {
            "created_at": self.created_at.isoformat(),
            "baseline_price_dkk_per_kwh": round(self.baseline_price_dkk_per_kwh, 4),
            "horizon_start": (
                self.horizon_start.isoformat() if self.horizon_start else None
            ),
            "horizon_end": (
                self.horizon_end.isoformat() if self.horizon_end else None
            ),
            "recommendation": self.summary(),
            "blocks": [block.as_dict() for block in self.blocks],
        }


def _describe_block(block: ForecastBlock) -> str:
    """Render a block as ``<verb> <start>-<end>`` for the summary line."""
    verb = "charge" if block.recommendation is Action.CHARGE else "use battery"
    fmt = "%a %H:%M"
    start = block.start.strftime(fmt)
    end = block.end.strftime(fmt)
    return f"{verb} {start}-{end}"


def _regime(
    price: float, baseline: float, dip_factor: float, high_factor: float
) -> str | None:
    """Return ``"charge"``, ``"discharge"``, or ``None`` for a single price.

    A price is a *dip* when it is at least ``dip_factor`` below the baseline and
    a *spike* when it is at least ``high_factor`` above it.  Nothing can be both
    because ``dip_factor < high_factor`` and the baseline is strictly positive.
    A price in between is neutral (``None``) and breaks any contiguous run.
    """
    if price <= baseline * dip_factor:
        return "charge"
    if price >= baseline * high_factor:
        return "discharge"
    return None


def _block_duration_hours(start: datetime, end: datetime) -> float:
    return (end - start).total_seconds() / 3600.0


def build_forecast_plan(
    forecast_slots,
    *,
    now: datetime,
    known_slots: Any = (),
    dip_factor: float = DIP_FACTOR,
    high_factor: float = HIGH_FACTOR,
    min_block_hours: float = MIN_BLOCK_HOURS,
) -> ForecastPlan:
    """Build a coarse charge/discharge guideline from forecast-only slots.

    ``forecast_slots`` are the uncertain future price intervals (already
    validated by the caller).  ``known_slots`` supply the recent-price baseline
    so "high" and "low" are judged against confirmed reality rather than the
    forecast's own average.  Only forecast windows that start *after* the known
    price horizon end are considered, keeping the guideline focused on "what to
    do after the confirmed prices run out".
    """
    baseline = _baseline_price(known_slots, forecast_slots)
    if baseline <= 0:
        # No reliable reference to measure deviations against: stay silent.
        return ForecastPlan(now)

    known_ends = [slot.end for slot in known_slots if slot.source == "known"]
    known_end = max(known_ends) if known_ends else now

    horizon_slots = sorted(
        (
            slot
            for slot in forecast_slots
            if slot.end > now and slot.start >= known_end
        ),
        key=lambda item: item.start,
    )
    if not horizon_slots:
        return ForecastPlan(
            now,
            baseline_price_dkk_per_kwh=round(baseline, 4),
        )

    blocks = _group_windows(
        horizon_slots,
        baseline=baseline,
        dip_factor=dip_factor,
        high_factor=high_factor,
        min_block_hours=min_block_hours,
    )
    return ForecastPlan(
        now,
        blocks=blocks,
        baseline_price_dkk_per_kwh=round(baseline, 4),
        horizon_start=horizon_slots[0].start,
        horizon_end=horizon_slots[-1].end,
    )


def _baseline_price(known_slots, forecast_slots) -> float:
    """Reference price: mean of the last known slots, else the forecast median.

    Known prices are preferred because they are confirmed reality; the forecast
    median is only a last-resort anchor when no known prices are available, so
    the guideline still reports relative highs and lows within the forecast.
    """
    known = sorted(
        (slot for slot in known_slots if slot.source == "known"),
        key=lambda item: item.start,
    )
    recent = known[-min(24, len(known)):]
    if recent:
        return sum(slot.price for slot in recent) / len(recent)
    prices = [slot.price for slot in forecast_slots]
    return median(prices) if prices else 0.0


def _group_windows(
    horizon_slots,
    *,
    baseline: float,
    dip_factor: float,
    high_factor: float,
    min_block_hours: float,
) -> tuple[ForecastBlock, ...]:
    """Collapse contiguous same-regime slots into reported charge/discharge runs."""
    blocks: list[ForecastBlock] = []
    run: dict[str, Any] | None = None

    def flush() -> None:
        nonlocal run
        if run is None:
            return
        hours = _block_duration_hours(run["start"], run["end"])
        if hours >= min_block_hours:
            regime = run["regime"]
            representative = median(run["prices"])
            deviation_pct = (
                (representative - baseline) / baseline * 100 if baseline > 0 else 0.0
            )
            if regime == "charge":
                recommendation = Action.CHARGE
                reason = (
                    f"Forecast price ~{deviation_pct:.0f}% below baseline "
                    f"({representative:.3f} vs {baseline:.3f} DKK) — cheap window, "
                    "charge the battery"
                )
            else:
                recommendation = Action.BATTERY
                reason = (
                    f"Forecast price ~{deviation_pct:.0f}% above baseline "
                    f"({representative:.3f} vs {baseline:.3f} DKK) — expensive "
                    "window, use the battery"
                )
            blocks.append(
                ForecastBlock(
                    start=run["start"],
                    end=run["end"],
                    recommendation=recommendation,
                    reason=reason,
                    price_dkk_per_kwh=representative,
                )
            )
        run = None

    for slot in horizon_slots:
        regime = _regime(slot.price, baseline, dip_factor, high_factor)
        if run is not None and (
            regime != run["regime"] or slot.start != run["end"]
        ):
            # Regime change, neutral slot, or a gap in the forecast: close the
            # current run before starting a new one.
            flush()
        if regime is None:
            flush()
            continue
        if run is None:
            run = {
                "regime": regime,
                "start": slot.start,
                "end": slot.end,
                "prices": [slot.price],
            }
        else:
            run["end"] = slot.end
            run["prices"].append(slot.price)
    flush()
    return tuple(blocks)
