"""Walk-forward battery economics for the deployed historical-price fallback.

Uses cached official Energinet DK2 quarters, aggregates them into UTC hours,
and executes the actual optimizer against the existing replay-model plant.
This is a synthetic experiment, not household savings or an external-forecast
benchmark. Run with the repository's Home Assistant Python environment.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import asdict, replace
from datetime import UTC, date, datetime, time, timedelta
import json
from pathlib import Path
from statistics import mean
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "custom_components"))

from house_battery.capabilities import ControlCapabilities
from house_battery.const import DEFAULT_SETTINGS
from house_battery.models import Action, PlannerSettings, PriceSlot
from house_battery.planner import optimize
from house_battery.policy import has_forecast_backed_movement
from house_battery.price_patterns import PricePatternLearner
from house_battery.replay_model import _Case, _execute, _settle

from zoneinfo import ZoneInfo

ZONE = ZoneInfo("Europe/Copenhagen")
HOUR = timedelta(hours=1)


def load_hourly(path: Path) -> dict[datetime, float]:
    """Validate and duration-average four official quarters in each UTC hour."""
    payload = json.loads(path.read_text())
    quarters: dict[datetime, float] = {}
    for row in payload["records"]:
        if row["PriceArea"] != "DK2":
            continue
        start = datetime.fromisoformat(row["TimeUTC"]).replace(tzinfo=UTC)
        if start in quarters:
            raise ValueError("Duplicate official quarter")
        quarters[start] = float(row["DayAheadPriceDKK"]) / 1000
    points = sorted(quarters)
    if any(right - left != timedelta(minutes=15) for left, right in zip(points, points[1:])):
        raise ValueError("Official input has missing quarters")
    buckets: dict[datetime, list[float]] = defaultdict(list)
    for start, price in quarters.items():
        buckets[start.replace(minute=0)].append(price)
    return {start: mean(values) for start, values in buckets.items() if len(values) == 4}


def import_price(start: datetime, spot: float, basis: str) -> float:
    """Synthetic import additions are explicit, not claimed as an actual tariff."""
    if basis == "spot":
        return spot
    hour = start.astimezone(ZONE).hour
    return spot + 1.0 + (0.5 if 17 <= hour < 21 else 0.1)


def demand_wh(start: datetime) -> float:
    """One shared deterministic connected-load profile; perfect load forecast."""
    hour = start.astimezone(ZONE).hour
    return 180 if hour < 6 else 350 if hour < 9 else 230 if hour < 17 else 650 if hour < 21 else 250


def settings(step_wh: float, profit: float) -> PlannerSettings:
    """Configured defaults except disclosed discretization and profit variant."""
    return PlannerSettings(
        capacity_wh=DEFAULT_SETTINGS["capacity_kwh"] * 1000,
        reserve_soc=DEFAULT_SETTINGS["reserve_soc"],
        target_soc=DEFAULT_SETTINGS["target_soc"],
        charge_power_w=DEFAULT_SETTINGS["charge_power_w"],
        discharge_power_w=DEFAULT_SETTINGS["discharge_power_w"],
        round_trip_efficiency=DEFAULT_SETTINGS["round_trip_efficiency"] / 100,
        degradation_cost_dkk_per_kwh=DEFAULT_SETTINGS["degradation_cost_dkk_per_kwh"],
        minimum_profit_dkk_per_kwh=profit,
        switching_penalty_dkk=0.05,
        low_soc_charge_premium_dkk_per_kwh=0.25,
        high_soc_discharge_discount_dkk_per_kwh=0.25,
        energy_step_wh=step_wh,
        control_capabilities=ControlCapabilities.direct_local(mains_isolated=True),
    )


def published_end(now: datetime) -> datetime:
    """Assume tomorrow's prices become usable at 14:00 local, never earlier."""
    local = now.astimezone(ZONE)
    days = 2 if local.hour >= 14 else 1
    return datetime.combine(local.date() + timedelta(days=days), time(), ZONE).astimezone(UTC)


def totals(case: _Case, settlement: float) -> dict:
    _settle(case, settlement)
    result = dict(case.metrics)
    result["final_soc_pct"] = 100 * case.stored_wh / case.settings.capacity_wh
    result["net_cost_dkk"] = sum(result[key] for key in (
        "cash_cost_dkk", "wear_cost_dkk", "switching_cost_dkk", "inventory_settlement_dkk",
    ))
    return result


