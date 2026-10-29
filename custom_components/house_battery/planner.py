"""Pure dynamic-programming battery optimizer."""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from math import ceil, floor, sqrt

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
    charge_target_soc: float | None = None


_TERMINAL_PRICE_LOOKBACK = timedelta(hours=3)
_TERMINAL_PRICE_HAIRCUT = 0.75


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
                slot.tariff_start,
            )
        if valid and slot.start != expected_start:
            break
        valid.append(candidate)
        expected_start = slot.end
    return valid


def _terminal_price(slots: list[PriceSlot]) -> float:
    """Value carryover energy at a conservative price near the horizon edge.

    The cheapest price anywhere in the plan can come from a midday dip many
    hours before the horizon ends. Use the lowest uncertainty-adjusted price in
    the final three hours as a continuation anchor. When the horizon contains a
    meaningful price swing, apply a 25% haircut because prices beyond it are
    unknown. On a flat-price horizon, keep the unadjusted anchor so uncertainty
    alone does not create a fictitious arbitrage opportunity.
    """
    if not slots:
        return 0.0
    end = max(slot.end for slot in slots)
    tail_start = end - _TERMINAL_PRICE_LOOKBACK
    tail = [slot for slot in slots if slot.end > tail_start]
    tail_price = min(slot.discharge_price_dkk_per_kwh for slot in tail or slots)
    adjusted_prices = [slot.discharge_price_dkk_per_kwh for slot in slots]
    haircut = (
        _TERMINAL_PRICE_HAIRCUT
        if max(adjusted_prices) - min(adjusted_prices) > 0.05
        else 1.0
    )
    return max(0.0, tail_price * haircut)


def _allowed_actions() -> tuple[Action, ...]:
    """All physical actions are candidates at every price interval."""
    return (Action.GRID, Action.BATTERY, Action.CHARGE)


def _low_soc_weight(
    start_wh: float, end_wh: float, capacity_wh: float, threshold_soc: float
) -> float:
    """Average charge premium weight over the SOC interval being filled."""
    if end_wh <= start_wh or capacity_wh <= 0 or threshold_soc <= 0:
        return 0.0
    start_soc = 100 * start_wh / capacity_wh
    end_soc = 100 * end_wh / capacity_wh
    lower = max(0.0, start_soc)
    upper = min(threshold_soc, end_soc)
    if upper <= lower:
        return 0.0
    # Integrate (threshold - SOC) / threshold over the affected interval.
    area = (
        threshold_soc * (upper - lower) - (upper**2 - lower**2) / 2
    ) / threshold_soc
    return min(1.0, max(0.0, area / (end_soc - start_soc)))


def _high_soc_weight(
    start_wh: float, end_wh: float, capacity_wh: float, threshold_soc: float
) -> float:
    """Average discharge discount weight over the SOC interval being emptied."""
    if start_wh <= end_wh or capacity_wh <= 0 or threshold_soc >= 100:
        return 0.0
    start_soc = 100 * start_wh / capacity_wh
    end_soc = 100 * end_wh / capacity_wh
    lower = max(threshold_soc, end_soc)
    upper = min(100.0, start_soc)
    if upper <= lower:
        return 0.0
    # Integrate (SOC - threshold) / (100 - threshold) over the affected interval.
    denominator = 100 - threshold_soc
    area = ((upper - threshold_soc) ** 2 - (lower - threshold_soc) ** 2) / (
        2 * denominator
    )
    return min(1.0, max(0.0, area / (start_soc - end_soc)))


