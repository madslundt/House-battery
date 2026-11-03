"""Differential tests against an exhaustive small-horizon planner oracle."""

from __future__ import annotations

import random
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import ceil, floor, sqrt
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components"))

from house_battery.models import Action, PlannerSettings, PriceSlot
from house_battery.planner import optimize

BASE = datetime(2026, 9, 26, tzinfo=UTC)
_ACTIONS = (Action.GRID, Action.BATTERY, Action.CHARGE)


@dataclass(frozen=True, slots=True)
class _OraclePath:
    energy_step: int
    # Physical activity at the boundary can be idle even though the command
    # during the preceding interval was CHARGE with an early SOC ceiling.
    action: Action
    objective_cost: float
    throughput_wh: float
    actions: tuple[Action, ...]
    energy_steps: tuple[int, ...] = ()


def _low_soc_area(start_wh: float, end_wh: float, settings: PlannerSettings) -> float:
    """Trapezoid integration of the piecewise-linear charging preference."""
    threshold = settings.capacity_wh * settings.low_soc_charge_threshold / 100
    if threshold <= 0 or end_wh <= start_wh:
        return 0.0
    points = sorted({start_wh, end_wh, min(end_wh, max(start_wh, threshold))})
    weights = [max(0.0, 1 - value / threshold) for value in points]
    return sum(
        (points[index + 1] - points[index]) * (weights[index + 1] + weights[index]) / 2
        for index in range(len(points) - 1)
    )


def _settings(**changes: float) -> PlannerSettings:
    values = {
        "capacity_wh": 200.0,
        "reserve_soc": 0.0,
        "target_soc": 100.0,
        "charge_power_w": 200.0,
        "discharge_power_w": 200.0,
        "round_trip_efficiency": 0.81,
        "degradation_cost_dkk_per_kwh": 0.2,
        "minimum_profit_dkk_per_kwh": 0.3,
        "switching_penalty_dkk": 0.02,
        "energy_step_wh": 25.0,
    }
    values.update(changes)
    return PlannerSettings(**values)


def _price_slots(
    prices: list[float],
    *,
    minutes: list[int] | None = None,
    loads_wh: list[float] | None = None,
    sources: list[str] | None = None,
    uncertainties: list[float] | None = None,
) -> list[PriceSlot]:
    minutes = minutes or [15] * len(prices)
    loads_wh = loads_wh or [50.0] * len(prices)
    sources = sources or ["known"] * len(prices)
    uncertainties = uncertainties or [0.0] * len(prices)
    result: list[PriceSlot] = []
    start = BASE
    for price, duration, load, source, uncertainty in zip(
        prices,
        minutes,
        loads_wh,
        sources,
        uncertainties,
        strict=True,
    ):
        end = start + timedelta(minutes=duration)
        result.append(
            PriceSlot(
                start,
                end,
                price,
                expected_load_wh=load,
                source=source,
                uncertainty_dkk_per_kwh=uncertainty,
            )
        )
        start = end
    return result


