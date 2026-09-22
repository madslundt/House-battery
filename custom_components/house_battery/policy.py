"""Small, explainable policy adjustments around the core optimizer."""

from __future__ import annotations

from dataclasses import dataclass

from .models import Action, PlannerSettings

_GRID_AVAILABLE = {"1", "on", "available", "grid", "on_grid", "connected", "true"}
_GRID_UNAVAILABLE = {"0", "off", "unavailable", "no_grid", "disconnected", "false"}


def parse_grid_available(value: str | None) -> bool | None:
    """Parse an explicitly bound physical on-grid state, never power flow."""
    normalised = (value or "").lower()
    if normalised in _GRID_AVAILABLE:
        return True
    if normalised in _GRID_UNAVAILABLE:
        return False
    return None


def action_from_operating_mode(value: str | None) -> Action:
    """Translate the reference integration's local mode read-back."""
    return {
        "Charge": Action.CHARGE,
        "Idle": Action.GRID,
        "Discharge": Action.BATTERY,
        "Self-Gen/Zero Export": Action.BATTERY,
    }.get(value, Action.SAFE)


# Extra-storage target and spread knobs were removed (2025-09).  They created
# wasteful discharge→recharge cycles by inflating the charge ceiling above
# target_soc.  The target_soc is now a hard ceiling and the optimizer never
# discharges below it just to recharge at a worse price later.


def apply_storage_policy(
    settings: PlannerSettings,
) -> PlannerSettings:
    """Return settings unchanged – target_soc is the hard charge ceiling."""
    return settings
