"""Pure, shared constraints for planning and issuing battery commands."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from math import isfinite
from typing import Any

DISCHARGE_EXPORT_SAFETY_MARGIN_W = 50.0
DIRECT_SETPOINT_CEILING_W = 1200.0


class DeliveryTopology(StrEnum):
    """Whether the grid can supply a shortfall during battery operation."""

    GRID_PARALLEL = "grid_parallel"
    MAINS_ISOLATED = "mains_isolated"


@dataclass(frozen=True, slots=True)
class ControlCapabilities:
    """Known command constraints; unverified power semantics stay explicit."""

    topology: DeliveryTopology = DeliveryTopology.GRID_PARALLEL
    discharge_margin_w: float = 0.0
    maximum_setpoint_w: float | None = None
    charge_power_semantics: str = "combined_ac_input_budget"
    backup_setpoint_semantics_verified: bool = False

    @classmethod
    def direct_local(cls, *, mains_isolated: bool = False) -> ControlCapabilities:
        return cls(
            topology=(DeliveryTopology.MAINS_ISOLATED if mains_isolated
                      else DeliveryTopology.GRID_PARALLEL),
            discharge_margin_w=DISCHARGE_EXPORT_SAFETY_MARGIN_W,
            maximum_setpoint_w=DIRECT_SETPOINT_CEILING_W,
            charge_power_semantics="unverified_custom_charge_setpoint",
        )

    def validate(self) -> None:
        if not isinstance(self.topology, DeliveryTopology):
            raise TypeError("Unknown battery delivery topology")
        if not isfinite(self.discharge_margin_w) or self.discharge_margin_w < 0:
            raise ValueError("Discharge margin must be finite and nonnegative")
        if self.maximum_setpoint_w is not None and (
            not isfinite(self.maximum_setpoint_w) or self.maximum_setpoint_w <= 0
        ):
            raise ValueError("Setpoint ceiling must be finite and positive")

    def discharge_setpoint_w(self, requested_w: float, load_w: float | None) -> float:
        """Fail closed when a command cannot stay below a fresh load reading."""
        if load_w is None or not isfinite(load_w) or not isfinite(requested_w):
            return 0.0
        power = min(max(0.0, requested_w), max(0.0, load_w - self.discharge_margin_w))
        if self.maximum_setpoint_w is not None:
            power = min(power, self.maximum_setpoint_w)
            # Direct-local custom slots accept integer watts. Planning must not
            # count a sub-watt command that execution rounds to Idle/0 W.
            power = float(round(power))
        return power

    def discharge_load_feasible(self, requested_w: float, load_w: float | None) -> bool:
        """Check command eligibility and the entire isolated load's power."""
        if self.discharge_setpoint_w(requested_w, load_w) <= 0:
            return False
        if self.topology is DeliveryTopology.MAINS_ISOLATED:
            bound = min(requested_w, self.maximum_setpoint_w or requested_w)
            return load_w <= bound + 1e-9
        return True

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self), "topology": self.topology.value}
