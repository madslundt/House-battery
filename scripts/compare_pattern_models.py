"""Compare a predeclared candidate pool before a locked chronological holdout.

Input is the official DK2 API JSON used by validate_price_patterns.py. Prices
are spot DKK/kWh, not an installation tariff. Earlier exploratory work has seen
this year: the split prevents algorithmic lookahead, not human research reuse.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import UTC, date, datetime, time, timedelta
import json
from pathlib import Path
from statistics import mean
import sys
from typing import Any
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "custom_components"))

from house_battery.pattern_models import BASELINE, CONTROL_MODEL, MODEL_NAMES, build_profiles, lead_band
from house_battery.price_patterns import PricePatternLearner
from validate_price_patterns import load_prices

ZONE = ZoneInfo("Europe/Copenhagen")
HOUR = timedelta(hours=1)
QUARTER = timedelta(minutes=15)


def _metrics(rows: list[dict[str, Any]], model: str, expensive: float) -> dict[str, Any]:
    errors = [row["predictions"][model] - row["actual"] for row in rows]
    peak = [row["predictions"][model] - row["actual"] for row in rows if row["actual"] >= expensive]
    return {"instances": len(rows), "mae": mean(map(abs, errors)),
            "bias": mean(errors), "expensive_mae": mean(map(abs, peak)) if peak else None,
            "expensive_bias": mean(peak) if peak else None}


def _adaptive(rows: list[dict[str, Any]], expensive: float) -> dict[str, Any]:
    active = [row for row in rows if row["active_price"] is not None]
    if not active:
        return {"active_instances": 0, "coverage_pct": 0}
    errors = [row["active_price"] - row["actual"] for row in active]
    return {"active_instances": len(active), "coverage_pct": len(active) / len(rows) * 100,
            "mae_when_active": mean(map(abs, errors)),
            "baseline_mae_on_same_instances": _metrics(active, BASELINE, expensive)["mae"],
            "flat_mae_on_same_instances": _metrics(active, "flat_7d", expensive)["mae"],
            "uncertainty_coverage_pct": mean(abs(error) <= row["uncertainty"]
                                             for error, row in zip(errors, active)) * 100,
            "mean_uncertainty": mean(row["uncertainty"] for row in active),
            "selected_counts": dict(Counter(row["selected"] for row in active))}


def normalize_prices(payload: dict) -> tuple[list, str]:
    """Accept official quarter data or retain the meaning of native hourly data."""
    records = payload.get("records", [])
    if not records or "HourUTC" not in records[0]:
        return load_prices(payload), "native_quarter_hour"
    expanded = []
    for row in records:
        start = datetime.fromisoformat(row["HourUTC"])
        start = start.replace(tzinfo=UTC) if start.tzinfo is None else start.astimezone(UTC)
        for quarter in range(4):
            expanded.append({"TimeUTC": (start + quarter * QUARTER).isoformat(),
                             "DayAheadPriceDKK": row["SpotPriceDKK"], "PriceArea": row["PriceArea"]})
    return load_prices({"records": expanded}), "native_hourly_repeated_for_common_scoring_no_intrahour_information"


def compare(
    payload: dict, cutoff: date = date(2026, 8, 1), locked_model: str | None = None,
) -> dict:
    """Lock fixed-model selection using earlier target dates, then score later issues."""
    quarters, resolution = normalize_prices(payload)
    by_day: dict[date, list] = defaultdict(list)
    for slot in quarters:
        by_day[slot.start.astimezone(ZONE).date()].append(slot)
    actual = {slot.start: slot.price for slot in quarters}
    learner = PricePatternLearner()
    learner.configure("official_dk2_locked_comparison", ZONE.key)
    first = min(by_day)
    cutoff_time = datetime.combine(cutoff, time(), ZONE).astimezone(UTC)
    rows = []
    for day, known in sorted(by_day.items()):
        now = datetime.combine(day, time(), ZONE).astimezone(UTC)
        if day - timedelta(days=1) in by_day:
            learner.observe(by_day[day - timedelta(days=1)], now)
        learner.shadow_issue(known, now)
        if day < first + timedelta(days=56):
            continue
        profiles = build_profiles(learner.samples, now, ZONE.key)
        active = learner.extend(known, now, .25)[len(known):]
        active_by_hour = {slot.start: slot for slot in active}
        selections = {band: learner.evidence.select(band, now, ZONE.key)
                      for band in ("0_24h", "24_48h", "48_72h")}
        target = known[-1].end
        while target < now + timedelta(hours=72):
            predictions = {model: value[0] for model in MODEL_NAMES
                           if (value := profiles.predict(model, target)) is not None}
            if set(predictions) != set(MODEL_NAMES):
                target += HOUR
                continue
            selected = selections[lead_band(now, target)]
            controlled = active_by_hour.get(target)
            for quarter in range(4):
                stamp = target + quarter * QUARTER
                if stamp not in actual or stamp < now + timedelta(hours=24):
                    continue
                local = stamp.astimezone(ZONE)
                rows.append({"issue": now, "target": stamp, "actual": actual[stamp],
                             "predictions": predictions, "selected": selected,
                             "active_price": controlled.price if controlled else None,
                             "uncertainty": controlled.uncertainty_dkk_per_kwh if controlled else None,
                             "lead_band": lead_band(now, stamp),
                             "day_type": "weekend" if local.weekday() >= 5 else "weekday",
                             "season": "apr_sep" if 4 <= local.month <= 9 else "oct_mar"})
            target += HOUR
    # Keep the economic evaluation's July window out of forecast-model selection.
    excluded = {date(2026, 7, day) for day in range(20, 24)}
    validation = [row for row in rows if row["target"] < cutoff_time
                  and row["target"].astimezone(ZONE).date() not in excluded]
    holdout = [row for row in rows if row["issue"] >= cutoff_time]
    if not validation or not holdout:
        raise ValueError("Need earlier validation and later holdout dates")
    validation_maes = {model: _metrics(validation, model, float("inf"))["mae"] for model in MODEL_NAMES}
    locked = locked_model or min(MODEL_NAMES, key=lambda model: (validation_maes[model], MODEL_NAMES.index(model)))
    prices = sorted(row["actual"] for row in validation)
    expensive = prices[int(.9 * (len(prices) - 1))]
    return {
        "source": ("https://www.energidataservice.dk/tso-electricity/Elspotprices"
                   if resolution.startswith("native_hourly") else
                   "https://www.energidataservice.dk/tso-electricity/DayAheadPrices"),
        "native_resolution": resolution,
        "pool_declared_before_this_run": list(MODEL_NAMES),
        "production_control_model": CONTROL_MODEL,
        "production_policy": "fixed28day_daytypehour_with_strict_issued_skill_gate; other candidates shadow only",
        "split": {"warmup_days": 56, "selection_targets_before": str(cutoff),
                  "selection_excluded_dates": [str(day) for day in sorted(excluded)],
                  "heldout_issues_from": str(cutoff), "last_day": str(max(by_day))},
        "unit": "DKK/kWh spot; excludes VAT, network tariffs and supplier additions",
        "selection_maes": validation_maes, "locked_fixed_model": locked,
        "fixed_model_selected_elsewhere": locked_model is not None,
        "heldout": {"instances": len(holdout), "candidate_metrics": {
            model: _metrics(holdout, model, expensive) for model in MODEL_NAMES},
            "skill_gated_control": _adaptive(holdout, expensive)},
        "heldout_groups": {group: {value: {
            "locked": _metrics([row for row in holdout if row[group] == value], locked, expensive),
            "old_baseline": _metrics([row for row in holdout if row[group] == value], BASELINE, expensive),
            "skill_gated_control": _adaptive([row for row in holdout if row[group] == value], expensive)}
            for value in sorted({row[group] for row in holdout})}
            for group in ("lead_band", "day_type", "season")},
        "final_learner": learner.summary(max(slot.end for slot in quarters)),
        "limitations": ["Same year previously examined during exploratory research; not a pristine unseen market year.",
                        "Overlapping lead targets are forecast instances, not independent observations.",
                        "Online model selection uses only previously resolved targets, including during holdout.",
                        "No external forecast archive, actual household load, or battery economics in this comparison.",
                        "Strict activation reports coverage and paired errors on identical active targets."],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--cutoff", type=date.fromisoformat, default=date(2026, 8, 1))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--locked-model", choices=MODEL_NAMES,
                        help="Evaluate a model chosen on another dataset without reselecting it")
    args = parser.parse_args()
    result = compare(json.loads(args.input.read_text()), args.cutoff, args.locked_model)
    output = json.dumps(result, indent=2, allow_nan=False)
    if args.output:
        args.output.write_text(output)
    else:
        print(output)


if __name__ == "__main__":
    main()
