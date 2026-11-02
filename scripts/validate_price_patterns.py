"""Walk-forward validation of the actual pattern learner on official DK2 data.

Run with .venv/bin/python scripts/validate_price_patterns.py --input <API JSON>.
At each Danish midnight, only elapsed observations train the model. The current
local day's published prices form the known prefix; the learned tail is scored
at 24-72 elapsed hours. No weather-aware external forecast archive is available.
Prices are spot DKK/kWh, excluding VAT, tariffs and supplier additions.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta
import json
from math import exp, isfinite, log, sqrt
from pathlib import Path
from statistics import mean, median
import sys
from typing import Any
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "custom_components"))

from house_battery.models import PriceSlot
from house_battery.price_patterns import PricePatternLearner

ZONE = ZoneInfo("Europe/Copenhagen")
QUARTER = timedelta(minutes=15)
HOUR = timedelta(hours=1)
MODELS = (
    "current_qualified_pattern_model", "flat_previous_7d_mean",
    "previous_week_same_quarter", "previous_56d_weekday_quarter_mean",
    "previous_28d_weekday_weekend_hour_median",
    "previous_56d_weekday_hour_mean_21d_half_life",
    "known_prefix_level_adjusted_actual_profile",
)


def load_prices(payload: dict[str, Any]) -> list[PriceSlot]:
    """Validate and normalize official quarter-hour DK2 records."""
    by_time = {}
    for row in payload["records"]:
        if row["PriceArea"] != "DK2":
            continue
        stamp = datetime.fromisoformat(row["TimeUTC"])
        stamp = stamp.replace(tzinfo=UTC) if stamp.tzinfo is None else stamp.astimezone(UTC)
        price = float(row["DayAheadPriceDKK"]) / 1000
        if not isfinite(price) or stamp in by_time:
            raise ValueError("Non-finite or duplicate DK2 price")
        by_time[stamp] = price
    times = sorted(by_time)
    if not times or any(b - a != QUARTER for a, b in zip(times, times[1:])):
        raise ValueError("DK2 input must be a contiguous nonempty quarter-hour series")
    first, last = times[0].astimezone(ZONE), (times[-1] + QUARTER).astimezone(ZONE)
    if first.timetz().replace(tzinfo=None) != time() or last.timetz().replace(tzinfo=None) != time():
        raise ValueError("Input must start and end at Danish local midnight")
    return [PriceSlot(stamp, stamp + QUARTER, by_time[stamp]) for stamp in times]


def _quantile(values: list[float], proportion: float) -> float:
    return sorted(values)[int((len(values) - 1) * proportion)]


def _hourly(quarters: list[PriceSlot]) -> list[PriceSlot]:
    buckets = defaultdict(list)
    for slot in quarters:
        buckets[slot.start.replace(minute=0)].append(slot)
    return [PriceSlot(start, start + HOUR, mean(q.price for q in values))
            for start, values in sorted(buckets.items()) if len(values) == 4]


def _metrics(rows: list[dict[str, Any]], expensive_cutoff: float) -> dict[str, Any]:
    result = {}
    for model in MODELS:
        errors = [row[model] - row["actual"] for row in rows]
        expensive = [row[model] - row["actual"] for row in rows
                     if row["actual"] >= expensive_cutoff]
        result[model] = {
            "forecast_instances": len(errors),
            "mae": mean(abs(error) for error in errors),
            "rmse": sqrt(mean(error * error for error in errors)),
            "bias_forecast_minus_actual": mean(errors),
            "p90_absolute_error": _quantile([abs(error) for error in errors], .9),
            "expensive_top_decile_mae": mean(abs(error) for error in expensive) if expensive else None,
            "expensive_top_decile_bias": mean(expensive) if expensive else None,
        }
    errors = [abs(row[MODELS[0]] - row["actual"]) for row in rows]
    result[MODELS[0]].update({
        "within_model_uncertainty_pct": 100 * mean(
            error <= row["uncertainty"] for row, error in zip(rows, errors)
        ),
        "mean_model_uncertainty": mean(row["uncertainty"] for row in rows),
        "median_model_uncertainty": median(row["uncertainty"] for row in rows),
    })
    return result


def validate(quarters: list[PriceSlot], *, warmup_days: int = 56) -> dict[str, Any]:
    """Score matched forecast instances without observing any target actuals."""
    if warmup_days < 28:
        raise ValueError("Use at least 28 warmup days")
    hourly = _hourly(quarters)
    actual = {slot.start: slot.price for slot in quarters}
    days = defaultdict(list)
    for slot in quarters:
        days[slot.start.astimezone(ZONE).date()].append(slot)
    learner = PricePatternLearner()
    learner.configure("official_DK2_spot_validation", ZONE.key)
    first_day = min(days)
    rows, issues = [], []
    first_ready_issue = None
    eligible_instances = 0
    for day, known in sorted(days.items()):
        now = datetime.combine(day, time(), ZONE).astimezone(UTC)
        if day - timedelta(days=1) in days:
            learner.observe(days[day - timedelta(days=1)], now)
        summary = learner.summary(now)
        if first_ready_issue is None and summary["status"] == "ready":
            first_ready_issue = now.isoformat()
        if day < first_day + timedelta(days=warmup_days):
            continue
        history = [slot for slot in hourly if now - timedelta(days=90) <= slot.start < now]
        profile = learner._profile(now)
        flat = mean(slot.price for slot in history if slot.start >= now - timedelta(days=7))
        grouped, weighted, quarter_profile = defaultdict(dict), defaultdict(dict), defaultdict(dict)
        for slot in history:
            local = slot.start.astimezone(ZONE)
            if slot.start >= now - timedelta(days=28):
                grouped[local.weekday() >= 5, local.hour].setdefault(local.date(), []).append(slot.price)
            if slot.start >= now - timedelta(days=56):
                weighted[local.weekday(), local.hour].setdefault(local.date(), []).append(slot.price)
        for slot in quarters:
            if not now - timedelta(days=56) <= slot.start < now:
                continue
            local = slot.start.astimezone(ZONE)
            quarter_profile[local.weekday(), local.hour, local.minute].setdefault(local.date(), []).append(slot.price)
        # Published current-day prices are already available at midnight. They
        # may be features, but never enter actual learner training before elapsed.
        prefix_residuals = []
        for slot in known:
            prediction = learner._prediction(slot.start, profile)
            if prediction:
                prefix_residuals.append(slot.price - prediction[0])
        level_adjustment = median(prefix_residuals) if prefix_residuals else 0.0
        forecast = learner.extend(known, now, .25)[len(known):]
        issue_rows = 0
        target = max(now + timedelta(hours=24), known[-1].end)
        while target < now + timedelta(hours=72):
            eligible_instances += target in actual
            target += QUARTER
        for slot in forecast:
            target = slot.start
            while target < slot.end:
                lead = (target - now).total_seconds() / 3600
                if not 24 <= lead < 72 or target not in actual:
                    target += QUARTER
                    continue
                local = target.astimezone(ZONE)
                candidates = quarter_profile.get((local.weekday(), local.hour, local.minute), {})
                grouped_values = grouped.get((local.weekday() >= 5, local.hour), {})
                weighted_values = weighted.get((local.weekday(), local.hour), {})
                # Calendar persistence uses the same Danish wall-clock quarter,
                # rather than shifting a fixed168 UTC hours across a DST change.
                last_week = actual.get((local - timedelta(days=7)).astimezone(UTC))
                if last_week is None or not candidates or not grouped_values or not weighted_values:
                    target += QUARTER
                    continue
                numerator = denominator = 0.0
                for old_day, values in weighted_values.items():
                    age = (day - old_day).days
                    weight = exp(-log(2) * age / 21)
                    numerator += weight * mean(values)
                    denominator += weight
                row = {
                    "issue": now.isoformat(), "target": target.isoformat(),
                    "actual": actual[target], "uncertainty": slot.uncertainty_dkk_per_kwh,
                    "lead_band": "24_48h" if lead < 48 else "48_72h",
                    "season": "apr_sep" if 4 <= local.month <= 9 else "oct_mar",
                    "day_type": "weekend" if local.weekday() >= 5 else "weekday",
                    "month": local.strftime("%Y-%m"),
                }
                row.update(zip(MODELS, (
                    slot.price, flat, last_week,
                    mean(mean(v) for v in candidates.values()),
                    median(mean(v) for v in grouped_values.values()),
                    numerator / denominator, slot.price + level_adjustment,
                )))
                rows.append(row)
                issue_rows += 1
                target += QUARTER
        issues.append({"issue": now.isoformat(), "scored_quarters": issue_rows,
                       "learned_hours": len(forecast),
                       "reported_chronological_hour_mae": summary["validation_mae_dkk_per_kwh"]})
    if not rows:
        raise ValueError("No qualified matched forecasts; provide enough history")
    expensive_cutoff = _quantile([row["actual"] for row in rows], .9)
    hourly_rows = defaultdict(list)
    for row in rows:
        target = datetime.fromisoformat(row["target"]).replace(minute=0)
        hourly_rows[row["issue"], target].append(row)
    return {
        "source": "https://www.energidataservice.dk/tso-electricity/DayAheadPrices",
        "period": {"start": str(first_day), "last_local_date": str(max(days))},
        "input_quarters": len(quarters), "warmup_days": warmup_days,
        "unit": "DKK/kWh spot, excluding VAT, taxes, tariffs and supplier additions",
        "method": "Daily Danish-midnight issue; current local day known; only elapsed prior days train; score learned tail at24-72h, matched issues/targets across candidates",
        "forecast_issue_count": len(issues), "eligible_quarter_instances": eligible_instances,
        "first_fully_ready_issue": first_ready_issue,
        "matched_quarter_instances": len(rows),
        "forecast_instance_coverage_pct": 100 * len(rows) / eligible_instances,
        "expensive_top_decile_cutoff": expensive_cutoff,
        "overall": _metrics(rows, expensive_cutoff),
        "by_group": {
            group: {value: _metrics([row for row in rows if row[group] == value], expensive_cutoff)
                    for value in sorted({row[group] for row in rows})}
            for group in ("lead_band", "season", "day_type", "month")
        },
        "actual_model_hourly_target_mae": mean(
            abs(mean(r[MODELS[0]] for r in values) - mean(r["actual"] for r in values))
            for values in hourly_rows.values() if len(values) == 4
        ),
        "mean_reported_chronological_hour_mae": mean(
            issue["reported_chronological_hour_mae"] for issue in issues
            if issue["reported_chronological_hour_mae"] is not None
        ),
        "limitations": [
            "Spot-only data, not installation-specific all-in prices.",
            "No archived external weather-aware forecasts; cannot prove incremental benefit over them.",
            "Overlapping lead targets are forecast instances, not independent statistical samples.",
            "Candidates exploratory on this single year; require held-out validation before selection.",
            "Forecast MAE is not battery-economic savings; evaluate actual dispatch separately.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--warmup-days", type=int, default=56)
    args = parser.parse_args()
    try:
        output = validate(load_prices(json.loads(args.input.read_text())), warmup_days=args.warmup_days)
        print(json.dumps(output, indent=2, allow_nan=False))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
