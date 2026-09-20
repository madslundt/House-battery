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


def slots(prices: list[float]) -> list[PriceSlot]:
    start = datetime(2026, 9, 20, tzinfo=UTC)
    return [
        PriceSlot(
            start + timedelta(minutes=15 * index),
            start + timedelta(minutes=15 * (index + 1)),
            price,
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


def test_grid_availability_is_parsed_only_from_its_explicit_state() -> None:
    assert parse_grid_available("on") is True
    assert parse_grid_available("disconnected") is False
    assert parse_grid_available("3.7") is None
