"""Bounded connected-load history and evidence-gated battery sizing advice."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from math import ceil, isclose, isfinite
from typing import Any

from .models import PlannerSettings, PriceSlot

MINIMUM_DAYS = 28
MINIMUM_WEEKEND_DAYS = 8
RETENTION_DAYS = 90
_MAX_HOURS = RETENTION_DAYS * 24 + 2
_MAX_GAP_SECONDS = 120
_MIN_HOUR_COVERAGE = .90
_MIN_DAY_COVERAGE = .95
_MAX_LATEST_DAY_AGE = 7


def simulate_storage(slots: list[PriceSlot], settings: PlannerSettings) -> dict[str, Any]:
    """Load the sizing solver only when a qualified assessment is requested."""
    from .sizing_model import simulate_storage as simulate

    return simulate(slots, settings)


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if isfinite(number) else None


def _timestamp(value: Any) -> float | None:
    try:
        stamp = datetime.fromisoformat(value)
        return stamp.timestamp() if stamp.tzinfo is not None else None
    except (ValueError, TypeError, OverflowError):
        return None


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, UTC).isoformat()


@dataclass(slots=True)
class SizingHistory:
    """Integrate fresh samples; persisted buckets never preserve continuity."""

    source: str = ""
    _hours: dict[int, dict[str, float]] = field(default_factory=dict, repr=False)
    _last: dict[str, float | None] | None = field(default=None, repr=False)

    def configure(self, source: str) -> None:
        """Reset when connected-load, confirmed-price, or installation scope changes."""
        source = source.strip() if isinstance(source, str) else ""
        if source != self.source:
            self.source = source
            self._hours.clear()
            self._last = None

    def prune(self, now: datetime) -> None:
        if now.tzinfo is None:
            raise ValueError("Sizing timestamps must be timezone-aware")
        timestamp = now.timestamp()
        cutoff = timestamp - RETENTION_DAYS * 86400
        self._hours = dict(sorted(
            (start, values) for start, values in self._hours.items()
            if start >= cutoff and values["covered_until"] <= timestamp + 1e-6
        )[-_MAX_HOURS:])
        if self._last and not 0 <= timestamp - self._last["timestamp"] <= _MAX_GAP_SECONDS:
            self._last = None

    def observe(
        self, now: datetime, *, load_w: float | None, price: float | None,
        price_start: datetime | None = None,
    ) -> bool:
        """Integrate the previous fresh sample, splitting hour and tariff boundaries.

        Return whether a closed hour changed. Invalid samples break continuity;
        gaps over two minutes and the first sample after restart add no energy.
        ``price`` must be the confirmed current import price supplied by the caller.
        """
        if now.tzinfo is None:
            raise ValueError("Sizing timestamps must be timezone-aware")
        if self._last and now.timestamp() <= self._last["timestamp"]:
            self._last = None
            return False
        if self._hours and now.timestamp() < max(v["covered_until"] for v in self._hours.values()):
            self._last = None
            return False
        self.prune(now)
        timestamp = now.timestamp()
        load, tariff = _number(load_w), _number(price)
        boundary = price_start.timestamp() if price_start and price_start.tzinfo else None
        if (not self.source or load is None or load < 0 or tariff is None
                or not -20 <= tariff <= 100
                or price_start is not None and (boundary is None or boundary > timestamp)):
            self._last = None
            return False
        previous = self._last
        self._last = {"timestamp": timestamp, "load_w": load, "price": tariff}
        if previous is None:
            return False
        elapsed = timestamp - previous["timestamp"]
        if not 0 < elapsed <= _MAX_GAP_SECONDS:
            if elapsed <= 0:
                self._last = None
            return False
        left = previous["timestamp"]
        cuts = [left, timestamp]
        next_hour = (int(left // 3600) + 1) * 3600
        if next_hour < timestamp:
            cuts.append(next_hour)
        if boundary is not None and left < boundary < timestamp:
            cuts.append(boundary)
        closed_changed = False
        for start, end in zip(sorted(set(cuts)), sorted(set(cuts))[1:]):
            hour = int(start // 3600) * 3600
            bucket = self._hours.setdefault(hour, {
                "coverage_seconds": 0, "load_wh": 0, "price_seconds": 0,
                "covered_until": start,
            })
            start = max(start, bucket["covered_until"])
            if start >= end:
                continue
            seconds = end - start
            applied_price = tariff if boundary is not None and start >= boundary else previous["price"]
            bucket["coverage_seconds"] += seconds
            bucket["load_wh"] += previous["load_w"] * seconds / 3600
            bucket["price_seconds"] += applied_price * seconds
            bucket["covered_until"] = end
            closed_changed |= hour + 3600 <= timestamp
        return closed_changed

    def as_dict(self) -> dict[str, Any]:
        return {"source": self.source, "hours": [
            {"start": _iso(start), **{key: value for key, value in values.items()
                                      if key != "covered_until"},
             "covered_until": _iso(values["covered_until"])}
            for start, values in sorted(self._hours.items())[-_MAX_HOURS:]
        ]}

    @classmethod
    def from_dict(cls, data: Any) -> SizingHistory:
        model = cls()
        if not isinstance(data, dict):
            return model
        model.configure(data.get("source"))
        rows = data.get("hours")
        if not model.source or not isinstance(rows, list):
            return model
        for row in rows[-_MAX_HOURS:]:
            if not isinstance(row, dict):
                continue
            start = _timestamp(row.get("start"))
            coverage, load, weighted = (_number(row.get(key)) for key in (
                "coverage_seconds", "load_wh", "price_seconds",
            ))
            if (start is None or start % 3600 or start in model._hours
                    or coverage is None or not 0 < coverage <= 3600
                    or load is None or load < 0 or weighted is None
                    or not -20 <= weighted / coverage <= 100):
                continue
            until = _timestamp(row.get("covered_until")) if "covered_until" in row else start + 3600
            if until is None or not start + coverage <= until <= start + 3600:
                continue
            model._hours[int(start)] = {
                "coverage_seconds": coverage, "load_wh": load,
                "price_seconds": weighted, "covered_until": until,
            }
        return model

    def completed_slots(self, now: datetime) -> tuple[list[PriceSlot], dict[str, Any]]:
        """Select recent covered local days, extending for sufficient weekend evidence."""
        self.prune(now)
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        complete_end = midnight.timestamp()
        dates = sorted({datetime.fromtimestamp(start, now.tzinfo).date()
                        for start in self._hours if start < complete_end})
        valid = []
        for day in dates:
            begin = datetime.combine(day, datetime.min.time(), now.tzinfo)
            end = datetime.combine(day + timedelta(days=1), datetime.min.time(), now.tzinfo)
            first_hour = int(begin.timestamp() // 3600) * 3600
            hours = [
                (hour, max(hour, begin.timestamp()), min(hour + 3600, end.timestamp()))
                for hour in range(first_hour, ceil(end.timestamp() / 3600) * 3600, 3600)
            ]
            if (not hours or any(hour not in self._hours for hour, _, _ in hours)
                    or any(self._hours[hour]["coverage_seconds"] < 3600 * _MIN_HOUR_COVERAGE
                           for hour, _, _ in hours)):
                continue
            covered = sum(self._hours[hour]["coverage_seconds"] / 3600 * (right - left)
                          for hour, left, right in hours)
            expected = end.timestamp() - begin.timestamp()
            if covered + 1e-6 >= expected * _MIN_DAY_COVERAGE:
                valid.append((day, hours, covered))
        first = max(0, len(valid) - MINIMUM_DAYS)
        weekends = sum(day.weekday() >= 5 for day, _, _ in valid[first:])
        while first and weekends < MINIMUM_WEEKEND_DAYS:
            first -= 1
            weekends += valid[first][0].weekday() >= 5
        selected = valid[first:]
        slots = []
        for _, hours, _ in selected:
            for hour, left, right in hours:
                values = self._hours[hour]
                coverage = values["coverage_seconds"]
                slots.append(PriceSlot(
                    datetime.fromtimestamp(left, UTC), datetime.fromtimestamp(right, UTC),
                    values["price_seconds"] / coverage,
                    expected_load_wh=values["load_wh"] * (right - left) / coverage,
                ))
        expected = sum(right - left for _, hours, _ in selected for _, left, right in hours)
        covered = sum(seconds for _, _, seconds in selected)
        latest = selected[-1][0] if selected else None
        return slots, {
            "day_count": len(selected), "available_valid_days": len(valid),
            "weekend_days": sum(day.weekday() >= 5 for day, _, _ in selected),
            "coverage_pct": covered / expected * 100 if expected else 0,
            "span_days": (latest - selected[0][0]).days + 1 if selected else 0,
            "period_start": slots[0].start.isoformat() if slots else None,
            "period_end": slots[-1].end.isoformat() if slots else None,
            "latest_day": latest.isoformat() if latest else None,
            "stale": latest is None or (now.date() - latest).days > _MAX_LATEST_DAY_AGE,
            "timezone": str(now.tzinfo), "recorded_hours": len(self._hours),
            "required_days": MINIMUM_DAYS, "required_weekend_days": MINIMUM_WEEKEND_DAYS,
        }


def sizing_prerequisites(
    settings: PlannerSettings, *, evidence: dict[str, Any], efficiency_summary: dict[str, Any],
) -> str | None:
    """Cheap shared gate for scheduling, invalidating and explaining an assessment."""
    if any(_number(getattr(settings, name)) is None for name in (
        "capacity_wh", "reserve_soc", "target_soc", "charge_power_w", "discharge_power_w",
        "round_trip_efficiency", "degradation_cost_dkk_per_kwh", "switching_penalty_dkk",
    )) or settings.switching_penalty_dkk < 0:
        return "Battery planning settings are invalid"
    measured = _number(efficiency_summary.get("planning_efficiency_pct"))
    samples = efficiency_summary.get("sample_count")
    source = efficiency_summary.get("source")
    if (not isinstance(source, str) or not source.strip()
            or efficiency_summary.get("method") != "matched_soc_grid_meter"
            or efficiency_summary.get("status") != "ready"
            or isinstance(samples, bool) or not isinstance(samples, int) or samples < 3
            or measured is None or not 50 <= measured <= 98
            or not isclose(settings.round_trip_efficiency, measured / 100, rel_tol=0, abs_tol=1e-6)):
        return "Awaiting three fresh independent matched-SOC efficiency cycles applied to planning"
    try:
        settings.validate()
    except ValueError:
        return "Battery planning settings are invalid"
    if (_number(evidence.get("day_count")) or 0) < MINIMUM_DAYS:
        return "Awaiting 28 completed local days with sufficient load and confirmed-price coverage"
    if (_number(evidence.get("weekend_days")) or 0) < MINIMUM_WEEKEND_DAYS:
        return "Awaiting at least eight covered weekend days for a representative capacity comparison"
    coverage = _number(evidence.get("coverage_pct"))
    span = _number(evidence.get("span_days"))
    if coverage is None or not 95 <= coverage <= 100.000001 or span is None or not 28 <= span <= RETENTION_DAYS:
        return "Sizing history has insufficient coverage or exceeds the 90-day evidence window"
    if evidence.get("stale") is not False:
        return "Sizing history needs a covered completed day within the last seven days"
    return None


def assess_sizing(
    slots: list[PriceSlot], settings: PlannerSettings, *, evidence: dict[str, Any],
    efficiency_summary: dict[str, Any], expansion_capacity_kwh: float,
    expansion_cost_dkk: float, payback_target_years: float,
) -> dict[str, Any]:
    """Compare capacity using hourly hindsight with closed stored-energy balances."""
    result: dict[str, Any] = {
        "state": "learning", "reason": "Awaiting qualified sizing evidence",
        "evidence": dict(evidence), "period_days": evidence.get("day_count", 0),
        "coverage_pct": evidence.get("coverage_pct", 0), "capacity_cases": {},
        "added_annualized_savings_dkk": None, "payback_years": None,
        "expansion_capacity_kwh": expansion_capacity_kwh, "expansion_cost_dkk": expansion_cost_dkk,
        "payback_target_years": payback_target_years,
        "method": "hourly_closed_soc_hindsight",
        "assumptions": [
            "Hourly hindsight estimate using confirmed import prices and observed connected load",
            "Qualified hours use time-weighted observed means; coverage is reported separately",
            "Partial UTC hours at local midnight use the source hour's observed mean load and price",
            "All capacities use the same SOC limits and measured round-trip efficiency",
            "Each continuous segment starts and ends at reserve; no free starting energy",
            "Annualized recent-period savings are a run rate, not a seasonal annual forecast",
            "Configured battery wear and estimated switching costs are included",
            "Feasible schedules include approximate switching-cost optimization, not a proof of global optimality",
        ],
    }
    reason = sizing_prerequisites(settings, evidence=evidence, efficiency_summary=efficiency_summary)
    values = [_number(value) for value in (expansion_capacity_kwh, expansion_cost_dkk, payback_target_years)]
    if reason is None and (any(value is None for value in values)
                           or values[0] <= 0 or values[1] < 0 or values[2] <= 0):
        reason = "A positive expansion capacity and payback target, and nonnegative purchase cost, are required"
    if reason is None and (not slots or sum(slot.hours for slot in slots) < 23 * MINIMUM_DAYS):
        reason = "The qualified history does not contain enough hourly load and price evidence"
    if reason:
        result["reason"] = reason
        return result
    cases = {}
    incumbent = None
    for name, capacity in (
        ("half", settings.capacity_wh / 2), ("current", settings.capacity_wh),
        ("expanded", settings.capacity_wh + expansion_capacity_kwh * 1000),
    ):
        modeled = simulate_storage(slots, replace(settings, capacity_wh=capacity))
        if _number(modeled.get("net_savings_dkk")) is None:
            result["reason"] = "The capacity model could not produce a finite net-savings estimate"
            return result
        candidate = {**modeled, "schedule_capacity_kwh": capacity / 1000,
                     "schedule_source": name}
        # The solver estimates fixed switching costs after finding its flow.
        # A smaller capacity's entire schedule remains feasible with more
        # storage above the same reserve. Keep that feasible incumbent whenever
        # the approximate candidate is worse, retaining all cash/energy fields.
        if incumbent is not None and candidate["net_savings_dkk"] < incumbent["net_savings_dkk"]:
            candidate = dict(incumbent)
        cases[name] = {**candidate, "capacity_kwh": capacity / 1000}
        incumbent = cases[name]
    days = evidence["day_count"]
    current, half, expanded = (cases[name]["net_savings_dkk"] for name in ("current", "half", "expanded"))
    extra = (expanded - current) * 365 / days
    forgone = max(0.0, current - half) * 365 / days
    retained = half / current if current > 0 else None
    if extra > 0 and extra + 1e-9 >= expansion_cost_dkk / payback_target_years:
        state = "bigger_battery_better"
        reason = "The added pack clears the selected payback target in this recent-period hindsight estimate"
    elif current / days >= .1 and retained >= .95 and forgone <= 50:
        state = "battery_too_big"
        reason = "Half the capacity retains at least 95% of meaningful net savings and forgoes at most 50 DKK/year"
    else:
        state = "size_is_fine"
        reason = "The current capacity has no demonstrated need to increase or halve under the selected thresholds"
    result.update({
        "state": state, "reason": reason, "capacity_cases": cases,
        "added_annualized_savings_dkk": extra,
        "payback_years": expansion_cost_dkk / extra if extra > 0 else None,
        "half_capacity_savings_retained_pct": retained * 100 if retained is not None else None,
        "half_capacity_forgone_annualized_savings_dkk": forgone,
    })
    return result
