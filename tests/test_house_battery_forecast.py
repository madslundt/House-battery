"""Tests for external-price forecast safety and evidence."""

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components"))

from house_battery.forecast import ForecastAccuracy, extend_known_horizon
from house_battery.models import Action, PlannerSettings, PriceSlot
from house_battery.planner import optimize


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
