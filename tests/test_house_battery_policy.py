"""Tests for normal and opt-in opportunistic storage policy."""

import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components"))

from house_battery.models import PlannerSettings, PriceSlot
from house_battery.planner import optimize
from house_battery.policy import (
    apply_storage_policy,
    parse_grid_available,
    relative_storage_value,
    select_opportunistic_plan,
)


def test_relative_storage_value_uses_future_prices_and_qualified_history():
    start = datetime(2026, 11, 1, tzinfo=UTC)
    future = [PriceSlot(start + timedelta(hours=i), start + timedelta(hours=i + 1), 2)
              for i in range(48)]
    history = [PriceSlot(start - timedelta(hours=i + 1), start - timedelta(hours=i), 1)
               for i in range(72)]

    assert relative_storage_value(future) == 1.5
    assert relative_storage_value(future, historical_slots=history) == .75


def _hourly_prices(prices, *, uncertainty=0):
    start = datetime(2026, 11, 1, tzinfo=UTC)
    return [PriceSlot(start + timedelta(hours=i), start + timedelta(hours=i + 1),
                      price, source="forecast" if uncertainty else "known",
                      uncertainty_dkk_per_kwh=uncertainty)
            for i, price in enumerate(prices)]


@pytest.mark.parametrize("prices,uncertainty,expected", [
    ([.2] * 2 + [2] * 43 + [.3] * 3, 0, 1.5),
    ([2] * 48, 1, .75),
    ([2] * 48, 3, 0),
    ([4] * 24 + [.2] * 24, 0, .15),
    ([1] * (7 * 24) + [100] * (7 * 24), 0, .75),
    ([2] * 23, 0, 0),
])
def test_relative_storage_value_uses_a_conservative_week_ahead(prices, uncertainty, expected):
    assert relative_storage_value(_hourly_prices(prices, uncertainty=uncertainty)) == pytest.approx(expected)


def test_relative_storage_value_weights_duration_instead_of_interval_count():
    hourly = _hourly_prices([.2] * 6 + [2] * 42)
    mixed = hourly[:6] + [
        replace(slot, start=slot.start + timedelta(minutes=15 * i),
                end=slot.start + timedelta(minutes=15 * (i + 1)))
        for slot in hourly[6:] for i in range(4)
    ]

    assert relative_storage_value(hourly) == 1.5
    assert relative_storage_value(mixed) == 1.5
    assert relative_storage_value(hourly[:12] + hourly[13:]) == 0


def test_relative_storage_value_ignores_invalid_price_evidence():
    future = _hourly_prices([2] * 48)
    invalid = replace(future[0], price=float("nan"))
    naive = replace(future[0], start=future[0].start.replace(tzinfo=None))
    reversed_slot = replace(future[0], end=future[0].start - timedelta(hours=1))

    assert relative_storage_value([invalid, naive, reversed_slot, *future]) == 1.5
    assert relative_storage_value(future, historical_slots=[invalid, naive]) == 1.5


def test_history_can_temper_a_forecast_but_cannot_override_lower_future_prices():
    future = _hourly_prices([4] * 24 + [.2] * 24)
    start = future[0].start
    history = [PriceSlot(start - timedelta(hours=i + 1), start - timedelta(hours=i), 10)
               for i in range(72)]

    assert relative_storage_value(future, historical_slots=history) == pytest.approx(.15)
    low_history = [replace(slot, price=.1) for slot in history]
    assert relative_storage_value(future, historical_slots=low_history) == pytest.approx(.075)
    stale_history = [replace(slot, start=slot.start - timedelta(days=8),
                             end=slot.end - timedelta(days=8)) for slot in low_history]
    assert relative_storage_value(future, historical_slots=stale_history) == pytest.approx(.15)
    assert relative_storage_value(future, historical_slots=low_history[:23]) == pytest.approx(.15)
    assert relative_storage_value(future, historical_slots=low_history[:48]) == pytest.approx(.15)


def settings() -> PlannerSettings:
    return PlannerSettings(
        capacity_wh=1958,
        reserve_soc=20,
        target_soc=90,
        charge_power_w=1200,
        discharge_power_w=800,
        round_trip_efficiency=0.85,
        degradation_cost_dkk_per_kwh=0.35,
        minimum_profit_dkk_per_kwh=0.75,
        switching_penalty_dkk=0.05,
    )


def test_policy_returns_settings_unchanged() -> None:
    """The simplified policy no longer lifts the charge ceiling."""
    result = apply_storage_policy(settings())
    assert result.target_soc == 90


def test_policy_ignores_any_additional_arguments() -> None:
    # Backward-compat: callers that still pass old kwargs must not break.
    result = apply_storage_policy(settings())
    assert result.target_soc == 90


def _profitable_prices() -> list[PriceSlot]:
    start = datetime(2026, 9, 26, tzinfo=UTC)
    prices = [0.2] * 8 + [5.0] * 12
    return [
        PriceSlot(
            start=start + timedelta(minutes=15 * index),
            end=start + timedelta(minutes=15 * (index + 1)),
            price=price,
            expected_load_wh=25 if index < 8 else 200,
        )
        for index, price in enumerate(prices)
    ]


def test_opportunistic_policy_uses_100_percent_for_a_known_profitable_cycle() -> None:
    normal_settings = settings()
    full_settings = replace(normal_settings, target_soc=100)
    prices = _profitable_prices()
    now = prices[0].start
    normal = optimize(prices, now=now, soc=20, settings=normal_settings)
    full = optimize(prices, now=now, soc=20, settings=full_settings)

    decision = select_opportunistic_plan(
        normal,
        full,
        normal_settings,
        full_settings,
        enabled=True,
    )

    assert decision.active
    assert decision.settings.target_soc == 100
    assert max(slot.soc_end for slot in decision.plan.slots) > 99
    assert (
        decision.plan.optimization_objective_dkk
        < normal.optimization_objective_dkk
    )
    assert decision.incremental_savings_dkk > 0


def test_opportunistic_policy_is_opt_in() -> None:
    normal_settings = settings()
    full_settings = replace(normal_settings, target_soc=100)
    prices = _profitable_prices()
    now = prices[0].start
    normal = optimize(prices, now=now, soc=20, settings=normal_settings)
    full = optimize(prices, now=now, soc=20, settings=full_settings)

    decision = select_opportunistic_plan(
        normal,
        full,
        normal_settings,
        full_settings,
        enabled=False,
    )

    assert not decision.active
    assert decision.plan is normal
    assert decision.settings.target_soc == 90


def test_opportunistic_policy_rejects_a_worse_risk_adjusted_objective() -> None:
    normal_settings = settings()
    full_settings = replace(normal_settings, target_soc=100)
    prices = _profitable_prices()
    now = prices[0].start
    normal = optimize(prices, now=now, soc=20, settings=normal_settings)
    full = optimize(prices, now=now, soc=20, settings=full_settings)
    banking_plan = replace(
        full,
        optimization_objective_dkk=(normal.optimization_objective_dkk or 0) + 0.02,
    )

    decision = select_opportunistic_plan(
        normal,
        banking_plan,
        normal_settings,
        full_settings,
        enabled=True,
    )

    assert not decision.active
    assert decision.plan is normal
    assert "risk-adjusted plan objective" in decision.reason


def test_grid_availability_is_parsed_only_from_its_explicit_state() -> None:
    assert parse_grid_available("on") is True
    assert parse_grid_available("off") is False
    assert parse_grid_available("disconnected") is False
    assert parse_grid_available("3.7") is None
    assert parse_grid_available("unavailable") is None
    assert parse_grid_available("unknown") is None
    assert parse_grid_available(None) is None