def _oracle_transition(
    path: _OraclePath,
    action: Action,
    slot: PriceSlot,
    settings: PlannerSettings,
    minimum_step: int,
    maximum_step: int,
) -> _OraclePath | None:
    """Independently reproduce one discretized physical/economic transition."""
    step_wh = settings.energy_step_wh
    energy_wh = path.energy_step * step_wh
    load_wh = max(0.0, slot.expected_load_wh)
    efficiency = sqrt(settings.round_trip_efficiency)
    grid_wh = load_wh
    changed = action is not path.action
    physical_end_action = action
    stop_cost = 0.0

    if action is Action.CHARGE:
        load_power_w = load_wh / slot.hours if slot.hours > 0 else 0.0
        available_input_w = max(0.0, settings.charge_power_w - load_power_w)
        headroom_wh = max(0.0, maximum_step * step_wh - energy_wh)
        input_wh = min(available_input_w * slot.hours, headroom_wh / efficiency)
        # Enumerate stored-energy states satisfying the input-energy budget.
        # This oracle must independently reject physically impossible steps,
        # rather than repeat the optimizer's rounding/clamping implementation.
        feasible = [
            step for step in range(path.energy_step + 1, maximum_step + 1)
            if (step - path.energy_step) * step_wh / efficiency <= input_wh + 1e-9
        ]
        if not feasible:
            return None
        new_step = max(feasible)
    elif action is Action.BATTERY:
        available_wh = max(0.0, energy_wh - minimum_step * step_wh)
        delivered_wh = min(
            load_wh,
            settings.discharge_power_w * slot.hours,
            available_wh * efficiency,
        )
        feasible = [
            step for step in range(minimum_step, path.energy_step)
            if (path.energy_step - step) * step_wh * efficiency <= delivered_wh + 1e-9
        ]
        if not feasible:
            return None
        new_step = min(feasible)
    else:
        new_step = path.energy_step
    delta_wh = (new_step - path.energy_step) * step_wh
    if action is Action.CHARGE:
        if delta_wh <= 0:
            return None
        available_stored_wh = available_input_w * slot.hours * efficiency
        if available_stored_wh - delta_wh >= step_wh - 1e-9:
            # A ceiling leaves at least one whole model step of unused input
            # time. It causes a physical stop; resuming next interval is a new
            # start even if the published command label remains CHARGE.
            physical_end_action = Action.GRID
            stop_cost = settings.switching_penalty_dkk
        throughput_wh = delta_wh
        grid_wh = load_wh + delta_wh / efficiency
        movement_cost = delta_wh / efficiency / 1000 * slot.uncertainty_dkk_per_kwh
        movement_cost -= (
            _low_soc_area(energy_wh, new_step * step_wh, settings)
            / efficiency / 1000 * settings.low_soc_charge_premium_dkk_per_kwh
        )
    elif action is Action.BATTERY:
        if delta_wh >= 0:
            return None
        throughput_wh = -delta_wh
        delivered_wh = throughput_wh * efficiency
        grid_wh = max(0.0, load_wh - delivered_wh)
        movement_cost = delivered_wh / 1000 * (
            settings.degradation_cost_dkk_per_kwh
            + settings.minimum_profit_dkk_per_kwh
            + slot.uncertainty_dkk_per_kwh
        )
    else:
        throughput_wh = 0.0
        movement_cost = 0.0

    interval_cost = grid_wh / 1000 * slot.price + movement_cost + stop_cost
    if changed:
        interval_cost += settings.switching_penalty_dkk
    return _OraclePath(
        energy_step=new_step,
        action=physical_end_action,
        objective_cost=path.objective_cost + interval_cost,
        throughput_wh=path.throughput_wh + throughput_wh,
        actions=path.actions + (action,),
        energy_steps=path.energy_steps + (new_step,),
    )


def _oracle(
    slots: list[PriceSlot],
    *,
    soc: float,
    settings: PlannerSettings,
    current_action: Action,
) -> tuple[tuple[float, float, str], _OraclePath]:
    """Enumerate every feasible action path without dynamic-programming pruning."""
    step_wh = settings.energy_step_wh
    minimum_step = ceil(settings.capacity_wh * settings.reserve_soc / 100 / step_wh)
    initial_step = max(0, round(settings.capacity_wh * soc / 100 / step_wh))
    maximum_step = int(
        settings.capacity_wh * settings.target_soc / 100 // step_wh
    )
    paths = [
        _OraclePath(
            energy_step=initial_step,
            action=current_action,
            objective_cost=0.0,
            throughput_wh=0.0,
            actions=(),
        )
    ]
    native_targets = (
        range(floor(settings.reserve_soc) + 1, floor(settings.target_soc) + 1)
        if settings.partial_charge_targets is None else settings.partial_charge_targets
    )
    native_ceilings = {
        floor(settings.capacity_wh * target / 100 / step_wh)
        for target in native_targets
        if settings.reserve_soc < target <= settings.target_soc
        if minimum_step < floor(settings.capacity_wh * target / 100 / step_wh) <= maximum_step
    }
    for slot in slots:
        expanded = []
        for path in paths:
            alternatives = set()
            for action in _ACTIONS:
                ceilings = native_ceilings | {maximum_step} if action is Action.CHARGE else {maximum_step}
                for ceiling in ceilings:
                    candidate = _oracle_transition(
                        path, action, slot, settings, minimum_step, ceiling
                    )
                    if candidate is not None:
                        alternatives.add(candidate)
            expanded.extend(alternatives)
        paths = expanded

    if soc < settings.reserve_soc and initial_step < minimum_step:
        known_prefix = 0
        for slot in slots:
            if slot.source != "known":
                break
            known_prefix += 1
        if known_prefix:
            # Inspect complete physical paths independently of DP pruning.
            # Their stored energy at the confirmed-price boundary establishes
            # the achievable reserve coverage required of the final winner.
            coverage = min(
                minimum_step,
                max(path.energy_steps[known_prefix - 1] for path in paths),
            )
            paths = [
                path for path in paths
                if path.energy_steps[known_prefix - 1] >= coverage
            ]

    discharge_efficiency = sqrt(settings.round_trip_efficiency)
    def continuation_reference(prices: list[PriceSlot]) -> float:
        boundary = max(slot.end for slot in prices) - timedelta(hours=3)
        conservative = [slot.price - slot.uncertainty_dkk_per_kwh for slot in prices]
        edge = min(slot.price - slot.uncertainty_dkk_per_kwh
                   for slot in prices if slot.end > boundary)
        return max(0.0, edge * (.75 if max(conservative) - min(conservative) > .05 else 1))

    acquisition_quote = inventory_quote = continuation_reference(slots)
    known = [slot for slot in slots if slot.source == "known"]
    if known and any(slot.source == "forecast" for slot in slots):
        boundary = max(slot.end for slot in slots) - timedelta(hours=3)
        tail = [slot for slot in slots if slot.end > boundary]
        lower = max(0, min(slot.price - slot.uncertainty_dkk_per_kwh for slot in tail))
        upper = max(lower, min(slot.price + slot.uncertainty_dkk_per_kwh for slot in tail))
        acquisition_quote = lower
        # Nearest plausible value to the confirmed continuation reference.
        # Explicit uncertainty replaces the extra heuristic discount; it must
        # neither liquidate existing stock nor finance speculative new stock.
        inventory_quote = sorted((lower, continuation_reference(known), upper))[1]
    acquisition_net = max(0, acquisition_quote - settings.degradation_cost_dkk_per_kwh)
    inventory_net = max(0, inventory_quote - settings.degradation_cost_dkk_per_kwh)
    initial_inventory_wh = max(0, initial_step - minimum_step) * step_wh

    def final_key(path: _OraclePath) -> tuple[float, float, str]:
        stored_wh = max(0, path.energy_step - minimum_step) * step_wh
        existing_wh = min(stored_wh, initial_inventory_wh)
        added_wh = stored_wh - existing_wh
        terminal_value = discharge_efficiency / 1000 * (
            existing_wh * inventory_net + added_wh * acquisition_net
        )
        return (
            round(path.objective_cost - terminal_value, 6),
            path.throughput_wh,
            path.action.value,
        )

    winner = min(paths, key=final_key)
    return final_key(winner), winner


