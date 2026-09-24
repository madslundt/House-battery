"""Typed domain model for deterministic battery optimization."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
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
    # Keep the price provenance with the decision.  This lets long-term
    # evidence distinguish a known tariff from an uncertainty-buffered forecast.
    price_source: str = "known"
    price_uncertainty_dkk_per_kwh: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["start"] = self.start.isoformat()
        data["end"] = self.end.isoformat()
        data["action"] = self.action.value
        return data


# Grid-meter sign convention. The configured ``grid_import_power_entity`` is
# treated as positive when power flows *into* the house from the grid and
# negative when the house flows *out* to the grid. A single normalisation point
# (``normalize_grid_flow``) is the only place this assumption is applied, so a
# meter with the opposite convention needs exactly one documented override.
GRID_POWER_IMPORT_POSITIVE = 1.0

# Physical-flow thresholds (kept here so the pure model is self-contained).
# The coordinator re-exports these from ``const`` for the export-safety layer.
FLOW_NOISE_FLOOR_W = 5.0
FLOW_UI_ACTIVE_THRESHOLD_W = 20.0
# Allowed power-source states for the read-only ``Power source`` sensor.
POWER_SOURCE_STATES = ("charging", "battery", "grid", "off")


@dataclass(frozen=True, slots=True)
class BatteryTelemetry:
    """Normalised, export-aware physical snapshot consumed by every layer.

    The grid quantity is stored both raw (``grid_power_w``, signed) and
    decomposed (``grid_import_w`` / ``grid_export_w``, both non-negative) so no
    consumer ever has to guess the sign again. Export is a first-class
    quantity here: it is never silently clamped to zero, which is precisely the
    behaviour that hid export in the accounting layer before.
    """

    soc: float
    load_w: float
    grid_power_w: float
    grid_import_w: float
    grid_export_w: float
    charge_w: float
    discharge_w: float
    timestamp: datetime


def derive_power_flow(
    soc: float | None,
    load_w: float | None,
    grid_power_w: float | None,
    *,
    timestamp: datetime | None = None,
    sign: float = GRID_POWER_IMPORT_POSITIVE,
) -> PowerFlowSnapshot | None:
    """Return the canonical physical power flow for one load/grid sample.

    ``load_w`` is the connected-load consumption regardless of supplier.
    ``grid_power_w`` is signed with the grid convention (positive = import,
    negative = export). Battery flow is *inferred* from the balance between
    them, never taken from the device's reported charge/discharge. Returns
    ``None`` when either primary measurement is missing so the caller can mark
    the interval incomplete instead of inventing zero-energy evidence.
    """
    if load_w is None or grid_power_w is None:
        return None
    if not (math.isfinite(load_w) and math.isfinite(grid_power_w)):
        return None
    grid_import_w, grid_export_w = normalize_grid_flow(grid_power_w, sign)
    battery_net_power_w = load_w - grid_power_w
    return PowerFlowSnapshot(
        soc=soc if (soc is not None and math.isfinite(soc)) else 0.0,
        load_w=load_w,
        grid_power_w=grid_power_w,
        grid_import_w=grid_import_w,
        grid_export_w=grid_export_w,
        battery_net_power_w=battery_net_power_w,
        battery_output_power_w=max(battery_net_power_w, 0.0),
        battery_charge_power_w=max(-battery_net_power_w, 0.0),
        timestamp=timestamp or datetime.now(UTC),
    )


@dataclass(frozen=True, slots=True)
class PowerFlowSnapshot:
    """Single physical interpretation of load versus grid power.

    This is the one normalisation boundary every downstream layer consumes:
    the planner, accounting, learning, sensors and safety all read from this
    structure instead of reaching for the FBP1200's raw charge/discharge
    telemetry. ``battery_net_power_w`` is positive when the battery supplies
    energy, negative when it charges. ``battery_output_power_w`` /
    ``battery_charge_power_w`` are the non-negative decompositions.
    """

    soc: float
    load_w: float
    grid_power_w: float
    grid_import_w: float
    grid_export_w: float
    battery_net_power_w: float
    battery_output_power_w: float
    battery_charge_power_w: float
    timestamp: datetime

    @property
    def flow_available(self) -> bool:
        """True when both primary measurements are present and finite."""
        return all(
            math.isfinite(value)
            for value in (self.load_w, self.grid_power_w)
        )

    def classify_power_source(
        self, active_threshold: float = FLOW_UI_ACTIVE_THRESHOLD_W
    ) -> str | None:
        """Return the observed physical power source, or ``None`` when unavailable.

        The classifier describes *what the battery is actually doing* per the
        measured load and grid meter, never what the optimizer requested. A
        missing/invalid sample returns ``None`` (Home Assistant ``unavailable``),
        which is deliberately distinct from ``off``.
        """
        if not self.flow_available:
            return None
        if self.battery_charge_power_w > active_threshold:
            return "charging"
        if self.battery_output_power_w > active_threshold:
            return "battery"
        if self.load_w > active_threshold or self.grid_import_w > active_threshold:
            return "grid"
        return "off"


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
    terminal_value_dkk: float = 0.0

    @property
    def current_action(self) -> Action:
        return self.slots[0].action if self.slots else Action.SAFE

    @property
    def realized_savings_dkk(self) -> float:
        """Savings from actually moving the battery, excluding the terminal credit.

        ``expected_savings_dkk`` bundles the terminal value the optimizer credits
        for energy held to the end of the horizon.  That credit is *notional*: it
        only becomes real money if the battery is actually discharged at that
        price.  When throughput is zero the headline savings is entirely a
        terminal-value accounting line, so consumers that want "money in the
        pocket" should read this field instead.
        """
        return self.expected_savings_dkk - self.terminal_value_dkk

    def as_dict(self) -> dict[str, Any]:
        return {
            "created_at": self.created_at.isoformat(),
            "expected_cost_dkk": round(self.expected_cost_dkk, 4),
            "baseline_cost_dkk": round(self.baseline_cost_dkk, 4),
            "expected_savings_dkk": round(self.expected_savings_dkk, 4),
            "realized_savings_dkk": round(self.realized_savings_dkk, 4),
            "terminal_value_dkk": round(self.terminal_value_dkk, 4),
            "battery_throughput_kwh": round(self.battery_throughput_kwh, 4),
            "terminal_price_dkk_per_kwh": round(self.terminal_price_dkk_per_kwh, 4),
            "reason": self.reason,
            "slots": [slot.as_dict() for slot in self.slots],
        }

    def today_dict(self, now: datetime) -> dict[str, Any]:
        """Return plan dict filtered to the local day containing *now*.

        ``now`` must be expressed in the user's local time zone (e.g.
        ``dt_util.as_local``).  The day boundary follows that local calendar
        day — not UTC — so a plan created at 00:19 local (UTC+2) still shows
        the whole remaining local day instead of only the two hours before
        UTC midnight.

        This gives a clean daily overview without past slots or tomorrow's
        forecast bleeding into the user's view.
        """
        day_end = now.replace(hour=23, minute=59, second=59, microsecond=0)
        today_slots = tuple(
            slot for slot in self.slots if slot.start <= day_end
        )
        if not today_slots:
            return self.as_dict()
        expected_cost = sum(s.interval_cost_dkk for s in today_slots)
        baseline_cost = sum(s.baseline_cost_dkk for s in today_slots)
        savings = baseline_cost - expected_cost
        throughput = sum(s.battery_charge_wh + s.battery_discharge_wh for s in today_slots) / 1000
        return {
            "created_at": self.created_at.isoformat(),
            "expected_cost_dkk": round(expected_cost, 4),
            "baseline_cost_dkk": round(baseline_cost, 4),
            "expected_savings_dkk": round(savings + self.terminal_value_dkk, 4),
            "realized_savings_dkk": round(savings, 4),
            "terminal_value_dkk": round(self.terminal_value_dkk, 4),
            "battery_throughput_kwh": round(throughput, 4),
            "terminal_price_dkk_per_kwh": round(self.terminal_price_dkk_per_kwh, 4),
            "reason": self.reason,
            "slots": [slot.as_dict() for slot in today_slots],
        }


def normalize_grid_flow(
    signed_power_w: float, sign: float = GRID_POWER_IMPORT_POSITIVE
) -> tuple[float, float]:
    """Decompose a signed grid power value into (import_w, export_w).

    Sign convention: with the default ``sign`` of +1, a positive value is grid
    import and a negative value is grid export. Invert ``sign`` for a meter that
    reports the opposite. Both returned quantities are non-negative; the caller
    decides what a meaningful (vs. sensor-noise) export is.
    """
    imported = signed_power_w * sign
    exported = -imported
    return max(0.0, imported), max(0.0, exported)