def confirmed_prefix(prices: dict[datetime, float], now: datetime) -> list[PriceSlot]:
    """Use only the day-ahead publication coverage available at issue time."""
    result = []
    position = now
    while position < published_end(now):
        result.append(PriceSlot(position, position + HOUR, prices[position], demand_wh(position)))
        position += HOUR
    return result


def warmup_model(
    learner: PricePatternLearner, prices: dict[datetime, float], start: datetime,
) -> dict:
    """Issue real shadow forecasts before scoring their subsequent actuals.

    Batched retrospective price history can fit profiles but cannot establish
    honest first-issued skill. Replay hourly price publications and elapsed
    actuals through the same observe/shadow API used by the coordinator.
    Warmup issues are subsampled to 00:00, 12:00 and 14:00 local to cover all
    lead bands economically; evaluation itself issues every hourly tariff.
    """
    now = max(min(prices), start - timedelta(days=90))
    first = now
    calls = 0
    while now < start:
        previous = now - HOUR
        if previous >= first:
            learner.observe([PriceSlot(previous, now, prices[previous])], now)
        if now.astimezone(ZONE).hour in (0, 12, 14):
            learner.shadow_issue(confirmed_prefix(prices, now), now)
            calls += 1
        now += HOUR
    if now - HOUR >= first:
        learner.observe([PriceSlot(now - HOUR, now, prices[now - HOUR])], now)
    if any(datetime.fromisoformat(stamp) + HOUR > start for stamp in learner.samples):
        raise AssertionError("Future actual entered the pattern learner")
    return {"shadow_issue_count": calls, "issue_local_hours": [0, 12, 14],
            "first_issue": first.isoformat(), "summary": learner.summary(start)}


