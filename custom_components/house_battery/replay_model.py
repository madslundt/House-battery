"""Offline rolling policy comparison against one common realized-load plant.

No Home Assistant or device access. Realized interval means drive execution,
never forecasts. Inventory settlement is a separate common replacement-cost
adjustment, not cash earned. This is a simulation, not measured policy savings.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from math import isfinite, sqrt
from typing import Any

from .capabilities import DeliveryTopology
from .models import Action, PlannedSlot, PlannerSettings, PriceSlot
from .planner import optimize
from .replay import (
    MAX_TARIFF_HANDOFF_DELAY,
    ReplayHistory,
    restore_commitment,
    restore_settings,
    restore_slots,
    timestamp,
    validity_end,
)


def _empty_metrics() -> dict[str, float]:
    return dict.fromkeys(("cash_cost_dkk", "wear_cost_dkk", "switching_cost_dkk",
                          "inventory_settlement_dkk", "charge_kwh", "discharge_kwh",
                          "reserve_shortfall_wh", "energy_balance_error_wh",
                          "corrective_interventions", "reserve_shortfall_hours",
                          "equivalent_full_cycles"), 0.0)


@dataclass(slots=True)
class _Case:
    stored_wh: float
    initial_wh: float
    settings: PlannerSettings
    action: Action = Action.GRID
    held: PlannedSlot | None = None
    metrics: dict[str, float] = field(default_factory=_empty_metrics)


def _settle(case: _Case, price: float) -> None:
    # Positive means buying missing inventory; excess receives identical credit.
    case.metrics["inventory_settlement_dkk"] += (
        (case.initial_wh - case.stored_wh) / sqrt(case.settings.round_trip_efficiency) / 1000 * price
    )


def _execute(
    case: _Case, action: Action, *, load_wh: float, hours: float, price: float,
    target_soc: float, plant: PlannerSettings,
) -> None:
    efficiency = sqrt(plant.round_trip_efficiency)
    start = case.stored_wh
    reserve = plant.capacity_wh * plant.reserve_soc / 100
    upper = plant.capacity_wh * target_soc / 100
    charge = discharge = 0.0
    if action is Action.CHARGE:
        requested = max(0, plant.charge_power_w * hours - load_wh)
        charge = min(requested, max(0, upper - start) / efficiency)
    elif action is Action.BATTERY:
        requested = plant.control_capabilities.discharge_setpoint_w(
            plant.discharge_power_w, load_wh / hours) * hours
        discharge = min(requested, max(0, start - reserve) * efficiency)
        if plant.control_capabilities.topology is DeliveryTopology.MAINS_ISOLATED:
            if (plant.control_capabilities.discharge_load_feasible(plant.discharge_power_w, load_wh / hours)
                    and load_wh <= max(0, start - reserve) * efficiency + 1e-6):
                requested = discharge = load_wh
            else:
                # The setpoint is only an eligibility guard for backup output.
                # Restore grid mode if the whole isolated load cannot be served.
                action, discharge = Action.GRID, 0
    else:
        requested = 0
    case.stored_wh = start + charge * efficiency - discharge / efficiency
    case.metrics["cash_cost_dkk"] += (load_wh + charge - discharge) / 1000 * price
    case.metrics["wear_cost_dkk"] += discharge / 1000 * plant.degradation_cost_dkk_per_kwh
    physical_action = Action.GRID if action is Action.CHARGE and charge <= 1e-9 else action
    stopped_early = (
        action is Action.CHARGE and charge > 1e-9
        and (requested - charge) * efficiency >= plant.energy_step_wh - 1e-9
    )
    case.metrics["switching_cost_dkk"] += (
        (physical_action != case.action) + int(stopped_early)
    ) * plant.switching_penalty_dkk
    case.metrics["charge_kwh"] += charge / 1000
    case.metrics["discharge_kwh"] += discharge / 1000
    case.metrics["equivalent_full_cycles"] += (charge * efficiency + discharge / efficiency) / (2 * plant.capacity_wh)
    case.metrics["energy_balance_error_wh"] += abs(
        case.stored_wh - start - charge * efficiency + discharge / efficiency)
    shortfall = max(0, reserve - case.stored_wh)
    case.metrics["reserve_shortfall_wh"] = max(case.metrics["reserve_shortfall_wh"], shortfall)
    case.metrics["reserve_shortfall_hours"] += hours if shortfall > 1e-6 else 0
    case.metrics["corrective_interventions"] += abs(requested - charge - discharge) > 1e-6
    case.action = Action.GRID if stopped_early else physical_action


def _restore_inputs(
    record: dict[str, Any], issued: datetime,
) -> tuple[PlannerSettings, list[PriceSlot], datetime, Action, PlannedSlot | None]:
    settings = restore_settings(record["settings"])
    slots = restore_slots(record["slots"])
    expires = validity_end(record, issued)
    if "valid_until" in record:
        tariff_end = next((slot.end for slot in slots
                           if slot.source == "known" and slot.start <= issued < slot.end), issued)
        expires = min(expires, tariff_end)
    return settings, slots, expires, Action(record["current_action"]), restore_commitment(
        record.get("committed_slot"),
    )


def _quarter_start(slot: PriceSlot) -> datetime | None:
    start = slot.tariff_start or slot.start
    return start if slot.end - start == timedelta(minutes=15) else None


def evaluate_policies(
    history: ReplayHistory, *, settlement_price_dkk_per_kwh: float,
    since: datetime | None = None,
) -> dict[str, Any]:
    """Compare captured settings against a zero-profit-hurdle variant and grid.

    Captured snapshots supply only information issued by that decision time.
    Both candidates use their own simulated SOC and the same captured physical
    parameters. A short issue delay between consecutive confirmed quarters
    can preserve inventory. Missing data, interruptions and changed physical
    scope/settings start independent segments. Realized loads are piecewise
    constant within each measured interval. No annualization.
    """
    if (isinstance(settlement_price_dkk_per_kwh, bool)
            or not isfinite(settlement_price_dkk_per_kwh)
            or settlement_price_dkk_per_kwh < 0):
        raise ValueError("A finite nonnegative common inventory settlement price is required")
    since = timestamp(since) if since is not None else None
    records = iter(history.iter_records())
    pending = next(records, None)
    pending_at = timestamp(pending["issued_at"]) if pending is not None else None
    record: dict[str, Any] | None = None
    inputs = None
    issued = None
    scope = None
    totals: dict[str, dict[str, float]] = {}
    cases: dict[str, _Case] = {}
    baseline = evaluated = skipped = 0.0
    measured_coverage = available_actual = actual_gap = 0.0
    segments = 0
    previous_end: datetime | None = None
    previous_quarter_end: datetime | None = None
    handoff_at: datetime | None = None
    previous_actual_end: datetime | None = None
    previous_scope: Any = None

    def close() -> None:
        nonlocal cases
        for name, case in cases.items():
            _settle(case, settlement_price_dkk_per_kwh)
            total = totals.setdefault(name, dict.fromkeys(case.metrics, 0.0))
            for key, value in case.metrics.items():
                if key == "reserve_shortfall_wh":
                    total[key] = max(total[key], value)
                else:
                    total[key] += value
        cases = {}

    for row in history.iter_actuals():
        start, end = timestamp(row["start"]), timestamp(row["end"])
        if since is not None and end <= since:
            continue
        left = max(start, since) if since is not None else start
        elapsed = (end - left).total_seconds()
        measured_coverage += row["covered_seconds"] * elapsed / (end - start).total_seconds()
        available_actual += elapsed
        if previous_actual_end is not None:
            actual_gap += max(0, (left - previous_actual_end).total_seconds())
        previous_actual_end = end
        while left < end:
            # Advance on issue time, even if a newer vintage cannot be used.
            # Expired/superseded vintages never become current again.
            while pending_at is not None and pending_at <= left:
                record, issued = pending, pending_at
                pending = next(records, None)
                pending_at = timestamp(pending["issued_at"]) if pending is not None else None
                inputs = None
                if pending_at is not None and pending_at <= left:
                    continue
                try:
                    inputs = _restore_inputs(record, issued)
                    scope = (record["scope"], replace(inputs[0], minimum_profit_dkk_per_kwh=0))
                except (TypeError, ValueError, KeyError):
                    pass
            right = min(end, pending_at) if pending_at is not None else end
            expires = None
            eligible = False
            if inputs is not None and record is not None:
                settings, slots, expires, action, commitment = inputs
                if left < expires and row.get("scope") in (None, record["scope"]):
                    right = min(right, expires)
                    current = next((slot for slot in slots
                                    if slot.source == "known" and slot.start <= left < slot.end), None)
                    if current is None:
                        # Legacy vintages can outlast their first tariff. Split
                        # at the next known edge if the interval has a gap.
                        right = min((slot.start for slot in slots
                                     if slot.source == "known" and left < slot.start < right),
                                    default=right)
                    else:
                        right = min(right, current.end)
                        eligible = True
            hours = (right - left).total_seconds() / 3600
            if not eligible:
                skipped += hours
                if (previous_end == previous_quarter_end == expires == left
                        and pending_at == right
                        and right - left <= MAX_TARIFF_HANDOFF_DELAY
                        and record is not None and "valid_until" in record):
                    # Keep inventory in memory until the adjacent tariff's
                    # plan is issued. The elapsed delay remains unevaluated.
                    handoff_at = right
                else:
                    close()
                    previous_end = handoff_at = None
                left = right
                continue
            quarter_start = _quarter_start(current)
            continuous = previous_end == left or (
                handoff_at == left and quarter_start == previous_end
            )
            handoff_at = None
            if not cases or not continuous or previous_scope != scope:
                close()
                segments += 1
                energy = settings.capacity_wh * record["soc"] / 100
                cases = {name: _Case(energy, energy, settings, action, commitment)
                         for name in ("current", "zero_profit")}
            load = row["load_wh"] * (right - left) / (end - start)
            discharge_enabled = record.get("current_discharge_enabled", True) if current.start <= issued < current.end else True
            for name, case in cases.items():
                candidate = settings if name == "current" else replace(settings, minimum_profit_dkk_per_kwh=0)
                plan = optimize(slots, now=left, soc=case.stored_wh / settings.capacity_wh * 100,
                                settings=candidate, current_action=case.action,
                                committed_slot=case.held if case.held and case.held.end > left else None,
                                terminal_price_floor_dkk_per_kwh=record.get("terminal_price_floor_dkk_per_kwh", 0.0),
                                current_discharge_enabled=discharge_enabled)
                decision = plan.slots[0] if plan.slots else None
                action = decision.action if decision else Action.GRID
                case.held = decision
                _execute(case, action, load_wh=load, hours=hours, price=row["price"],
                         target_soc=(decision.charge_target_soc or settings.target_soc)
                         if decision else settings.target_soc, plant=settings)
            baseline += load / 1000 * row["price"]
            evaluated += hours
            previous_end = None if right == expires and record.get("interrupted", False) else right
            previous_scope = scope
            previous_quarter_end = current.end if quarter_start is not None else None
            left = right
    close()
    for name in ("current", "zero_profit"):
        total = totals.setdefault(name, _empty_metrics())
        total["net_cost_dkk"] = sum(total[k] for k in (
            "cash_cost_dkk", "wear_cost_dkk", "switching_cost_dkk", "inventory_settlement_dkk"))
        total["net_benefit_dkk"] = baseline - total["net_cost_dkk"]
    totals["no_battery"] = {"cash_cost_dkk": baseline, "net_cost_dkk": baseline, "net_benefit_dkk": 0.0}
    return {
        "method": "causal_issued_inputs_interval_mean_plant",
        "evaluated_hours": evaluated, "skipped_hours": skipped, "segments": segments,
        "measured_coverage_hours": measured_coverage / 3600,
        "available_actual_hours": available_actual / 3600,
        "actual_gap_hours": actual_gap / 3600,
        "settlement_price_dkk_per_kwh": settlement_price_dkk_per_kwh, "policies": totals,
        "assumptions": ["Realized load is constant within each measured interval",
                        "Only constant-price measured intervals are eligible; mixed-price quarters excluded by capture",
                        "Common captured conservative scalar-efficiency plant; no physical device execution",
                        "Charge setpoint semantics, standby and power-dependent losses remain unmeasured",
                        "Final inventory settled separately at a common replacement price",
                        "Does not establish field savings or include missing-data periods"],
    }
