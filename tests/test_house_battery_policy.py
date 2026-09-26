"""Tests for normal and opt-in opportunistic storage policy."""

import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components"))

from house_battery.models import PlannerSettings, PriceSlot
from house_battery.planner import optimize
from house_battery.policy import (
    apply_storage_policy,
    parse_grid_available,
    select_opportunistic_plan,
)


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
    assert decision.plan.slots[-1].soc_end <= normal_settings.target_soc
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


def test_opportunistic_policy_rejects_energy_left_above_normal_target() -> None:
    normal_settings = settings()
    full_settings = replace(normal_settings, target_soc=100)
    prices = _profitable_prices()
    now = prices[0].start
    normal = optimize(prices, now=now, soc=20, settings=normal_settings)
    full = optimize(prices, now=now, soc=20, settings=full_settings)
    banked_last_slot = replace(full.slots[-1], soc_end=95)
    banking_plan = replace(full, slots=(*full.slots[:-1], banked_last_slot))

    decision = select_opportunistic_plan(
        normal,
        banking_plan,
        normal_settings,
        full_settings,
        enabled=True,
    )

    assert not decision.active
    assert decision.plan is normal
    assert "not scheduled for use" in decision.reason


def test_grid_availability_is_parsed_only_from_its_explicit_state() -> None:
    assert parse_grid_available("on") is True
    assert parse_grid_available("disconnected") is False
    assert parse_grid_available("3.7") is None