def replay_week(
    spots: dict[datetime, float], start_day: date, *, days: int, basis: str,
    planner_settings: PlannerSettings, settlement: float, allow_terminal_only: bool = False,
) -> dict:
    """Issue each plan before execution, learn only settled actual intervals."""
    start = datetime.combine(start_day, time(), ZONE).astimezone(UTC)
    end = datetime.combine(start_day + timedelta(days=days), time(), ZONE).astimezone(UTC)
    prices = {when: import_price(when, price, basis) for when, price in spots.items()}
    learner = PricePatternLearner()
    learner.configure(f"simulation:{basis}", "Europe/Copenhagen")
    warmup = warmup_model(learner, prices, start)
    initial = planner_settings.capacity_wh * .5
    cases = {name: _Case(initial, initial, planner_settings) for name in ("known", "learned")}
    counts = dict.fromkeys(("hours", "different_actions", "different_targets", "pattern_slots",
                           "lower_terminal", "lower_terminal_different_action",
                           "different_action_no_forecast_throughput", "full_external_72h_unchanged",
                           "pattern_active_hours", "history_ready_hours", "skill_ready_hours",
                           "accepted_forecast_movement_hours", "rejected_terminal_only_hours",
                           "terminal_only_candidate_hours", "allowed_terminal_only_hours",
                           "rejected_would_change_action", "rejected_would_change_target"), 0)
    selected_models: dict[str, int] = defaultdict(int)
    terminal_examples = []
    grid_cost = 0.0
    now = start
    while now < end:
        if now > start:
            learner.observe([PriceSlot(now - HOUR, now, prices[now - HOUR])], now)
        prefix = confirmed_prefix(prices, now)
        learner.shadow_issue(prefix, now)
        extended = [replace(slot, expected_load_wh=demand_wh(slot.start))
                    for slot in learner.extend(prefix, now, .25)]
        counts["pattern_slots"] += len(extended) - len(prefix)
        counts["pattern_active_hours"] += len(extended) > len(prefix)
        model_status = learner.summary(now)
        counts["history_ready_hours"] += model_status.get("history_ready", False)
        counts["skill_ready_hours"] += model_status["status"] == "ready"
        for band, model in model_status.get("selected_models", {}).items():
            selected_models[f"{band}:{model or 'unqualified'}"] += 1
        plans = {}
        for name, inputs in (("known", prefix), ("learned", extended)):
            case = cases[name]
            plans[name] = optimize(inputs, now=now, soc=case.stored_wh / planner_settings.capacity_wh * 100,
                                   settings=planner_settings, current_action=case.action)
            if not plans[name].slots:
                raise ValueError(f"No executable {name} plan at {now}: {plans[name].reason}")
            if name == "learned" and len(extended) > len(prefix):
                backed = has_forecast_backed_movement(
                    plans[name], prefix[-1].end, planner_settings.energy_step_wh,
                )
                if backed:
                    counts["accepted_forecast_movement_hours"] += 1
                else:
                    counts["terminal_only_candidate_hours"] += 1
                    if not allow_terminal_only:
                        counts["rejected_terminal_only_hours"] += 1
                        # Recompute using THIS policy's simulated SOC/action;
                        # copying the other policy becomes wrong after divergence.
                        trusted = optimize(
                            prefix, now=now, soc=case.stored_wh / planner_settings.capacity_wh * 100,
                            settings=planner_settings, current_action=case.action,
                        )
                        counts["rejected_would_change_action"] += (
                            trusted.current_action != plans[name].current_action)
                        counts["rejected_would_change_target"] += (
                            trusted.slots[0].charge_target_soc != plans[name].slots[0].charge_target_soc)
                        plans[name] = trusted
                    else:
                        counts["allowed_terminal_only_hours"] += 1
        known, learned = plans["known"], plans["learned"]
        different = known.slots[0].action != learned.slots[0].action
        inventory_quote = (
            learned.terminal_inventory_price_dkk_per_kwh
            if learned.terminal_inventory_price_dkk_per_kwh is not None
            else learned.terminal_price_dkk_per_kwh
        )
        lower_terminal = inventory_quote + 1e-9 < known.terminal_price_dkk_per_kwh
        future_throughput = sum(slot.battery_charge_wh + slot.battery_discharge_wh
                                for slot in learned.slots if slot.price_source == "forecast")
        counts["hours"] += 1
        counts["different_actions"] += different
        counts["different_targets"] += known.slots[0].charge_target_soc != learned.slots[0].charge_target_soc
        counts["lower_terminal"] += lower_terminal
        counts["lower_terminal_different_action"] += lower_terminal and different
        counts["different_action_no_forecast_throughput"] += different and future_throughput < 1e-9
        if different and len(terminal_examples) < 2:
            # Same initial state distinguishes current horizon influence from
            # the carried-SOC differences accumulated by the two policies.
            common = cases["known"]
            same_soc = optimize(extended, now=now, soc=common.stored_wh / planner_settings.capacity_wh * 100,
                                settings=planner_settings, current_action=common.action)
            restored_terminal = optimize(
                extended, now=now, soc=common.stored_wh / planner_settings.capacity_wh * 100,
                settings=planner_settings, current_action=common.action,
                terminal_price_floor_dkk_per_kwh=known.terminal_price_dkk_per_kwh,
            )
            terminal_examples.append({
                "at": now.isoformat(), "common_soc_pct": 100 * common.stored_wh / planner_settings.capacity_wh,
                "known_action": known.current_action.value,
                "same_soc_learned_action": same_soc.current_action.value,
                "same_soc_restored_terminal_action": restored_terminal.current_action.value,
                "known_terminal": known.terminal_price_dkk_per_kwh,
                "learned_terminal": same_soc.terminal_price_dkk_per_kwh,
                "learned_inventory_terminal": same_soc.terminal_inventory_price_dkk_per_kwh,
                "forecast_throughput_wh": future_throughput,
            })
        # Structural control: a complete external horizon leaves no room for
        # learned slots, hence exactly the same optimizer inputs and decisions.
        if counts["hours"] == 1:
            full = list(prefix)
            position = full[-1].end
            while position < now + timedelta(hours=72):
                # Synthetic external prices are a structural precedence
                # control, not a forecast-accuracy or economic comparison.
                full.append(PriceSlot(position, position + HOUR, 3, demand_wh(position),
                                      source="forecast", uncertainty_dkk_per_kwh=.25))
                position += HOUR
            if learner.extend(full, now, .25) != full:
                raise AssertionError("Pattern changed an already complete external horizon")
            counts["full_external_72h_unchanged"] += 1
        for name, case in cases.items():
            decision = plans[name].slots[0]
            _execute(case, decision.action, load_wh=demand_wh(now), hours=1, price=prices[now],
                     target_soc=decision.charge_target_soc or planner_settings.target_soc,
                     plant=planner_settings)
        grid_cost += demand_wh(now) / 1000 * prices[now]
        now += HOUR
    metrics = {name: totals(case, settlement) for name, case in cases.items()}
    return {"start": start_day.isoformat(), "days": days, "basis": basis, "counts": counts,
            "grid_cost_dkk": grid_cost, "policies": metrics,
            "learned_improvement_dkk": metrics["known"]["net_cost_dkk"] - metrics["learned"]["net_cost_dkk"],
            "terminal_examples": terminal_examples, "warmup": warmup,
            "selected_model_hours": dict(selected_models), "final_model_summary": learner.summary(now)}


