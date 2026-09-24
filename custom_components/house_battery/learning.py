"""Persistent, local load and battery learning models."""

from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, tzinfo
from math import sqrt
from statistics import median
from typing import Any

BUCKETS_PER_DAY = 96
MIN_HISTORY_WEIGHT_COUNT = 4
FULL_HISTORY_WEIGHT_COUNT = 12

# Horizon-dependent load blending.  The weight given to the historical
# weekday/time-of-day profile grows with how far ahead the prediction reaches:
# the very near future tracks recent usage, the medium horizon blends, and the
# longer horizon increasingly trusts the historical profile.  A fixed blend for
# every future interval would over-trust noisy recent usage far out and
# under-trust the profile that is the only signal we have past recent memory.
NEAR_HORIZON_HOURS = 2.0
LONG_HORIZON_HOURS = 12.0
# Bounding history weights (fraction of the prediction drawn from history).
NEAR_HISTORY_WEIGHT = 0.10
LONG_HISTORY_WEIGHT = 0.70


def _distance_history_weight(hours_ahead: float) -> float:
    """Return the history weight for a prediction ``hours_ahead`` into the future.

    Ramps linearly from ``NEAR_HISTORY_WEIGHT`` (recent usage dominates) to
    ``LONG_HISTORY_WEIGHT`` (historical profile dominates) between
    ``NEAR_HORIZON_HOURS`` and ``LONG_HORIZON_HOURS``, and is flat outside that
    band.  A prediction exactly now (``hours_ahead == 0``) always weights recent
    usage heavily.
    """
    if hours_ahead <= NEAR_HORIZON_HOURS:
        return NEAR_HISTORY_WEIGHT
    if hours_ahead >= LONG_HORIZON_HOURS:
        return LONG_HISTORY_WEIGHT
    fraction = (
        hours_ahead - NEAR_HORIZON_HOURS
    ) / (LONG_HORIZON_HOURS - NEAR_HORIZON_HOURS)
    return NEAR_HISTORY_WEIGHT + (
        LONG_HISTORY_WEIGHT - NEAR_HISTORY_WEIGHT
    ) * fraction


@dataclass(slots=True)
class LoadBucket:
    """Online mean and error for one weekday/time-of-day bucket."""

    count: int = 0
    mean_w: float = 0.0
    m2: float = 0.0

    def observe(self, watts: float) -> None:
        self.count += 1
        delta = watts - self.mean_w
        self.mean_w += delta / self.count
        self.m2 += delta * (watts - self.mean_w)

    @property
    def standard_deviation_w(self) -> float:
        return sqrt(self.m2 / (self.count - 1)) if self.count > 1 else 0.0


