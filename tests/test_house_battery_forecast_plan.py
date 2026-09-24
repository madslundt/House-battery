"""Tests for the coarse forecast-horizon guideline (forecast_plan module)."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components"))

from house_battery.models import Action, PriceSlot  # noqa: E402
from house_battery.forecast_plan import (  # noqa: E402
    MIN_BLOCK_HOURS,
    ForecastBlock,
    build_forecast_plan,
)

_ORIGIN = datetime(2026, 9, 25, 10, tzinfo=timezone.utc)


def h(offset_hours: float) -> datetime:
    """Return ``_ORIGIN + offset_hours`` for readable assertions."""
    return _ORIGIN + timedelta(hours=offset_hours)


def _slot(
    start_offset: float,
    price: float,
    *,
    source: str = "forecast",
    duration_hours: float = 1.0,
) -> PriceSlot:
    """Build a slot starting ``start_offset`` hours from the origin."""
    start = h(start_offset)
    return PriceSlot(
        start=start,
        end=start + timedelta(hours=duration_hours),
        price=price,
        source=source,
    )


def _forecast_slots(prices: list[float], source: str = "forecast") -> list[PriceSlot]:
    """Build contiguous hourly forecast slots from a price list."""
    return [_slot(index, price, source=source) for index, price in enumerate(prices)]


def test_dip_and_spike_are_reported_as_charge_and_discharge() -> None:
    """A clear overnight dip and a late spike are turned into hints."""
    # Baseline ~2.0 (flat known prices).  Dip at hours 2-4, spike at hours 10-12.
    prices = [2.0, 2.0, 0.5, 0.5, 0.6, 2.0, 2.0, 2.0, 2.0, 2.0, 3.5, 3.0, 2.0, 2.0]
    now = _slot(0, 2.0).start
    plan = build_forecast_plan(
        _forecast_slots(prices), now=now, known_slots=_forecast_slots(prices)
    )

    assert plan.has_blocks
    assert plan.baseline_price_dkk_per_kwh == 2.0
    assert [block.recommendation for block in plan.blocks] == [
        Action.CHARGE,
        Action.BATTERY,
    ]
    # Charge window covers hours 2-5; discharge covers 10-12.
    assert plan.blocks[0].start == h(2)
    assert plan.blocks[0].end == h(5)
    assert plan.blocks[1].start == h(10)
    assert plan.blocks[1].end == h(12)


def test_minor_swings_below_threshold_are_ignored() -> None:
    """Swings smaller than the dip/high factors are neutral noise."""
    # Baseline 2.0; swings stay within 20% below / 30% above.
    prices = [2.0, 1.7, 1.75, 2.1, 2.2, 2.4, 2.0, 2.0]
    now = _slot(0, 2.0).start
    plan = build_forecast_plan(
        _forecast_slots(prices), now=now, known_slots=_forecast_slots(prices)
    )

    assert not plan.has_blocks
    assert plan.summary() == "no major forecast swings detected"


def test_short_blip_below_min_block_is_ignored() -> None:
    """A sub-hour dip does not reach the minimum block duration."""
    slots = [
        _slot(0, 2.0),
        _slot(1, 0.5, duration_hours=0.25),  # 15-minute dip
        _slot(1.25, 2.0),
    ]
    now = _slot(0, 2.0).start
    plan = build_forecast_plan(slots, now=now, known_slots=_forecast_slots([2.0] * 3))

    assert not plan.has_blocks


def test_known_prices_are_baseline_and_forecast_is_after_known() -> None:
    """The guideline is judged against recent known prices, forecast-only after."""
    # Known prices run hours 0-6 (baseline ~2.0); forecast covers the next 12h.
    known = [_slot(i, 2.0, source="known") for i in range(6)]
    forecast = [
        _slot(6 + i, price, source="forecast")
        for i, price in enumerate([0.5, 0.4, 2.0, 2.0, 3.6, 3.2])
    ]
    now = _slot(0, 2.0).start

    plan = build_forecast_plan(forecast, now=now, known_slots=known)

    assert plan.baseline_price_dkk_per_kwh == 2.0
    # Only two blocks: a charge dip (hours 6-8) and a discharge spike (hours 14-16).
    assert [block.recommendation for block in plan.blocks] == [
        Action.CHARGE,
        Action.BATTERY,
    ]
    assert plan.blocks[0].start == h(6)


def test_baseline_falls_back_to_forecast_median_without_known_prices() -> None:
    """With no known prices, highs/lows are relative to the forecast median."""
    # Seven prices; the median (4th sorted value) is 2.0; dip at start, spike end.
    prices = [0.5, 1.9, 2.0, 2.0, 2.2, 3.8, 3.9]
    now = _slot(0, 0.5).start
    plan = build_forecast_plan(_forecast_slots(prices), now=now)

    assert plan.baseline_price_dkk_per_kwh == 2.0
    recommendations = [block.recommendation for block in plan.blocks]
    assert Action.CHARGE in recommendations
    assert Action.BATTERY in recommendations


def test_no_baseline_when_no_prices() -> None:
    """Without any price data the plan is empty and silent."""
    now = _slot(0, 0.0).start
    plan = build_forecast_plan([], now=now)

    assert not plan.has_blocks
    assert plan.baseline_price_dkk_per_kwh == 0.0


def test_disjoint_forecast_windows_are_not_merged() -> None:
    """A gap in the forecast splits a run instead of bridging it."""
    # Known prices set the 2.0 baseline and end at hour 6; the forecast plan
    # only looks after that.  Two dips separated by a one-hour gap at hour 8.
    known = [_slot(i, 2.0, source="known") for i in range(6)]
    forecast = [
        _slot(6, 0.5),
        _slot(7, 0.5),  # dip hours 6-7
        _slot(9, 0.5),
        _slot(10, 0.5),  # dip hours 9-10, gap at hour 8
    ]
    now = _slot(0, 2.0).start
    plan = build_forecast_plan(forecast, now=now, known_slots=known)

    # Two separate charge blocks; the gap at hour 8 is *not* bridged.
    charges = [b for b in plan.blocks if b.recommendation is Action.CHARGE]
    assert len(charges) == 2
    assert charges[0].start == h(6)
    assert charges[0].end == h(8)
    assert charges[1].start == h(9)
    assert charges[1].end == h(11)


def test_min_block_hours_default_is_one_hour() -> None:
    """The module default keeps the guideline focused on meaningful windows."""
    assert MIN_BLOCK_HOURS == 1.0


def test_as_dict_and_summary_are_round_trippable() -> None:
    """The dict representation carries the same guidance as the object."""
    prices = [2.0, 0.5, 0.5, 2.0, 3.6, 3.6, 2.0]
    now = _slot(0, 2.0).start
    plan = build_forecast_plan(_forecast_slots(prices), now=now)

    data = plan.as_dict()
    assert data["recommendation"] == plan.summary()
    assert len(data["blocks"]) == len(plan.blocks)
    block: ForecastBlock = plan.blocks[0]
    assert block.as_dict()["recommendation"] == Action.CHARGE.value
    # Every block exposes a representative price that is a label, not an input.
    assert all("price_dkk_per_kwh" in slot for slot in data["blocks"])