def synthetic_control_checks() -> dict:
    """Check the guard rejects quote-only effects but admits a useful peak cycle.

    This isolates the optimizer/control seam using explicit hypothetical
    forecasts, rather than bypassing the model's first-issued skill gate.
    """
    now = datetime(2026, 7, 20, tzinfo=UTC)
    configuration = settings(5, .75)
    flat_known = [PriceSlot(now + HOUR * i, now + HOUR * (i + 1), 3, 100)
                  for i in range(12)]
    flat_extended = [*flat_known, *[
        PriceSlot(now + HOUR * i, now + HOUR * (i + 1), 3, 100,
                  source="forecast", uncertainty_dkk_per_kwh=.25)
        for i in range(12, 72)
    ]]
    flat_plan = optimize(flat_extended, now=now, soc=90, settings=configuration)
    flat_accepted = has_forecast_backed_movement(flat_plan, flat_known[-1].end, 5)
    if flat_accepted:
        raise AssertionError("A flat forecast-only terminal effect passed the utility guard")
    known = [PriceSlot(now + HOUR * i, now + HOUR * (i + 1), .2, 200) for i in range(4)]
    extended = [*known, *[
        PriceSlot(now + HOUR * i, now + HOUR * (i + 1), price, 200,
                  source="forecast", uncertainty_dkk_per_kwh=.25)
        for i, price in enumerate((4, 4, .2, .2), 4)
    ]]
    plan = optimize(extended, now=now, soc=20, settings=configuration)
    accepted = has_forecast_backed_movement(plan, known[-1].end, 5)
    if not accepted or not any(s.battery_charge_wh > 0 for s in plan.slots[:4]):
        raise AssertionError("A credible cheap-charge/forecast-peak cycle was blocked")
    initial = configuration.capacity_wh * .2
    base, learned = _Case(initial, initial, configuration), _Case(initial, initial, configuration)
    for slot in plan.slots:
        _execute(base, Action.GRID, load_wh=200, hours=1, price=slot.price,
                 target_soc=configuration.target_soc, plant=configuration)
        _execute(learned, slot.action, load_wh=200, hours=1, price=slot.price,
                 target_soc=slot.charge_target_soc or configuration.target_soc, plant=configuration)
    base_metrics, learned_metrics = totals(base, .2), totals(learned, .2)
    improvement = base_metrics["net_cost_dkk"] - learned_metrics["net_cost_dkk"]
    if improvement <= 0 or learned_metrics["reserve_shortfall_wh"] > 1e-9:
        raise AssertionError("Accepted synthetic cycle was not profitable and physically feasible")
    return {"terminal_only_rejected": not flat_accepted, "credible_peak_accepted": accepted,
            "synthetic_realized_improvement_dkk": improvement,
            "known_charge_wh": sum(s.battery_charge_wh for s in plan.slots[:4]),
            "forecast_discharge_wh": sum(s.battery_discharge_wh for s in plan.slots[4:]),
            "baseline": base_metrics, "learned": learned_metrics}