def _slot_transition(
    state: _State,
    action: Action,
    slot: PriceSlot,
    settings: PlannerSettings,
    minimum_step: int,
    maximum_step: int,
    *,
    allow_substep_continuation: bool = False,
) -> tuple[_State, tuple[float, float, float, float, float, str], float] | None:
    if action is Action.BATTERY and not settings.discharge_enabled:
        return None
    energy_wh = state.energy_step * settings.energy_step_wh
    load_wh = max(0.0, slot.expected_load_wh)
    charge_efficiency = sqrt(settings.round_trip_efficiency)
    discharge_efficiency = charge_efficiency
    changed = action != state.action
    switch_cost = settings.switching_penalty_dkk if changed else 0.0

    grid_wh = load_wh
    charged_wh = 0.0
    discharged_wh = 0.0
    can_move = False
    reason = "Grid supplies forecast load while energy is held for a better interval"
    if not settings.discharge_enabled:
        reason = "Grid supplies forecast load; battery discharge is disabled"

    if action is Action.CHARGE:
        headroom_wh = max(0.0, maximum_step * settings.energy_step_wh - energy_wh)
        load_power_w = load_wh / slot.hours if slot.hours > 0 else 0.0
        available_charge_input_w = max(0.0, settings.charge_power_w - load_power_w)
        input_wh = min(
            available_charge_input_w * slot.hours,
            headroom_wh / charge_efficiency,
        )
        can_move = input_wh > 0
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
        can_move = deliverable_wh > 0
        if deliverable_wh > settings.energy_step_wh / 4:
            discharged_wh = deliverable_wh / discharge_efficiency
            grid_wh -= deliverable_wh
            reason = "Battery avoids expensive grid import and clears the configured economic margin"

    new_energy_wh = energy_wh + charged_wh - discharged_wh
    # Round movement toward the starting energy. Rounding to the nearest step
    # can buy more input than the charge limit permits, or deliver more output
    # than the load/power cap allows. The discretized model may leave less than
    # one step unused, but must never invent physically unavailable movement.
    if action is Action.CHARGE:
        new_step = floor(new_energy_wh / settings.energy_step_wh + 1e-9)
    elif action is Action.BATTERY:
        new_step = ceil(new_energy_wh / settings.energy_step_wh - 1e-9)
    else:
        new_step = state.energy_step
    # An observed battery may legitimately start above the configured charge
    # target. Preserve that real energy while holding, but do not turn the
    # observed SOC into a higher charge ceiling for the rest of the horizon.
    # Once discharged, CHARGE may restore energy only up to ``maximum_step``.
    state_ceiling = (
        maximum_step
        if action is Action.CHARGE
        else max(maximum_step, state.energy_step)
    )
    # Reserve limits discharge; it does not establish the actual stored energy.
    # A depleted battery must remain below reserve until feasible charging
    # restores it, including across multiple tariff intervals.
    state_floor = min(minimum_step, state.energy_step)
    new_step = min(state_ceiling, max(state_floor, new_step))
    actual_delta_wh = (new_step - state.energy_step) * settings.energy_step_wh
    # An operating-mode label must correspond to real battery movement. At the
    # reserve/target boundary the requested mode can otherwise do nothing;
    # retaining BATTERY there merely avoids a switch penalty while publishing a
    # misleading action for hours. Let GRID represent those idle intervals.
    # The current tariff tail is different: a few seconds of real movement
    # may round to zero without reaching a physical limit. Preserve its mode
    # without inventing energy or a switch that execution will never make.
    substep_continuation = (
        allow_substep_continuation and can_move and actual_delta_wh == 0
    )
    if action is Action.BATTERY and actual_delta_wh >= 0 and not substep_continuation:
        return None
    if action is Action.CHARGE and actual_delta_wh <= 0 and not substep_continuation:
        return None
    if substep_continuation:
        reason = (
            f"Continue {action.value} mode through the price boundary; "
            "remaining energy is below planning resolution"
        )
    if action is Action.CHARGE:
        charged_wh = max(0.0, actual_delta_wh)
        input_wh = charged_wh / charge_efficiency
        grid_wh = load_wh + input_wh
        low_soc_charge_weight = _low_soc_weight(
            energy_wh,
            energy_wh + charged_wh,
            settings.capacity_wh,
            settings.low_soc_charge_threshold,
        )
        if low_soc_charge_weight > 0:
            reason += (
                "; low-SOC charging premium weighted "
                f"{low_soc_charge_weight:.0%}"
            )
    elif action is Action.BATTERY:
        discharged_wh = max(0.0, -actual_delta_wh)
        delivered_wh = discharged_wh * discharge_efficiency
        grid_wh = max(0.0, load_wh - delivered_wh)
        high_soc_discharge_weight = _high_soc_weight(
            energy_wh,
            energy_wh - discharged_wh,
            settings.capacity_wh,
            settings.high_soc_discharge_threshold,
        )
        if (
            high_soc_discharge_weight > 0
            and settings.high_soc_discharge_discount_dkk_per_kwh > 0
        ):
            reason += (
                "; high-SOC discharge discount weighted "
                f"{high_soc_discharge_weight:.0%}"
            )

    interval_cost = grid_wh / 1000 * slot.price
    # Forecast uncertainty is a hurdle on battery movement, not a discount on
    # any residual grid import. Applying p-u to the residual import for a
    # BATTERY action made disagreement perversely reduce its cost. Keep all
    # imported energy at the common forecast price, then charge uncertainty on
    # the energy whose value depends on the forecasted arbitrage decision.
    optimization_cost = grid_wh / 1000 * slot.price
    if action is Action.BATTERY:
        delivered_kwh = discharged_wh * discharge_efficiency / 1000
        discharge_discount = (
            settings.high_soc_discharge_discount_dkk_per_kwh
            * high_soc_discharge_weight
        )
        required_profit = max(
            0.0, settings.minimum_profit_dkk_per_kwh - discharge_discount
        )
        interval_cost += delivered_kwh * settings.degradation_cost_dkk_per_kwh
        optimization_cost += delivered_kwh * (
            settings.degradation_cost_dkk_per_kwh
            + required_profit
            + slot.uncertainty_dkk_per_kwh
        )
    elif action is Action.CHARGE:
        optimization_cost += (
            input_wh / 1000 * slot.uncertainty_dkk_per_kwh
        )
        charge_premium = (
            settings.low_soc_charge_premium_dkk_per_kwh
            * low_soc_charge_weight
        )
        optimization_cost -= input_wh / 1000 * charge_premium
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


