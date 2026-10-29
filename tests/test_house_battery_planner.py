"""Deterministic tests for the hardware-independent FBP1200 planner."""

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components"))

from house_battery.models import Action, PlannedSlot, PlannerSettings, PriceSlot
from house_battery.planner import optimize

BASE = datetime(2026, 9, 20, 0, 0, tzinfo=UTC)


@pytest.mark.parametrize("soc", [20, 70])
def test_disabled_discharge_has_no_arbitrage_or_terminal_credit(soc) -> None:
    plan = optimize(
        slots([.2] * 8 + [4.0] * 16), now=BASE, soc=soc,
        settings=settings(discharge_enabled=False), current_action=Action.BATTERY,
    )
    assert plan.slots
    assert all(slot.action is Action.GRID for slot in plan.slots)
    assert plan.battery_throughput_kwh == 0
    assert plan.terminal_value_dkk == 0
    assert plan.expected_savings_dkk <= 0


def test_disabled_discharge_still_allows_profitable_negative_price_charging() -> None:
    plan = optimize(
        slots([-1.0] * 8), now=BASE, soc=20,
        settings=settings(discharge_enabled=False),
    )
    assert any(slot.action is Action.CHARGE for slot in plan.slots)
    assert all(slot.action is not Action.BATTERY for slot in plan.slots)


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
        "energy_step_wh": 5,
    }
    values.update(changes)
    return PlannerSettings(**values)


def test_large_spread_charges_then_uses_battery() -> None:
    plan = optimize(
        slots([0.2] * 8 + [1.5] * 4 + [4.0] * 8),
        now=BASE,
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
        now=BASE,
        soc=60,
        settings=settings(),
    )
    assert all(slot.action is Action.GRID for slot in plan.slots)
    assert plan.battery_throughput_kwh == 0


def test_higher_wear_and_profit_hurdle_do_not_increase_battery_use() -> None:
    prices = slots([0.2] * 8 + [2.0] * 8)
    common = {
        "now": BASE,
        "soc": 60,
        "current_action": Action.GRID,
    }
    low_hurdle = optimize(
        prices,
        **common,
        settings=settings(degradation_cost_dkk_per_kwh=0.0, minimum_profit_dkk_per_kwh=0.0),
    )
    high_hurdle = optimize(
        prices,
        **common,
        settings=settings(degradation_cost_dkk_per_kwh=1.0, minimum_profit_dkk_per_kwh=1.5),
    )

    assert high_hurdle.battery_throughput_kwh <= low_hurdle.battery_throughput_kwh
    assert high_hurdle.expected_savings_dkk <= low_hurdle.expected_savings_dkk


def test_reserve_is_never_crossed() -> None:
    plan = optimize(
        slots([5.0] * 24, load_w=800),
        now=BASE,
        soc=30,
        settings=settings(minimum_profit_dkk_per_kwh=0, switching_penalty_dkk=0),
    )
    assert min(slot.soc_end for slot in plan.slots) >= 20


@pytest.mark.parametrize("price", [5.0, -1.0])
def test_below_reserve_energy_changes_only_by_feasible_charging(price: float) -> None:
    """A drained battery cannot acquire its reserve without buying that energy."""
    data = [PriceSlot(BASE, BASE + timedelta(minutes=1), price, 0)]
    plan = optimize(
        data, now=BASE, soc=0,
        settings=settings(
            capacity_wh=2000, round_trip_efficiency=0.81,
            switching_penalty_dkk=0,
        ),
    )

    slot = plan.slots[0]
    assert slot.soc_start == 0
    assert slot.battery_discharge_wh == 0
    # 1200 W for one minute permits 20 Wh input and 18 Wh stored.
    assert slot.grid_import_wh <= 20 + 1e-9
    assert slot.battery_charge_wh <= 18 + 1e-9
    assert slot.soc_end * 20 == pytest.approx(slot.battery_charge_wh)
    if price > 0:
        assert slot.action is Action.GRID
        assert slot.soc_end == 0
    else:
        assert slot.action is Action.CHARGE
        assert slot.battery_charge_wh > 0