@dataclass(slots=True)
class LoadLearner:
    """Learns a 7-day/15-minute profile and adapts to recent load."""

    buckets: dict[str, LoadBucket] = field(default_factory=dict)
    recent_w: deque[float] = field(default_factory=lambda: deque(maxlen=8))
    observations: int = 0
    absolute_error_sum_w: float = 0.0
    time_zone: tzinfo = field(default=UTC, repr=False, compare=False)
    bucket_timezone: str | None = None

    def __post_init__(self) -> None:
        if self.bucket_timezone is None:
            self.bucket_timezone = _timezone_name(self.time_zone)

    def configure_time_zone(self, time_zone: tzinfo) -> None:
        """Set the Home Assistant local zone and discard incompatible buckets."""
        name = _timezone_name(time_zone)
        if self.bucket_timezone != name:
            self.buckets.clear()
            self.observations = 0
            self.absolute_error_sum_w = 0.0
            self.bucket_timezone = name
        self.time_zone = time_zone

    def key(self, when: datetime) -> str:
        """Return the configured local weekday/quarter-hour bucket."""
        if when.tzinfo is None:
            raise ValueError("Load-learning timestamps must be timezone-aware")
        local = when.astimezone(self.time_zone)
        return f"{local.weekday()}:{local.hour * 4 + local.minute // 15}"

    def predict_w(
        self, when: datetime, fallback_w: float = 0.0, now: datetime | None = None
    ) -> float:
        """Predict load watts at ``when``.

        ``now`` is optional: when supplied the blend between recent and
        historical usage depends on how far ``when`` lies ahead of ``now`` (see
        :func:`_distance_history_weight`), so nearer intervals trust recent
        usage and farther intervals trust the historical profile.  When
        ``now`` is omitted the distance is treated as zero, which is what the
        online :meth:`observe` path wants: a live observation is "now", so it
        never borrows history weight from a distant prediction.
        """
        if when.tzinfo is None:
            raise ValueError("Load-learning timestamps must be timezone-aware")
        hours_ahead = 0.0
        if now is not None:
            hours_ahead = max(0.0, (when - now).total_seconds() / 3600)
        bucket = self.buckets.get(self.key(when))
        recent = (
            sum(self.recent_w) / len(self.recent_w)
            if self.recent_w
            else max(0.0, fallback_w)
        )
        if bucket is None or bucket.count < MIN_HISTORY_WEIGHT_COUNT:
            # Not enough history yet: track recent usage (or the fallback).
            return recent
        # Scale the distance-based history weight by how much evidence backs the
        # bucket, so a sparse bucket still leans on recent usage far out while a
        # well-sampled bucket lets the historical profile dominate.
        evidence_factor = min(
            1.0,
            (bucket.count - MIN_HISTORY_WEIGHT_COUNT + 1)
            / (FULL_HISTORY_WEIGHT_COUNT - MIN_HISTORY_WEIGHT_COUNT + 1),
        )
        distance_weight = _distance_history_weight(hours_ahead)
        history_weight = NEAR_HISTORY_WEIGHT + (
            distance_weight - NEAR_HISTORY_WEIGHT
        ) * evidence_factor
        return recent * (1 - history_weight) + bucket.mean_w * history_weight

    def observe(self, when: datetime, watts: float) -> None:
        watts = max(0.0, watts)
        prediction = self.predict_w(when, watts)
        self.absolute_error_sum_w += abs(watts - prediction)
        bucket = self.buckets.setdefault(self.key(when), LoadBucket())
        bucket.observe(watts)
        self.recent_w.append(watts)
        self.observations += 1

    @property
    def confidence(self) -> float:
        covered = sum(
            1
            for bucket in self.buckets.values()
            if bucket.count >= MIN_HISTORY_WEIGHT_COUNT
        )
        return min(1.0, covered / (7 * BUCKETS_PER_DAY))

    @property
    def mean_absolute_error_w(self) -> float | None:
        return (
            self.absolute_error_sum_w / self.observations if self.observations else None
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "buckets": {key: asdict(value) for key, value in self.buckets.items()},
            "recent_w": list(self.recent_w),
            "observations": self.observations,
            "absolute_error_sum_w": self.absolute_error_sum_w,
            "bucket_timezone": self.bucket_timezone,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LoadLearner:
        learner = cls()
        for key, value in data.get("buckets", {}).items():
            learner.buckets[key] = LoadBucket(
                count=int(value.get("count", 0)),
                mean_w=float(value.get("mean_w", 0)),
                m2=float(value.get("m2", 0)),
            )
        learner.recent_w.extend(float(value) for value in data.get("recent_w", []))
        learner.observations = int(data.get("observations", 0))
        learner.absolute_error_sum_w = float(data.get("absolute_error_sum_w", 0))
        learner.bucket_timezone = str(data.get("bucket_timezone", "UTC"))
        return learner


def _timezone_name(time_zone: tzinfo) -> str:
    return getattr(time_zone, "key", str(time_zone))


@dataclass(slots=True)
class BatteryLearner:
    """Conservatively promotes learned capacity and efficiency estimates."""

    capacity_samples_wh: list[float] = field(default_factory=list)
    efficiency_samples: list[float] = field(default_factory=list)
    capacity_soc_movements: list[float] = field(default_factory=list)
    efficiency_soc_movements: list[float] = field(default_factory=list)

    @staticmethod
    def _ready(samples: list[float], movements: list[float]) -> bool:
        if len(samples) < 5:
            return False
        center = median(samples)
        return (
            center > 0
            and sum(movements[-5:]) >= 100
            and all(abs(value - center) / center <= 0.10 for value in samples[-5:])
        )

    @property
    def capacity_ready(self) -> bool:
        return self._ready(self.capacity_samples_wh, self.capacity_soc_movements)

    @property
    def efficiency_ready(self) -> bool:
        return self._ready(self.efficiency_samples, self.efficiency_soc_movements)

    @property
    def learned_capacity_wh(self) -> float | None:
        return median(self.capacity_samples_wh[-5:]) if self.capacity_ready else None

    @property
    def learned_efficiency(self) -> float | None:
        return median(self.efficiency_samples[-5:]) if self.efficiency_ready else None

    def add_capacity_sample(
        self, capacity_wh: float, soc_movement: float = 20.0
    ) -> None:
        if 500 <= capacity_wh <= 20000:
            self.capacity_samples_wh = (self.capacity_samples_wh + [capacity_wh])[-20:]
            self.capacity_soc_movements = (
                self.capacity_soc_movements + [soc_movement]
            )[-20:]

    def add_efficiency_sample(
        self, efficiency: float, soc_movement: float = 20.0
    ) -> None:
        if 0.5 <= efficiency <= 1:
            self.efficiency_samples = (self.efficiency_samples + [efficiency])[-20:]
            self.efficiency_soc_movements = (
                self.efficiency_soc_movements + [soc_movement]
            )[-20:]

    def as_dict(self) -> dict[str, Any]:
        return {
            "capacity_samples_wh": self.capacity_samples_wh,
            "efficiency_samples": self.efficiency_samples,
            "capacity_soc_movements": self.capacity_soc_movements,
            "efficiency_soc_movements": self.efficiency_soc_movements,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BatteryLearner:
        capacities = [float(value) for value in data.get("capacity_samples_wh", [])][
            -20:
        ]
        efficiencies = [float(value) for value in data.get("efficiency_samples", [])][
            -20:
        ]
        return cls(
            capacities,
            efficiencies,
            [
                float(value)
                for value in data.get("capacity_soc_movements", [20] * len(capacities))
            ][-20:],
            [
                float(value)
                for value in data.get(
                    "efficiency_soc_movements", [20] * len(efficiencies)
                )
            ][-20:],
        )
