"""Conservative real-grid, matched-SOC round-trip efficiency evidence.

The selected grid meter may share a phase with other appliances. Each charge
therefore needs stable idle baselines on both sides. Raw battery power only
corroborates a charge; connected-load power measures delivered energy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from itertools import pairwise
from math import isfinite, sqrt
from statistics import median
from typing import Any

_MAX_GAP_SECONDS = 120
_BASELINE_SECONDS = 600
_RETENTION_DAYS = 30
_MAX_WINDOWS = 20
_SOC_TOLERANCE = 0.5


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return result if isfinite(result) else None


def _timestamp(value: Any) -> datetime | None:
    try:
        result = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    return result if result.tzinfo is not None else None


@dataclass(slots=True)
class MatchedSocEfficiency:
    """Bounded online evidence; three independent stable cycles permit planning."""

    source: str = ""
    accepted_windows: list[dict[str, Any]] = field(default_factory=list)
    _capacity_kwh: float | None = field(default=None, repr=False)
    _last: dict[str, Any] | None = field(default=None, repr=False)
    _idle: list[dict[str, float]] = field(default_factory=list, repr=False)
    _cycle: dict[str, Any] | None = field(default=None, repr=False)
    _charge: dict[str, Any] | None = field(default=None, repr=False)
    _reason: str = "Awaiting stable idle baseline and a matching SOC cycle"

    def configure(self, source: str) -> None:
        """Discard incompatible evidence when meter, load, or battery changes."""
        source = source if isinstance(source, str) else ""
        if source != self.source:
            self.source = source
            self.accepted_windows.clear()
            self._capacity_kwh = None
            self._invalidate("Measurement sources changed; collecting new evidence")

    def _invalidate(self, reason: str) -> None:
        self._last = self._cycle = self._charge = None
        self._idle.clear()
        self._reason = reason

    def prune(self, now: datetime) -> None:
        """Expire evidence independently of receiving a new valid sample."""
        if now.tzinfo is None:
            raise ValueError("Efficiency timestamps must be timezone-aware")
        cutoff = now - timedelta(days=_RETENTION_DAYS)
        self.accepted_windows = [
            w for w in self.accepted_windows
            if (end := _timestamp(w.get("end"))) is not None and cutoff <= end <= now
        ][-_MAX_WINDOWS:]
        if self._cycle and now.timestamp() - self._cycle["start_ts"] > 7 * 86400:
            self._invalidate("Matching SOC was not reached within seven days")

    def observe(
        self, now: datetime, *, soc: float | None, grid_power_w: float | None,
        load_power_w: float | None, activity: str, healthy: bool = True,
        capacity_kwh: float = 1.958, reported_charge_power_w: float | None = None,
    ) -> bool:
        """Observe a fresh simultaneous sample; return whether a window closed.

        Power is integrated using actual elapsed time. At observed activity
        transitions, half the inter-sample interval belongs to each activity.
        A restart or missing sample discards the in-flight cycle entirely.
        """
        self.prune(now)
        values = [_number(v) for v in (soc, grid_power_w, load_power_w, capacity_kwh)]
        s, grid, load, capacity = values
        if (
            not self.source or not healthy or any(v is None for v in values)
            or not 0 <= s <= 100 or grid < -5 or load < 0 or capacity <= 0
            or activity not in {"idle", "charging", "discharging"}
            or (activity == "discharging" and load <= 0)
        ):
            self._invalidate("Unavailable, unhealthy, exporting, or invalid measurements")
            return False
        if self._capacity_kwh is not None and capacity != self._capacity_kwh:
            self.accepted_windows.clear()
            self._invalidate("Nominal battery capacity changed")
        self._capacity_kwh = capacity
        point = {"t": now.timestamp(), "soc": s, "grid": max(0, grid),
                 "load": load, "activity": activity,
                 "reported": _number(reported_charge_power_w)}
        previous = self._last
        if previous is None:
            self._last = point
            self._add_idle(point)
            return False
        elapsed = point["t"] - previous["t"]
        if not 0 < elapsed <= _MAX_GAP_SECONDS:
            self._invalidate("Telemetry gap or non-monotonic timestamp")
            self._last = point
            self._add_idle(point)
            return False
        movement = s - previous["soc"]
        if (
            abs(movement) > max(3, 5 * elapsed / 60)
            or (activity == previous["activity"] == "charging" and movement < -1)
            or (activity == previous["activity"] == "discharging" and movement > 1)
        ):
            self._invalidate("SOC movement contradicts measured battery activity")
            self._last = point
            return False
        if self._charge and self._charge.get("ended") and activity != "idle":
            self._invalidate("Charging has no settled post-charge idle baseline")
            self._last = point
            return False
        if activity == "charging" and previous["activity"] != "charging":
            baseline = self._baseline()
            if baseline is None or self._cycle is None:
                self._invalidate("Charging has no stable pre-charge idle baseline")
                self._last = point
                return False
            self._charge = {"baseline": baseline, "wh": 0.0, "seconds": 0.0,
                            "reported_wh": 0.0, "reported_seconds": 0.0}
        if self._cycle:
            self._cycle["soc_min"] = min(self._cycle["soc_min"], s)
            self._cycle["soc_max"] = max(self._cycle["soc_max"], s)
            self._integrate(previous, point, elapsed)
        if self._charge and previous["activity"] == "charging" and activity != "charging":
            self._charge["ended"] = point["t"]
        self._add_idle(point)
        self._last = point
        baseline = self._baseline() if activity == "idle" else None
        if baseline is None:
            return False
        if self._charge and self._charge.get("ended"):
            if point["t"] - self._charge["ended"] < _BASELINE_SECONDS:
                return False
            if not self._finish_charge(baseline):
                return False
        closed = self._finish_cycle(now, s) if self._cycle else False
        if self._cycle is None:
            self._cycle = {
                "start": now.isoformat(), "start_ts": point["t"], "soc_start": s,
                "soc_min": s, "soc_max": s, "charge_wh": 0.0,
                "discharge_wh": 0.0, "charge_events": [],
            }
        return closed

    def _add_idle(self, point: dict[str, Any]) -> None:
        if point["activity"] != "idle":
            self._idle.clear()
            return
        self._idle.append({"t": point["t"], "w": point["grid"] - point["load"],
                           "soc": point["soc"]})
        self._idle = [s for s in self._idle if s["t"] >= point["t"] - 1800][-2048:]

    def _baseline(self) -> dict[str, float] | None:
        if len(self._idle) < 6 or self._idle[-1]["t"] - self._idle[0]["t"] < _BASELINE_SECONDS:
            return None
        if max(s["soc"] for s in self._idle) - min(s["soc"] for s in self._idle) > 1:
            return None
        seconds = sum(b["t"] - a["t"] for a, b in pairwise(self._idle))
        mean = sum(
            (a["w"] + b["w"]) / 2 * (b["t"] - a["t"])
            for a, b in pairwise(self._idle)
        ) / seconds
        spread = sqrt(sum((s["w"] - mean) ** 2 for s in self._idle) / len(self._idle))
        if mean < -5 or spread > 25:
            return None
        return {"mean_w": mean, "spread_w": spread}

    def _integrate(self, previous: dict[str, Any], point: dict[str, Any], elapsed: float) -> None:
        assert self._cycle is not None
        for sample in (previous, point):
            half_hours = elapsed / 7200
            if sample["activity"] == "charging" and self._charge is not None:
                self._charge["wh"] += (sample["grid"] - sample["load"]) * half_hours
                self._charge["seconds"] += elapsed / 2
                if sample["reported"] is not None and sample["reported"] >= 0:
                    self._charge["reported_wh"] += sample["reported"] * half_hours
                    self._charge["reported_seconds"] += elapsed / 2
            elif sample["activity"] == "discharging":
                self._cycle["discharge_wh"] += sample["load"] * half_hours

    def _finish_charge(self, post: dict[str, float]) -> bool:
        assert self._charge is not None and self._cycle is not None
        charge = self._charge
        pre = charge["baseline"]
        hours = charge["seconds"] / 3600
        measured_wh = charge["wh"] - (pre["mean_w"] + post["mean_w"]) / 2 * hours
        uncertainty_wh = max(
            pre["spread_w"], post["spread_w"],
            abs(pre["mean_w"] - post["mean_w"]) / 2,
        ) * hours
        if (
            measured_wh < 50 or abs(pre["mean_w"] - post["mean_w"]) > 40
            or uncertainty_wh > measured_wh * 0.05
        ):
            self._invalidate("Other grid loads make the charge baseline uncertain")
            return False
        if 0 < charge["reported_seconds"] < charge["seconds"] * 0.95:
            self._invalidate("Battery charge corroboration has incomplete coverage")
            return False
        if charge["reported_seconds"] >= charge["seconds"] * 0.95:
            reference = charge["reported_wh"]
            if reference < 50 or abs(reference - measured_wh) > measured_wh * 0.15:
                self._invalidate("Grid charge energy disagrees with battery charge telemetry")
                return False
        self._cycle["charge_wh"] += measured_wh
        self._cycle["charge_events"].append({
            "charge_kwh": measured_wh / 1000,
            "baseline_pre_w": pre["mean_w"], "baseline_post_w": post["mean_w"],
            "uncertainty_kwh": uncertainty_wh / 1000,
        })
        self._cycle["charge_events"] = self._cycle["charge_events"][-32:]
        self._charge = None
        return True

    def _finish_cycle(self, now: datetime, soc: float) -> bool:
        assert self._cycle is not None
        cycle = self._cycle
        if (
            self._charge or abs(soc - cycle["soc_start"]) > _SOC_TOLERANCE
            or cycle["soc_max"] - cycle["soc_min"] < 20
            or cycle["charge_wh"] < 500 or cycle["discharge_wh"] < 300
        ):
            return False
        efficiency = cycle["discharge_wh"] / cycle["charge_wh"]
        if not 0.5 <= efficiency <= 0.98:
            self._invalidate("Matched cycle efficiency lies outside the plausible range")
            return False
        recent = self.accepted_windows[-3:]
        if len(recent) >= 3 and abs(efficiency - median(w["efficiency"] for w in recent)) > 0.10:
            self._invalidate("Matched cycle disagrees with established efficiency evidence")
            return False
        self.accepted_windows.append({
            "start": cycle["start"], "end": now.isoformat(),
            "soc_start": cycle["soc_start"], "soc_end": soc,
            "soc_min": cycle["soc_min"], "soc_max": cycle["soc_max"],
            "charge_kwh": cycle["charge_wh"] / 1000,
            "discharge_kwh": cycle["discharge_wh"] / 1000,
            "efficiency": efficiency, "charge_events": cycle["charge_events"],
            "quality": "matched_soc_grid_baseline",
        })
        self.accepted_windows = self.accepted_windows[-_MAX_WINDOWS:]
        self._cycle = None
        self._reason = "Matching SOC cycle measured from real grid input and delivered load"
        return True

    def summary(self, now: datetime) -> dict[str, Any]:
        """Expose candidate evidence and a separately gated planning estimate."""
        self.prune(now)
        recent = self.accepted_windows[-3:]
        charge = sum(w["charge_kwh"] for w in recent)
        discharge = sum(w["discharge_kwh"] for w in recent)
        candidate = discharge / charge * 100 if charge else None
        stable = bool(self.source) and len(recent) >= 3 and max(w["efficiency"] for w in recent) - min(
            w["efficiency"] for w in recent
        ) <= 0.08
        return {
            "efficiency_pct": candidate,
            "planning_efficiency_pct": candidate if stable else None,
            "status": "ready" if stable else "learning" if self.source else "not_configured",
            "method": "matched_soc_grid_meter",
            "reason": self._reason, "source": self.source,
            "sample_count": len(self.accepted_windows), "required_samples": 3,
            "charge_kwh": charge, "discharge_kwh": discharge,
            "accepted_windows": [dict(w) for w in self.accepted_windows],
            "soc_tolerance_pct_points": _SOC_TOLERANCE,
            "baseline_minutes": _BASELINE_SECONDS / 60,
            "window_start": recent[0]["start"] if recent else None,
            "window_end": recent[-1]["end"] if recent else None,
        }

    def as_dict(self) -> dict[str, Any]:
        """Persist completed evidence only; active windows cannot cross restarts."""
        return {"source": self.source, "capacity_kwh": self._capacity_kwh,
                "accepted_windows": [dict(w) for w in self.accepted_windows]}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MatchedSocEfficiency:
        model = cls()
        if not isinstance(data, dict):
            return model
        model.source = data.get("source") if isinstance(data.get("source"), str) else ""
        model._capacity_kwh = _number(data.get("capacity_kwh"))
        windows = data.get("accepted_windows", [])
        if not isinstance(windows, list):
            return model
        previous_end: datetime | None = None
        for window in windows[-_MAX_WINDOWS:]:
            if not isinstance(window, dict):
                continue
            start, end = _timestamp(window.get("start")), _timestamp(window.get("end"))
            values = [_number(window.get(k)) for k in (
                "soc_start", "soc_end", "soc_min", "soc_max", "charge_kwh", "discharge_kwh",
            )]
            if start is None or end is None or start >= end or any(v is None for v in values):
                continue
            s, e, low, high, charge, discharge = values
            if (
                previous_end is not None and start < previous_end
                or not 0 <= low <= min(s, e) <= max(s, e) <= high <= 100
                or abs(s - e) > _SOC_TOLERANCE or high - low < 20
                or charge < 0.5 or discharge < 0.3 or not 0.5 <= discharge / charge <= 0.98
            ):
                continue
            model.accepted_windows.append({
                "start": start.isoformat(), "end": end.isoformat(),
                "soc_start": s, "soc_end": e, "soc_min": low, "soc_max": high,
                "charge_kwh": charge, "discharge_kwh": discharge,
                "efficiency": discharge / charge,
                "charge_events": _restore_events(window.get("charge_events")),
                "quality": "matched_soc_grid_baseline",
            })
            previous_end = end
        return model


def _restore_events(value: Any) -> list[dict[str, float]]:
    """Retain the baseline audit trail without trusting arbitrary persisted data."""
    if not isinstance(value, list):
        return []
    events = []
    keys = ("charge_kwh", "baseline_pre_w", "baseline_post_w", "uncertainty_kwh")
    for event in value[-32:]:
        if not isinstance(event, dict):
            continue
        fields = {key: _number(event.get(key)) for key in keys}
        if any(v is None for v in fields.values()):
            continue
        if fields["charge_kwh"] <= 0 or fields["uncertainty_kwh"] < 0:
            continue
        events.append(fields)
    return events