@pytest.mark.parametrize("soc", [0, 20, 60])
@pytest.mark.parametrize("price", [-1.0, 5.0])
def test_discretized_movement_respects_load_power_and_energy_conservation(soc, price) -> None:
    """The energy grid may reduce movement, but cannot enlarge physical budgets."""
    data = [
        PriceSlot(BASE, BASE + timedelta(minutes=1), price, 0 if price < 0 else 8),
        PriceSlot(BASE + timedelta(minutes=1), BASE + timedelta(minutes=2), 0, 0),
    ]
    plan = optimize(
        data, now=BASE, soc=soc,
        settings=settings(
            capacity_wh=2000, round_trip_efficiency=0.81,
            minimum_profit_dkk_per_kwh=0, degradation_cost_dkk_per_kwh=0,
            switching_penalty_dkk=0,
        ),
    )

    slot = plan.slots[0]
    assert slot.battery_charge_wh / 0.9 + slot.expected_load_wh <= 20 + 1e-9
    assert slot.battery_discharge_wh * 0.9 <= slot.expected_load_wh + 1e-9
    assert (slot.soc_end - slot.soc_start) * 20 == pytest.approx(
        slot.battery_charge_wh - slot.battery_discharge_wh
    )
    if price < 0:
        assert slot.action is Action.CHARGE
    elif soc > 20:
        assert slot.action is Action.BATTERY


@pytest.mark.parametrize("action", [Action.GRID, Action.CHARGE, Action.BATTERY])
def test_preclipped_tariff_keeps_mode_without_prorating_remaining_load_again(action):
    now = BASE + timedelta(minutes=45)
    data = [
        PriceSlot(
            now, BASE + timedelta(hours=1), 1, 25,
            tariff_start=BASE,
        ),
        PriceSlot(BASE + timedelta(hours=1), BASE + timedelta(hours=2), 1, 100),
    ]
    plan = optimize(data, now=now, soc=50, settings=settings(), current_action=action)

    assert plan.slots[0].start == now
    assert plan.slots[0].expected_load_wh == 25
    assert plan.slots[0].action is action


def test_profitable_small_load_uses_a_reachable_partial_native_charge_target():
    data = [
        PriceSlot(BASE, BASE + timedelta(hours=1), 1, 0),
        PriceSlot(BASE + timedelta(hours=1), BASE + timedelta(hours=2), 5, 100),
        PriceSlot(BASE + timedelta(hours=2), BASE + timedelta(hours=3), 0, 0),
    ]
    plan = optimize(
        data, now=BASE, soc=20,
        settings=settings(capacity_wh=2000, round_trip_efficiency=0.81),
    )

    assert plan.slots[0].action is Action.CHARGE
    assert plan.slots[0].charge_target_soc == 26
    assert plan.slots[0].battery_charge_wh == 120
    assert plan.slots[1].battery_discharge_wh == 110
    # Includes input losses, wear, the required margin, and all three switches.
    assert plan.optimization_objective_dkk == pytest.approx(0.397233, abs=1e-6)
    assert plan.optimization_objective_dkk < 0.5  # Grid-only reference.


@pytest.mark.parametrize("targets,price,target", [
    (None, 1, 26),
    ((50, 60, 70, 80, 90), .1, 50),
    ((), .1, 90),
    ((20, 26, 100), 1, 26),
])
def test_partial_charging_uses_only_supported_native_ceilings(targets, price, target):
    data = [
        PriceSlot(BASE, BASE + timedelta(hours=1), price, 0),
        PriceSlot(BASE + timedelta(hours=1), BASE + timedelta(hours=2), 5, 100),
        PriceSlot(BASE + timedelta(hours=2), BASE + timedelta(hours=3), 0, 0),
    ]
    plan = optimize(
        data, now=BASE, soc=20,
        settings=settings(
            capacity_wh=2000, round_trip_efficiency=0.81,
            partial_charge_targets=targets,
        ),
    )

    assert plan.slots[0].action is Action.CHARGE
    assert plan.slots[0].charge_target_soc == target


