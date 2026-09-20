"""Deterministic tests for the hardware-independent FBP1200 planner."""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components"))

from fossibot_fbp1200.models import Action, PlannerSettings, PriceSlot
from fossibot_fbp1200.planner import optimize

BASE = datetime(2026, 9, 20, 0, 0, tzinfo=timezone.utc)


def slots(prices: list[float], load_w: float = 500) -> list[PriceSlot]:
    return [
        PriceSlot(
            BASE + timedelta(minutes=15 * index),
            BASE + timedelta(minutes=15 * (index + 1)),
            price,
            expected_load_wh=load_w / 4,
        )
        for index, price in enumerate(prices)
    ]


def settings(**changes: float) -> PlannerSettings:
    values = {
        "capacity_wh": 1958,
        "reserve_soc": 20,
        "target_soc": 90,
        "charge_power_w": 1200,
        "discharge_power_w": 800,
        "round_trip_efficiency": 0.85,
        "degradation_cost_dkk_per_kwh": 0.35,
        "minimum_profit_dkk_per_kwh": 0.75,
        "switching_penalty_dkk": 0.05,
        "minimum_mode_minutes": 30,
        "maximum_transitions": 4,
        "energy_step_wh": 25,
    }
    values.update(changes)
    return PlannerSettings(**values)


def test_large_spread_charges_then_uses_battery() -> None:
    plan = optimize(
        slots([0.2] * 8 + [1.5] * 4 + [4.0] * 8),
        now=BASE - timedelta(seconds=1),
        soc=20,
        settings=settings(),
    )
    actions = [slot.action for slot in plan.slots]
    assert Action.CHARGE in actions[:8]
    assert Action.BATTERY in actions[-8:]
    assert plan.expected_savings_dkk > 0


def test_small_spread_is_not_worth_battery_wear() -> None:
    plan = optimize(
        slots([1.00] * 8 + [1.25] * 8),
        now=BASE - timedelta(seconds=1),
        soc=60,
        settings=settings(),
    )
    assert all(slot.action is Action.GRID for slot in plan.slots)
    assert plan.battery_throughput_kwh == 0


def test_reserve_is_never_crossed() -> None:
    plan = optimize(
        slots([5.0] * 24, load_w=800),
        now=BASE - timedelta(seconds=1),
        soc=30,
        settings=settings(minimum_profit_dkk_per_kwh=0, switching_penalty_dkk=0),
    )
    assert min(slot.soc_end for slot in plan.slots) >= 20


def test_target_is_never_crossed() -> None:
    plan = optimize(
        slots([-0.5] * 16, load_w=100),
        now=BASE - timedelta(seconds=1),
        soc=85,
        settings=settings(minimum_profit_dkk_per_kwh=0, switching_penalty_dkk=0),
    )
    assert max(slot.soc_end for slot in plan.slots) <= 90


def test_gap_truncates_horizon_instead_of_inventing_prices() -> None:
    data = slots([1.0, 1.1, 1.2, 1.3])
    data[2] = PriceSlot(
        data[2].start + timedelta(minutes=15),
        data[2].end + timedelta(minutes=15),
        1.2,
        125,
    )
    plan = optimize(data, now=BASE - timedelta(seconds=1), soc=50, settings=settings())
    assert len(plan.slots) == 2


def test_mode_lock_prevents_early_transition() -> None:
    plan = optimize(
        slots([0.1, 0.1, 5, 5]),
        now=BASE - timedelta(seconds=1),
        soc=50,
        settings=settings(minimum_profit_dkk_per_kwh=0, switching_penalty_dkk=0),
        current_action=Action.GRID,
        mode_lock_remaining_minutes=30,
    )
    assert [item.action for item in plan.slots[:2]] == [Action.GRID, Action.GRID]


def test_transition_budget_keeps_current_mode() -> None:
    plan = optimize(
        slots([0.1] * 8 + [5.0] * 8),
        now=BASE - timedelta(seconds=1),
        soc=50,
        settings=settings(maximum_transitions=0),
        current_action=Action.GRID,
    )
    assert all(item.action is Action.GRID for item in plan.slots)


def test_current_partial_interval_is_planned_from_now() -> None:
    plan = optimize(
        slots([0.1, 5.0, 5.0]),
        now=BASE + timedelta(minutes=5),
        soc=20,
        settings=settings(minimum_profit_dkk_per_kwh=0, switching_penalty_dkk=0),
    )
    assert plan.slots[0].start == BASE + timedelta(minutes=5)
    assert plan.slots[0].end == BASE + timedelta(minutes=15)
    assert plan.slots[0].expected_load_wh == pytest.approx(500 / 6)


def test_planner_is_reproducible() -> None:
    arguments = {"now": BASE - timedelta(seconds=1), "soc": 20, "settings": settings()}
    first = optimize(slots([0.2] * 8 + [4.0] * 8), **arguments)
    second = optimize(slots([0.2] * 8 + [4.0] * 8), **arguments)
    assert first == second