def _plan_key(
    plan_slots: tuple,
    terminal_value_dkk: float,
    *,
    settings: PlannerSettings,
    current_action: Action,
) -> tuple[float, float, str]:
    previous_action = current_action
    objective_cost = 0.0
    throughput_wh = 0.0
    efficiency = sqrt(settings.round_trip_efficiency)
    for slot in plan_slots:
        if slot.action is Action.CHARGE:
            price = slot.price
            input_wh = slot.battery_charge_wh / efficiency
            movement_cost = input_wh / 1000 * slot.price_uncertainty_dkk_per_kwh
            movement_cost -= (
                _low_soc_area(
                    slot.soc_start * settings.capacity_wh / 100,
                    slot.soc_end * settings.capacity_wh / 100,
                    settings,
                ) / efficiency / 1000 * settings.low_soc_charge_premium_dkk_per_kwh
            )
        elif slot.action is Action.BATTERY:
            price = slot.price
            delivered_wh = slot.battery_discharge_wh * efficiency
            movement_cost = delivered_wh / 1000 * (
                settings.degradation_cost_dkk_per_kwh
                + settings.minimum_profit_dkk_per_kwh
                + slot.price_uncertainty_dkk_per_kwh
            )
        else:
            price = slot.price
            movement_cost = 0.0
        objective_cost += slot.grid_import_wh / 1000 * price + movement_cost
        if slot.action is not previous_action:
            objective_cost += settings.switching_penalty_dkk
        throughput_wh += slot.battery_charge_wh + slot.battery_discharge_wh
        previous_action = slot.action
        if slot.action is Action.CHARGE:
            hours = (slot.end - slot.start).total_seconds() / 3600
            available_stored_wh = max(
                0.0, settings.charge_power_w * hours - slot.expected_load_wh,
            ) * efficiency
            if available_stored_wh - slot.battery_charge_wh >= settings.energy_step_wh - 1e-9:
                objective_cost += settings.switching_penalty_dkk
                previous_action = Action.GRID
    return (
        round(objective_cost - terminal_value_dkk, 6),
        throughput_wh,
        previous_action.value,
    )


