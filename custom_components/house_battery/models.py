"""Typed domain model for deterministic FBP1200 optimization."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any


class Action(StrEnum):
    """Mutually exclusive operating decisions."""

    CHARGE = "charge"
    GRID = "grid"
    BATTERY = "battery"
    SAFE = "safe"


@dataclass(frozen=True, slots=True)
class PriceSlot:
    """A price interval, optionally extended from an external forecast."""

    start: datetime
    end: datetime
    price: float
    expected_load_wh: float = 0.0
    expected_pv_wh: float = 0.0
    source: str = "known"
    uncertainty_dkk_per_kwh: float = 0.0

    @property
    def hours(self) -> float:
        return (self.end - self.start).total_seconds() / 3600

    @property
    def charge_price_dkk_per_kwh(self) -> float:
        """Conservative price used when evaluating a forecast-driven charge."""
        return self.price + self.uncertainty_dkk_per_kwh

    @property
    def discharge_price_dkk_per_kwh(self) -> float:
        """Conservative price used when evaluating a forecast-driven discharge."""
        return self.price - self.uncertainty_dkk_per_kwh


@dataclass(frozen=True, slots=True)
class PlannerSettings:
    """Validated economic and physical planning inputs."""

    capacity_wh: float
    reserve_soc: float
    target_soc: float
    charge_power_w: float
    discharge_power_w: float
    round_trip_efficiency: float
    degradation_cost_dkk_per_kwh: float
    minimum_profit_dkk_per_kwh: float
    switching_penalty_dkk: float
    minimum_mode_minutes: int
    maximum_transitions: int
    energy_step_wh: float = 25.0

    def validate(self) -> None:
        if self.capacity_wh <= 0 or self.energy_step_wh <= 0:
            raise ValueError("Capacity and energy step must be positive")
        if not 0 <= self.reserve_soc < self.target_soc <= 100:
            raise ValueError("reserve_soc must be lower than target_soc")
        if self.charge_power_w <= 0 or self.discharge_power_w <= 0:
            raise ValueError("Power limits must be positive")
        if not 0 < self.round_trip_efficiency <= 1:
            raise ValueError("round_trip_efficiency must be in (0, 1]")
        if min(self.degradation_cost_dkk_per_kwh, self.minimum_profit_dkk_per_kwh) < 0:
            raise ValueError("Economic thresholds cannot be negative")
        if self.minimum_mode_minutes < 0 or self.maximum_transitions < 0:
            raise ValueError("Mode constraints cannot be negative")


@dataclass(frozen=True, slots=True)
class PlannedSlot:
    """One executable, explainable optimizer decision."""

    start: datetime
    end: datetime
    action: Action
    price: float
    expected_load_wh: float
    grid_import_wh: float
    battery_charge_wh: float
    battery_discharge_wh: float
    soc_start: float
    soc_end: float
    interval_cost_dkk: float
    baseline_cost_dkk: float
    reason: str

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["start"] = self.start.isoformat()
        data["end"] = self.end.isoformat()
        data["action"] = self.action.value
        return data


@dataclass(frozen=True, slots=True)
class Plan:
    """Complete plan and economics for the available price horizon."""

    created_at: datetime
    slots: tuple[PlannedSlot, ...]
    expected_cost_dkk: float
    baseline_cost_dkk: float
    expected_savings_dkk: float
    battery_throughput_kwh: float
    terminal_price_dkk_per_kwh: float
    reason: str

    @property
    def current_action(self) -> Action:
        return self.slots[0].action if self.slots else Action.SAFE

    def as_dict(self) -> dict[str, Any]:
        return {
            "created_at": self.created_at.isoformat(),
            "expected_cost_dkk": round(self.expected_cost_dkk, 4),
            "baseline_cost_dkk": round(self.baseline_cost_dkk, 4),
            "expected_savings_dkk": round(self.expected_savings_dkk, 4),
            "battery_throughput_kwh": round(self.battery_throughput_kwh, 4),
            "terminal_price_dkk_per_kwh": round(self.terminal_price_dkk_per_kwh, 4),
            "reason": self.reason,
            "slots": [slot.as_dict() for slot in self.slots],
        }