def terminal_shock_case(*, shock_price: float = 9, known_hours: int = 24) -> dict:
    """Reproduce uncertainty changing current decisions through terminal value.

    The default case represents 42 elapsed days at 3 DKK/kWh, with a genuine
    9 DKK/kWh surprise in the final elapsed hour, followed by 24 flat known hours.
    Set shock_price=3 and known_hours=12 to check the simpler flat-price case.
    No future actual target price is supplied to the learner.
    """
    now = datetime(2026, 3, 9, tzinfo=UTC)
    learner = PricePatternLearner()
    learner.configure("synthetic_terminal_review", "Europe/Copenhagen")
    history = [PriceSlot(now - HOUR * offset, now - HOUR * (offset - 1),
                         shock_price if offset == 1 else 3.0)
               for offset in range(42 * 24, 0, -1)]
    learner.observe(history, now)
    known = [PriceSlot(now + HOUR * offset, now + HOUR * (offset + 1), 3.0, 100)
             for offset in range(known_hours)]
    # Exercise terminal valuation directly. A flat model correctly fails the
    # new skill gate, which must not make this regression check vacuous.
    extended = list(known)
    for offset in range(known_hours, 72):
        extended.append(PriceSlot(now + HOUR * offset, now + HOUR * (offset + 1),
                                  3.0, 100, source="forecast",
                                  uncertainty_dkk_per_kwh=max(.25, abs(shock_price - 3))))
    configuration = settings(5, .75)
    base = optimize(known, now=now, soc=90, settings=configuration)
    learned = optimize(extended, now=now, soc=90, settings=configuration)
    restored = optimize(extended, now=now, soc=90, settings=configuration,
                        terminal_price_floor_dkk_per_kwh=base.terminal_price_dkk_per_kwh)
    result = {"history": f"42days flat3; latest elapsed hour{shock_price}",
              "next_known": f"{known_hours}hours flat3", "initial_soc_pct": 90,
              "forecast_uncertainty": min(slot.uncertainty_dkk_per_kwh
                                           for slot in extended if slot.source == "forecast"),
              "policies": {}}
    for name, plan in (("known", base), ("learned", learned), ("restored_terminal", restored)):
        initial = configuration.capacity_wh * .9
        case = _Case(initial, initial, configuration)
        for decision in plan.slots[:known_hours]:
            _execute(case, decision.action, load_wh=100, hours=1, price=3,
                     target_soc=decision.charge_target_soc or configuration.target_soc,
                     plant=configuration)
        result["policies"][name] = totals(case, 3) | {
            "first_action": plan.current_action.value,
            "terminal_price": plan.terminal_price_dkk_per_kwh,
            "terminal_inventory_price": plan.terminal_inventory_price_dkk_per_kwh,
            "planned_known_discharge_wh": sum(slot.battery_discharge_wh
                                               for slot in plan.slots[:known_hours]),
            "planned_forecast_throughput_wh": sum(slot.battery_charge_wh + slot.battery_discharge_wh
                                                  for slot in plan.slots if slot.price_source == "forecast"),
        }
    result["learned_improvement_dkk"] = (
        result["policies"]["known"]["net_cost_dkk"] - result["policies"]["learned"]["net_cost_dkk"])
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("data", type=Path)
    parser.add_argument("--weeks", default="2026-01-05,2026-03-09,2026-07-06,2026-10-19")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--basis", choices=("spot", "stylized", "both"), default="both")
    parser.add_argument("--profit", type=float, default=.75)
    parser.add_argument("--energy-step", type=float, default=50)
    parser.add_argument("--settlement-price", type=float, default=1)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--allow-terminal-only", action="store_true",
                        help="Disable the production forecast-movement guard for comparison")
    args = parser.parse_args()
    if not 1 <= args.days <= 14 or args.energy_step <= 0 or args.profit < 0 or args.settlement_price < 0:
        parser.error("Require 1–14 days, positive discretization and nonnegative profit/settlement")
    spots = load_hourly(args.data)
    configuration = settings(args.energy_step, args.profit)
    results = []
    for basis in (("spot", "stylized") if args.basis == "both" else (args.basis,)):
        for day in args.weeks.split(","):
            result = replay_week(spots, date.fromisoformat(day), days=args.days, basis=basis,
                                 planner_settings=configuration, settlement=args.settlement_price,
                                 allow_terminal_only=args.allow_terminal_only)
            results.append(result)
            print(json.dumps({"completed": day, "basis": basis,
                              "learned_improvement_dkk": result["learned_improvement_dkk"],
                              "different_actions": result["counts"]["different_actions"]}), flush=True)
    output = {"method": {
        "source": "official DK2 quarter prices aggregated into UTC hours",
        "training": "elapsed actual hours; shadow issues at00/12/14local during up-to90-daywarmup; hourlyevaluation",
        "known_availability": "today; tomorrow added at 14:00 Europe/Copenhagen",
        "load": "shared deterministic profile, perfect load forecasts",
        "plant": "existing replay_model, direct-local mains isolated, initial SOC50% each segment",
        "opportunistic": False, "settlement_dkk_per_kwh": args.settlement_price,
        "forecast_movement_guard": not args.allow_terminal_only,
        "settings": asdict(configuration),
        "limitations": ["hour aggregation and coarser energy steps", "synthetic load and tariff additions",
                        "no captured external forecast comparison", "representative weeks, not annual savings"],
    }, "synthetic_control_checks": synthetic_control_checks(), "results": results}
    if args.output:
        args.output.write_text(json.dumps(output, indent=2))
    else:
        print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
