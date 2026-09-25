"""Pure dynamic-programming battery optimizer."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from math import ceil, sqrt

from .models import Action, Plan, PlannedSlot, PlannerSettings, PriceSlot


@dataclass(frozen=True, slots=True)
class _State:
    energy_step: int
    action: Action


@dataclass(frozen=True, slots=True)
class _Node:
    cost: float
    throughput_wh: float
    previous: _State | None
    detail: tuple[float, float, float, float, float, str] | None


def _future_slots(slots: Iterable[PriceSlot], now: datetime) -> list[PriceSlot]:
    ordered = sorted(
        (slot for slot in slots if slot.end > now), key=lambda item: item.start
    )
    if not ordered:
        return []
    valid: list[PriceSlot] = []
    expected_start = ordered[0].start
    for slot in ordered:
        if slot.end <= slot.start or slot.price != slot.price:
            continue
        if slot.price < -20 or slot.price > 100:
            continue
        candidate = slot
        if slot.start < now < slot.end:
            remaining_fraction = (slot.end - now) / (slot.end - slot.start)
            candidate = PriceSlot(
                now,
                slot.end,
                slot.price,
                slot.expected_load_wh * remaining_fraction,
                slot.source,
                slot.uncertainty_dkk_per_kwh,
            )
        if valid and slot.start != expected_start:
            break
        valid.append(candidate)
        expected_start = slot.end
    return valid


def _terminal_price(slots: list[PriceSlot]) -> float:
    """Conservative per-kWh value of stored energy at the end of the horizon.

    Stored energy at the horizon edge is valued at the *avoided-purchase
    floor*: the cheapest known price over the horizon. This has to be
    conservative. The terminal value credits energy that cannot be spent once
    the horizon closes, so crediting it at the average market price (p50 + p75) / 2
    over-values the last unit and makes the DP *bank* energy at the horizon edge:
    it charges into the afternoon dip and then holds the battery full through the
    most expensive evening peak instead of discharging (realising a net loss).
    A floor well below the peak-discharge benefit keeps holding energy from
    dominating real arbitrage, so the optimizer still drains into spikes while
    never banking through them.
    """
    if not slots:
        return 0.0
    return min(slot.discharge_price_dkk_per_kwh for slot in slots)


def _allowed_actions() -> tuple[Action, ...]:
    """All physical actions are candidates at every price interval."""
    return (Action.GRID, Action.BATTERY, Action.CHARGE)


def _slot_transition(
    state: _State,
    action: Action,
    slot: PriceSlot,
    settings: PlannerSettings,
    minimum_step: int,
    maximum_step: int,
) -> tuple[_State, tuple[float, float, float, float, float, str], float] | None:
    energy_wh = state.energy_step * settings.energy_step_wh
    load_wh = max(0.0, slot.expected_load_wh)
    charge_efficiency = sqrt(settings.round_trip_efficiency)
    discharge_efficiency = charge_efficiency
    changed = action != state.action
    switch_cost = settings.switching_penalty_dkk if changed else 0.0

    grid_wh = load_wh
    charged_wh = 0.0
    discharged_wh = 0.0
    reason = "Grid supplies forecast load while energy is held for a better interval"

    if action is Action.CHARGE:
        headroom_wh = max(0.0, maximum_step * settings.energy_step_wh - energy_wh)
        load_power_w = load_wh / slot.hours if slot.hours > 0 else 0.0
        available_charge_input_w = max(0.0, settings.charge_power_w - load_power_w)
        input_wh = min(
            available_charge_input_w * slot.hours,
            headroom_wh / charge_efficiency,
        )
        if input_wh > settings.energy_step_wh / 4:
            # Never charge on a forecast price that is above the discharge
            # floor.  Forecasts are uncertain hints — charging on a forecast
            # that turns out wrong (price is higher than expected) wastes
            # round-trip efficiency.  Only charge on forecasts when the
            # conservative charge price is clearly below the floor.
            is_forecast = slot.source == "forecast"
            charged_wh = input_wh * charge_efficiency
            grid_wh += input_wh
            if is_forecast:
                reason = (
                    "Forecast dip below discharge floor justifies charging after losses"
                )
            else:
                reason = "Known low price justifies charging after losses, wear and profit threshold"
    elif action is Action.BATTERY:
        # Zero-export/physical correctness are enforced by the execution layer,
        # not by the planner. Here the planner only *values* stored energy; it
        # does not forbid it. We deliberately no longer treat the cheapest future
        # charge price as the universal acquisition cost of energy already inside
        # the battery: that hard gate made stored energy unusable whenever no
        # cheap recharge lingered in the horizon and duplicated the objective.
        # Degradation cost, the required profit margin, the future replacement
        # opportunity (terminal price) and the reserve floor all live in the
        # objective and bounds, so the dynamic program discharges only when doing
        # so is genuinely better than holding the energy for a later interval or
        # for its terminal value. Discharge is still capped by the load, which is
        # what keeps the physical model consistent with zero export.
        available_wh = max(0.0, energy_wh - minimum_step * settings.energy_step_wh)
        deliverable_wh = min(
            load_wh,
            settings.discharge_power_w * slot.hours,
            available_wh * discharge_efficiency,
        )
        if deliverable_wh > settings.energy_step_wh / 4:
            discharged_wh = deliverable_wh / discharge_efficiency
            grid_wh -= deliverable_wh
            reason = "Battery avoids expensive grid import and clears the configured economic margin"

    new_energy_wh = energy_wh + charged_wh - discharged_wh
    new_step = round(new_energy_wh / settings.energy_step_wh)
    new_step = min(maximum_step, max(minimum_step, new_step))
    actual_delta_wh = (new_step - state.energy_step) * settings.energy_step_wh
    if action is Action.CHARGE:
        charged_wh = max(0.0, actual_delta_wh)
        input_wh = charged_wh / charge_efficiency
        grid_wh = load_wh + input_wh
    elif action is Action.BATTERY:
        discharged_wh = max(0.0, -actual_delta_wh)
        delivered_wh = discharged_wh * discharge_efficiency
        grid_wh = max(0.0, load_wh - delivered_wh)

    interval_cost = grid_wh / 1000 * slot.price
    optimization_price = (
        slot.charge_price_dkk_per_kwh
        if action is Action.CHARGE
        else slot.discharge_price_dkk_per_kwh
        if action is Action.BATTERY
        else slot.price
    )
    optimization_cost = grid_wh / 1000 * optimization_price
    if action is Action.BATTERY:
        delivered_kwh = discharged_wh * discharge_efficiency / 1000
        interval_cost += delivered_kwh * settings.degradation_cost_dkk_per_kwh
        optimization_cost += delivered_kwh * (
            settings.degradation_cost_dkk_per_kwh + settings.minimum_profit_dkk_per_kwh
        )
    interval_cost += switch_cost
    optimization_cost += switch_cost
    next_state = _State(new_step, action)
    detail = (
        grid_wh,
        charged_wh,
        discharged_wh,
        interval_cost,
        optimization_cost,
        reason,
    )
    return next_state, detail, discharged_wh + charged_wh


def optimize(
    slots: Iterable[PriceSlot],
    *,
    now: datetime,
    soc: float,
    settings: PlannerSettings,
    current_action: Action = Action.GRID,
) -> Plan:
    """Return the least-cost executable plan across every known price slot."""
    settings.validate()
    valid = _future_slots(slots, now)
    if not valid:
        return Plan(
            now, (), 0, 0, 0, 0, 0, "No complete contiguous future price intervals"
        )
    if not 0 <= soc <= 100:
        return Plan(now, (), 0, 0, 0, 0, 0, "Battery SOC is invalid")

    step_wh = settings.energy_step_wh
    minimum_step = ceil(settings.capacity_wh * settings.reserve_soc / 100 / step_wh)
    # The observed SOC is the source of truth for the starting energy.  Take it
    # directly from telemetry and never clamp it down to the charge *target*
    # ceiling.  A battery physically above target (e.g. 100% with target 90%)
    # must be able to plan and discharge that extra energy, so the planning SOC
    # ceiling is the higher of target_soc and the observed SOC.  A non-negative
    # floor keeps a drained battery planning sensibly; the dynamic program still
    # caps any *future* charge transitions at this ceiling.
    #
    # If the ceiling stayed pinned to target_soc while the initial energy was
    # anchored at the higher observed SOC, the DP clamp on every transition
    # would drag the first block down to target with zero battery throughput --
    # a phantom discharge that disagrees with the inverter's real power.
    initial_step = round(settings.capacity_wh * soc / 100 / step_wh)
    initial_step = max(0, initial_step)
    maximum_step = max(
        int(settings.capacity_wh * settings.target_soc / 100 // step_wh),
        initial_step,
    )
    initial = _State(initial_step, current_action)
    layers: list[dict[_State, _Node]] = [{initial: _Node(0.0, 0.0, None, None)}]
    for slot in valid:
        previous_layer = layers[-1]
        layer: dict[_State, _Node] = {}
        for state in sorted(
            previous_layer,
            key=lambda item: (
                item.energy_step,
                item.action.value,
            ),
        ):
            node = previous_layer[state]
            for action in _allowed_actions():
                result = _slot_transition(
                    state,
                    action,
                    slot,
                    settings,
                    minimum_step,
                    maximum_step,
                )
                if result is None:
                    continue
                next_state, detail, throughput = result
                candidate = _Node(
                    node.cost + detail[4],
                    node.throughput_wh + throughput,
                    state,
                    detail,
                )
                existing = layer.get(next_state)
                candidate_key = (
                    candidate.cost,
                    candidate.throughput_wh,
                    action.value,
                )
                if existing is None:
                    layer[next_state] = candidate
                else:
                    existing_key = (
                        existing.cost,
                        existing.throughput_wh,
                        next_state.action.value,
                    )
                    if candidate_key < existing_key:
                        layer[next_state] = candidate
        if not layer:
            return Plan(now, (), 0, 0, 0, 0, 0, "No plan satisfies battery constraints")
        layers.append(layer)

    terminal_price = _terminal_price(valid)
    discharge_efficiency = sqrt(settings.round_trip_efficiency)
    # Value leftover energy at its best future discharge opportunity: the
    # replacement price less the real degradation wear. The required profit
    # margin is a *hurdle* for the present discharge decision (it lives in the
    # discharge optimization cost above); it must not also discount the stored
    # energy the program is deciding whether to keep. Double-counting the margin
    # there made holding vs discharging a mathematical wash, which the
    # throughput tiebreak then turned into wasteful discharge at target SOC.
    # Keeping the margin only in the discharge cost makes "hold at target"
    # strictly preferred whenever there is no clear price advantage.
    terminal_net_price = max(
        0.0,
        terminal_price
        - settings.degradation_cost_dkk_per_kwh,
    )

    def final_key(item: tuple[_State, _Node]) -> tuple[float, float, str]:
        state, node = item
        stored_above_reserve_wh = (state.energy_step - minimum_step) * step_wh
        terminal_value = (
            stored_above_reserve_wh * discharge_efficiency / 1000 * terminal_net_price
        )
        # Absorb floating-point noise from the two accumulation paths (grid-only
        # vs. battery movement) so that a genuine economic *wash* ties on the
        # cost term. The existing tiebreak is "fewer transitions, then less
        # battery throughput", which deliberately prefers HOLDING when moving the
        # battery buys nothing. Without this rounding the wash decided itself on a
        # ~1e-8 difference, letting the greedy per-layer optimiser slip a
        # wasteful discharge (e.g. at target SOC with flat prices) through the
        # back door after the hard discharge floor was removed.
        cost_term = round(node.cost - terminal_value, 6)
        return (
            cost_term,
            node.throughput_wh,
            state.action.value,
        )

    final_state, _final_node = min(layers[-1].items(), key=final_key)
    path: list[tuple[_State, _Node]] = []
    state = final_state
    for index in range(len(valid), 0, -1):
        node = layers[index][state]
        path.append((state, node))
        if node.previous is None:
            break
        state = node.previous
    path.reverse()

    planned: list[PlannedSlot] = []
    previous_energy = initial.energy_step * step_wh
    for slot, (next_state, node) in zip(valid, path, strict=True):
        assert node.detail is not None
        grid_wh, charged_wh, discharged_wh, cost, _optimization_cost, reason = (
            node.detail
        )
        next_energy = next_state.energy_step * step_wh
        baseline = max(0.0, slot.expected_load_wh) / 1000 * slot.price
        planned.append(
            PlannedSlot(
                start=slot.start,
                end=slot.end,
                action=next_state.action,
                price=slot.price,
                expected_load_wh=slot.expected_load_wh,
                grid_import_wh=grid_wh,
                battery_charge_wh=charged_wh,
                battery_discharge_wh=discharged_wh,
                soc_start=100 * previous_energy / settings.capacity_wh,
                soc_end=100 * next_energy / settings.capacity_wh,
                interval_cost_dkk=cost,
                baseline_cost_dkk=baseline,
                reason=reason,
                price_source=slot.source,
                price_uncertainty_dkk_per_kwh=slot.uncertainty_dkk_per_kwh,
            )
        )
        previous_energy = next_energy

    expected_cost = sum(item.interval_cost_dkk for item in planned)
    baseline_cost = sum(item.baseline_cost_dkk for item in planned)
    # Include the terminal value in reported savings so that the plan's
    # economic benefit is not understated (the optimizer already credits
    # the terminal value internally when selecting the final state).
    terminal_value = (
        (final_state.energy_step - minimum_step)
        * step_wh
        * discharge_efficiency
        / 1000
        * terminal_net_price
    )
    savings = baseline_cost - expected_cost + terminal_value
    throughput = (
        sum(item.battery_charge_wh + item.battery_discharge_wh for item in planned)
        / 1000
    )
    action = planned[0].action
    reason = planned[0].reason if planned else "No action"
    return Plan(
        now,
        tuple(planned),
        expected_cost,
        baseline_cost,
        savings,
        throughput,
        terminal_price,
        f"{action.value}: {reason}",
        terminal_value,
    )