@pytest.mark.parametrize("price", [1, .1])
def test_exact_economic_wash_preserves_zero_throughput_path(price):
    data = [
        PriceSlot(BASE + timedelta(hours=index), BASE + timedelta(hours=index + 1), price, 50)
        for index in range(24)
    ]
    plan = optimize(
        data, now=BASE, soc=50,
        settings=settings(
            capacity_wh=200, reserve_soc=0, target_soc=100,
            charge_power_w=200, discharge_power_w=200, round_trip_efficiency=1,
            degradation_cost_dkk_per_kwh=0, minimum_profit_dkk_per_kwh=0,
            switching_penalty_dkk=0,
        ),
    )

    assert plan.battery_throughput_kwh == 0
    assert all(slot.action is Action.GRID for slot in plan.slots)


def test_target_is_never_crossed() -> None:
    plan = optimize(
        slots([-0.5] * 16, load_w=100),
        now=BASE,
        soc=85,
        settings=settings(minimum_profit_dkk_per_kwh=0, switching_penalty_dkk=0),
    )
    assert max(slot.soc_end for slot in plan.slots) <= 90


def test_starting_above_target_does_not_allow_later_recharge_above_target() -> None:
    """Observed excess energy may remain, but it must not raise the charge ceiling."""
    price_slots = [
        PriceSlot(
            BASE + timedelta(hours=index),
            BASE + timedelta(hours=index + 1),
            price,
            expected_load_wh=load_wh,
        )
        for index, (price, load_wh) in enumerate(
            [(5.0, 150.0), (0.1, 0.0), (5.0, 150.0)]
        )
    ]
    plan = optimize(
        price_slots,
        now=BASE,
        soc=100,
        settings=settings(
            capacity_wh=200,
            reserve_soc=0,
            target_soc=75,
            charge_power_w=400,
            discharge_power_w=400,
            round_trip_efficiency=0.81,
            degradation_cost_dkk_per_kwh=0,
            minimum_profit_dkk_per_kwh=0,
            switching_penalty_dkk=0,
            energy_step_wh=25,
        ),
    )

    charge_slot = next(slot for slot in plan.slots if slot.action is Action.CHARGE)
    assert charge_slot.soc_end <= 75


def test_gap_truncates_horizon_instead_of_inventing_prices() -> None:
    data = slots([1.0, 1.1, 1.2, 1.3])
    data[2] = PriceSlot(
        data[2].start + timedelta(minutes=15),
        data[2].end + timedelta(minutes=15),
        1.2,
        125,
    )
    plan = optimize(data, now=BASE, soc=50, settings=settings())
    assert len(plan.slots) == 2


def test_gap_covering_now_returns_no_executable_plan() -> None:
    future = slots([0.2, 4.0])
    future = [
        PriceSlot(
            item.start + timedelta(minutes=15),
            item.end + timedelta(minutes=15),
            item.price,
            item.expected_load_wh,
        )
        for item in future
    ]

    plan = optimize(future, now=BASE, soc=50, settings=settings())

    assert not plan.slots
    assert plan.current_action is Action.SAFE
    assert "current price interval" in plan.reason


def test_invalid_current_interval_cannot_promote_a_future_action() -> None:
    data = slots([1.0, 4.0])
    data[0] = PriceSlot(
        data[0].start,
        data[0].end,
        float("nan"),
        data[0].expected_load_wh,
    )

    plan = optimize(data, now=BASE, soc=50, settings=settings())

    assert not plan.slots
    assert plan.current_action is Action.SAFE


