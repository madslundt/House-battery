"""Persistent, local load and battery learning models."""

from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime
from math import sqrt
from statistics import median
from typing import Any

BUCKETS_PER_DAY = 96
MIN_HISTORY_WEIGHT_COUNT = 4
FULL_HISTORY_WEIGHT_COUNT = 12


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

    @staticmethod
    def key(when: datetime) -> str:
        return f"{when.weekday()}:{when.hour * 4 + when.minute // 15}"

    def predict_w(self, when: datetime, fallback_w: float = 0.0) -> float:
        bucket = self.buckets.get(self.key(when))
        recent = (
            sum(self.recent_w) / len(self.recent_w)
            if self.recent_w
            else max(0.0, fallback_w)
        )
        if bucket is None or bucket.count < MIN_HISTORY_WEIGHT_COUNT:
            return recent
        history_weight = min(
            0.30,
            0.30
            * (bucket.count - MIN_HISTORY_WEIGHT_COUNT + 1)
            / (FULL_HISTORY_WEIGHT_COUNT - MIN_HISTORY_WEIGHT_COUNT + 1),
        )
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
        return learner


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
