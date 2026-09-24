"""Deterministic tests for the hardware-independent FBP1200 planner."""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components"))

from house_battery.models import Action, PlannerSettings, PriceSlot
from house_battery.planner import optimize

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


def test_locked_charge_at_target_keeps_an_executable_energy_neutral_plan() -> None:
    plan = optimize(
        slots([0.1, 0.1]),
        now=BASE - timedelta(seconds=1),
        soc=90,
        settings=settings(minimum_profit_dkk_per_kwh=0, switching_penalty_dkk=0),
        current_action=Action.CHARGE,
        mode_lock_remaining_minutes=30,
    )

    assert [item.action for item in plan.slots] == [Action.CHARGE, Action.CHARGE]
    assert all(item.battery_charge_wh == 0 for item in plan.slots)
    assert all(item.grid_import_wh == 125 for item in plan.slots)
    assert "Energy-neutral" in plan.slots[0].reason


def test_locked_battery_at_reserve_keeps_an_executable_energy_neutral_plan() -> None:
    plan = optimize(
        slots([5.0, 5.0]),
        now=BASE - timedelta(seconds=1),
        soc=20,
        settings=settings(minimum_profit_dkk_per_kwh=0, switching_penalty_dkk=0),
        current_action=Action.BATTERY,
        mode_lock_remaining_minutes=30,
    )

    assert [item.action for item in plan.slots] == [Action.BATTERY, Action.BATTERY]
    assert all(item.battery_discharge_wh == 0 for item in plan.slots)
    assert all(item.grid_import_wh == 125 for item in plan.slots)
    assert "Energy-neutral" in plan.slots[0].reason


def test_locked_battery_exits_to_grid_below_the_economic_price_floor() -> None:
    """A mode lock cannot leave hardware in a self-discharging mode."""
    plan = optimize(
        slots([2.0, 2.0]),
        now=BASE - timedelta(seconds=1),
        soc=60,
        settings=settings(),
        current_action=Action.BATTERY,
        mode_lock_remaining_minutes=30,
    )

    assert plan.slots[0].action is Action.GRID
    assert plan.slots[0].battery_discharge_wh == 0
    assert plan.slots[0].grid_import_wh == 125
    assert "Grid supplies" in plan.slots[0].reason


def test_existing_stored_energy_discharges_on_opportunity_not_cheapest_floor() -> None:
    """Stored energy is valued by future opportunity cost, not the cheapest
    future *charge* price.

    Prices are 1.4 now and 1.0 thereafter. The cheapest-future-charge floor is
    1.0 / efficiency + degradation + margin = 1.53 (margin=0), so the old
    universal discharge gate rejected burning energy at 1.4. But the true
    opportunity cost of energy already inside the battery is ~1.0 (the future
    price net of wear), so discharging now at 1.4 clears it and is genuinely
    profitable. With the hard floor removed the dynamic program discharges;
    with a real required margin the discharge bar rises above 1.4 and the
    battery holds, proving the decision now follows the objective, not a global
    prohibition keyed on the cheapest future charging slot.
    """
    plan = optimize(
        slots([1.4, 1.0, 1.0, 1.0]),
        now=BASE - timedelta(seconds=1),
        soc=60,
        settings=settings(minimum_profit_dkk_per_kwh=0, switching_penalty_dkk=0),
    )
    assert plan.slots[0].action is Action.BATTERY
    assert plan.slots[0].battery_discharge_wh > 0

    plan_margin = optimize(
        slots([1.4, 1.0, 1.0, 1.0]),
        now=BASE - timedelta(seconds=1),
        soc=60,
        settings=settings(minimum_profit_dkk_per_kwh=0.75, switching_penalty_dkk=0),
    )
    assert plan_margin.slots[0].action is Action.GRID


def test_transition_budget_keeps_current_mode() -> None:
    plan = optimize(
        slots([0.1] * 8 + [5.0] * 8),
        now=BASE - timedelta(seconds=1),
        soc=50,
        settings=settings(maximum_transitions=0),
        current_action=Action.GRID,
    )
    assert all(item.action is Action.GRID for item in plan.slots)


def test_expired_transition_budget_allows_a_future_profitable_cycle() -> None:
    """A rolling limit must release capacity when old transitions expire."""
    now = BASE
    plan = optimize(
        slots([1.0] * 96 + [0.1] * 8 + [5.0] * 8),
        now=now - timedelta(seconds=1),
        soc=50,
        settings=settings(),
        current_action=Action.GRID,
        transition_times=[
            now - timedelta(hours=23, minutes=30) + timedelta(minutes=index)
            for index in range(4)
        ],
    )

    actions = [item.action for item in plan.slots]
    assert Action.CHARGE in actions
    assert Action.BATTERY in actions


