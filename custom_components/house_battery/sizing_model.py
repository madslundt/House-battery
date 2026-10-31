"""Bounded-memory, closed-SOC hindsight storage counterfactuals.

The relaxed continuous flow buys energy after an interval's discharge stage,
so it cannot buy and sell the same energy within an interval. Whole trade
packets are subsequently reduced to respect shared charge/discharge time.
Switching costs are applied afterwards; this is a feasible approximation,
not a promise of the globally optimal executable control schedule.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from heapq import heappop, heappush
from math import isfinite, sqrt
from typing import Any

from .models import PlannerSettings, PriceSlot

_ENERGY_EPS = 1e-7
_COST_EPS = 1e-13


@dataclass(slots=True)
class _Edge:
    target: int
    reverse: int
    capacity: float
    cost: float
    initial_capacity: float


@dataclass(slots=True)
class _Trade:
    buy: int
    sell: int
    stored_wh: float


def _edge(graph: list[list[_Edge]], start: int, end: int, capacity: float, cost: float) -> _Edge:
    forward = _Edge(end, len(graph[end]), max(0.0, capacity), cost, max(0.0, capacity))
    reverse = _Edge(start, len(graph[start]), 0.0, -cost, 0.0)
    graph[start].append(forward)
    graph[end].append(reverse)
    return forward


def _profitable_flow(graph: list[list[_Edge]], source: int, sink: int) -> None:
    """Successive shortest augmentations with DAG-initialized potentials."""
    count = len(graph)
    # A conceptual zero-cost super-source reaches every vertex. The original
    # positive-capacity edges form a DAG, so one pass produces feasible
    # potentials even with negative electricity prices and discharge credits.
    potential = [0.0] * count
    for node in [source, *range(source), sink]:
        for edge in graph[node]:
            if edge.capacity > _ENERGY_EPS:
                potential[edge.target] = min(potential[edge.target], potential[node] + edge.cost)
    while True:
        distance = [float("inf")] * count
        previous: list[tuple[int, int] | None] = [None] * count
        distance[source] = 0.0
        heap = [(0.0, source)]
        while heap:
            cost, node = heappop(heap)
            if cost > distance[node] + _COST_EPS:
                continue
            for index, edge in enumerate(graph[node]):
                if edge.capacity <= _ENERGY_EPS:
                    continue
                reduced = max(0.0, edge.cost + potential[node] - potential[edge.target])
                candidate = cost + reduced
                if candidate < distance[edge.target] - _COST_EPS:
                    distance[edge.target] = candidate
                    previous[edge.target] = node, index
                    heappush(heap, (candidate, edge.target))
        if previous[sink] is None or distance[sink] + potential[sink] - potential[source] >= -_COST_EPS:
            return
        for node, value in enumerate(distance):
            if isfinite(value):
                potential[node] += value
        amount, node = float("inf"), sink
        while node != source:
            parent, index = previous[node]
            amount = min(amount, graph[parent][index].capacity)
            node = parent
        node = sink
        while node != source:
            parent, index = previous[node]
            edge = graph[parent][index]
            edge.capacity -= amount
            graph[node][edge.reverse].capacity += amount
            node = parent


def _storage_trades(slots: Sequence[PriceSlot], settings: PlannerSettings) -> list[_Trade]:
    size = len(slots)
    source, sink = 2 * size, 2 * size + 1
    graph: list[list[_Edge]] = [[] for _ in range(sink + 1)]
    usable = settings.capacity_wh * (settings.target_soc - settings.reserve_soc) / 100
    efficiency = sqrt(settings.round_trip_efficiency)
    charge_edges, discharge_edges = [], []
    for index, slot in enumerate(slots):
        before, after = 2 * index, 2 * index + 1
        load = slot.expected_load_wh
        charge_budget = max(0.0, settings.charge_power_w * slot.hours - load) * efficiency
        discharge_budget = min(load, settings.discharge_power_w * slot.hours) / efficiency
        charge_edges.append(_edge(graph, source, after,
            charge_budget if index + 1 < size else 0.0, slot.price / (1000 * efficiency)))
        discharge_edges.append(_edge(graph, before, sink,
            discharge_budget if settings.discharge_enabled else 0.0,
            -(slot.price - settings.degradation_cost_dkk_per_kwh) * efficiency / 1000))
        _edge(graph, before, after, usable, 0.0)
        if index + 1 < size:
            _edge(graph, after, before + 2, usable, 0.0)
    _profitable_flow(graph, source, sink)

    # FIFO decomposition makes each repair a complete buy/sell packet, never
    # an isolated charge or discharge that would introduce free stored energy.
    lots: deque[list[float | int]] = deque()
    trades = []
    for index in range(size):
        needed = max(0.0, discharge_edges[index].initial_capacity - discharge_edges[index].capacity)
        while needed > _ENERGY_EPS and lots:
            buy, available = lots[0]
            amount = min(needed, available)
            trades.append(_Trade(int(buy), index, amount))
            needed -= amount
            lots[0][1] -= amount
            if lots[0][1] <= _ENERGY_EPS:
                lots.popleft()
        bought = max(0.0, charge_edges[index].initial_capacity - charge_edges[index].capacity)
        if bought > _ENERGY_EPS:
            lots.append([index, bought])
    return trades


def _repair_time(
    trades: list[_Trade], slots: Sequence[PriceSlot], settings: PlannerSettings,
) -> tuple[list[float], list[float], int, float]:
    efficiency = sqrt(settings.round_trip_efficiency)
    buys: list[list[int]] = [[] for _ in slots]
    sells: list[list[int]] = [[] for _ in slots]
    charge_coefficients, discharge_coefficients = [], []
    for index, slot in enumerate(slots):
        charge_budget = max(0.0, settings.charge_power_w * slot.hours - slot.expected_load_wh) * efficiency
        discharge_budget = min(slot.expected_load_wh, settings.discharge_power_w * slot.hours) / efficiency
        charge_coefficients.append(1 / charge_budget if charge_budget > 0 else 0.0)
        discharge_coefficients.append(1 / discharge_budget if discharge_budget > 0 else 0.0)
    for index, trade in enumerate(trades):
        buys[trade.buy].append(index)
        sells[trade.sell].append(index)
    repaired = 0
    for index in range(len(slots)):
        usage = sum(trades[t].stored_wh for t in buys[index]) * charge_coefficients[index]
        usage += sum(trades[t].stored_wh for t in sells[index]) * discharge_coefficients[index]
        if usage > 1 + 1e-10:
            repaired += 1
            for trade in buys[index] + sells[index]:
                trades[trade].stored_wh /= usage
    charges = [sum(trades[t].stored_wh for t in indices) for indices in buys]
    discharges = [sum(trades[t].stored_wh for t in indices) for indices in sells]
    maximum_usage = max((c * a + d * b for c, d, a, b in zip(
        charges, discharges, charge_coefficients, discharge_coefficients, strict=True)), default=0.0)
    return charges, discharges, repaired, maximum_usage


def _segment_result(slots: Sequence[PriceSlot], settings: PlannerSettings) -> dict[str, float]:
    efficiency = sqrt(settings.round_trip_efficiency)
    trades = _storage_trades(slots, settings)
    charges, discharges, repaired, utilization = _repair_time(trades, slots, settings)
    cash = wear = stored = peak = active = 0.0
    switches, previous_mode = 0, "grid"
    for slot, charge, discharge in zip(slots, charges, discharges, strict=True):
        charge_ac, output_ac = charge / efficiency, discharge * efficiency
        cash += (output_ac - charge_ac) * slot.price / 1000
        wear += output_ac * settings.degradation_cost_dkk_per_kwh / 1000
        stored += charge - discharge
        peak = max(peak, stored)
        charge_power = max(0.0, settings.charge_power_w - slot.expected_load_wh / slot.hours)
        output_power = min(slot.expected_load_wh / slot.hours, settings.discharge_power_w)
        charging_hours = charge_ac / charge_power if charge_power > 0 else 0.0
        discharging_hours = output_ac / output_power if output_power > 0 else 0.0
        active += charging_hours + discharging_hours
        sequence = []
        if discharge > _ENERGY_EPS:
            sequence.append("battery")
        if charge > _ENERGY_EPS:
            sequence.append("charge")
        if not sequence or charging_hours + discharging_hours < slot.hours - 1e-8:
            sequence.append("grid")
        for mode in sequence:
            switches += mode != previous_mode
            previous_mode = mode
    switches += previous_mode != "grid"
    return {
        "bill_savings_dkk": cash, "wear_cost_dkk": wear,
        "switching_cost_dkk": switches * settings.switching_penalty_dkk,
        "charge_kwh": sum(charges) / efficiency / 1000,
        "discharge_kwh": sum(discharges) * efficiency / 1000,
        "peak_stored_wh": peak, "active_hours": active,
        "stored_energy_balance_wh": stored, "repaired_hours": repaired,
        "maximum_hour_utilization": utilization,
    }


def _valid_segments(slots: Sequence[PriceSlot]) -> list[list[PriceSlot]] | None:
    valid = [s for s in slots if s.start.tzinfo is not None and s.end.tzinfo is not None
             and s.end > s.start and s.source == "known" and isfinite(s.price)
             and isfinite(s.expected_load_wh) and s.expected_load_wh >= 0]
    segments: list[list[PriceSlot]] = []
    for slot in sorted(valid, key=lambda s: s.start):
        if segments and slot.start < segments[-1][-1].end:
            return None
        if not segments or slot.start != segments[-1][-1].end:
            segments.append([])
        segments[-1].append(slot)
    return segments


def simulate_storage(slots: Sequence[PriceSlot], settings: PlannerSettings) -> dict[str, Any]:
    """Estimate net storage savings with equal reserve SOC at every gap boundary.

    Uses the planner's symmetric efficiency split and charge-power convention.
    Required-profit policy and SOC preference premiums are not cash costs and
    are excluded. Perfect price/load foresight, continuous within-hour actions,
    feasibility repair, and post-hoc switching make this an advisory model.
    """
    settings.validate()
    if not all(isfinite(value) for value in (
        settings.capacity_wh, settings.reserve_soc, settings.target_soc,
        settings.charge_power_w, settings.discharge_power_w,
        settings.round_trip_efficiency, settings.degradation_cost_dkk_per_kwh,
        settings.switching_penalty_dkk,
    )):
        raise ValueError("Storage sizing settings must be finite")
    result: dict[str, Any] = {
        "model": "hourly_closed_soc_hindsight",
        "optimization": "relaxed_flow_with_time_repair_and_posthoc_switching",
        "bill_savings_dkk": 0.0, "wear_cost_dkk": 0.0, "switching_cost_dkk": 0.0,
        "net_savings_dkk": 0.0, "charge_kwh": 0.0, "discharge_kwh": 0.0,
        "peak_stored_wh": 0.0, "active_hours": 0.0, "modeled_hours": 0.0,
        "stored_energy_balance_wh": 0.0, "maximum_hour_utilization": 0.0,
        "repaired_hours": 0, "segments": 0, "start_end_soc": settings.reserve_soc,
        "reason": "no_profitable_closed_soc_cycles",
    }
    segments = _valid_segments(slots)
    if segments is None:
        result["reason"] = "overlapping_history_intervals"
        return result
    result["segments"] = len(segments)
    result["modeled_hours"] = sum(s.hours for segment in segments for s in segment)
    for segment in segments:
        modeled = _segment_result(segment, settings)
        # Each independently closed segment may always choose to use the grid.
        if modeled["bill_savings_dkk"] - modeled["wear_cost_dkk"] - modeled["switching_cost_dkk"] <= 1e-9:
            continue
        for key, value in modeled.items():
            if key in {"peak_stored_wh", "maximum_hour_utilization"}:
                result[key] = max(result[key], value)
            else:
                result[key] += value
    result["net_savings_dkk"] = (
        result["bill_savings_dkk"] - result["wear_cost_dkk"] - result["switching_cost_dkk"]
    )
    if result["net_savings_dkk"] > 0:
        result["reason"] = "feasible_closed_soc_hindsight_estimate"
    return result