def _partial_charge_candidates(
    predecessors: list[_State],
    previous_layer: dict[_State, _Node],
    slot: PriceSlot,
    settings: PlannerSettings,
    minimum_step: int,
    maximum_step: int,
) -> Iterator[tuple[_State, _Node]]:
    """Find the best predecessor for each reachable native integer SOC target.

    Charge cost is a difference of energy potentials, including the integrated
    low-SOC premium. For a fixed endpoint only predecessors within one slot's
    input budget can reach it. A monotonic queue finds their minimum in linear
    time rather than trying every target from every energy/action state.
    """
    if not predecessors or settings.partial_charge_targets == ():
        return
    step_wh = settings.energy_step_wh
    efficiency = sqrt(settings.round_trip_efficiency)
    load_power_w = max(0.0, slot.expected_load_wh) / slot.hours
    budget_wh = max(0.0, settings.charge_power_w - load_power_w) * slot.hours * efficiency
    budget_steps = floor(budget_wh / step_wh + 1e-9)
    if budget_steps <= 0:
        return

    threshold_wh = settings.capacity_wh * settings.low_soc_charge_threshold / 100

    def potential(energy_wh: float) -> float:
        weighted_wh = 0.0
        if threshold_wh > 0:
            below_wh = min(energy_wh, threshold_wh)
            weighted_wh = below_wh - below_wh**2 / (2 * threshold_wh)
        return (
            energy_wh * slot.charge_price_dkk_per_kwh
            - weighted_wh * settings.low_soc_charge_premium_dkk_per_kwh
        ) / (efficiency * 1000)

    targets: dict[int, int] = {}
    allowed_targets = (
        range(floor(settings.reserve_soc) + 1, floor(settings.target_soc) + 1)
        if settings.partial_charge_targets is None
        else sorted(set(settings.partial_charge_targets))
    )
    for target_soc in allowed_targets:
        if not settings.reserve_soc < target_soc <= settings.target_soc:
            continue
        endpoint = floor(settings.capacity_wh * target_soc / 100 / step_wh)
        if minimum_step < endpoint <= maximum_step:
            targets.setdefault(endpoint, target_soc)

    window: deque[tuple[tuple[float, float], _State]] = deque()
    cursor = 0
    for endpoint, target_soc in targets.items():
        while cursor < len(predecessors) and predecessors[cursor].energy_step < endpoint:
            state = predecessors[cursor]
            node = previous_layer[state]
            energy_wh = state.energy_step * step_wh
            key = (
                round(
                    node.cost - potential(energy_wh)
                    + (settings.switching_penalty_dkk if state.action is not Action.CHARGE else 0),
                    12,
                ),
                node.throughput_wh - energy_wh,
            )
            while window and key < window[-1][0]:
                window.pop()
            window.append((key, state))
            cursor += 1
        while window and window[0][1].energy_step < endpoint - budget_steps:
            window.popleft()
        if not window:
            continue
        state = window[0][1]
        result = _slot_transition(
            state, Action.CHARGE, slot, settings, minimum_step, endpoint
        )
        if result is None:
            continue
        next_state, detail, throughput = result
        node = previous_layer[state]
        yield next_state, _Node(
            node.cost + detail[4], node.throughput_wh + throughput,
            state, detail, float(target_soc),
        )


