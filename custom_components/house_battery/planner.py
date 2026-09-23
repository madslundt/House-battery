"""Pure dynamic-programming battery optimizer."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from math import ceil, sqrt


from .models import Action, Plan, PlannedSlot, PlannerSettings, PriceSlot


@dataclass(frozen=True, slots=True)
class _State:
    energy_step: int
    action: Action
    locked_minutes: int
    planned_transitions: int


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
    """Value of stored energy at the end of the planning horizon.

    Uses the average of the *median* and *75th percentile* of ALL slot prices.
    The old approach used the last 48 slots (12 h) and took p95 + max / 2,
    which over-valued the terminal state when the tail happens to contain
    evening peaks (e.g. 2.97 DKK).  A moderate median-based estimate avoids
    encouraging premature discharge while still crediting stored energy.
    """
    prices = sorted(slot.discharge_price_dkk_per_kwh for slot in slots)
    n = len(prices)
    p50 = prices[n // 2]
    p75_idx = min(int(n * 0.75), n - 1)
    p75 = prices[p75_idx]
    return (p50 + p75) / 2


def _allowed_actions(
    state: _State,
    settings: PlannerSettings,
    *,
    active_transitions: int,
    force_grid_exit: bool = False,
) -> tuple[Action, ...]:
    """Return executable actions, letting physical discharge protection override locks.

    A direct battery's self-consumption mode can physically discharge whenever
    it remains selected above the native reserve.  The local mode must change
    to Idle/grid when the economic discharge floor is no longer met, even when
    that necessary protective exit exceeds the normal anti-chatter budget.
    """
    if force_grid_exit:
        return (Action.GRID,)
    if state.locked_minutes > 0:
        return (state.action,)
    if active_transitions >= settings.maximum_transitions:
        # This is a normal operational limit.  A required Grid exit from an
        # uneconomic battery mode is handled above so the inverter cannot keep
        # drawing stored energy just because the budget was exhausted.
        return (state.action,)
    return (Action.GRID, Action.BATTERY, Action.CHARGE)


def _slot_transition(
    state: _State,
    action: Action,
    slot: PriceSlot,
    settings: PlannerSettings,
    minimum_step: int,
    maximum_step: int,
    discharge_price_floor: float,
    active_transitions: int,
) -> tuple[_State, tuple[float, float, float, float, float, str], float] | None:
    energy_wh = state.energy_step * settings.energy_step_wh
    load_wh = max(0.0, slot.expected_load_wh)
    charge_efficiency = sqrt(settings.round_trip_efficiency)
    discharge_efficiency = charge_efficiency
    changed = action != state.action
    is_locked_continuation = not changed and state.locked_minutes > 0
    is_transition_limited_continuation = (
        not changed and active_transitions >= settings.maximum_transitions
    )
    permits_energy_neutral_continuation = (
        is_locked_continuation or is_transition_limited_continuation
    )
    elapsed_minutes = max(1, round(slot.hours * 60))
    locked = (
        max(0, settings.minimum_mode_minutes - elapsed_minutes)
        if changed
        else max(0, state.locked_minutes - elapsed_minutes)
    )
    switch_cost = settings.switching_penalty_dkk if changed else 0.0

    grid_wh = load_wh
    charged_wh = 0.0
    discharged_wh = 0.0
    reason = "Grid supplies forecast load while energy is held for a better interval"

    if action is Action.CHARGE:
        headroom_wh = max(0.0, maximum_step * settings.energy_step_wh - energy_wh)
        input_wh = min(
            settings.charge_power_w * slot.hours, headroom_wh / charge_efficiency
        )
        if input_wh <= settings.energy_step_wh / 4:
            if not permits_energy_neutral_continuation:
                return None
            reason = (
                "Energy-neutral continuation at the charge target while mode "
                "changes are constrained"
            )
        else:
            # Never charge on a forecast price that is above the discharge
            # floor.  Forecasts are uncertain hints — charging on a forecast
            # that turns out wrong (price is higher than expected) wastes
            # round-trip efficiency.  Only charge on forecasts when the
            # conservative charge price is clearly below the floor.
            is_forecast = slot.source == "forecast"
            if is_forecast and slot.charge_price_dkk_per_kwh >= discharge_price_floor:
                return None
            charged_wh = input_wh * charge_efficiency
            grid_wh += input_wh
            if is_forecast:
                reason = "Forecast dip below discharge floor justifies charging after losses"
            else:
                reason = "Known low price justifies charging after losses, wear and profit threshold"
    elif action is Action.BATTERY:
        # For forecast slots, raise the floor further: the forecast must be
        # clearly above the discharge floor to justify burning stored energy.
        # We require price >= floor + uncertainty so that a forecast error
        # (actual price lower than forecast) does not turn a profitable
        # discharge into a loss.
        if slot.source == "forecast":
            effective_floor = discharge_price_floor + slot.uncertainty_dkk_per_kwh
        else:
            effective_floor = discharge_price_floor
        below_price_floor = (
            slot.discharge_price_dkk_per_kwh + 1e-9 < effective_floor
        )
        if below_price_floor and energy_wh > minimum_step * settings.energy_step_wh:
            return None
        available_wh = max(0.0, energy_wh - minimum_step * settings.energy_step_wh)
        deliverable_wh = (
            0.0
            if below_price_floor
            else min(
                load_wh,
                settings.discharge_power_w * slot.hours,
                available_wh * discharge_efficiency,
            )
        )
        if deliverable_wh <= settings.energy_step_wh / 4:
            if not permits_energy_neutral_continuation:
                return None
            reason = (
                "Energy-neutral continuation at the reserve while mode changes "
                "are constrained"
            )
        else:
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
    next_state = _State(
        new_step,
        action,
        locked,
        state.planned_transitions + int(changed),
    )
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
    mode_lock_remaining_minutes: int = 0,
    transitions_used: int = 0,
    transition_times: Iterable[datetime] | None = None,
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
    maximum_step = int(settings.capacity_wh * settings.target_soc / 100 // step_wh)
    # The observed SOC is the source of truth for the starting energy.  The
    # initial energy is therefore taken directly from telemetry and is NOT
    # clamped to the charge *target* ceiling.  A battery physically at 100% SOC
    # must plan from 100%, even when target_soc < 100 (previously the
    # ``min(maximum_step, ...)`` clamp silently re-based every replan onto the
    # projected target SOC, so a 100% battery was planned as if it held ~90%).
    # A hard non-negative floor keeps a drained battery planning sensibly; the
    # dynamic program still caps any *future* charge transitions at target.
    initial_step = round(settings.capacity_wh * soc / 100 / step_wh)
    initial_step = max(0, initial_step)
    initial_lock = max(0, mode_lock_remaining_minutes)
    # Keep the legacy count argument for callers that cannot provide timestamps.
    # A timestamp-aware caller releases *executed* transitions at their true
    # rolling-24-hour expiry.  Planned transitions remain counted for this
    # provisional plan; the coordinator replans before executing later slots,
    # which both bounds the dynamic-programming state space and never exceeds
    # the configured transition limit.
    if transition_times is None:
        historical_expiries = tuple(
            now + timedelta(hours=24) for _ in range(transitions_used)
        )
    else:
        historical_expiries = tuple(
            timestamp + timedelta(hours=24)
            for timestamp in transition_times
            if timestamp + timedelta(hours=24) > now
        )
    initial = _State(initial_step, current_action, initial_lock, 0)
    layers: list[dict[_State, _Node]] = [{initial: _Node(0.0, 0.0, None, None)}]
    # Use the cheapest *available* charge price in the known-price window as
    # the floor basis.  A fixed 72-hour look-ahead window keeps the floor
    # stable across replans and prevents the floor from being driven by the
    # current slot (which shifts with every update).  Using a distant cheap
    # price (>72 h) is avoided because it misprices today's decision.
    horizon_start = valid[0].start
    known_charge_candidates = [
        s for s in valid
        if s.source == "known"
        and (s.start - horizon_start).total_seconds() <= 72 * 3600
    ]
    if not known_charge_candidates:
        known_charge_candidates = valid[:1]  # fallback
    cheapest_charge_price = min(s.charge_price_dkk_per_kwh for s in known_charge_candidates)
    discharge_price_floor = (
        cheapest_charge_price / settings.round_trip_efficiency
        + settings.degradation_cost_dkk_per_kwh
        + settings.minimum_profit_dkk_per_kwh
    )

    for slot in valid:
        previous_layer = layers[-1]
        layer: dict[_State, _Node] = {}
        for state in sorted(
            previous_layer,
            key=lambda item: (
                item.energy_step,
                item.action.value,
                item.planned_transitions,
            ),
        ):
            node = previous_layer[state]
            active_transitions = sum(
                expiry > slot.start for expiry in historical_expiries
            ) + state.planned_transitions
            force_grid_exit = (
                state.action is Action.BATTERY
                and state.energy_step > minimum_step
                and slot.discharge_price_dkk_per_kwh + 1e-9 < discharge_price_floor
            )
            for action in _allowed_actions(
                state,
                settings,
                active_transitions=active_transitions,
                force_grid_exit=force_grid_exit,
            ):
                result = _slot_transition(
                    state,
                    action,
                    slot,
                    settings,
                    minimum_step,
                    maximum_step,
                    discharge_price_floor,
                    active_transitions,
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
                    next_state.planned_transitions,
                    candidate.throughput_wh,
                    action.value,
                )
                if existing is None:
                    layer[next_state] = candidate
                else:
                    existing_key = (
                        existing.cost,
                        next_state.planned_transitions,
                        existing.throughput_wh,
                        next_state.action.value,
                    )
                    if candidate_key < existing_key:
                        layer[next_state] = candidate
        if not layer:
            return Plan(
                now, (), 0, 0, 0, 0, 0, "No plan satisfies battery and mode constraints"
            )
        layers.append(layer)

    terminal_price = _terminal_price(valid)
    discharge_efficiency = sqrt(settings.round_trip_efficiency)
    terminal_net_price = max(
        0.0,
        terminal_price
        - settings.degradation_cost_dkk_per_kwh
        - settings.minimum_profit_dkk_per_kwh,
    )

    def final_key(item: tuple[_State, _Node]) -> tuple[float, int, float, str]:
        state, node = item
        stored_above_reserve_wh = (state.energy_step - minimum_step) * step_wh
        terminal_value = (
            stored_above_reserve_wh * discharge_efficiency / 1000 * terminal_net_price
        )
        return (
            node.cost - terminal_value,
            state.planned_transitions,
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
        baseline = (
            max(0.0, slot.expected_load_wh) / 1000 * slot.price
        )
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
        (final_state.energy_step - minimum_step) * step_wh * discharge_efficiency / 1000 * terminal_net_price
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
    )