@pytest.mark.parametrize("reserve_soc,minutes", [(25, 60), (75, 15)])
def test_recovery_matches_exhaustive_paths_at_the_known_forecast_boundary(reserve_soc, minutes):
    configured = _settings(reserve_soc=reserve_soc)
    data = _price_slots(
        [4, 1, .01], minutes=[minutes, minutes, 60], loads_wh=[0, 0, 0],
        sources=["known", "known", "forecast"], uncertainties=[0, 0, .25],
    )
    expected_key, expected_path = _oracle(
        data, soc=0, settings=configured, current_action=Action.GRID,
    )
    plan = optimize(data, now=BASE, soc=0, settings=configured)

    assert _plan_key(
        plan.slots, plan.terminal_value_dkk,
        settings=configured, current_action=Action.GRID,
    ) == pytest.approx(expected_key)
    assert plan.slots[1].soc_end == expected_path.energy_steps[1] * 25 / 200 * 100
    if reserve_soc == 25:
        assert plan.slots[1].soc_end >= reserve_soc
        assert plan.slots[0].action is Action.GRID
    else:
        assert plan.slots[1].soc_end == 25  # Only 50 Wh can be restored before forecast.
        assert all(slot.action is Action.CHARGE for slot in plan.slots[:2])


@pytest.mark.parametrize(
    ("prices", "soc", "changes"),
    [
        ([3.0, 0.1, 2.9, 0.2, 4.0], 50.0, {}),
        ([1.00, 1.05, 0.98, 1.02, 1.01], 50.0, {}),
        ([-0.50, -0.10, 0.20, 3.00], 0.0, {}),
        ([5.0, 5.0, 0.1, 5.0], 0.0, {"reserve_soc": 25.0}),
        ([0.1, 4.0, 0.2, 3.5], 100.0, {"target_soc": 75.0}),
        ([1.00, 1.02, 1.01, 4.0, 4.0], 0.0, {"charge_power_w": 800.0}),
        ([1.00, 1.02, 1.01, 4.0, 4.0], 0.0, {
            "charge_power_w": 800.0, "switching_penalty_dkk": 0.0,
        }),
        ([1.0, 4.0, 0.0], 0.0, {
            "low_soc_charge_threshold": 60.0,
            "low_soc_charge_premium_dkk_per_kwh": 2.0,
        }),
    ],
)
def test_optimizer_matches_exhaustive_oracle_for_targeted_patterns(
    prices: list[float],
    soc: float,
    changes: dict[str, float],
) -> None:
    planner_settings = _settings(**changes)
    price_slots = _price_slots(prices)
    current_action = Action.GRID
    expected_key, expected_path = _oracle(
        price_slots,
        soc=soc,
        settings=planner_settings,
        current_action=current_action,
    )
    plan = optimize(
        price_slots,
        now=BASE,
        soc=soc,
        settings=planner_settings,
        current_action=current_action,
    )

    actual_key = _plan_key(
        plan.slots,
        plan.terminal_value_dkk,
        settings=planner_settings,
        current_action=current_action,
    )
    assert actual_key == pytest.approx(expected_key), (
        prices,
        [slot.action for slot in plan.slots],
        expected_path.actions,
    )


def test_optimizer_matches_oracle_for_variable_intervals_and_forecasts() -> None:
    planner_settings = _settings()
    price_slots = _price_slots(
        [1.5, 0.1, 3.5, 0.4],
        minutes=[15, 30, 60, 15],
        loads_wh=[20, 80, 100, 10],
        sources=["known", "known", "forecast", "forecast"],
        uncertainties=[0.0, 0.0, 0.4, 0.4],
    )
    expected_key, expected_path = _oracle(
        price_slots,
        soc=50,
        settings=planner_settings,
        current_action=Action.BATTERY,
    )
    plan = optimize(
        price_slots,
        now=BASE,
        soc=50,
        settings=planner_settings,
        current_action=Action.BATTERY,
    )

    actual_key = _plan_key(
        plan.slots,
        plan.terminal_value_dkk,
        settings=planner_settings,
        current_action=Action.BATTERY,
    )
    assert actual_key == pytest.approx(expected_key), (
        [slot.action for slot in plan.slots],
        expected_path.actions,
    )


@pytest.mark.parametrize("target_soc,charge_power_w,expected_charge_wh,stops", [
    (31.0, 803.6, 200.0, False),
    (30.0, 803.6, 195.0, True),
])
def test_nonintegral_native_ceiling_distinguishes_stops_from_budget_rounding(
    target_soc, charge_power_w, expected_charge_wh, stops,
) -> None:
    configured = _settings(
        capacity_wh=1958.0, reserve_soc=0.0, target_soc=target_soc,
        charge_power_w=charge_power_w, round_trip_efficiency=1.0,
        energy_step_wh=5.0,
    )
    data = _price_slots([-2.0], loads_wh=[0.0])
    plan = optimize(data, now=BASE, soc=20.0, settings=configured)
    expected_key, _ = _oracle(data, soc=20.0, settings=configured, current_action=Action.GRID)

    assert plan.slots[0].action is Action.CHARGE
    assert plan.slots[0].battery_charge_wh == expected_charge_wh
    # Initial observed energy rounds to 390 Wh. The 31% native ceiling is
    # 606.98 Wh, so 200.9 Wh of available input cannot reach it; the 30%
    # ceiling rounds to 585 Wh and genuinely leaves charging time unused.
    switches = 2 if stops else 1
    assert plan.expected_cost_dkk == pytest.approx(
        -2 * expected_charge_wh / 1000 + switches * configured.switching_penalty_dkk,
    )
    assert _plan_key(
        plan.slots, plan.terminal_value_dkk,
        settings=configured, current_action=Action.GRID,
    ) == pytest.approx(expected_key)