def optimize(
    slots: Iterable[PriceSlot],
    *,
    now: datetime,
    soc: float,
    settings: PlannerSettings,
    current_action: Action = Action.GRID,
    committed_slot: PlannedSlot | None = None,
) -> Plan:
    """Plan future intervals from observed SOC and an optional held current slot.

    The coordinator supplies the already-published action and permitted charge
    ceiling. They constrain only the remainder of that same tariff interval.
    """
    settings.validate()
    source_slots = tuple(slots)
    first_slot_is_partial = any(
        (slot.tariff_start or slot.start) < now < slot.end
        for slot in source_slots
    )
    valid = _future_slots(source_slots, now)
    if valid and valid[0].start > now:
        return Plan(
            now,
            (),
            0,
            0,
            0,
            0,
            0,
            "No valid current price interval; future prices cannot be promoted to now",
        )
    if not valid:
        return Plan(
            now, (), 0, 0, 0, 0, 0, "No complete contiguous future price intervals"
        )
    if committed_slot is not None and not (
        committed_slot.start < now < committed_slot.end
        and committed_slot.end == valid[0].end
    ):
        committed_slot = None
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
    maximum_step = int(
        settings.capacity_wh * settings.target_soc / 100 // step_wh
    )
    recovery_deadline: int | None = None
    if soc < settings.reserve_soc and initial_step < minimum_step:
        # Restoring an observed reserve deficit is a planning requirement,
        # rather than an arbitrage purchase that must pay for itself. Constrain
        # the known-price prefix only: an uncertain cheap forecast cannot push
        # recovery beyond the last confirmed tariff. The cost objective still
        # chooses when to buy recovery energy and values any optional cycling.
        for index, slot in enumerate(valid):
            if slot.source != "known":
                break
            recovery_deadline = index
    initial = _State(initial_step, current_action)
    layers: list[dict[_State, _Node]] = [{initial: _Node(0.0, 0.0, None, None)}]
    for slot_index, slot in enumerate(valid):
        holding_interval = slot_index == 0 and (
            first_slot_is_partial or committed_slot is not None
        )
        previous_layer = layers[-1]
        layer: dict[_State, _Node] = {}
        partial_charge_predecessors: list[_State] = []
        interval_maximum_step = maximum_step
        if (
            slot_index == 0 and committed_slot is not None
            and committed_slot.action is Action.CHARGE
            and committed_slot.charge_target_soc is not None
        ):
            interval_maximum_step = int(
                settings.capacity_wh * committed_slot.charge_target_soc / 100 // step_wh
            )
        for state in sorted(
            previous_layer,
            key=lambda item: (
                item.energy_step,
                item.action.value,
            ),
        ):
            node = previous_layer[state]
            actions = _allowed_actions()
            continuing_action = state.action
            if slot_index == 0 and committed_slot is not None:
                continuing_action = committed_slot.action
            locked_continuation = False
            if holding_interval:
                # The electricity price is constant for the whole source slot.
                # Keep the committed (or observed) action until that tariff
                # boundary instead of allowing every minute-level refresh to
                # create a new economic block. If the action can no longer move
                # energy (BATTERY at reserve or CHARGE at the ceiling), fall
                # back to the normal candidates so physical limits still win.
                continuing = _slot_transition(
                    state,
                    continuing_action,
                    slot,
                    settings,
                    minimum_step,
                    interval_maximum_step,
                    allow_substep_continuation=True,
                )
                if continuing is not None:
                    actions = (continuing_action,)
                    locked_continuation = True
            if Action.CHARGE in actions and not locked_continuation:
                partial_charge_predecessors.append(state)
            for action in actions:
                result = _slot_transition(
                    state,
                    action,
                    slot,
                    settings,
                    minimum_step,
                    interval_maximum_step,
                    allow_substep_continuation=(
                        holding_interval
                        and action is continuing_action
                    ),
                )
                if result is None:
                    continue
                next_state, detail, throughput = result
                candidate = _Node(
                    node.cost + detail[4],
                    node.throughput_wh + throughput,
                    state,
                    detail,
                    (
                        committed_slot.charge_target_soc
                        if slot_index == 0 and committed_slot is not None
                        and committed_slot.charge_target_soc is not None
                        else settings.target_soc
                    ) if action is Action.CHARGE else None,
                )
                existing = layer.get(next_state)
                candidate_key = (
                    # Keep floating accumulation noise from discarding the
                    # lower-throughput path before the final economic tie.
                    round(candidate.cost, 12),
                    candidate.throughput_wh,
                    action.value,
                )
                if existing is None:
                    layer[next_state] = candidate
                else:
                    existing_key = (
                        round(existing.cost, 12),
                        existing.throughput_wh,
                        next_state.action.value,
                    )
                    if candidate_key < existing_key:
                        layer[next_state] = candidate
        for next_state, candidate in _partial_charge_candidates(
            partial_charge_predecessors, previous_layer, slot, settings,
            minimum_step, interval_maximum_step,
        ):
            existing = layer.get(next_state)
            if existing is None or (
                round(candidate.cost, 12), candidate.throughput_wh
            ) < (round(existing.cost, 12), existing.throughput_wh):
                layer[next_state] = candidate
        if not layer:
            return Plan(now, (), 0, 0, 0, 0, 0, "No plan satisfies battery constraints")
        if slot_index == recovery_deadline:
            # Recover fully when possible; a short horizon or insufficient
            # power may only permit partial recovery. Retain its best physical
            # reserve coverage without inventing energy or relaxing limits.
            recoverable_step = max(min(state.energy_step, minimum_step) for state in layer)
            layer = {
                state: node for state, node in layer.items()
                if state.energy_step >= recoverable_step
            }
        layers.append(layer)

    # An unavailable discharge path cannot redeem stored energy later. Giving
    # it a carryover credit would buy power for an unexecutable arbitrage cycle.
    terminal_price = _terminal_price(valid) if settings.discharge_enabled else 0.0
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
        stored_above_reserve_wh = max(0, state.energy_step - minimum_step) * step_wh
        terminal_value = (
            stored_above_reserve_wh * discharge_efficiency / 1000 * terminal_net_price
        )
        # Absorb floating-point noise from the two accumulation paths (grid-only
        # vs. battery movement) so that a genuine economic *wash* ties on the
        # cost term. The existing tiebreak is "less battery throughput, then
        # stable action order", which deliberately prefers HOLDING when moving
        # the battery buys nothing. Without this rounding the wash decided itself on a
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
        charge_target = node.charge_target_soc
        if (
            recovery_deadline is not None and slot.source == "known"
            and charged_wh > 0 and previous_energy < minimum_step * step_wh
        ):
            reason = "Restore reserve at the cheapest feasible known prices; " + reason
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
                charge_target_soc=charge_target,
            )
        )
        previous_energy = next_energy

    expected_cost = sum(item.interval_cost_dkk for item in planned)
    baseline_cost = sum(item.baseline_cost_dkk for item in planned)
    # Include the terminal value in reported savings so that the plan's
    # economic benefit is not understated (the optimizer already credits
    # the terminal value internally when selecting the final state).
    terminal_value = (
        max(0, final_state.energy_step - minimum_step)
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
        round(_final_node.cost - terminal_value, 6),
    )