def test_transition_budget_yields_to_grid_when_battery_mode_would_lose_money() -> None:
    """Transition limits cannot retain a self-discharging battery above reserve."""
    plan = optimize(
        slots([1.0, 1.0], load_w=800),
        now=BASE - timedelta(seconds=1),
        soc=30,
        settings=settings(minimum_profit_dkk_per_kwh=0, switching_penalty_dkk=0),
        current_action=Action.BATTERY,
        mode_lock_remaining_minutes=15,
        transitions_used=4,
    )

    assert [item.action for item in plan.slots] == [Action.GRID, Action.GRID]
    assert all(item.battery_discharge_wh == 0 for item in plan.slots)
    assert all(item.grid_import_wh > 0 for item in plan.slots)


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


def test_no_wasteful_discharge_at_target_soc() -> None:
    """When at target SOC the battery must not discharge just to recharge later.

    This prevents the wasteful discharge→recharge cycle that burns round-trip
    efficiency with no net benefit.  The battery should hold and let the grid
    supply the load instead.
    """
    plan = optimize(
        slots([5.0] * 8, load_w=200),
        now=BASE - timedelta(seconds=1),
        soc=90,
        settings=settings(minimum_profit_dkk_per_kwh=0, switching_penalty_dkk=0),
    )
    # Battery must not discharge when already at target SOC
    assert all(
        slot.battery_discharge_wh == 0 for slot in plan.slots
    ), "Should not discharge at target SOC"


def test_battery_at_target_holds_and_uses_grid() -> None:
    """When at 90% target the battery holds charge and uses grid for load."""

    plan = optimize(
        slots([5.0] * 4, load_w=200),
        now=BASE - timedelta(seconds=1),
        soc=90,
        settings=settings(minimum_profit_dkk_per_kwh=0, switching_penalty_dkk=0),
    )
    # Grid should supply the load (battery doesn't discharge)
    assert all(
        slot.grid_import_wh >= slot.expected_load_wh
        for slot in plan.slots
    ), "Grid should supply at least the load"


def test_savings_without_battery_movement_is_terminal_only() -> None:
    """When the battery never moves, reported savings is a notional terminal credit.

    The optimizer credits stored energy at the terminal price so the objective
    is not understated, but that credit only becomes real money if the battery
    is actually discharged at that price.  With zero throughput the headline
    ``expected_savings_dkk`` is therefore entirely a terminal-value accounting
    line, and ``realized_savings_dkk`` (savings minus that credit) must be ~0.
    """
    plan = optimize(
        slots([1.00] * 8 + [1.25] * 8),
        now=BASE - timedelta(seconds=1),
        soc=60,
        settings=settings(),
    )
    assert plan.battery_throughput_kwh == 0
    assert plan.terminal_value_dkk > 0
    # The realized figure is exactly the headline figure minus the terminal credit.
    assert plan.realized_savings_dkk == plan.expected_savings_dkk - plan.terminal_value_dkk
    # No battery movement -> no realizable savings, only the notional terminal line.
    assert abs(plan.realized_savings_dkk) < 1e-9


def test_no_banking_through_horizon_end_evening_peak() -> None:
    """Energy held to the horizon edge must not be banked through a spike.

    A cheap afternoon dip followed by an expensive evening peak at the very end
    of the known-price horizon is the classic place a terminal value that is too
    high turns the optimizer into a hoarder: it charges into the dip and then
    holds the battery full through the most expensive slots instead of
    discharging. The headline ``expected_savings_dkk`` still looks positive (the
    over-inflated terminal credit), but ``realized_savings_dkk`` is negative - a
    real loss. The terminal value is a conservative avoided-purchase floor, so
    the optimizer must drain into the peak, realise a profit, and end well below
    target rather than banked full.
    """
    plan = optimize(
        slots([2.00] * 8 + [0.90] * 4 + [2.00] * 8 + [3.60] * 4, load_w=100),
        now=BASE - timedelta(seconds=1),
        soc=49,
        settings=settings(),
    )
    actions = [slot.action for slot in plan.slots]
    # The battery moves and drains into the late peak rather than holding it.
    assert plan.battery_throughput_kwh > 0
    assert Action.BATTERY in actions
    # Realised (not headline) savings must be positive: charging to hold was a loss.
    assert plan.realized_savings_dkk > 0
    # Ending well below target confirms the energy was spent, not banked full.
    assert plan.slots[-1].soc_end < 60
