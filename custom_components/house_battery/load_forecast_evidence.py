"""Bounded issued-load forecasts and independent covered actual-load evidence."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from math import isfinite
from statistics import quantiles
from typing import Any

STEP = timedelta(minutes=15)
LEAD_BANDS = ("0-2h", "2-6h", "6-12h", "12-24h", "24-72h")
MAX_PENDING = 1600
MAX_SCORED = 5000


def _time(value: Any) -> datetime | None:
    try:
        value = datetime.fromisoformat(value) if isinstance(value, str) else value
        return value.astimezone(UTC) if isinstance(value, datetime) and value.tzinfo else None
    except (ValueError, TypeError):
        return None


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if isfinite(number) else None
    except (ValueError, TypeError, OverflowError):
        return None


def _bucket(now: datetime) -> datetime:
    now = now.astimezone(UTC)
    return now.replace(minute=now.minute // 15 * 15, second=0, microsecond=0)


def _band(hours: float) -> str | None:
    return next((band for limit, band in zip((2, 6, 12, 24, 72), LEAD_BANDS)
                 if 0 <= hours <= limit), None)


@dataclass(slots=True)
class LoadForecastEvidence:
    """Score first-issued predictions per target/lead band, never revised hindsight.

    Sampling cursors deliberately do not serialize: restart cannot bridge an
    unobserved gap. Actual demand is independent of baseline-learning exclusions.
    """

    source: str | None = None
    pending: dict[str, dict[str, Any]] = field(default_factory=dict)
    scored: list[dict[str, Any]] = field(default_factory=list)
    daily: dict[str, dict[str, Any]] = field(default_factory=dict)
    _previous: tuple[datetime, float, float | None] | None = None
    _actual_start: datetime | None = None
    _actual_wh: float = 0.0
    _covered_seconds: float = 0.0
    _priced_seconds: float = 0.0
    _price_seconds: float = 0.0
    _first_price: float | None = None
    _price_varied: bool = False

    def configure(self, source: str) -> bool:
        """Separate evidence when a load source, scope or model version changes."""
        if source == self.source:
            return False
        self.source = source
        self.pending.clear()
        self.scored.clear()
        self.daily.clear()
        self._previous = None
        self._reset_actual(None)
        return True

    def record_forecasts(
        self, issued_at: datetime, forecasts: list[dict[str, Any]], source_revision: str,
    ) -> bool:
        """Keep the first full-quarter forecast in each lead band for a target."""
        issued = _time(issued_at)
        if issued is None:
            return False
        changed = False
        scored_keys = {row["key"] for row in self.scored}
        for row in forecasts:
            start, end = _time(row.get("start")), _time(row.get("end"))
            baseline = _number(row.get("baseline_wh"))
            scheduled = _number(row.get("scheduled_wh"))
            if (start is None or end is None or start < issued or start != _bucket(start)
                    or end - start != STEP or baseline is None or scheduled is None
                    or min(baseline, scheduled) < 0):
                continue
            band = _band((start - issued).total_seconds() / 3600)
            if band is None:
                continue
            key = f"{start.isoformat()}|{band}"
            if key in self.pending or key in scored_keys:
                continue
            self.pending[key] = {
                "key": key, "issued_at": issued.isoformat(), "start": start.isoformat(),
                "end": end.isoformat(), "baseline_wh": baseline,
                "scheduled_wh": scheduled, "total_wh": baseline + scheduled,
                "lead_band": band, "source_revision": source_revision,
            }
            changed = True
        self.prune(issued)
        return changed

    def observe_actual(
        self, start: datetime, end: datetime, load_wh: float, covered_seconds: float,
    ) -> bool:
        """Only substantially covered measured total demand may score a forecast."""
        start, end = _time(start), _time(end)
        energy, covered = _number(load_wh), _number(covered_seconds)
        if (start is None or end is None or end - start != STEP or start != _bucket(start)
                or energy is None or energy < 0 or covered is None or not 810 <= covered <= 900):
            return False
        # Normalize a substantially covered interval to its full duration rather
        # than treating missing readings as zero demand. Coverage remains visible.
        energy *= 900 / covered
        changed = False
        for key, row in list(self.pending.items()):
            if row["start"] != start.isoformat() or row["end"] != end.isoformat():
                continue
            error = energy - row["total_wh"]
            scored = {**row, "actual_wh": energy, "error_wh": error,
                      "covered_seconds": covered, "scored_at": end.isoformat()}
            self.scored.append(scored)
            day_key = f"{start.date().isoformat()}|{row['lead_band']}"
            daily = self.daily.setdefault(day_key, {
                "day": start.date().isoformat(), "lead_band": row["lead_band"],
                "samples": 0, "absolute_error_wh": 0.0, "error_wh": 0.0,
                "covered_seconds": 0.0,
            })
            daily["samples"] += 1
            daily["absolute_error_wh"] += abs(error)
            daily["error_wh"] += error
            daily["covered_seconds"] += covered
            del self.pending[key]
            changed = True
        self.prune(end)
        return changed

    def _reset_actual(self, start: datetime | None) -> None:
        self._actual_start = start
        self._actual_wh = self._covered_seconds = 0.0
        self._priced_seconds = self._price_seconds = 0.0
        self._first_price = None
        self._price_varied = False

    def _finish_actual(self) -> dict[str, Any] | None:
        if self._actual_start is None or self._covered_seconds <= 0:
            return None
        end = self._actual_start + STEP
        self.observe_actual(self._actual_start, end, self._actual_wh, self._covered_seconds)
        complete = self._covered_seconds >= 810
        return {
            "start": self._actual_start.isoformat(), "end": end.isoformat(),
            "load_wh": self._actual_wh * 900 / self._covered_seconds if complete else self._actual_wh,
            "covered_seconds": self._covered_seconds,
            "price": self._price_seconds / self._priced_seconds
            if complete and not self._price_varied
            and self._priced_seconds >= self._covered_seconds - 1e-6 else None,
            "price_quality": "mixed_price_interval" if self._price_varied else
            "known" if self._priced_seconds >= self._covered_seconds - 1e-6 else "unpriced",
            "quality": "good" if complete else "incomplete",
        }

    def observe(
        self, now: datetime, load_w: float | None, price: float | None = None,
    ) -> list[dict[str, Any]]:
        """Integrate fresh adjacent samples across UTC quarter-hour boundaries."""
        now, watts, price = _time(now), _number(load_w), _number(price)
        if now is None:
            return []
        completed = []
        bucket = _bucket(now)
        previous = self._previous
        if previous and 0 < (now - previous[0]).total_seconds() <= 120 and watts is not None and watts >= 0:
            cursor = previous[0]
            while cursor < now:
                start = _bucket(cursor)
                if self._actual_start != start:
                    if row := self._finish_actual():
                        completed.append(row)
                    self._reset_actual(start)
                stop = min(now, start + STEP)
                seconds = (stop - cursor).total_seconds()
                self._actual_wh += (previous[1] + watts) / 2 * seconds / 3600
                self._covered_seconds += seconds
                interval_price = previous[2] if start < bucket else (
                    price if previous[2] is not None else None
                )
                if interval_price is not None:
                    if self._first_price is None:
                        self._first_price = interval_price
                    elif abs(self._first_price - interval_price) > 1e-9:
                        self._price_varied = True
                    self._priced_seconds += seconds
                    self._price_seconds += interval_price * seconds
                cursor = stop
        if self._actual_start is not None and self._actual_start < bucket:
            if row := self._finish_actual():
                completed.append(row)
            self._reset_actual(bucket)
        self._previous = (now, watts, price) if watts is not None and watts >= 0 else None
        return completed

    def prune(self, now: datetime) -> None:
        now = _time(now)
        if now is None:
            return
        self.pending = dict(list({key: row for key, row in self.pending.items()
            if now - timedelta(days=7) <= _time(row["end"]) <= now + timedelta(hours=73)
        }.items())[-MAX_PENDING:])
        self.scored = [row for row in self.scored
                       if _time(row["end"]) >= now - timedelta(days=7)][-MAX_SCORED:]
        cutoff = (now - timedelta(days=28)).date().isoformat()
        self.daily = {key: row for key, row in self.daily.items() if row["day"] >= cutoff}

    def summary(self, now: datetime) -> dict[str, Any]:
        self.prune(now)
        bands = {}
        for band in LEAD_BANDS:
            rows = [row for row in self.scored if row["lead_band"] == band]
            errors = [row["error_wh"] for row in rows]
            recent = sorted(errors[-256:])
            bounds = quantiles(recent, n=10, method="inclusive") if len(recent) >= 10 else None
            bands[band] = {
                "samples": len(rows),
                "covered_hours": sum(row["covered_seconds"] for row in rows) / 3600,
                "mae_wh": sum(map(abs, errors)) / len(errors) if errors else None,
                "mae_w": 4 * sum(map(abs, errors)) / len(errors) if errors else None,
                "bias_wh": sum(errors) / len(errors) if errors else None,
                "actual_minus_predicted_p10_wh": bounds[0] if bounds else None,
                "actual_minus_predicted_p90_wh": bounds[-1] if bounds else None,
                "qualified": len(rows) >= 96 and len({row["start"][:10] for row in rows}) >= 3,
            }
        return {"source": self.source, "samples": len(self.scored), "pending": len(self.pending),
                "mae_w": 4 * sum(abs(row["error_wh"]) for row in self.scored) / len(self.scored)
                if self.scored else None,
                "by_lead_band": bands, "daily": list(self.daily.values()),
                "dispatch_uncertainty_enabled": False}

    def as_dict(self) -> dict[str, Any]:
        return {"version": 1, "source": self.source, "pending": list(self.pending.values()),
                "scored": self.scored, "daily": list(self.daily.values())}

    @classmethod
    def from_dict(cls, data: Any) -> LoadForecastEvidence:
        result = cls()
        if not isinstance(data, dict):
            return result
        result.source = data.get("source") if isinstance(data.get("source"), str) else None
        for category in ("pending", "scored"):
            rows = data.get(category, [])
            if not isinstance(rows, list):
                continue
            for row in rows[-(MAX_PENDING if category == "pending" else MAX_SCORED):]:
                if not isinstance(row, dict):
                    continue
                start, end, issued = (_time(row.get(key)) for key in ("start", "end", "issued_at"))
                if (start is None or end is None or issued is None or issued > start
                        or end - start != STEP or start != _bucket(start)
                        or row.get("lead_band") != _band((start - issued).total_seconds() / 3600)
                        or row.get("lead_band") not in LEAD_BANDS):
                    continue
                if any(_number(row.get(key)) is None or _number(row.get(key)) < 0
                       for key in ("baseline_wh", "scheduled_wh", "total_wh")):
                    continue
                clean = {"start": start.isoformat(), "end": end.isoformat(),
                         "issued_at": issued.isoformat(), "lead_band": row["lead_band"],
                         "source_revision": str(row.get("source_revision", ""))[:128]}
                for key in ("baseline_wh", "scheduled_wh", "total_wh"):
                    clean[key] = float(row[key])
                if abs(clean["total_wh"] - clean["baseline_wh"] - clean["scheduled_wh"]) > 1e-6:
                    continue
                clean["key"] = f"{start.isoformat()}|{row['lead_band']}"
                if category == "pending":
                    result.pending.setdefault(clean["key"], clean)
                elif (_number(row.get("error_wh")) is not None
                      and _number(row.get("actual_wh")) is not None
                      and float(row["actual_wh"]) >= 0
                      and _number(row.get("covered_seconds")) is not None
                      and 810 <= float(row["covered_seconds"]) <= 900):
                    clean.update(error_wh=float(row["error_wh"]),
                                 actual_wh=float(row["actual_wh"]),
                                 scored_at=end.isoformat(),
                                 covered_seconds=float(row["covered_seconds"]))
                    if abs(clean["actual_wh"] - clean["total_wh"] - clean["error_wh"]) <= 1e-6:
                        result.scored.append(clean)
        daily_rows = data.get("daily", [])
        if isinstance(daily_rows, list):
            for row in daily_rows[-145:]:
                if (isinstance(row, dict) and _time(f"{row.get('day')}T00:00:00+00:00")
                        and row.get("lead_band") in LEAD_BANDS
                        and all(_number(row.get(key)) is not None for key in
                                ("samples", "absolute_error_wh", "error_wh", "covered_seconds"))
                        and all(float(row[key]) >= 0 for key in
                                ("samples", "absolute_error_wh", "covered_seconds"))):
                    result.daily[f"{row['day']}|{row['lead_band']}"] = {
                        "day": row["day"], "lead_band": row["lead_band"],
                        **{key: float(row[key]) for key in (
                            "samples", "absolute_error_wh", "error_wh", "covered_seconds",
                        )},
                    }
        return result