def test_repeated_quarter_replans_keep_the_charging_episode_continuous() -> None:
    configured = _settings(
        capacity_wh=2000.0, reserve_soc=10.0, target_soc=90.0,
        charge_power_w=1000.0, discharge_power_w=800.0,
        round_trip_efficiency=1.0, degradation_cost_dkk_per_kwh=.35,
        minimum_profit_dkk_per_kwh=.5, switching_penalty_dkk=.05,
        energy_step_wh=5.0,
    )
    data = _price_slots(
        [2.40, 2.41, 2.42, 2.41, 2.40, 2.41] + [5.0] * 24 + [2.40] * 12,
    )
    soc = 40.0
    physical_action = Action.GRID
    charging_wh = []
    for index in range(6):
        plan = optimize(
            data[index:], now=data[index].start, soc=soc,
            settings=configured, current_action=physical_action,
        )
        first = plan.slots[0]
        charging_wh.append(first.battery_charge_wh)
        soc = first.soc_end
        physical_action = first.action
        # Execute the interval independently of the planner's hidden state.
        # At 200 W connected load, 1000 W input gives 200 Wh per quarter.
        if first.action is Action.CHARGE and 200.0 - first.battery_charge_wh >= 5.0:
            physical_action = Action.GRID

    assert charging_wh == [0.0, 0.0, 0.0, 200.0, 200.0, 200.0]
    assert soc == 70.0


def test_optimizer_matches_exhaustive_oracle_for_seeded_random_cases() -> None:
    rng = random.Random(20260926)
    for case_index in range(500):
        horizon = rng.randint(1, 6)
        prices = [
            rng.choice([-0.5, 0.0, 0.2, 0.95, 1.0, 1.05, 2.0, 4.0])
            for _ in range(horizon)
        ]
        durations = [rng.choice([15, 30, 60]) for _ in range(horizon)]
        loads = [rng.choice([0.0, 25.0, 50.0, 75.0]) for _ in range(horizon)]
        forecast_start = rng.randint(0, horizon)
        sources = [
            "known" if index < forecast_start else "forecast"
            for index in range(horizon)
        ]
        uncertainties = [
            0.0 if source == "known" else rng.choice([0.1, 0.3])
            for source in sources
        ]
        price_slots = _price_slots(
            prices,
            minutes=durations,
            loads_wh=loads,
            sources=sources,
            uncertainties=uncertainties,
        )
        planner_settings = _settings(
            reserve_soc=rng.choice([0.0, 25.0]),
            target_soc=rng.choice([75.0, 100.0]),
            switching_penalty_dkk=rng.choice([0.0, 0.02, 0.1]),
            minimum_profit_dkk_per_kwh=rng.choice([0.0, 0.3, 0.8]),
            low_soc_charge_premium_dkk_per_kwh=rng.choice([0.0, 0.25, 1.0]),
        )
        if planner_settings.reserve_soc >= planner_settings.target_soc:
            continue
        soc = rng.choice(
            [0.0, planner_settings.reserve_soc, 50.0, planner_settings.target_soc, 100.0]
        )
        current_action = rng.choice(_ACTIONS)
        expected_key, expected_path = _oracle(
            price_slots,
            soc=soc,
            settings=planner_settings,
            current_action=current_action,
        )
        plan = optimize(
            price_slots,
            now=BASE,
            soc=soc,
            settings=planner_settings,
            current_action=current_action,
        )
        actual_key = _plan_key(
            plan.slots,
            plan.terminal_value_dkk,
            settings=planner_settings,
            current_action=current_action,
        )
        assert actual_key == pytest.approx(expected_key), (
            f"case={case_index}",
            prices,
            durations,
            loads,
            sources,
            uncertainties,
            planner_settings,
            soc,
            current_action,
            [slot.action for slot in plan.slots],
            expected_path.actions,
        )