def test_optimizer_can_switch_modes_on_adjacent_intervals() -> None:
    plan = optimize(
        slots([5] + [0.1] * 8 + [5] * 4),
        now=BASE,
        soc=50,
        settings=settings(minimum_profit_dkk_per_kwh=0, switching_penalty_dkk=0),
        current_action=Action.GRID,
    )
    assert plan.slots[0].action is Action.BATTERY
    assert Action.CHARGE in [item.action for item in plan.slots]


def test_charge_at_target_does_not_change_grid_import() -> None:
    plan = optimize(
        slots([0.1, 0.1]),
        now=BASE,
        soc=90,
        settings=settings(minimum_profit_dkk_per_kwh=0, switching_penalty_dkk=0),
    )

    assert all(item.battery_charge_wh == 0 for item in plan.slots)
    assert all(item.grid_import_wh == 125 for item in plan.slots)


def test_battery_at_reserve_does_not_discharge() -> None:
    plan = optimize(
        slots([5.0, 5.0]),
        now=BASE,
        soc=20,
        settings=settings(minimum_profit_dkk_per_kwh=0, switching_penalty_dkk=0),
    )

    assert all(item.battery_discharge_wh == 0 for item in plan.slots)
    assert all(item.grid_import_wh == 125 for item in plan.slots)


def test_battery_action_is_costed_against_future_opportunity() -> None:
    plan = optimize(
        slots([2.0, 2.0]),
        now=BASE,
        soc=60,
        settings=settings(),
        current_action=Action.BATTERY,
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
        now=BASE,
        soc=60,
        settings=settings(minimum_profit_dkk_per_kwh=0, switching_penalty_dkk=0),
    )
    assert plan.slots[0].action is Action.BATTERY
    assert plan.slots[0].battery_discharge_wh > 0

    plan_margin = optimize(
        slots([1.4, 1.0, 1.0, 1.0]),
        now=BASE,
        soc=60,
        settings=settings(minimum_profit_dkk_per_kwh=0.75, switching_penalty_dkk=0),
    )
    assert plan_margin.slots[0].action is Action.GRID


def test_optimizer_can_use_multiple_transitions_for_spread() -> None:
    plan = optimize(
        slots([0.1] * 8 + [5.0] * 8),
        now=BASE,
        soc=50,
        settings=settings(minimum_profit_dkk_per_kwh=0, switching_penalty_dkk=0),
        current_action=Action.GRID,
    )
    actions = [item.action for item in plan.slots]
    assert Action.CHARGE in actions
    assert Action.BATTERY in actions


def test_long_horizon_can_use_a_future_profitable_cycle() -> None:
    now = BASE
    plan = optimize(
        slots([1.0] * 96 + [0.1] * 8 + [5.0] * 8),
        now=now,
        soc=50,
        settings=settings(),
        current_action=Action.GRID,
    )

    actions = [item.action for item in plan.slots]
    assert Action.CHARGE in actions
    assert Action.BATTERY in actions


