"""Tests for external-price forecast safety and evidence."""

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components"))

from house_battery.forecast import (
    ForecastAccuracy,
    assess_external_forecast,
    extend_known_horizon,
)
from house_battery.models import Action, PlannerSettings, PriceSlot
from house_battery.planner import optimize
from house_battery.price import normalize_price_rows


def _slot(hour: int, price: float) -> PriceSlot:
    start = datetime(2026, 1, 1, hour, tzinfo=UTC)
    return PriceSlot(start, start + timedelta(hours=1), price)


def test_external_forecast_only_extends_known_horizon_conservatively() -> None:
    """Known prices win and forecast slots are marked with their uncertainty."""
    known = [_slot(0, 1.0)]
    forecast = [_slot(0, 9.0), _slot(1, 0.5), _slot(2, 2.5)]

    merged = extend_known_horizon(known, forecast, uncertainty_dkk_per_kwh=0.25)

    assert [slot.price for slot in merged] == [1.0, 0.5, 2.5]
    assert [slot.source for slot in merged] == ["known", "forecast", "forecast"]
    assert merged[1].uncertainty_dkk_per_kwh == 0.25


def test_hourly_known_prices_and_quarterly_forecasts_keep_their_source_cadence() -> (
    None
):
    """The planner can join a 60-minute known horizon to 15-minute forecasts."""
    known = normalize_price_rows(
        [
            {
                "start": "2026-01-01T00:00:00+00:00",
                "end": "2026-01-01T01:00:00+00:00",
                "price": 1.0,
            }
        ]
    )
    forecast_start = datetime(2026, 1, 1, 1, tzinfo=UTC)
    forecast = normalize_price_rows(
        [
            {
                "start": forecast_start + timedelta(minutes=minute),
                "end": forecast_start + timedelta(minutes=minute + 15),
                "price": 0.5,
            }
            for minute in range(0, 60, 15)
        ]
    )

    merged = extend_known_horizon(known, forecast, uncertainty_dkk_per_kwh=0.25)

    assert [slot.hours for slot in merged] == [1.0, 0.25, 0.25, 0.25, 0.25]


def test_missing_end_uses_the_source_cadence_inferred_from_adjacent_starts() -> None:
    """Quarter-hour rows without `end` do not silently become hourly prices."""
    slots = normalize_price_rows(
        [
            {"start": f"2026-01-01T00:{minute:02d}:00+00:00", "price": 1.0}
            for minute in (0, 15, 30)
        ]
    )

    assert [slot.hours for slot in slots] == [0.25, 0.25, 0.25]


def test_forecast_accuracy_scores_forecast_once_actual_price_is_known() -> None:
    """Accuracy reports absolute error, bias, and whether the error fit the buffer."""
    accuracy = ForecastAccuracy()
    accuracy.record_forecasts([_slot(1, 1.20)])

    matched = accuracy.score_actual_prices(
        [_slot(1, 1.00)], uncertainty_dkk_per_kwh=0.25
    )

    assert matched == 1
    assert accuracy.samples == 1
    assert accuracy.mean_absolute_error_dkk_per_kwh == 0.20
    assert accuracy.mean_bias_dkk_per_kwh == 0.20
    assert accuracy.within_uncertainty_pct == 100.0


def test_forecast_accuracy_compares_hourly_forecast_to_quarterly_actual_prices() -> (
    None
):
    """Accuracy is duration-weighted when the two sources use different cadences."""
    accuracy = ForecastAccuracy()
    forecast = _slot(1, 1.20)
    actual_start = forecast.start
    actual = [
        PriceSlot(
            actual_start + timedelta(minutes=15 * index),
            actual_start + timedelta(minutes=15 * (index + 1)),
            price,
        )
        for index, price in enumerate((1.00, 1.10, 1.30, 1.40))
    ]
    accuracy.record_forecasts([forecast])

    matched = accuracy.score_actual_prices(actual, uncertainty_dkk_per_kwh=0.25)

    assert matched == 1
    assert accuracy.samples == 1
    assert accuracy.mean_absolute_error_dkk_per_kwh == 0.0


def test_forecast_uncertainty_rejects_a_marginal_forecast_discharge() -> None:
    """A nominally profitable forecast cannot bypass the conservative buffer."""
    start = datetime(2026, 1, 1, tzinfo=UTC)
    slots = [
        PriceSlot(
            start + timedelta(minutes=15 * index),
            start + timedelta(minutes=15 * (index + 1)),
            0.20 if index < 2 else 1.50,
            expected_load_wh=125,
            source="forecast" if index >= 2 else "known",
            uncertainty_dkk_per_kwh=0.50 if index >= 2 else 0.0,
        )
        for index in range(4)
    ]
    settings = PlannerSettings(
        capacity_wh=1958,
        reserve_soc=20,
        target_soc=90,
        charge_power_w=1200,
        discharge_power_w=800,
        round_trip_efficiency=0.85,
        degradation_cost_dkk_per_kwh=0.35,
        minimum_profit_dkk_per_kwh=0.75,
        switching_penalty_dkk=0,
        minimum_mode_minutes=15,
        maximum_transitions=4,
    )

    plan = optimize(
        slots,
        now=datetime(2025, 12, 31, 23, 59, tzinfo=UTC),
        soc=20,
        settings=settings,
    )

    assert all(slot.action is not Action.BATTERY for slot in plan.slots)


def test_invalid_external_forecast_is_rejected_without_affecting_known_prices() -> None:
    """Malformed external forecast data is unusable, not a partial plan input."""
    assessment = assess_external_forecast(
        [
            {
                "start": "2026-01-01T02:00:00+00:00",
                "end": "2026-01-01T03:00:00+00:00",
                "price": 0.50,
            },
            {"start": "not-a-date", "price": "broken"},
        ],
        now=datetime(2026, 1, 1, 1, tzinfo=UTC),
        reported_at=datetime(2026, 1, 1, 1, tzinfo=UTC),
        maximum_age=timedelta(hours=3),
    )

    assert assessment.status == "invalid"
    assert not assessment.usable
    assert assessment.slots == ()


def test_stale_external_forecast_is_rejected_even_when_its_rows_are_valid() -> None:
    """Old forecast data cannot extend an otherwise healthy known-price plan."""
    assessment = assess_external_forecast(
        [
            {
                "start": "2026-01-01T02:00:00+00:00",
                "end": "2026-01-01T03:00:00+00:00",
                "price": 0.50,
            }
        ],
        now=datetime(2026, 1, 1, 4, tzinfo=UTC),
        reported_at=datetime(2026, 1, 1, 0, tzinfo=UTC),
        maximum_age=timedelta(hours=3),
    )

    assert assessment.status == "stale"
    assert not assessment.usable
