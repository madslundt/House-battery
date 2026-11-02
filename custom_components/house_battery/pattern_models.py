"""Small price-model candidates and causal, paired champion comparisons."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from math import isfinite
from statistics import mean, median
from typing import Any
from zoneinfo import ZoneInfo

BASELINE = "weekday_90d"
CONTROL_MODEL = "day_type_28d"
MODEL_NAMES = (BASELINE, "day_type_28d", "day_type_14d", "weighted_day_type_56d", "flat_7d")
LEAD_BANDS = ("0_24h", "24_48h", "48_72h")
_WINDOWS = dict(zip(MODEL_NAMES, (90, 28, 14, 56, 7)))
_HOUR = timedelta(hours=1)


def lead_band(issued: datetime, target: datetime) -> str:
    """Identify elapsed-time lead without losing repeated local DST hours."""
    hours = (target.astimezone(UTC) - issued.astimezone(UTC)).total_seconds() / 3600
    return LEAD_BANDS[0 if hours < 24 else 1 if hours < 48 else 2]


def quantile(values: list[float], proportion: float) -> float:
    """An empirical prediction-error quantile, not a standard error of a mean."""
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * proportion))]


@dataclass(slots=True)
class CandidateProfiles:
    """Profiles collapse repeated DST hours into one independent local date."""

    issued: datetime
    zone: ZoneInfo
    values: dict[str, dict[tuple[int, int], dict[str, list[float]]]]
    _cache: dict[tuple[str, tuple[int, int]], tuple[float, list[float]] | None] = field(default_factory=dict)

    def predict(self, model: str, target: datetime) -> tuple[float, list[float]] | None:
        """Return the candidate expectation and its observed price dispersion."""
        local = target.astimezone(self.zone)
        key = (local.weekday(), local.hour) if model == BASELINE else (
            (0, 0) if model == "flat_7d" else (int(local.weekday() >= 5), local.hour)
        )
        cache_key = (model, key)
        if cache_key in self._cache:
            return self._cache[cache_key]
        dates = self.values[model].get(key, {})
        if len(dates) < 4:
            self._cache[cache_key] = None
            return None
        prices = [mean(values) for values in dates.values()]
        if model == "flat_7d":
            prices = [price for values in dates.values() for price in values]
            result = mean(prices), prices
        elif model == "weighted_day_type_56d":
            weighted = [(mean(values), .5 ** ((self.issued.astimezone(self.zone).date()
                         - datetime.fromisoformat(day).date()).days / 14))
                        for day, values in dates.items()]
            result = sum(price * weight for price, weight in weighted) / sum(
                weight for _, weight in weighted), prices
        else:
            result = median(prices), prices
        self._cache[cache_key] = result
        return result


def build_profiles(
    samples: dict[str, tuple[float, float | None]], before: datetime, time_zone: str,
) -> CandidateProfiles:
    """Fit a predeclared bounded candidate pool using only elapsed actuals."""
    before = before.astimezone(UTC)
    zone = ZoneInfo(time_zone)
    values: dict[str, dict] = {model: {} for model in MODEL_NAMES}
    for stamp, (price, _) in samples.items():
        start = datetime.fromisoformat(stamp).astimezone(UTC)
        if start + _HOUR > before:
            continue
        local = start.astimezone(zone)
        day = local.date().isoformat()
        for model, days in _WINDOWS.items():
            if start < before - timedelta(days=days):
                continue
            key = (local.weekday(), local.hour) if model == BASELINE else (
                (0, 0) if model == "flat_7d" else (int(local.weekday() >= 5), local.hour)
            )
            values[model].setdefault(key, {}).setdefault(day, []).append(price)
    return CandidateProfiles(before, zone, values)


@dataclass(slots=True)
class PatternEvidence:
    """First-issued forecasts compete on identical targets and lead bands."""

    issued: dict[str, dict[str, Any]] = field(default_factory=dict)
    scores: list[dict[str, Any]] = field(default_factory=list)
    champions: dict[str, str] = field(default_factory=dict)

    def prune(self, now: datetime) -> None:
        cutoff = now - timedelta(days=28)
        self.issued = {key: row for key, row in self.issued.items()
                       if now - timedelta(days=3) <= datetime.fromisoformat(row["start"])
                       <= now + timedelta(days=3)}
        self.scores = [row for row in self.scores
                       if cutoff <= datetime.fromisoformat(row["start"]) + _HOUR <= now][-2200:]

    def record(
        self, issued: datetime, target: datetime, predictions: dict[str, float],
    ) -> bool:
        """Keep one immutable target/lead observation despite repeated polls."""
        if issued.tzinfo is None or target.tzinfo is None:
            return False
        issued, target = issued.astimezone(UTC), target.astimezone(UTC)
        band = lead_band(issued, target)
        key = f"{target.isoformat()}|{band}"
        if (key in self.issued or target < issued or target + _HOUR > issued + timedelta(days=3)
                or target.minute or target.second or target.microsecond
                or set(predictions) != set(MODEL_NAMES)
                or not all(isfinite(price) for price in predictions.values())):
            return False
        self.issued[key] = {"start": target.isoformat(), "issued_at": issued.isoformat(),
                            "lead_band": band, "predictions": dict(predictions)}
        return True

    def score(self, start: datetime, actual: float) -> None:
        """Score before the realized price can influence any later forecast."""
        if start.tzinfo is None or not isfinite(actual):
            return
        start = start.astimezone(UTC)
        for key, row in list(self.issued.items()):
            if row["start"] != start.isoformat():
                continue
            self.scores.append({"start": row["start"], "issued_at": row["issued_at"],
                                "lead_band": row["lead_band"],
                                "errors": {model: price - actual for model, price
                                           in row["predictions"].items()}})
            self.issued.pop(key)

    def comparison(self, band: str, now: datetime, time_zone: str) -> dict[str, Any]:
        """Expose comparable evidence; sample count alone is never model skill."""
        unique = {row["start"]: row for row in self.scores if row["lead_band"] == band
                  and now - timedelta(days=28) <= datetime.fromisoformat(row["start"])
                  and datetime.fromisoformat(row["start"]) + _HOUR <= now}
        rows = list(unique.values())
        covered_dates: dict[str, int] = {}
        for row in rows:
            day = datetime.fromisoformat(row["start"]).astimezone(ZoneInfo(time_zone)).date().isoformat()
            covered_dates[day] = covered_dates.get(day, 0) + 1
        days = sum(hours >= 6 for hours in covered_dates.values())
        maes = {model: mean(abs(row["errors"][model]) for row in rows)
                for model in MODEL_NAMES} if rows else {}
        return {"hours": len(rows), "local_dates": days, "mae_by_model": maes,
                "qualified": len(rows) >= 24 and days >= 3}

    def select(self, band: str, now: datetime, time_zone: str) -> str | None:
        """Use the independently checked model only after issued skill qualifies.

        Other candidates remain auditable shadow challengers. An exploratory
        online switching policy lost to the incumbent on an independent year;
        recent error rankings alone therefore cannot promote it into control.
        """
        evidence = self.comparison(band, now, time_zone)
        if not evidence["qualified"]:
            return None
        maes = evidence["mae_by_model"]
        if not (maes[CONTROL_MODEL] <= maes["flat_7d"] * .9
                and maes["flat_7d"] - maes[CONTROL_MODEL] >= .01):
            self.champions.pop(band, None)
            return None
        self.champions[band] = CONTROL_MODEL
        return CONTROL_MODEL

    def error_allowance(self, band: str, model: str, now: datetime) -> float:
        """Calibrate each lead's allowance from actual first-issued residuals."""
        unique = {row["start"]: row for row in self.scores
                  if row["lead_band"] == band
                  and now - timedelta(days=28) <= datetime.fromisoformat(row["start"])
                  and datetime.fromisoformat(row["start"]) + _HOUR <= now}
        errors = [abs(row["errors"][model]) for row in unique.values()]
        return quantile(errors, .9) if len(errors) >= 24 else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {"issued": {key: {**row, "predictions": dict(row["predictions"])}
                           for key, row in self.issued.items()},
                "scores": [{**row, "errors": dict(row["errors"])} for row in self.scores],
                "champions": dict(self.champions)}

    @classmethod
    def from_dict(cls, data: Any) -> PatternEvidence:
        """Restore only finite, causal, complete candidate comparisons."""
        result = cls()
        if not isinstance(data, dict):
            return result
        champions = data.get("champions", {})
        champions = champions if isinstance(champions, dict) else {}
        result.champions = {band: model for band, model in champions.items()
                            if band in LEAD_BANDS and model in MODEL_NAMES}
        seen: set[tuple[str, str]] = set()
        for collection, field_name, limit in (("scores", "errors", 2200), ("issued", "predictions", 512)):
            stored = data.get(collection, {}) if collection == "issued" else data.get(collection, [])
            rows = list(stored.values()) if isinstance(stored, dict) else stored if isinstance(stored, list) else []
            for row in rows[-limit:]:
                try:
                    start, issued = (datetime.fromisoformat(row[key]) for key in ("start", "issued_at"))
                    band = row["lead_band"]
                    numbers = {model: float(row[field_name][model]) for model in MODEL_NAMES}
                    if (start.tzinfo is None or issued.tzinfo is None or issued > start
                            or start + _HOUR > issued + timedelta(days=3)
                            or start.minute or start.second or start.microsecond
                            or band != lead_band(issued, start)
                            or not all(isfinite(value) for value in numbers.values())):
                        continue
                    clean = {"start": start.astimezone(UTC).isoformat(),
                             "issued_at": issued.astimezone(UTC).isoformat(),
                             "lead_band": band, field_name: numbers}
                    identity = (clean["start"], band)
                    if identity in seen:
                        continue
                    seen.add(identity)
                    if collection == "issued":
                        result.issued[f"{clean['start']}|{band}"] = clean
                    else:
                        result.scores.append(clean)
                except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
                    continue
        return result