def test_battery_mode_yields_to_grid_when_discharge_is_not_profitable() -> None:
    plan = optimize(
        slots([1.0, 1.0], load_w=800),
        now=BASE,
        soc=30,
        settings=settings(minimum_profit_dkk_per_kwh=0, switching_penalty_dkk=0),
        current_action=Action.BATTERY,
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


def test_current_partial_price_interval_keeps_the_observed_action() -> None:
    """Economic replans must not switch modes inside one tariff interval."""
    plan = optimize(
        slots([5.0, 5.0, 0.1]),
        now=BASE + timedelta(minutes=5),
        soc=60,
        settings=settings(
            minimum_profit_dkk_per_kwh=0,
            switching_penalty_dkk=0,
        ),
        current_action=Action.GRID,
    )

    assert plan.slots[0].start == BASE + timedelta(minutes=5)
    assert plan.slots[0].end == BASE + timedelta(minutes=15)
    assert plan.slots[0].action is Action.GRID
    assert Action.BATTERY in [item.action for item in plan.slots[1:]]


@pytest.mark.parametrize("action", [Action.CHARGE, Action.BATTERY])
def test_last_seconds_preserve_mode_when_movement_rounds_to_zero(action: Action) -> None:
    plan = optimize(
        slots([1.1, 1.15, 4.0, 4.0], load_w=250),
        now=BASE + timedelta(minutes=15, seconds=-4),
        soc=60,
        settings=settings(),
        current_action=action,
    )

    assert plan.slots[0].action is action
    assert plan.slots[0].soc_end == plan.slots[0].soc_start
    assert plan.slots[0].battery_charge_wh == 0
    assert plan.slots[0].battery_discharge_wh == 0


@pytest.mark.parametrize("action,soc", [(Action.CHARGE, 90), (Action.BATTERY, 20)])
def test_last_seconds_still_release_mode_at_physical_limit(action: Action, soc: float) -> None:
    plan = optimize(
        slots([1.0, 1.0], load_w=250),
        now=BASE + timedelta(minutes=15, seconds=-4),
        soc=soc,
        settings=settings(),
        current_action=action,
    )

    assert plan.slots[0].action is Action.GRID


@pytest.mark.parametrize("action,load_w", [(Action.CHARGE, 1200), (Action.BATTERY, 0)])
def test_last_seconds_release_mode_without_available_power(action: Action, load_w: float) -> None:
    plan = optimize(
        slots([1.0, 1.0], load_w=load_w),
        now=BASE + timedelta(minutes=15, seconds=-4),
        soc=60, settings=settings(), current_action=action,
    )

    assert plan.slots[0].action is Action.GRID


@pytest.mark.parametrize("action", [Action.CHARGE, Action.GRID])
def test_future_soc_uses_committed_interval_instead_of_observed_mode(action: Action) -> None:
    now = BASE + timedelta(minutes=5)
    committed = PlannedSlot(
        start=BASE, end=BASE + timedelta(minutes=15), action=action,
        price=1.0, expected_load_wh=62.5, grid_import_wh=62.5,
        battery_charge_wh=0, battery_discharge_wh=0,
        soc_start=95, soc_end=100 if action is Action.CHARGE else 95,
        interval_cost_dkk=0, baseline_cost_dkk=0, reason="published",
        charge_target_soc=100 if action is Action.CHARGE else None,
    )
    plan = optimize(
        slots([1.0, 1.0, 4.0, 4.0], load_w=250), now=now, soc=95,
        settings=settings(),
        current_action=Action.GRID if action is Action.CHARGE else Action.CHARGE,
        committed_slot=committed,
    )

    assert plan.slots[0].action is action
    if action is Action.CHARGE:
        assert plan.slots[0].soc_end > 99
        assert plan.slots[0].charge_target_soc == 100
    else:
        assert plan.slots[0].soc_end == plan.slots[0].soc_start
    assert plan.slots[1].soc_start == plan.slots[0].soc_end


@pytest.mark.parametrize("end_minutes", [0, 30])
def test_expired_or_different_tariff_commitment_is_ignored(end_minutes: int) -> None:
    committed = PlannedSlot(
        start=BASE - timedelta(minutes=15), end=BASE + timedelta(minutes=end_minutes),
        action=Action.GRID, price=1.0, expected_load_wh=0, grid_import_wh=0,
        battery_charge_wh=0, battery_discharge_wh=0, soc_start=60, soc_end=60,
        interval_cost_dkk=0, baseline_cost_dkk=0, reason="stale commitment",
    )
    plan = optimize(
        slots([1.0, 4.0], load_w=250), now=BASE + timedelta(minutes=5),
        soc=60, settings=settings(), current_action=Action.CHARGE,
        committed_slot=committed,
    )

    assert plan.slots[0].action is Action.CHARGE


def test_planner_is_reproducible() -> None:
    arguments = {"now": BASE, "soc": 20, "settings": settings()}
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
        now=BASE,
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
        now=BASE,
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
        now=BASE,
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
        now=BASE,
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
