"""Differential tests against an exhaustive small-horizon planner oracle."""

from __future__ import annotations

import random
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import ceil, sqrt
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components"))

from house_battery.models import Action, PlannerSettings, PriceSlot
from house_battery.planner import optimize

BASE = datetime(2026, 9, 26, tzinfo=timezone.utc)
_ACTIONS = (Action.GRID, Action.BATTERY, Action.CHARGE)


@dataclass(frozen=True, slots=True)
class _OraclePath:
    energy_step: int
    action: Action
    objective_cost: float
    throughput_wh: float
    actions: tuple[Action, ...]


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

    if action is Action.CHARGE:
        load_power_w = load_wh / slot.hours if slot.hours > 0 else 0.0
        available_input_w = max(0.0, settings.charge_power_w - load_power_w)
        headroom_wh = max(0.0, maximum_step * step_wh - energy_wh)
        input_wh = min(available_input_w * slot.hours, headroom_wh / efficiency)
        charged_wh = input_wh * efficiency if input_wh > step_wh / 4 else 0.0
        new_energy_wh = energy_wh + charged_wh
    elif action is Action.BATTERY:
        available_wh = max(0.0, energy_wh - minimum_step * step_wh)
        delivered_wh = min(
            load_wh,
            settings.discharge_power_w * slot.hours,
            available_wh * efficiency,
        )
        discharged_wh = delivered_wh / efficiency if delivered_wh > step_wh / 4 else 0.0
        new_energy_wh = energy_wh - discharged_wh
    else:
        new_energy_wh = energy_wh

    new_step = round(new_energy_wh / step_wh)
    state_ceiling = (
        maximum_step
        if action is Action.CHARGE
        else max(maximum_step, path.energy_step)
    )
    new_step = min(state_ceiling, max(minimum_step, new_step))
    delta_wh = (new_step - path.energy_step) * step_wh
    if action is Action.CHARGE:
        if delta_wh <= 0:
            return None
        throughput_wh = delta_wh
        grid_wh = load_wh + delta_wh / efficiency
        effective_price = slot.charge_price_dkk_per_kwh
        movement_cost = 0.0
    elif action is Action.BATTERY:
        if delta_wh >= 0:
            return None
        throughput_wh = -delta_wh
        delivered_wh = throughput_wh * efficiency
        grid_wh = max(0.0, load_wh - delivered_wh)
        effective_price = slot.discharge_price_dkk_per_kwh
        movement_cost = delivered_wh / 1000 * (
            settings.degradation_cost_dkk_per_kwh
            + settings.minimum_profit_dkk_per_kwh
        )
    else:
        throughput_wh = 0.0
        effective_price = slot.price
        movement_cost = 0.0

    interval_cost = grid_wh / 1000 * effective_price + movement_cost
    if changed:
        interval_cost += settings.switching_penalty_dkk
    return _OraclePath(
        energy_step=new_step,
        action=action,
        objective_cost=path.objective_cost + interval_cost,
        throughput_wh=path.throughput_wh + throughput_wh,
        actions=path.actions + (action,),
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
    for slot in slots:
        paths = [
            candidate
            for path in paths
            for action in _ACTIONS
            if (
                candidate := _oracle_transition(
                    path,
                    action,
                    slot,
                    settings,
                    minimum_step,
                    maximum_step,
                )
            )
            is not None
        ]

    discharge_efficiency = sqrt(settings.round_trip_efficiency)
    terminal_price = min(slot.discharge_price_dkk_per_kwh for slot in slots)
    terminal_net_price = max(
        0.0,
        terminal_price - settings.degradation_cost_dkk_per_kwh,
    )

    def final_key(path: _OraclePath) -> tuple[float, float, str]:
        stored_wh = (path.energy_step - minimum_step) * step_wh
        terminal_value = stored_wh * discharge_efficiency / 1000 * terminal_net_price
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
    for slot in plan_slots:
        if slot.action is Action.CHARGE:
            price = slot.price + slot.price_uncertainty_dkk_per_kwh
            movement_cost = 0.0
        elif slot.action is Action.BATTERY:
            price = slot.price - slot.price_uncertainty_dkk_per_kwh
            delivered_wh = slot.battery_discharge_wh * sqrt(
                settings.round_trip_efficiency
            )
            movement_cost = delivered_wh / 1000 * (
                settings.degradation_cost_dkk_per_kwh
                + settings.minimum_profit_dkk_per_kwh
            )
        else:
            price = slot.price
            movement_cost = 0.0
        objective_cost += slot.grid_import_wh / 1000 * price + movement_cost
        if slot.action is not previous_action:
            objective_cost += settings.switching_penalty_dkk
        throughput_wh += slot.battery_charge_wh + slot.battery_discharge_wh
        previous_action = slot.action
    return (
        round(objective_cost - terminal_value_dkk, 6),
        throughput_wh,
        plan_slots[-1].action.value,
    )


@pytest.mark.parametrize(
    ("prices", "soc", "changes"),
    [
        ([3.0, 0.1, 2.9, 0.2, 4.0], 50.0, {}),
        ([1.00, 1.05, 0.98, 1.02, 1.01], 50.0, {}),
        ([-0.50, -0.10, 0.20, 3.00], 0.0, {}),
        ([5.0, 5.0, 0.1, 5.0], 0.0, {"reserve_soc": 25.0}),
        ([0.1, 4.0, 0.2, 3.5], 100.0, {"target_soc": 75.0}),
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
        )
        if planner_settings.reserve_soc >= planner_settings.target_soc:
            continue
        soc = rng.choice(
            [planner_settings.reserve_soc, 50.0, planner_settings.target_soc, 100.0]
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
