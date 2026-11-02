"""Summarize official DK2 quarter-hour prices; no third-party dependencies.

Example: python3 scripts/analyze_dk2_prices.py --start 2025-11-01 --end 2026-11-01
Dates use Danish local time, with end excluded. Raw downloads are not saved.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta
import json
from math import isfinite
from pathlib import Path
from statistics import mean
from urllib.parse import urlencode
from urllib.request import urlopen
from zoneinfo import ZoneInfo

TIME_ZONE = ZoneInfo("Europe/Copenhagen")
DATASET = "https://api.energidataservice.dk/dataset/DayAheadPrices"


def summarize(payload: dict, start: date, end: date) -> dict:
    points = {}
    for row in payload["records"]:
        if row["PriceArea"] != "DK2":
            continue
        utc = datetime.fromisoformat(row["TimeUTC"]).replace(tzinfo=UTC)
        local = utc.astimezone(TIME_ZONE)
        price = float(row["DayAheadPriceDKK"]) / 1000
        if start <= local.date() < end:
            if not isfinite(price) or utc in points:
                raise ValueError("Nonfinite or duplicate prices")
            points[utc] = (local, price)
    if not points:
        raise ValueError("No DK2 records in the requested period")
    ordered = sorted(points)
    if any(right - left != timedelta(minutes=15)
           for left, right in zip(ordered, ordered[1:])):
        raise ValueError("Missing or unexpected quarter-hour intervals")
    if (ordered[0] != datetime.combine(start, time(), TIME_ZONE).astimezone(UTC)
            or ordered[-1] + timedelta(minutes=15)
            != datetime.combine(end, time(), TIME_ZONE).astimezone(UTC)):
        raise ValueError("The requested date range is incomplete")
    samples = list(points.values())
    days = defaultdict(list)
    for local, price in samples:
        days[local.date()].append((local, price))
    weeks = defaultdict(list)
    for day in days:
        weeks[day.isocalendar()[:2]].append(day)
    complete_weeks = [week for week in weeks.values() if len(week) == 7]
    cheaper_weekends = sum(
        mean(price for day in week if day.weekday() >= 5 for _, price in days[day])
        < mean(price for day in week if day.weekday() < 5 for _, price in days[day])
        for week in complete_weeks
    )

    # Chronological, day-ahead replay. Fit once at local midnight with only
    # previous dates, giving eight independent dates per weekday/quarter bucket.
    # Repeated fall-back quarters are averaged for that date's training weight.
    buckets = defaultdict(list)
    day_totals = []
    baseline_errors, profile_errors = [], []
    for day, observations in sorted(days.items()):
        previous = [price for old_day, values in day_totals
                    if day - timedelta(days=7) <= old_day < day for price in values]
        if day >= start + timedelta(days=56) and previous:
            baseline = mean(previous)
            for local, price in observations:
                key = (local.weekday(), local.hour * 4 + local.minute // 15)
                history = [value for old_day, value in buckets[key]
                           if day - timedelta(days=56) <= old_day < day]
                if len(history) >= 8:
                    baseline_errors.append(abs(price - baseline))
                    profile_errors.append(abs(price - mean(history)))
        by_quarter = defaultdict(list)
        for local, price in observations:
            by_quarter[(local.weekday(), local.hour * 4 + local.minute // 15)].append(price)
        for key, values in by_quarter.items():
            buckets[key].append((day, mean(values)))
        day_totals.append((day, [price for _, price in observations]))

    def average(predicate) -> float | None:
        selected = [price for local, price in samples if predicate(local)]
        return round(mean(selected), 4) if selected else None

    return {
        "period": {"start_inclusive": str(start), "end_exclusive": str(end)},
        "source": DATASET,
        "timezone": TIME_ZONE.key,
        "unit": "DKK/kWh spot, excluding VAT, tariffs and supplier additions",
        "quarter_hours": len(samples),
        "local_days": len(days),
        "mean": average(lambda _: True),
        "weekday_mean": average(lambda t: t.weekday() < 5),
        "weekend_mean": average(lambda t: t.weekday() >= 5),
        "complete_iso_weeks": len(complete_weeks),
        "weekends_below_same_week_weekdays": cheaper_weekends,
        "weekday_means_monday_first": [average(lambda t: t.weekday() == d) for d in range(7)],
        "hour_means": {str(h): average(lambda t: t.hour == h) for h in range(24)},
        "season_hour_means": {
            season: {str(h): average(lambda t: t.month in months and t.hour == h)
                     for h in range(24)}
            for season, months in (("oct_mar", (10, 11, 12, 1, 2, 3)),
                                   ("apr_sep", (4, 5, 6, 7, 8, 9)))
        },
        "monthly_means": {month: average(lambda t: t.strftime("%Y-%m") == month)
                          for month in sorted({t.strftime("%Y-%m") for t, _ in samples})},
        "day_ahead_backtest": {
            "profile": "mean of prior 8 weeks, same local weekday and quarter",
            "baseline": "flat mean of previous 7 complete local days",
            "scored_quarters": len(profile_errors),
            "profile_mae": round(mean(profile_errors), 4) if profile_errors else None,
            "baseline_mae": round(mean(baseline_errors), 4) if baseline_errors else None,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--input", type=Path, help="Read an existing official API JSON response")
    args = parser.parse_args()
    if not date(2025, 10, 1) <= args.start < args.end:
        parser.error("Expected 2025-10-01 <= start < end for the quarter-hour dataset")
    try:
        if args.input:
            payload = json.loads(args.input.read_text())
        else:
            params = {"start": str(args.start), "end": str(args.end),
                      "filter": json.dumps({"PriceArea": ["DK2"]}),
                      "sort": "TimeUTC", "limit": 0}
            with urlopen(DATASET + "?" + urlencode(params), timeout=60) as response:
                payload = json.load(response)
        print(json.dumps(summarize(payload, args.start, args.end), indent=2, allow_nan=False))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
