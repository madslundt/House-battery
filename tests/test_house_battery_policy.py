"""Tests for the conservative extra-storage policy."""

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components"))

from house_battery.models import PlannerSettings, PriceSlot
from house_battery.policy import apply_storage_policy, parse_grid_available


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
        minimum_mode_minutes=30,
        maximum_transitions=4,
    )


def slots(
    prices: list[float], *, source: str = "known", uncertainty: float = 0.0
) -> list[PriceSlot]:
    start = datetime(2026, 9, 20, tzinfo=UTC)
    return [
        PriceSlot(
            start + timedelta(minutes=15 * index),
            start + timedelta(minutes=15 * (index + 1)),
            price,
            source=source,
            uncertainty_dkk_per_kwh=uncertainty,
        )
        for index, price in enumerate(prices)
    ]


def test_large_profitable_spread_lifts_only_the_effective_target() -> None:
    adjusted, policy = apply_storage_policy(
        settings(),
        slots([0.2, 0.2, 4.0, 4.0]),
        extra_storage_spread_dkk_per_kwh=2.0,
        opportunistic_target_soc=100,
    )
    assert policy.active
    assert adjusted.target_soc == 100
    assert policy.effective_margin_dkk_per_kwh > 0.75


def test_small_spread_keeps_normal_target() -> None:
    adjusted, policy = apply_storage_policy(
        settings(),
        slots([1.0, 1.2, 1.25]),
        extra_storage_spread_dkk_per_kwh=2.0,
        opportunistic_target_soc=100,
    )
    assert not policy.active
    assert adjusted.target_soc == 90


def test_forecast_extremes_cannot_enable_extra_storage() -> None:
    adjusted, policy = apply_storage_policy(
        settings(),
        [
            *slots([1.0, 1.1]),
            *slots([0.01, 10.0], source="forecast", uncertainty=0.25),
        ],
        extra_storage_spread_dkk_per_kwh=2.0,
        opportunistic_target_soc=100,
    )

    assert not policy.active
    assert adjusted.target_soc == 90
    assert policy.known_slot_count == 2
    assert policy.conservative_charge_price_dkk_per_kwh == 1.0
    assert policy.conservative_discharge_price_dkk_per_kwh == 1.1
    assert "known-price" in policy.reason


def test_known_price_evidence_uses_conservative_charge_and_discharge_prices() -> None:
    adjusted, policy = apply_storage_policy(
        settings(),
        [
            *slots([1.0], uncertainty=0.40),
            *slots([4.0], uncertainty=0.50),
        ],
        extra_storage_spread_dkk_per_kwh=2.0,
        opportunistic_target_soc=100,
    )

    assert policy.active
    assert adjusted.target_soc == 100
    assert policy.conservative_charge_price_dkk_per_kwh == 1.4
    assert policy.conservative_discharge_price_dkk_per_kwh == 3.5
    assert policy.price_spread_dkk_per_kwh == 2.1
    assert policy.effective_margin_dkk_per_kwh == 3.5 - 1.4 / 0.85 - 0.35


def test_forecast_only_prices_are_not_an_extra_storage_opportunity() -> None:
    adjusted, policy = apply_storage_policy(
        settings(),
        slots([0.01, 10.0], source="forecast", uncertainty=0.25),
        extra_storage_spread_dkk_per_kwh=2.0,
        opportunistic_target_soc=100,
    )

    assert not policy.active
    assert adjusted.target_soc == 90
    assert policy.known_slot_count == 0
    assert policy.conservative_charge_price_dkk_per_kwh is None
    assert "no valid known-price opportunity" in policy.reason.lower()


def test_grid_availability_is_parsed_only_from_its_explicit_state() -> None:
    assert parse_grid_available("on") is True
    assert parse_grid_available("disconnected") is False
    assert parse_grid_available("3.7") is None
