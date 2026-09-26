"""Regression scenario from the 2026-09-26 live HA price and forecast data."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components"))

from house_battery.dailyplan import reconcile_daily_plan
from house_battery.forecast import extend_known_horizon
from house_battery.models import Action, PlannerSettings, PriceSlot
from house_battery.planner import optimize


FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "prices_and_forecasts_2026-09-26.json"
)


def _series(data: dict, name: str, *, source: str) -> list[PriceSlot]:
    series = data["series"][name]
    start = datetime.fromisoformat(series["start"])
    step = timedelta(minutes=series["interval_minutes"])
    return [
        PriceSlot(
            start=start + index * step,
            end=start + (index + 1) * step,
            price=price,
            source=source,
        )
        for index, price in enumerate(series["prices"])
    ]


def _scenario():
    data = json.loads(FIXTURE.read_text())
    now = datetime.fromisoformat(data["captured_at"])
    load_w = data["load_forecast_w"]
    known = _series(data, "today", source="known") + _series(
        data, "tomorrow", source="known"
    )
    known = [
        PriceSlot(
            slot.start,
            slot.end,
            slot.price,
            expected_load_wh=load_w * slot.hours,
            source="known",
        )
        for slot in known
    ]
    settings = PlannerSettings(**data["settings"])
    return data, now, known, settings


def _operation_blocks(plan, now: datetime):
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    daily = reconcile_daily_plan(
        None,
        plan.slots,
        cutoff=now,
        day_start=day_start,
        horizon_end=plan.slots[-1].end,
    )
    return [
        (block["start"], block["end"], Action(block["action"]))
        for block in daily.view_dict()["blocks"]
    ]


def test_known_prices_only_reproduce_the_agreed_two_day_plan() -> None:
    """Without external forecasts, optimize only today's and tomorrow's prices."""
    data, now, known, settings = _scenario()
    plan = optimize(
        known,
        now=now,
        soc=data["soc_pct"],
        settings=settings,
        current_action=Action.GRID,
    )

    assert _operation_blocks(plan, now) == [
        ("2026-09-26T23:07:00+02:00", "2026-09-26T23:15:00+02:00", Action.GRID),
        ("2026-09-26T23:15:00+02:00", "2026-09-27T04:45:00+02:00", Action.BATTERY),
        ("2026-09-27T04:45:00+02:00", "2026-09-27T14:45:00+02:00", Action.GRID),
        ("2026-09-27T14:45:00+02:00", "2026-09-27T16:45:00+02:00", Action.CHARGE),
        ("2026-09-27T16:45:00+02:00", "2026-09-28T00:00:00+02:00", Action.BATTERY),
    ]


@pytest.mark.parametrize(
    ("forecast_name", "next_forecast_discharge"),
    [
        ("forecast_radius", "2026-09-28T07:00:00+02:00"),
        ("forecast_strømligning", "2026-09-28T17:00:00+02:00"),
    ],
)
def test_attached_forecasts_change_the_plan_for_their_later_price_peaks(
    forecast_name: str, next_forecast_discharge: str
) -> None:
    """The forecast horizon preserves energy for Monday's higher prices.

    The captured forecasts put Monday's evening peak at 3.49–3.87 DKK/kWh,
    above the known Sunday peak of 2.42 DKK/kWh. The 152 W load prediction and
    live battery settings are held constant to isolate forecast-horizon value.
    """
    data, now, known, settings = _scenario()
    forecast = _series(data, forecast_name, source="forecast")
    sunday_peak = max(
        slot.price for slot in known if slot.start.date().isoformat() == "2026-09-27"
    )
    monday_evening = next(
        slot.price
        for slot in forecast
        if slot.start.isoformat() == "2026-09-28T19:00:00+02:00"
    )
    assert sunday_peak == pytest.approx(2.417491)
    assert monday_evening == pytest.approx(
        3.8698 if forecast_name == "forecast_radius" else 3.491411
    )
    assert monday_evening - data["forecast_uncertainty_dkk_per_kwh"] > sunday_peak
    extended = extend_known_horizon(
        known,
        forecast,
        uncertainty_dkk_per_kwh=data["forecast_uncertainty_dkk_per_kwh"],
    )
    forecasted = [
        PriceSlot(
            slot.start,
            slot.end,
            slot.price,
            expected_load_wh=data["load_forecast_w"] * slot.hours,
            source=slot.source,
            uncertainty_dkk_per_kwh=slot.uncertainty_dkk_per_kwh,
        )
        for slot in extended
    ]

    plan = optimize(
        forecasted,
        now=now,
        soc=data["soc_pct"],
        settings=settings,
        current_action=Action.GRID,
    )
    blocks = _operation_blocks(plan, now)

    assert blocks[:4] == [
        ("2026-09-26T23:07:00+02:00", "2026-09-26T23:15:00+02:00", Action.GRID),
        ("2026-09-26T23:15:00+02:00", "2026-09-27T04:45:00+02:00", Action.BATTERY),
        ("2026-09-27T04:45:00+02:00", "2026-09-27T13:30:00+02:00", Action.GRID),
        ("2026-09-27T13:30:00+02:00", "2026-09-27T16:00:00+02:00", Action.CHARGE),
    ]
    # The forecast-aware plan holds its charge across Sunday's known evening
    # peak, then uses it in a later forecast window selected by the full horizon.
    assert not any(
        start.startswith("2026-09-27T")
        and end.startswith("2026-09-27T")
        and action is Action.BATTERY
        for start, end, action in blocks
    )
    forecast_discharges = [
        start
        for start, _end, action in blocks
        if action is Action.BATTERY and start.startswith("2026-09-28T")
    ]
    assert forecast_discharges[0] == next_forecast_discharge
